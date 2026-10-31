"""Маршрутизация HTTP/WS запросов эмулятора (шаг декомпозиции DEBT-01, TASK-004).

Dispatcher получает runtime как аргумент и ведёт запрос через цепочку:
каталог картинок → websocket (uniproxy) → OTA-обновления → builtin Quasar
(quasar.yandex.net) → каталог эндпоинтов (static/template/script) → честный 404
с note_miss. Алиасы хостов (settings.aliases) приводят Host к каноническому;
WS-сессия остаётся у runtime (sessions — его состояние).

resolve DeviceId (B03), IP клиента через client_peer и журнал recent — здесь;
публичные имена runtime.dispatch / handle_update / emulation_excluded_sn /
client_ip сохранены как делегации — их зовут встраивающий прокси и server.py.
"""
import asyncio
import json
import logging

from aiohttp import web

from .identity import collect_device_id

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

    def emulation_excluded_sn(self, request):
        """SN устройства из settings.emulation_excluded для этого запроса или None.
        Идентичность — заголовки соединения (uprx/quasar), затем query device_id;
        разрешение чистым resolve (без регистрации pending)."""
        runtime = self.runtime
        excluded = runtime.settings.get("emulation_excluded") or []
        if not excluded:
            return None
        device_id = (request.headers.get('x-uprx-device-id')
                     or request.headers.get('X-YaQuasarDeviceId')
                     or request.query.get('device_id') or '')
        device = runtime.identities.resolve(device_id) if device_id else None
        return device.get('sn') if device and device.get('sn') in excluded else None

    async def handle_update(self, request, host, path, record=None):
        runtime = self.runtime
        endpoint = runtime.updates.match(host, path, request.method)
        if not endpoint:
            return None
        if request.headers.get('X-Alice-Update-Probe') == '1':
            return web.json_response({'error': 'update_probe_loop'}, status=508)
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
        if request.path.startswith('/__catalog/'):
            request.match_info['hash'] = request.path.rsplit('/', 1)[-1]
            return await (runtime.catalog.icon(request) if '/icon/' in request.path else runtime.catalog.download(request))
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await runtime.websocket(request, record)
        body = await request.read()
        host = host or request.host.split(':', 1)[0]
        canonical_host = runtime.settings.get('aliases', {}).get(host.lower(), host.lower())
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
            # «прозрачность» конкретного маршрута заменяется режимом прокси.
            output = b'{"error":"emulator_handler_not_found"}'
            headers, status = {"Content-Type": "application/json"}, 404
            if not (canonical_host == 'rpc.alice.yandex.net' and (path or request.path) == '/gproxy/get_app_catalog'):
                runtime.endpoints.note_miss(canonical_host, request.method, path or request.path, request.query_string,
                                            body, request.headers.get('Content-Type', ''), list(request.headers.items()))
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
        return web.Response(status=status, body=output, headers=headers)
