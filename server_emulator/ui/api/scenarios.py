"""Домен scenarios: admin-API (RF-03), перенесён дословно из legacy-цепочки legacy_api
в runtime.py (mount_ui).

scenarios (S01/S09): каталог пользовательских сценариев, полные определения
встроенных, S-экспорт сценария как пакета реестра, индивидуальные
переключатели, сохранение/удаление и dry-run прогон без устройства.

Реестр пакетов сценариев (маршрут registry) перенесён в каталог расширений
(TASK-036): extensions/legacy_registry.py и «Дополнительно → Расширения».
"""
import datetime
import os

from aiohttp import web

from ...scenario_engine import library as scenario_library
from ...scenario_engine.library import CATEGORIES, ERRORS, SCENARIOS
from ...scenario_packages.validate import validate_scenario

from . import route

# TASK-019: заготовка кода py-сценария (GET ?script= без файла) — SDK из docs/contracts/scripts-sdk.md
SCRIPT_STUB = '''"""py-сценарий: handle(ctx) вызывается на совпавший триггер
(docs/contracts/scenario-json-v2.md, раздел «Исполнитель script»).

Изменённый код перечитывается без рестарта; таймаут действует только на async
handle — сетевой код пишите async."""
from server_emulator.sdk import say


async def handle(ctx):
    return say("Привет!")
'''


