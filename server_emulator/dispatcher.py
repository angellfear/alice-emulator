"""Маршрутизация HTTP/WS запросов эмулятора (шаг декомпозиции DEBT-01, TASK-004).

Dispatcher получает runtime как аргумент и ведёт запрос через цепочку:
каталог картинок → websocket (uniproxy) → OTA-обновления → builtin Quasar
(quasar.yandex.net) → каталог эндпоинтов (static/template/script) → честный 404
с note_miss. Алиасы хостов (settings.aliases) приводят Host к каноническому;
WS-сессия остаётся у runtime (sessions — его состояние).

resolve DeviceId (B03), IP клиента через client_peer и журнал recent — здесь;
маршрут запроса — route_mode_for (TASK-044): (sn, device_id, mode) по заголовкам/
query и реестру; устройство без своего маршрута и неизвестное — emulate (TASK-064).
Режимы: emulate — локально; proxy — upstream; hybrid — локально, где есть обработчик,
промах — наверх с отметкой в «Без ответа». Что писать в журнал, решает не маршрут, а опция
устройства «Запись» (TASK-054): local_handler определяет «есть ли обработчик» одной функцией
для всех режимов.
"""
import asyncio
import json
import logging
import time

from aiohttp import web
from multidict import CIMultiDict

from .identity import collect_device_id
from .quasar_replace import apply_replacements, replaced_headers
from .upstream import UpstreamError, forward_headers
from .upstream.forward import decoded_json

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, runtime):
        self.runtime = runtime

    def client_ip(self, request):
        """Реальный IP клиента. Во встроенном режиме соединение приходит
        внутренним хопом с 127.0.0.1 (фронт терминирует TLS колонки) — тогда
        оригинальный пир берётся у внедрённого резолвера client_peer."""
        runtime = self.runtime
        if runtime.client_peer is not None:
            try:
                peer = runtime.client_peer(request)
                if peer and peer[0]:
                    return peer[0]
            except Exception:
                pass
        return request.remote

    @staticmethod
    def application_device_id(value):
        """DeviceId из JSON-заголовка X-Ya-Application — маркер rpc-запросов станции
        (device_id сидит внутри JSON, отдельного заголовка у этих запросов нет)."""
        if not value:
            return ''
        try:
            decoded = json.loads(value)
        except ValueError:
            return ''
        identifier = decoded.get('device_id') if isinstance(decoded, dict) else None
        return str(identifier).strip() if identifier else ''

    def route_mode_for(self, request):
        """(sn, device_id, mode) маршрута запроса (TASK-044). Идентичность —
        заголовки соединения (uprx/quasar), затем query device_id; разрешение чистым
        resolve (без регистрации). Устройство без route_mode и неизвестное — emulate."""
        runtime = self.runtime
        device_id = (request.headers.get('x-uprx-device-id')
                     or request.headers.get('X-YaQuasarDeviceId')
                     or request.query.get('device_id') or '')
        device = runtime.identities.resolve(device_id) if device_id else None
        if device:
            return device.get('sn'), device_id or None, (device.get('route_mode') or 'emulate')
        return None, device_id or None, 'emulate'

    def canonical_host(self, request, host=None):
        """Канонический хост запроса: алиасы settings.aliases приводят Host к настоящему."""
        host = host or request.host.split(':', 1)[0]
        alias = str(self.runtime.settings.get('aliases', {}).get(host.lower()) or '')
        if alias.lower().startswith(('http://', 'https://')):
            return host.lower()  # полный upstream (тесты, кастомные сервера): хост устройства каноничен
        return (alias or host).lower()

    def local_handler(self, host, method, path):
        """Есть ли у эмулятора обработчик HTTP-запроса (host — канонический, path — без query).
        Без побочных эффектов: ничего не выполняет, только сверяет маршруты. Сомнение
        (скриптовый эндпоинт, ответ builtin может зависеть от устройства) — builtin считается
        обработчиком, скрипт — нет: запрос пишется в режиме «Нет обработчиков»."""
        runtime = self.runtime
        host = (host or '').lower()
        if host == 'rpc.alice.yandex.net' and path == '/gproxy/get_app_catalog':
            return True  # каталог приложений: ответ собирает эмулятор или отдаёт наблюдённый
        if runtime.updates.match(host, path, method):
            return True
        if host == 'quasar.yandex.net' and runtime.quasar.handles(method, path):
            return True
        endpoint = runtime.endpoints.find(host, method, path)
        return bool(endpoint and endpoint.get('enabled') and endpoint.get('mode') != 'script')

    def journal_emulated(self, request, host, path, started, sn, device_id, body, status, headers, output):
        """Запись ответа эмулятора в журнал — по опции «Запись» устройства (source=emulator)."""
        upstream = getattr(self.runtime, 'upstream', None)
        if upstream is None or upstream.record_mode(sn) == 'off':
            return
        handled = self.local_handler(host, request.method, path)
        upstream.record_emulated(sn, device_id, handled, host, request.method, str(request.rel_url), status,
                                 list(request.headers.items()), list(headers.items()), body, output, started)

    def remember_sync_headers(self, request, device):
        """TASK-052: заголовки последнего опроса конфига — только в памяти, по SN
        (кнопка «Получить с оригинального сервера»); в БД/state не пишутся."""
        self.runtime.quasar_sync_headers[device['sn']] = {
            'ts': time.time(), 'query': request.query_string,
            'headers': [[k, v] for k, v in request.headers.items()
                        if k.lower() not in ('host', 'content-length')]}

    @staticmethod
    def sync_info_transparent(device):
        """TASK-065: замены get_sync_info в маршруте «Прозрачно» — источник «Напрямую» и
        галочка «И в прозрачном маршруте» (state.quasar_direct_transparent) устройства."""
        state = device.get('state') or {}
        return state.get('quasar_source') == 'direct' and state.get('quasar_direct_transparent') is True

    async def direct_sync_info(self, request, device, device_id, sn, body, upstream):
        """TASK-052, «Напрямую»: get_sync_info пересылается настоящему quasar.yandex.net,
        ответ колонке без изменений (кроме замен regexp устройства, TASK-063 — quasar_replace.py);
        успешный JSON сохраняется в state.quasar_direct_last (оригинал; body_sent — после замен).
        Ошибка соединения/петли — 502 (как отвечает механизм TASK-043); сохранённый
        последний ответ не затирается."""
        runtime = self.runtime
        target = upstream.target_for(request, host='quasar.yandex.net')
        try:
            status, headers, payload = await upstream.forwarder.http(
                request.method, target, request.headers, body,
                journal=upstream.recording(sn, handled=True),  # у эмулятора для get_sync_info обработчик есть
                host='quasar.yandex.net', path=str(request.rel_url),
                device_sn=sn, device_id=device_id)
        except UpstreamError as exc:
            return web.json_response({'error': 'upstream_unavailable', 'detail': str(exc)[:200]}, status=502)
        replaced = None
        if status == 200 and sn:
            # TASK-063: замены regexp в ответе (правила устройства); сломанный JSON колонке не уходит
            replaced = apply_replacements((device.get('state') or {}).get('quasar_direct_replace'), headers, payload)
            if replaced and 'error' in replaced:
                runtime.recent = (runtime.recent + [{'kind': 'quasar_replace', 'device_sn': sn,
                                                     'error': replaced['error']}])[-30:]
                replaced = None
            parsed = decoded_json(headers, payload)
            if parsed is not None:
                # state перечитывается под lock: снимок до пересылки устарел — правки UI за время
                # запроса (режим, переопределения) иначе откатились бы
                last = {'ts': time.time(), 'status': status, 'body': parsed}
                if replaced:
                    last.update(body_sent=json.loads(replaced['text']), replaced=replaced['applied'])
                async with runtime.lock:
                    current = next((d for d in runtime.identities.data['devices'] if d.get('sn') == sn), None)
                    if current is not None:
                        state = dict(current.get('state') or {})
                        state['quasar_direct_last'] = last
                        runtime.identities.mutate({'operation': 'state', 'sn': sn, 'state': state})
        if replaced:
            return web.Response(status=status, body=replaced['payload'],
                                headers=CIMultiDict(replaced_headers(forward_headers(headers))))
        return web.Response(status=status, body=payload, headers=CIMultiDict(forward_headers(headers)))

    async def handle_update(self, request, host, path, record=None):
        runtime = self.runtime
        endpoint = runtime.updates.match(host, path, request.method)
        if not endpoint:
            return None
        if request.headers.get('X-Alice-Update-Probe') == '1':
            return web.json_response({'error': 'update_probe_loop'}, status=508)
        started = time.monotonic()
        body = await request.read()
        # B03: единый resolver транспортных DeviceId (приоритет query > quasar > uprx);
        # расхождение независимых источников журналируется, не разрешается молча
        device_id, id_report = collect_device_id(
            ('query.device_id', request.query.get('device_id')),
            ('header.x-yaquasar-device-id', request.headers.get('X-YaQuasarDeviceId')),
            ('header.x-uprx-device-id', request.headers.get('x-uprx-device-id')))
        if id_report['mismatch']:
            runtime.recent = (runtime.recent + [{'kind': 'http', 'host': host, 'device_id_report': id_report}])[-30:]
        # OTA-запрос — тоже представление устройства: маркер есть — строка в реестре
        device = runtime.identities.observe(device_id, source=id_report['resolved_from'], ip=self.client_ip(request),
                                            register=True) if device_id else None
        status, output, headers, original, attempt = await runtime.updates.handle(
            endpoint, request.query_string, request.headers, body, device, device_id, runtime.update_fetch)
        if record:
            record({'request_original': body, 'request_sent': body, 'response_original': original, 'response_sent': output},
                   kind='http', direction='exchange', status=status, size=len(body) + len(output), request_size=len(body),
                   response_size=len(output), headers={'request_original': list(request.headers.items()),
                                                      'response_original': list(headers.items())},
                   update_attempt=attempt['id'], update_policy=attempt['policy'],
                   device_sn=attempt['device_sn'], user_id=attempt['user_id'])
        self.journal_emulated(request, host, path, started, device['sn'] if device else None, device_id,
                              body, status, headers, output)
        return web.Response(status=status, body=output, headers=headers)

    async def _endpoint_script(self, endpoint, request, host, path, body, device, device_id):
        """Обработчик <id>.py эндпоинта → (status, bytes, headers) или None (ответ JSON-файла).
        Ошибка скрипта — 500 с текстом ошибки, в журнал recent; сервер не падает."""
        runtime = self.runtime
        from .endpoint_scripts import ScriptRequest, encode, failure
        script = ScriptRequest(request.method, host, path, dict(request.query), request.headers, bytes(body or b""),
                               client_ip=self.client_ip(request), device_id=device_id or "", device=device)
        try:
            result = await runtime.endpoint_scripts.run(runtime.endpoints.script_path(endpoint), script, runtime)
        except Exception as exc:  # noqa: BLE001 — любой сбой пользовательского кода
            detail = failure(exc)
            runtime.recent = (runtime.recent + [{"kind": "endpoint_script", "id": endpoint["id"], "error": detail[-300:]}])[-30:]
            return 500, json.dumps({"error": "endpoint_script", "detail": detail}, ensure_ascii=False).encode(), \
                {"Content-Type": "application/json"}
        if result is None:
            return None
        status, output, content_type, extra = encode(result, endpoint.get("content_type") or "application/json")
        return status, output, {"Content-Type": content_type, **extra}

    async def handle(self, request, host=None, path=None, record=None):
        """Единая точка HTTP-маршрутизации эмуляции (прежний Runtime.dispatch)."""
        runtime = self.runtime
        started = time.monotonic()
        if request.path.startswith('/__catalog/'):
            request.match_info['hash'] = request.path.rsplit('/', 1)[-1]
            return await (runtime.catalog.icon(request) if '/icon/' in request.path else runtime.catalog.download(request))
        upstream = getattr(runtime, "upstream", None)
        sn, device_id, route_mode = self.route_mode_for(request)
        is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
        if upstream is not None and route_mode == "proxy":
            # TASK-044: прямой проход — WS мостом, HTTP пересылкой. Запись в журнал — по опции
            # «Запись» устройства (TASK-054): HTTP решается здесь, WS — на каждом кадре.
            # Устройство наблюдается: колонка остаётся в реестре, маршрут виден и сменяем в UI.
            observed = None
            if device_id:
                observed = runtime.identities.observe(device_id, source='header.x-uprx-device-id',
                                                      ip=self.client_ip(request), register=True)
                sn = observed['sn'] if observed else sn
            target = upstream.target_for(request, host=host, path=path, websocket=is_ws)
            if is_ws:
                return await upstream.serve(request, target, host=host, path=path,
                                            journal=lambda: upstream.record_mode(sn),
                                            device_sn=sn, device_id=device_id)
            canonical = self.canonical_host(request, host)
            if observed is not None and canonical == 'quasar.yandex.net' and (path or request.path) == '/get_sync_info' \
                    and self.sync_info_transparent(observed):
                # TASK-065: «Напрямую» + «И в прозрачном маршруте» — замены применяются и здесь;
                # остальной трафик маршрута идёт прямым проходом ниже
                body = await request.read()
                self.remember_sync_headers(request, observed)
                return await self.direct_sync_info(request, observed, device_id, observed['sn'], body, upstream)
            handled = upstream.record_mode(sn) == 'off' or self.local_handler(canonical, request.method, path or request.path)
            return await upstream.serve(request, target, host=host, path=path,
                                        journal=upstream.recording(sn, handled), unhandled=not handled,
                                        device_sn=sn, device_id=device_id)
        if is_ws:
            # emulate/hybrid (фаза 1): WS целиком эмулируется — необработанные AAPI-сообщения
            # попадают в «Без ответа», как сейчас; пересылка отдельных сообщений наверх —
            # фаза 2, отдельное задание.
            return await runtime.websocket(request, record)
        body = await request.read()
        host = host or request.host.split(':', 1)[0]
        canonical_host = self.canonical_host(request, host)
        # v9 §8: в /glagol/token query device_id — ЦЕЛЬ (токен другого устройства),
        # вызывающий определяется заголовками; цель resolve-only (не регистрируется
        # как наблюдение вызывающего). Вызывающий с заголовками — представление
        # устройства: неизвестный DeviceId регистрируется (без владельца).
        quasar_token_route = canonical_host == 'quasar.yandex.net' and (path or request.path) == '/glagol/token'
        if quasar_token_route:
            device_id = request.query.get('device_id', '')
            caller_id, caller_report = collect_device_id(
                ('header.x-yaquasar-device-id', request.headers.get('X-YaQuasarDeviceId')),
                ('header.x-uprx-device-id', request.headers.get('x-uprx-device-id')))
            caller = runtime.identities.observe(caller_id, source=caller_report['resolved_from'],
                                                ip=self.client_ip(request), register=True) if caller_id else None
            device = runtime.identities.resolve(device_id) if device_id else None
        else:
            # B03: единый resolver; IP/источник — метаданные наблюдения, не ownership.
            # Любой HTTP-запрос с маркером устройства (заголовки uprx/quasar/ya,
            # JSON X-Ya-Application, query) — представление: неизвестный DeviceId
            # сразу становится строкой реестра (без владельца).
            device_id, id_report = collect_device_id(
                ('header.x-uprx-device-id', request.headers.get('x-uprx-device-id')),
                ('header.x-yaquasar-device-id', request.headers.get('X-YaQuasarDeviceId')),
                ('header.x-ya-device-id', request.headers.get('X-Ya-Device-Id')),
                ('header.x-ya-application.device_id', self.application_device_id(request.headers.get('X-Ya-Application'))),
                ('query.device_id', request.query.get('device_id')))
            if id_report['mismatch']:
                runtime.recent = (runtime.recent + [{'kind': 'http', 'host': host, 'device_id_report': id_report}])[-30:]
            device = runtime.identities.observe(device_id, source=id_report['resolved_from'], ip=self.client_ip(request),
                                                register=True) if device_id else None
        if canonical_host == 'quasar.yandex.net' and (path or request.path) == '/get_sync_info':
            # TASK-052: заголовки последнего опроса конфига — только в памяти, по SN
            # (кнопка «Получить с оригинального сервера»); в БД/state не пишутся.
            if device is not None:
                self.remember_sync_headers(request, device)
            if upstream is not None and (device or {}).get('state', {}).get('quasar_source') == 'direct':
                return await self.direct_sync_info(request, device, device_id, device['sn'], body, upstream)
        update = await self.handle_update(request, canonical_host, path or request.path, record)
        if update is not None:
            return update
        if quasar_token_route:
            builtin = runtime.quasar.handle(request.method, path or request.path, device, device_id, body,
                                            caller=caller, caller_id=caller_id, client_ip=self.client_ip(request))
        else:
            builtin = runtime.quasar.handle(request.method, path or request.path, device, device_id, body, client_ip=self.client_ip(request)) if canonical_host == 'quasar.yandex.net' else None
        if builtin:
            status, value = builtin
            output = json.dumps(value, ensure_ascii=False).encode()
            headers = {'Content-Type': 'application/json'}
        elif (endpoint := runtime.endpoints.find(canonical_host, request.method, path or request.path)) is not None \
                and endpoint.get("enabled"):
            # каталог эндпоинтов: захваченный ответ или динамический шаблон
            # ({{device_id}}, {{client_ip}}, {{now}}… — endpoints.TOKENS);
            # приоритетнее фолбэка, но не затеняет builtin и обработчики
            from .endpoints import request_context
            context = request_context({
                "device_id": request.query.get("device_id", ""),
                "platform": request.query.get("platform", ""),
                "client_ip": self.client_ip(request),
                "host": canonical_host, "method": request.method, "path": path or request.path})
            scripted = await self._endpoint_script(endpoint, request, canonical_host, path or request.path,
                                                   body, device, device_id) if endpoint.get("mode") == "script" else None
            if scripted is None:
                status, output, content_type = runtime.endpoints.serve(endpoint, context)
                headers = {"Content-Type": content_type}
            else:
                status, output, headers = scripted
            runtime.recent = (runtime.recent + [{"kind": "endpoint", "id": endpoint["id"],
                                                 "host": canonical_host, "method": request.method,
                                                 "path": path or request.path}])[-30:]
        else:
            # Маршрут не поддержан локально (builtin/эндпоинт): честный 404.
            # Реестр «трёх состояний» удалён — реплей закрывает каталог эндпоинтов,
            # «прозрачность» конкретного маршрута заменяется режимами маршрута (TASK-044).
            catalog_covered = canonical_host == 'rpc.alice.yandex.net' and (path or request.path) == '/gproxy/get_app_catalog'
            miss_args = (canonical_host, request.method, path or request.path, request.query_string,
                         body, request.headers.get('Content-Type', ''), list(request.headers.items()))
            if route_mode == "hybrid" and upstream is not None and not catalog_covered:
                # Гибрид: обработчика нет — промах уходит в Яндекс (журнал пишет upstream)
                # и остаётся в «Без ответа» с отметкой «ушёл в Яндекс» (UI — misses.js).
                try:
                    miss_path = path or request.path
                    target = upstream.target_for(request, host=canonical_host, path=miss_path)
                    forwarded = await upstream.serve(
                        request, target, host=canonical_host, path=miss_path,
                        journal=upstream.recording(device['sn'] if device else sn, handled=False),
                        unhandled=True, device_sn=device['sn'] if device else sn, device_id=device_id)
                except Exception as exc:  # noqa: BLE001 — петля/недоступен: честная ошибка колонке
                    runtime.endpoints.note_miss(*miss_args,
                                                forwarded=f"error: {type(exc).__name__}: {exc}"[:200])
                    return web.json_response({"error": "upstream_unavailable",
                                              "detail": str(exc)[:200]}, status=502)
                runtime.endpoints.note_miss(*miss_args, forwarded="upstream")
                return forwarded
            if not catalog_covered:
                runtime.endpoints.note_miss(*miss_args)
            output = b'{"error":"emulator_handler_not_found"}'
            headers, status = {"Content-Type": "application/json"}, 404
        original_output = output
        if canonical_host == 'rpc.alice.yandex.net' and (path or request.path) == '/gproxy/get_app_catalog' and runtime.catalog.value['enabled']:
            if status == 404:
                await asyncio.to_thread(runtime.catalog.observed)
                output = getattr(runtime.catalog, 'observed_raw', b'')
                headers = {'Content-Type': 'application/x-protobuf'}; status = 200
            elif status == 200:
                encoding = next((v for k, v in headers.items() if k.lower() == 'content-encoding'), '')
                if encoding.lower() == 'gzip':
                    import zlib
                    inflater = zlib.decompressobj(31); output = inflater.decompress(output, 8 * 1024 ** 2 + 1)
                    if len(output) > 8 * 1024 ** 2 or not inflater.eof:
                        raise ValueError('Каталог превышает лимит')
                    headers = {k: v for k, v in headers.items() if k.lower() != 'content-encoding'}
            if status == 200:
                output = runtime.catalog.patch(output, f'{request.scheme}://{request.host}')
        if record:
            record({"request_original": body, "request_sent": body, "response_original": original_output, "response_sent": output},
                   kind="http", direction="exchange", status=status, size=len(body) + len(output), request_size=len(body), response_size=len(output),
                   headers={"request_original": list(request.headers.items()), "response_original": list(headers.items())}, emulated=True,
                   device_sn=device['sn'] if device else None, user_id=device.get('user_id') if device else None)
        if quasar_token_route:  # вызывающий — по заголовкам; device — цель токена
            journal_sn, journal_id = (caller['sn'] if caller else None), caller_id or None
        else:
            journal_sn, journal_id = (device['sn'] if device else sn), device_id
        self.journal_emulated(request, canonical_host, path or request.path, started, journal_sn, journal_id,
                              body, status, headers, output)
        return web.Response(status=status, body=output, headers=headers)