@route("scenarios", methods=("GET", "PUT", "DELETE", "POST"))
async def scenarios(request, runtime, app):
    # S01/S09: каталог пользовательских сценариев + полные определения встроенных
    if request.method == 'GET' and request.query.get('export'):
        # S-экспорт: сценарий как пакет реестра (без credentials/личного)
        scenario_id = request.query['export']
        scenario = next((x for x in runtime.settings.get('scenarios', [])
                         if x.get('id') == scenario_id), None)
        if scenario is None:
            raise ValueError('Сценарий не найден')
        clean = validate_scenario({k: v for k, v in scenario.items()
                                   if not k.startswith('_') and k != 'enabled'})
        package = {'manifest': {'format': 'scenario-package', 'version': 1},
                   'scenarios': [clean]}
        return web.json_response(package)
    if request.method == 'GET' and 'script' in request.query:
        # TASK-019: код .py py-сценария (встроенного и расширения — только чтение)
        # или заготовка; TASK-030: сценарий расширения ищется в его каталоге
        scenario_id = request.query['script']
        scenario = next((x for x in runtime.settings.get('scenarios', [])
                         if x.get('id') == scenario_id), None) or SCENARIOS.get(scenario_id)
        if scenario is None:
            extension, _errors = scenario_library.extension_scenarios(runtime.extensions)
            scenario = extension.get(scenario_id)
        if scenario is None:
            raise ValueError('Сценарий не найден')
        path = runtime.scenario_executor.script_path(scenario)
        code = path.read_text(encoding='utf-8') if path is not None and path.is_file() else None
        return web.json_response({'code': code, 'stub': SCRIPT_STUB})
    if request.method == 'GET':
        disabled = set(runtime.settings.get('builtin_disabled') or [])
        master = bool(runtime.settings.get('builtin_scenarios'))
        # библиотека scenarios/<категория>/*.json — встроенные сценарии по разделам
        builtin = [{**{k: v for k, v in s.items() if k != '_origin'}, 'catalog': 'builtin',
                    'origin': 'builtin',
                    'enabled': master and key not in disabled}
                   for key, s in SCENARIOS.items()]
        # слой расширений (TASK-030): сценарии включённых расширений — только чтение,
        # правка сохранит пользовательскую копию с тем же id
        extension, extension_errors = scenario_library.extension_scenarios(runtime.extensions)
        extensions_list = [{**{k: v for k, v in s.items() if not k.startswith('_')},
                            'catalog': 'extension', 'origin': s.get('_origin'),
                            'extension': s.get('_origin', '').split(':', 1)[-1], 'enabled': True}
                           for s in extension.values()]
        return web.json_response({'scenarios': [dict(s, origin='user') if isinstance(s, dict) else s
                                                for s in runtime.settings.get('scenarios', [])],
                                  'builtin': builtin, 'extensions': extensions_list,
                                  'extension_errors': extension_errors,
                                  'categories': CATEGORIES,
                                  'library_errors': ERRORS,
                                  'builtin_enabled': master})
    value = await request.json() if request.method in ('PUT', 'POST') else None
    if request.method == 'PUT' and set(value or {}) == {'id', 'enabled'}:
        # индивидуальный переключатель без редактора (пользовательские и builtin)
        if value['id'] in SCENARIOS:
            off = set(runtime.settings.get('builtin_disabled') or [])
            (off.discard if value['enabled'] else off.add)(value['id'])
            await runtime.save_settings({**runtime.settings,
                                         'builtin_disabled': sorted(off)})
            return web.json_response({'id': value['id'], 'enabled': bool(value['enabled'])})
        scenarios = runtime.settings.get('scenarios', [])
        scenario = next((x for x in scenarios if x.get('id') == value['id']), None)
        if scenario is None:
            raise ValueError('Сценарий не найден')
        scenario['enabled'] = bool(value['enabled'])
        await runtime.save_settings({**runtime.settings, 'scenarios': scenarios})
        return web.json_response({'id': value['id'], 'enabled': scenario['enabled']})
    if request.method == 'PUT':
        payload = dict(value or {})
        script = payload.pop('script', None)  # TASK-019: код py-сценария — рядом с JSON, не внутри
        body = payload.pop('scenario', None) or payload
        scenario = validate_scenario(body)
        if (scenario.get('executor') or {}).get('kind') == 'script':
            target = runtime.scenario_dir / (scenario['id'] + '.py')  # пишем только пользовательские
            if script is not None:
                try:  # синтаксис проверяется до записи (как EndpointCatalog.write_script)
                    compile(script, scenario['id'] + '.py', 'exec')
                except SyntaxError as exc:
                    raise ValueError(f"Скрипт: строка {exc.lineno}: {exc.msg}") from None
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix('.py.tmp')
                temporary.write_text(script, encoding='utf-8')
                os.replace(temporary, target)  # атомарно; ScriptRunner перечитает по mtime
            elif not target.is_file():
                raise ValueError('executor script: нет кода и файла обработчика')
        scenarios = [x for x in runtime.settings.get('scenarios', [])
                     if x.get('id') != scenario['id']] + [scenario]
        await runtime.save_settings({**runtime.settings, 'scenarios': scenarios})
        return web.json_response({'saved': scenario['id']})
    if request.method == 'DELETE':
        removed = request.query.get('id')
        scenarios = [x for x in runtime.settings.get('scenarios', []) if x.get('id') != removed]
        if len(scenarios) == len(runtime.settings.get('scenarios', [])):
            raise ValueError('Сценарий не найден')
        try:  # файл — источник правды: удаляем и его (иначе защита
            # от stale-формы вернёт сценарий при следующем сохранении)
            (runtime.scenario_dir / (removed + '.json')).unlink()
        except OSError:
            pass
        try:  # TASK-019: py-сценарий без JSON не нужен
            (runtime.scenario_dir / (removed + '.py')).unlink()
        except OSError:
            pass
        await runtime.save_settings({**runtime.settings, 'scenarios': scenarios})
        return web.json_response({'deleted': removed})
    if request.method == 'POST':  # dry-run без устройства
        text = (value or {}).get('text', '')
        sent = []
        async def collector(data):
            sent.append(len(data))
        ctx = {'input': {'message_id': 'dry-run',
            'request_id': 'dry-run', 'dialog_id': 'dry', 'sn': (value or {}).get('sn', ''), 'text': text},
            'clock': {'now': datetime.datetime.now()}}
        ctx['dry_run'] = True  # TASK-019: py-сценарии не выполняют действия — пишут effects
        if runtime._scenario_context is not None:  # guard/bindings видят контекст
            ctx.update(runtime._scenario_context(sn=(value or {}).get('sn', ''),
                                                 device_id=(value or {}).get('device_id', '')))
        report = await runtime.dialog.handle_text(text, ctx, 'dry-run', collector)
        report['dry_run'] = True
        return web.json_response(report)


@route("scenario-data", methods=("GET", "DELETE"))
async def scenario_data(request, runtime, app):
    """Данные сценариев (store-шаги): GET ?scenario= — строки; DELETE ?scenario=[&scope&owner&key]."""
    scenario_id = request.query.get('scenario') or None
    if request.method == 'GET':
        return web.json_response({'rows': runtime.store.scenario_data_list(scenario_id)})
    if not scenario_id:
        raise ValueError('scenario обязателен')
    removed = runtime.store.scenario_data_delete(scenario_id, request.query.get('scope'),
                                                 request.query.get('owner'), request.query.get('key'))
    return web.json_response({'deleted': removed})
