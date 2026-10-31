"""Standalone emulator state, providers and management API."""
import asyncio
import copy
import datetime
import json
import logging
import os
import time
import uuid
from pathlib import Path

from aiohttp import ClientTimeout, WSMsgType, web
from google.protobuf import text_format

from .dispatcher import Dispatcher
from .provider_manager import ProviderManager
from .lifecycle import Lifecycle, RequestContext
from .settings_service import DEFAULT, RECORD_MODES, ROUTE_MODES, SettingsService, masked_secret, validate_url
from .schema import SetupRequired, load_type, schemas_ready
from .device_images import DeviceImages, images_dir
from .state_store import StateStore, migrate_legacy
from .endpoints import EndpointCatalog
from .params import ParamStore
from .quasar_config import PlatformStore
from .aapi_handlers import has_handler as aapi_has_handler
from .voice import Conversation
from .identity import IdentityRegistry, collect_device_id
from .quasar import QuasarBackend
from .actions import push_message
from .updates import Updates
from .auth import AuthStore, secret

# AAPI-сообщения, на которые облако не отвечает (или выключенные настройками aapi_model/aapi_voice):
# в очередь «Без ответа» не попадают.
AAPI_NO_REPLY = {'Ack', 'StreamControl', 'TextInput', 'VoiceInput'}

ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)
STANDALONE_KEY = web.AppKey("emulator_standalone", bool)


class Runtime:
    def __init__(self, directory=None, catalog_root=None):
        self.directory = Path(directory or ROOT / "state")
        self.directory.mkdir(parents=True, exist_ok=True)
        from . import proto_schemas
        proto_schemas.DIRECTORY = self.directory / "proto"  # схемы, импортированные из libquasar_daemons.so
        # B02: единое SQLite-хранилище; legacy JSON мигрируется один раз с backup.
        self.store = StateStore(self.directory / "emulator.sqlite3")
        self.migration_report = migrate_legacy(self.directory, self.store)
        # TASK-003 (DEBT-01): дефолты, миграции, валидация и сохранение настроек —
        # в settings_service.SettingsService; runtime держит текущий dict и оркестрацию.
        self.settings_service = SettingsService(self.store, self.directory)
        self.settings = self.settings_service.load()
        self.scenario_dir = self.settings_service.scenario_dir  # один JSON-файл на сценарий
        self.provider_manager = ProviderManager(self)  # TASK-007 (DEBT-01): ASR/TTS/LLM-резолвинг и legacy-цикл
        self.recent = []
        self.pending_calls = []
        self.speech_log = []
        import itertools
        self.sequence = itertools.count(1)  # SequenceNumber ответов (реальный счётчик сервера)
        self.lock = asyncio.Lock()
        self.identities = IdentityRegistry(self.directory, self.store)
        # TASK-044: очередь миграции emulation_excluded — известным устройствам сразу
        # route_mode=proxy, подключившиеся позже получают его при регистрации.
        for sn in list(self.identities.pending_route_migration()):
            if any(d['sn'] == sn for d in self.identities.data['devices']):
                self.identities.apply_pending_route(sn)
        self.quasar_params = ParamStore(self.store.db,  # TASK-032: params/ расширений поверх БД
                                        extension_dirs=lambda: self._extension_layers("params"))
        self.quasar_params.seed(self.identities.data)  # каталог+legacy-перенос, идемпотентно
        self.sessions = {}
        self.quasar_sync_headers = {}  # TASK-052: последние заголовки get_sync_info по SN, только память
        self.quasar = QuasarBackend(self)
        self.action_log = []
        self.tool_log = []  # вызовы инструментов моделью (MCP-хаб), последние 100
        from . import agent
        self.agent = agent.create(self.directory)  # None — LangChain не установлен, только legacy
        self.routines = agent.create_routines(self)  # задания по расписанию (agent/routines.py) или None
        self.mcp_errors = {}  # последняя ошибка tools/list по серверу mcp.json (вкладка MCP)
        self.disconnect_proxy = None
        from .catalog import Catalog
        self.catalog = Catalog(catalog_root or self.directory,  # TASK-032: catalog/ расширений
                               extension_dirs=lambda: self._extension_layers("catalog"))
        self.updates = Updates(self.directory,  # TASK-032: updates/ расширений
                               extension_dirs=lambda: self._extension_layers("updates"))
        self.endpoints = EndpointCatalog(self.directory / "endpoints",  # каталог захваченных ответов
                                         extension_dirs=lambda: self._extension_layers("endpoints"))
        from .miss_journal import MissJournal, exact  # очередь «Эмулятор → Без ответа»
        self.phrase_misses = MissJournal(self.directory / "phrase_misses.json")  # фразы без сценария
        self.aapi_misses = MissJournal(self.directory / "aapi_misses.json", key=exact)  # AAPI без ответа
        self.callback_misses = MissJournal(self.directory / "callback_misses.json", key=exact)  # колбэки без сценария
        from .endpoint_scripts import ScriptRunner
        self.endpoint_scripts = ScriptRunner()  # обработчики <id>.py (режим script)
        from .voice_stack import VoiceStack
        self.voice_stack = VoiceStack(self)  # варианты ASR/TTS/LLM + локальные речевые серверы
        self.quasar_platforms = PlatformStore(self.directory / "quasar",  # базовые sync-конфиги по платформам
                                              extension_dirs=lambda: self._extension_layers("quasar"))
        self.device_images = DeviceImages()  # картинки с веб-версии умного дома — только по согласию (api/setup/images)
        self.auth = AuthStore(self.directory)  # B04: принципалы/сессии панели (нет файла — открытый режим)
        # TASK-029: менеджер расширений; включённые загружаются сразу (без сети и хуков),
        # точки подключения TASK-030+ берут каталоги из runtime.extensions.contribution_dirs().
        from .extensions.manager import ExtensionManager
        self.extensions = ExtensionManager(self)
        from .extensions.legacy_registry import migrate_registry
        migrate_registry(self)  # TASK-036: прежний реестр сценариев → источник и расширения (единожды)
        self.extensions.load_enabled()
        # TASK-030: check_template (validate.py про runtime не знает) разрешает {ссылки}
        # сценариев на списки включённых расширений — как при матчинге шаблонов.
        from .scenario_engine import templates as scenario_templates
        scenario_templates.set_extension_sources(self._extension_lexicon_sources)
        self.client_peer = None  # внедряет встроенный прокси: резолвер реального пира фронта
        self.update_fetch = None
        # Скрипты-расширения UI от хоста (прокси подключает свои кнопки);
        # standalone-эмулятор их не имеет — список пуст.
        self.ui_extensions = []
        self.lifecycle = Lifecycle()
        from .animations import AnimationStore
        self.animations = AnimationStore(self.directory / 'animations',  # TASK-032: animations/ расширений
                                         extension_dirs=lambda: self._extension_layers("animations"))
        from .behavior import set_extension_layers  # TASK-032: replies/ расширений
        set_extension_layers(lambda: self._extension_layers("replies"))
        # R07/M08: диалоговый слой сценариев; пуст по умолчанию — заглушки не меняются
        from .scenario_engine.dialog import DialogManager
        from .scenario_engine.executor import Executor
        from .scenario_engine import library
        from .scenario_engine.library import LEXICONS
        from .scenario_engine.scheduler import TimerScheduler
        # M08: сервер — владелец расписания таймеров; просроченные после рестарта — missed
        self.timer_scheduler = TimerScheduler(self.store, fire=self._fire_timer)
        self.timer_scheduler.recover_missed()
        self.fetch_cache = {}  # (url, query) → (ts, json): кеш GET JSON (ctx.fetch_json, плагины)

        # --- эффекты-примитивы сценариев: без текстов, словарей и выбора веток ---
        def _input(execution):
            return (getattr(execution, "ctx", None) or {}).get("input") or {}

        def timer_start(arguments, execution):
            source = _input(execution)
            timer, _created = self.timer_scheduler.schedule(
                sn=arguments.get("sn") or source.get("sn") or "", seconds=int(arguments["seconds"]),
                label=arguments.get("label"), user_id=source.get("user_id"),
                dialog_id=source.get("dialog_id"))
            return {"id": timer["id"], "seconds": timer["duration_s"], "label": timer["label"]}

        def alarm_start(arguments, execution):
            source = _input(execution)
            alarm = self.timer_scheduler.schedule_alarm(
                sn=arguments.get("sn") or source.get("sn") or "", hhmm=str(arguments["time"]),
                label=arguments.get("label"), tz=arguments.get("timezone") or None,
                user_id=source.get("user_id"), dialog_id=source.get("dialog_id"))
            alarm = alarm[0] if isinstance(alarm, tuple) else alarm
            return {"id": alarm.get("id"), "time": arguments["time"]}

        def timer_cancel(arguments, execution):
            return self.timer_scheduler.cancel(sn=arguments.get("sn") or _input(execution).get("sn") or "",
                                               kind=arguments.get("kind") or "timer")

        async def device_push(arguments, execution):
            item = await self.push_action(arguments.get("sn") or _input(execution).get("sn") or "", arguments["action"],
                                          arguments.get("arguments") or {})
            return item["delivery"]

        def light_set(arguments, execution):
            from .scenario_engine.light import apply_light
            on = arguments.get("on")
            on = on.strip().lower() in ("1", "true", "on") if isinstance(on, str) else bool(on)
            brightness = arguments.get("brightness")
            return apply_light(self.identities, _input(execution).get("sn") or "", str(arguments["room"]),
                               "on" if on else "off", int(brightness) if brightness not in (None, "") else None)

        async def fetch_json(url, query, ttl):
            """GET JSON для ctx.fetch_json и плагинов: кеш TTL, лимит размера, честная ошибка."""
            import time as _time
            key = (url, tuple(sorted(query.items())))
            cached = self.fetch_cache.get(key)
            if ttl and cached and _time.time() - cached[0] < ttl:
                return copy.deepcopy(cached[1])
            from aiohttp import ClientTimeout
            session = self.lifecycle.client_session()
            async with session.get(url, params=query, timeout=ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                raw = await response.content.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError("ответ больше 512 КиБ")
            data = json.loads(raw)
            if ttl:
                self.fetch_cache[key] = (_time.time(), data)
            return copy.deepcopy(data)

        from .scenario_plugins import weather_plugin  # плагины датасетов (weather — заглушка)
        # TASK-033: встроенные реестры — база пересборки (включённые расширения добавляются
        # поверх при каждом load_enabled); executor.plugins — тот же объект, что scenario_plugins
        # (заглушки тестов и tools подменяют weather на месте).
        self._builtin_actions = {"timer.start": timer_start, "alarm.start": alarm_start,
                                 "timer.cancel": timer_cancel, "device.push": device_push,
                                 "light.set": light_set}
        self._builtin_plugins = {"weather": weather_plugin(fetch_json, self.lifecycle.client_session)}
        self.scenario_plugins = dict(self._builtin_plugins)
        from .tts_cache import TtsCache
        self.tts_cache = TtsCache(self.directory / "tts_cache")
        self.tts_cache.is_static = lambda text: text in self._static_phrases()

        def scenario_script_path(scenario):
            """TASK-019: файл .py py-сценария. Встроенный — рядом с его JSON в
            библиотеке (_file — «<раздел>/<id>.json» относительно library.ROOT);
            сценарий расширения (TASK-030, _origin «extension:<id>») — рядом с его
            JSON в каталоге расширения (только чтение: правка кода UI пишет в
            state/scenarios); пользовательский — state/scenarios/<id>.py.
            library.ROOT читается при вызове — подмена библиотеки в тестах."""
            origin = str(scenario.get("_origin") or "")
            if origin == "builtin":
                return (library.ROOT / scenario["_file"]).with_suffix(".py")
            if origin.startswith("extension:"):
                manager = getattr(self, "extensions", None)
                item = (manager.loaded.get(origin.split(":", 1)[1]) if manager is not None else None) or {}
                directory = item.get("dir")
                return (Path(directory) / scenario["_file"]).with_suffix(".py") if directory else None
            return self.scenario_dir / f"{scenario.get('id', '')}.py"

        self.scenario_executor = Executor(
            actions=dict(self._builtin_actions),
            fetcher=fetch_json, data_store=self.store, lexicons=LEXICONS,
            plugins=self.scenario_plugins,
            schemas_root=ROOT / "schemas", store=self.store,
            on_error=self.note_recent, script_path=scenario_script_path)
        self.rebuild_extension_registries()  # TASK-033: действия/датасеты включённых расширений

        def _scenario_context(sn="", device_id=""):
            """Контекст запроса для условий/шаблонов: профиль владельца (гео), запись
            устройства (комната, лампы, приложения), настройки — данные, а не решение."""
            device = None
            if sn or device_id:
                device = next((d for d in self.identities.data["devices"]
                               if (sn and d.get("sn") == sn) or
                               (device_id and device_id in (d.get("device_ids") or []))), None)
            user = None
            if device and device.get("user_id"):
                user = next((u for u in self.identities.data["users"]
                             if u.get("id") == device["user_id"]), None)
            state = (device or {}).get("state") or {}
            light = state.get("light") if isinstance(state.get("light"), dict) else {}
            platform = ((device or {}).get("profile") or {}).get("platform") or ""
            params = self.quasar_params.context(user_id=(user or {}).get("id"), sn=(device or {}).get("sn"),
                                                platform=platform)
            online = {x.get("device_sn") for x in self.sessions.values() if x.get("send")}
            home = []  # колонки владельца: адресация команд между колонками по имени/псевдониму
            for other in self.identities.data["devices"]:
                if not user or other.get("user_id") != user.get("id"):
                    continue
                own = self.quasar_params.context(sn=other["sn"])["device"]
                home.append({"sn": other["sn"], "name": own.get("name") or other.get("name") or other["sn"],
                             "aliases": own.get("aliases") or [], "room": own.get("room") or other.get("room") or "",
                             "platform": (other.get("profile") or {}).get("platform") or "",
                             "model": other.get("model") or "", "online": other["sn"] in online,
                             "current": other["sn"] == (device or {}).get("sn")})
            from .animations import display_of
            own_params = params.get("device") or {}  # параметры колонки перекрывают поля реестра
            return {"user": {"id": (user or {}).get("id"),
                             "name": (params.get("user") or {}).get("name") or (user or {}).get("name"),
                             "location": copy.deepcopy((user or {}).get("location") or {}),
                             "fields": copy.deepcopy((user or {}).get("fields") or {})},
                    "device": {"sn": (device or {}).get("sn"),
                               "name": own_params.get("name") or (device or {}).get("name"),
                               "room": own_params.get("room") or (device or {}).get("room") or "",
                               "user_id": (device or {}).get("user_id"),
                               "model": (device or {}).get("model") or "",
                               "display": display_of((device or {}).get("model")),
                               "light": copy.deepcopy(light), "lights": sorted(light),
                               "apps": copy.deepcopy(state.get("apps") if isinstance(state.get("apps"), list) else []),
                               "platform": platform,
                               "fields": copy.deepcopy((device or {}).get("fields") or {})},
                    "params": params, "home": {"devices": home},
                    "settings": {"weather": copy.deepcopy(self.settings.get("weather") or {})}}
        self._scenario_context = _scenario_context
        self.dialog = DialogManager(self.scenario_executor, self._scenarios,
                                    lambda: load_type(ROOT / "schemas", "alice-api.pb",
                                                      "NAlice.NAliceApi.TServerMessage")(),
                                    lexicon_sources=self._lexicon_sources())
        # TASK-004 (DEBT-01): HTTP-маршрутизация (dispatch) вынесена в Dispatcher;
        # runtime остаётся владельцем WS-сессий и состояния, к которому dispatcher обращается.
        self.dispatcher = Dispatcher(self)
        # TASK-043: проход наверх — резолвер внешнего DNS, пересылка HTTP и мост WS
        # + журнал трафика state/traffic.sqlite3. Правило пересылки — режимы маршрута
        # устройств (TASK-044): dispatcher.route_mode_for + upstream.target_for.
        from .upstream import Upstream
        self.upstream = Upstream(self)

    async def close(self):
        """B01: идемпотентная остановка общих ресурсов (клиент, фоновые задачи)."""
        for variant in list(self.voice_stack.local.processes):  # локальные речевые серверы
            self.voice_stack.local.stop_key(variant)
        if self.routines is not None:
            self.routines.shutdown()
        await self.upstream.close()  # TASK-043: доработать очередь журнала, закрыть клиента прохода
        await self.lifecycle.close()
        self.store.close()

    # --- каталоги точек подключения расширений (TASK-030) ---------------------------

    def _extension_layers(self, name):
        """Слои sources точки подключения name из включённых расширений: [(origin, Path)]
        (helper «слои источников» — extensions/layers.source_layers). Лениво: менеджер
        расширений создаётся позже каталогов endpoints/quasar в __init__; сбой — слой пуст."""
        from .extensions.layers import source_layers
        manager = getattr(self, "extensions", None)
        if manager is None:
            return []
        try:
            return source_layers(manager, name)
        except Exception:  # noqa: BLE001 — расширения не должны ломать каталог
            return []

    def _extension_lexicon_sources(self):
        """Словари lists/ включённых расширений: каждый JSON — источник ({lists, skip_words}
        или asr-раздел), порядок — extensions.loaded (поздний перекрывает ранний)."""
        sources = []
        for _origin, path in self._extension_layers("lists"):
            try:
                files = sorted(Path(path).glob("*.json"))
            except OSError:
                continue
            for file in files:
                try:
                    item = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
                if isinstance(item, dict):
                    sources.append(item)
        return sources

    def _lexicon_sources(self):
        """Источники словарей шаблонов для DialogManager (TASK-022/030): встроенный
        lexicons.json → списки включённых расширений. Пользовательские слои добавятся
        после расширений, когда появится механизм пользовательских списков."""
        from .scenario_engine.templates import builtin_source
        return [builtin_source()] + self._extension_lexicon_sources()

    def _scenarios(self):
        """Каталог для движка: пользовательские (state/scenarios) → сценарии включённых
        расширений (только чтение) → библиотека (scenarios/<раздел>). Перекрытие в
        обратном порядке приоритета: пользовательская запись с тем же id убирает
        сценарий расширения, сценарий расширения — встроенный; builtin_disabled
        действует только на встроенные. Порядок выдачи — fallback последними,
        затем priority, затем id."""
        from .scenario_engine import library
        from .scenario_engine.library import catalog_order
        result = [{"category": "custom", **s} for s in self.settings.get("scenarios", [])
                  if isinstance(s, dict) and s.get("enabled", True) and s.get("format_version") == 2]
        known = {s["id"] for s in result}
        extension, _errors = library.extension_scenarios(getattr(self, "extensions", None))
        result += [s for key, s in extension.items() if key not in known]
        known.update(extension)
        if self.settings.get("builtin_scenarios", True):
            off = set(self.settings.get("builtin_disabled") or [])
            result += [s for key, s in library.SCENARIOS.items()
                       if key not in known and key not in off]
        return sorted(result, key=catalog_order)

    def _static_phrases(self):
        """Фразы, озвучиваемые как есть: база — tts_cache.static_phrases (стандартные
        ответы + пользовательские и встроенные сценарии); поверх — фразы сценариев
        включённых расширений (TASK-030), их каталог tts_cache не видит. Пользовательская
        копия сценария расширения уже учтена базой — её фразы не дублируются."""
        from .scenario_engine import library
        from .tts_cache import scenario_phrases, static_phrases
        result = static_phrases(self)
        users = {s.get("id") for s in self.settings.get("scenarios", []) if isinstance(s, dict)}
        extension, _errors = library.extension_scenarios(getattr(self, "extensions", None))
        extra = [s for key, s in extension.items() if key not in users]
        for text, title, ident in scenario_phrases(extra):
            result.setdefault(text, {"text": text, "source": "scenario", "name": title,
                                     "id": ident, "builtin": False})
        return result

    def note_recent(self, entry):
        """Запись в журнал recent (TASK-019: сбои сценариев — Executor вызывает
        этот колбэк); тот же формат и лимит, что у остальных записей."""
        self.recent = (self.recent + [entry])[-30:]

    # TASK-033: реестры действий и датасетов включённых расширений -------------------
    def rebuild_extension_registries(self):
        """Пересборка реестров Executor из включённых расширений (старт и каждое
        включение/выключение — хук в extensions.load_enabled): actions/*.py объявляют
        ACTIONS = {"имя": fn}, datasets/*.py — DATASETS = {"имя": async (key, args) →
        значение}. Модули грузит общий ScriptRunner (mtime-кеш — правка файла в режиме
        разработки без рестарта, как у режимов TASK-034); записи обновляются на месте —
        объекты реестров (и алиас scenario_plugins) не подменяются.

        Сверка разрешения (правило TASK-033): имя действия обязано начинаться с префикса
        расширения (contributes.actions.prefix или id, manager.action_prefix) ИЛИ быть
        объявленным в permissions.actions; имя вне префикса без объявления — противоречие.
        Имя датасета — всегда с префикса (контракт extensions.md, «Имена элементов»).
        Конфликт со встроенным реестром (timer.start…, weather) или другим расширением
        (владеет тот, чей id раньше по сортировке) → расширение НЕ включается целиком:
        запись уходит из extensions.loaded с понятной ошибкой в extensions.warnings
        (как у битого манифеста в TASK-029); битый модуль — та же реакция. Остальные
        расширения и встроенные примитивы работают как раньше."""
        executor = getattr(self, "scenario_executor", None)
        extensions = getattr(self, "extensions", None)
        if executor is None or extensions is None or not hasattr(extensions, "loaded"):
            return  # старт: Executor ещё не собран — соберёт вызов в конце __init__
        runners = getattr(self, "_extension_runners", None)
        if runners is None:
            from .scripting import ScriptRunner
            runners = self._extension_runners = (ScriptRunner(prefix="ext_actions"),
                                                 ScriptRunner(prefix="ext_datasets"))

        def folders(kind):
            found = {}
            for ext_id, path in extensions.contribution_dirs(kind):
                found.setdefault(ext_id, []).append(Path(path))
            return found

        def modules(paths, runner, attr):
            """Модули точки подключения: [(файл, ACTIONS|DATASETS)]; файл без атрибута
            (вспомогательный модуль) не участвует; битый модуль — ValueError."""
            result = []
            for path in paths:
                for file in sorted(path.glob("*.py")):
                    try:
                        module = runner._module(file)
                    except Exception as exc:  # noqa: BLE001 — битый модуль отключает расширение
                        raise ValueError(f"{file.name}: модуль не загружается "
                                         f"({type(exc).__name__}: {str(exc)[:150]})") from exc
                    table = getattr(module, attr, None)
                    if table is not None:
                        result.append((file, table))
            return result

        action_dirs, dataset_dirs = folders("actions"), folders("datasets")
        actions, plugins = dict(self._builtin_actions), dict(self._builtin_plugins)
        action_owner = {name: "" for name in self._builtin_actions}  # "" — встроенное
        dataset_owner = {name: "" for name in self._builtin_plugins}
        bad = {}
        for ext_id in sorted(extensions.loaded):
            manifest = extensions.loaded[ext_id]["manifest"]
            prefix = str(extensions.action_prefix(ext_id)).rstrip(".")
            declared = [name for name in (manifest["permissions"].get("actions") or [])
                        if isinstance(name, str)]
            ext_actions, ext_datasets, error = {}, {}, None
            try:
                for file, table in modules(action_dirs.get(ext_id, []), runners[0], "ACTIONS"):
                    if not isinstance(table, dict):
                        error = f"{file.name}: ACTIONS — словарь {{имя: функция}}"
                        break
                    for name, fn in table.items():
                        if not isinstance(name, str) or not callable(fn):
                            error = f"{file.name}: ACTIONS[{name!r}] — имя-строка и функция"
                        elif name not in declared and not name.startswith(prefix + "."):
                            error = (f"Действие {name!r} вне пространства имён расширения "
                                     f"{ext_id} (префикс {prefix!r}): переименуйте действие или "
                                     f"объявите его в permissions.actions")
                        elif name in action_owner:
                            who = "встроенным действием" if not action_owner[name] \
                                else "расширением " + action_owner[name]
                            error = f"Имя действия {name!r} уже занято {who}"
                        else:
                            ext_actions[name] = fn
                    if error:
                        break
            except ValueError as exc:  # битый модуль actions/*.py
                error = str(exc)
            if error is None:
                try:
                    for file, table in modules(dataset_dirs.get(ext_id, []), runners[1], "DATASETS"):
                        if not isinstance(table, dict):
                            error = f"{file.name}: DATASETS — словарь {{имя: функция}}"
                            break
                        for name, fn in table.items():
                            if not isinstance(name, str) or not callable(fn):
                                error = f"{file.name}: DATASETS[{name!r}] — имя-строка и функция"
                            elif not name.startswith(prefix + "."):
                                error = (f"Имя датасета {name!r} вне пространства имён расширения "
                                         f"{ext_id} (префикс {prefix!r})")
                            elif name in dataset_owner:
                                who = "встроенным плагином" if not dataset_owner[name] \
                                    else "расширением " + dataset_owner[name]
                                error = f"Имя датасета {name!r} уже занято {who}"
                            else:
                                ext_datasets[name] = fn
                        if error:
                            break
                except ValueError as exc:  # битый модуль datasets/*.py
                    error = str(exc)
            if error:
                bad[ext_id] = error
                continue
            for name in ext_actions:
                action_owner[name] = ext_id
            for name in ext_datasets:
                dataset_owner[name] = ext_id
            actions.update(ext_actions)
            plugins.update(ext_datasets)
        for ext_id, error in bad.items():
            extensions.loaded.pop(ext_id, None)  # «не включается»: без сценариев, режимов и т.д.
            extensions.warnings.append({"id": ext_id, "error": error[:200]})
            log.warning("Расширение %s не подключено к реестрам: %s", ext_id, error)
        executor.actions.clear()
        executor.actions.update(actions)
        executor.plugins.clear()
        executor.plugins.update(plugins)
        # TASK-030: набор включённых расширений сменился — пересобрать каталоги точек
        # подключения (эндпоинты) и источники словарей шаблонов. На старте Executor ещё
        # без dialog (источники придут при его создании), endpoints.reload просто перечитает.
        endpoints = getattr(self, "endpoints", None)
        if endpoints is not None and hasattr(endpoints, "reload"):
            endpoints.reload()
        dialog = getattr(self, "dialog", None)
        if dialog is not None and hasattr(dialog, "lexicon_sources"):
            dialog.lexicon_sources = self._lexicon_sources()  # новый список — кеш шаблонов пересоберётся

    def warm_sound_cache(self, delay=0):
        """Фразы раздела «AI агент» звукового кеша — синтез заранее, в фоне (PCM-движок)."""
        from .tts_cache import agent_phrases

        async def run():
            await asyncio.sleep(delay)
            tts = self.get_tts()
            if tts is not None:
                await self.tts_cache.warm(tts, agent_phrases(self))
        job = run()
        try:
            self.lifecycle.spawn(job)
        except RuntimeError:  # вне цикла событий или после остановки — прогреется при следующем сохранении
            job.close()

    def prune_sound_cache(self):
        """Звуковой кеш без мусора: фразы, которых нет в сценариях и стандартных ответах, удаляются."""
        try:
            self.tts_cache.drop_dynamic()  # фразы с переменными больше не кешируются — остатки прежних версий
            return self.tts_cache.cleanup(self._static_phrases())
        except Exception as exc:  # noqa: BLE001 — очистка кеша не должна ломать сохранение настроек
            log.warning("Очистка звукового кеша: %s", exc)
            return []

    def get_tts(self):
        return self.provider_manager.pcm_tts(self)  # PCM-движок звукового кеша или None

    def write(self, name, value):
        if name == "settings.json":
            self.settings_service.persist(value)
            return
        target = self.directory / name
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    async def push_action(self, sn, action, arguments):
        """Отправка типизированного Push-действия в живую сессию устройства по SN.
        Возвращает запись action_log ('sent' | 'failed' | None-сессии)."""
        cls = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        session = next((x for x in self.sessions.values()
                        if x.get("device_sn") == sn and x.get("send")), None)
        if session is None:
            return {"action": action, "delivery": "no_session", "execution": "unconfirmed"}
        message = push_message(cls, action, arguments)
        item = {"message_id": message.Event.Header.MessageId, "connection_id": session["connection_id"],
                "device_sn": sn, "user_id": session.get("user_id"), "action": action,
                "delivery": "sending", "execution": "unconfirmed"}
        self.action_log = (self.action_log + [item])[-100:]
        try:
            await session["send"](b"AAPI" + message.SerializeToString())
        except (ConnectionError, RuntimeError):
            item["delivery"] = "failed"
            return item
        item["delivery"] = "sent"
        return item

    def speech_sink(self, record):
        """Приёмник событий SpeechTrace. In-place: scoped-копии runtime в WS-сессиях
        делят этот список с основным — трасса видна из API корневого runtime."""
        from .speech_trace import SPEECH_LOG_LIMIT
        self.speech_log.append(record)
        del self.speech_log[:-SPEECH_LOG_LIMIT]

    def _fire_timer(self, timer):
        """Срабатывание таймера/будильника → служебное событие @@timer_fired в живую
        WS-сессию устройства: ответ (текст, директивы) описывает JSON-сценарий."""
        for session in list(self.sessions.values()):
            if session.get("device_sn") != timer.get("sn") or not session.get("send"):
                continue
            ctx = {"input": {"message_id": uuid.uuid4().hex, "request_id": uuid.uuid4().hex,
                             "dialog_id": "", "sn": timer.get("sn") or "", "text": "",
                             "event_name": "@@timer_fired", "purpose": timer.get("kind") or "timer",
                             "callback": "", "frame": "", "label": timer.get("label") or "",
                             "kind": timer.get("kind") or "timer", "timer_id": timer.get("id")},
                   "clock": {"now": datetime.datetime.now()}, "sequence": next(self.sequence)}
            ctx.update(self._scenario_context(sn=timer.get("sn") or ""))
            self.lifecycle.spawn(self.dialog.handle_event("@@timer_fired", ctx, uuid.uuid4().hex,
                                                          session["send"]))
            return True
        return False  # сессии нет — факт срабатывания остаётся в инвентаре таймеров

    async def _timer_loop(self):
        while True:
            await asyncio.sleep(1)
            try:
                self.timer_scheduler.poll()
            except Exception as exc:  # планировщик не должен умирать от одной итерации
                self.recent = (self.recent + [{"kind": "timer", "error": type(exc).__name__ + ": " + str(exc)[:120]}])[-30:]

    def start_background(self):
        """Фоновые задачи runtime (планировщик таймеров, задания агента); идемпотентно."""
        if not getattr(self, "_timer_task", None):
            self._timer_task = self.lifecycle.spawn(self._timer_loop())
            self.lifecycle.spawn(self.voice_stack.autostart())
            if self.routines is not None:
                self.routines.start()
            self.warm_sound_cache(delay=20)  # локальный TTS поднимается autostart'ом

    def public(self):
        return self.settings_service.public(self.settings)

    @staticmethod
    def masked_secret(value):
        return masked_secret(value)  # B04: в UI — только configured/masked, env:VAR открыт

    def refresh_profiles(self):
        for session in self.sessions.values():
            device=self.identities.resolve(session.get('device_id',''))
            session['runtime'].settings=self.identities.settings_for(self.settings,device)

    async def save_settings(self, value, preserve_mode=False):
        value = self.settings_service.validate(value, self.settings)
        tts_changed = value.get("tts") != self.settings.get("tts")
        async with self.lock:
            if preserve_mode:  # форма настроек не меняет маршрут по умолчанию
                value["default_route_mode"] = self.settings["default_route_mode"]
            self.write("settings.json", value)
            self.settings = value
            self.refresh_profiles()
        self.prune_sound_cache()
        self.warm_sound_cache()
        if tts_changed:  # значения переменных озвучены прежним голосом — не копим их под новым движком
            self.tts_cache.drop_dynamic()
        return self.public()

    def _session_sn(self, session):
        """SN устройства WS-сессии: прямой (после sync) или через DeviceId handshake."""
        sn = session.get('device_sn')
        if sn:
            return sn
        device_id = session.get('device_id')
        device = self.identities.resolve(device_id) if device_id else None
        return device.get('sn') if device else None

    async def set_default_route_mode(self, mode, connections='wait'):
        """Маршрут по умолчанию (TASK-044). При connections=disconnect рвутся
        соединения только устройств без собственного route_mode — у них изменился
        эффективный режим; устройства с явным маршрутом не трогаются."""
        if mode not in ROUTE_MODES:
            raise ValueError("mode: " + " | ".join(ROUTE_MODES))
        if connections not in ('wait', 'disconnect'):
            raise ValueError('connections: wait или disconnect')
        async with self.lock:
            value = copy.deepcopy(self.settings)
            value["default_route_mode"] = mode
            self.write("settings.json", value)
            self.settings = value
            closed = 0
            if connections == 'disconnect':
                explicit = {d['sn'] for d in self.identities.data['devices'] if d.get('route_mode')}
                sockets = [s['ws'] for s in self.sessions.values() if self._session_sn(s) not in explicit]
                await asyncio.gather(*(socket.close(code=1012, message=b'Mode changed') for socket in sockets))
                closed = len(sockets)
        return {"mode": mode, 'connections': connections, 'disconnected': closed}

    async def set_device_route_mode(self, sn, mode):
        """Маршрут одного устройства (TASK-044); mode=None — снова default.
        Открытые соединения устройства закрываются — колонка переподключается сама."""
        if mode not in (None, *ROUTE_MODES):
            raise ValueError("mode: " + " | ".join(ROUTE_MODES) + " или null")
        async with self.lock:
            self.identities.mutate({"operation": "route_mode", "sn": sn, "mode": mode})
            sockets = [s['ws'] for s in self.sessions.values() if self._session_sn(s) == sn]
            await asyncio.gather(*(socket.close(code=1012, message=b'Route changed') for socket in sockets))
        return {"sn": sn, "mode": mode, "disconnected": len(sockets)}

    async def set_device_record_mode(self, sn, mode):
        """Опция «Запись» устройства (TASK-054): off | on | misses. Соединения не закрываются:
        опция читается на каждом обмене и кадре, действует сразу."""
        if mode not in RECORD_MODES:
            raise ValueError("mode: " + " | ".join(RECORD_MODES))
        async with self.lock:
            self.identities.mutate({"operation": "record", "sn": sn, "mode": mode})
        return {"sn": sn, "mode": mode}

    @staticmethod
    def validate_url(value):
        validate_url(value)  # http(s) без credentials — реализация в settings_service

    # TASK-007 (DEBT-01): резолвинг ASR/TTS/LLM-адаптеров, потоковые режимы и legacy-цикл
    # инструментов — в provider_manager.ProviderManager. Вызовы идут с self-аргументом:
    # scoped-копии runtime в WS-сессиях несут настройки профиля устройства.
    # provider/chat — публичные имена (voice.py, ui/api, тесты подменяют их заглушками).
    def voice_adapter(self, kind):
        return self.provider_manager.adapter(self, kind)

    async def provider(self, kind, payload=None, audio=None, filename="input.wav"):
        return await self.provider_manager.call(self, kind, payload, audio, filename)

    def asr_streaming(self):
        return self.provider_manager.asr_streaming(self)

    async def recognize_stream(self, chunks, fmt, on_partial=None):
        return await self.provider_manager.recognize_stream(self, chunks, fmt, on_partial)

    def tts_streaming(self):
        return self.provider_manager.tts_streaming(self)

    def speech_stream(self, text):
        return self.provider_manager.speech_stream(self, text)

    async def chat(self, text, history=None, thread=None, **context):
        """Ответ модели: stub → заглушка, graph → LangGraph-агент, иначе legacy-цикл (provider_manager)."""
        return await self.provider_manager.chat(self, text, history, thread=thread, **context)

    async def _chat_legacy(self, text, history=None):
        return await self.provider_manager._chat_legacy(self, text, history)
    async def mcp(self, server, method, params):
        """MCP-запрос к серверу (streamable HTTP: JSON или SSE) — см. mcp_hub.request."""
        from . import mcp_hub
        return await mcp_hub.request(self, server, method, params)

    # TASK-004 (DEBT-01): маршрутизация HTTP — в dispatcher.Dispatcher; публичные
    # имена сохранены как делегации (их зовут server.py и тесты).
    async def handle_update(self, request, host, path, record=None):
        return await self.dispatcher.handle_update(request, host, path, record)

    def route_mode_for(self, request):
        """(sn, device_id, mode) маршрута запроса — TASK-044 (делегация в dispatcher)."""
        return self.dispatcher.route_mode_for(request)

    def client_ip(self, request):
        return self.dispatcher.client_ip(request)

    async def dispatch(self, request, host=None, path=None, record=None):
        try:
            return await self.dispatcher.handle(request, host=host, path=path, record=record)
        except SetupRequired as exc:  # нет протокола из APK станции: UI и admin-API работают, трафик устройств — нет
            return web.json_response({"error": "setup_required", "message": str(exc)}, status=503)

    async def websocket(self, request, record):
        header_device_id = request.headers.get('x-uprx-device-id')
        if header_device_id:
            try:
                self.identities.text(header_device_id,'DeviceId')
            except ValueError:
                raise web.HTTPBadRequest(text='Invalid device identifier')
        ws = web.WebSocketResponse(max_msg_size=16 * 1024**2, heartbeat=30)
        await ws.prepare(request)
        if not schemas_ready():  # протокол ещё не извлечён из APK станции
            await ws.close(code=1013, message=b"setup_required")
            return ws
        context = RequestContext(recorder=record)  # идентичность достраивается ниже
        context.record({}, kind="ws", direction="lifecycle", status=101, event="open", size=0, emulated=True)
        # Журнал трафика (TASK-054): опция «Запись» устройства читается на каждом кадре; звук не пишется
        tap = self.upstream.ws_tap("emulator", request.host.split(':', 1)[0].lower(), request.path,
                                   list(request.headers.items()),
                                   handled=lambda trigger: aapi_has_handler(trigger, scoped_runtime.settings))
        client_type = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
        server_type = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        send_lock = asyncio.Lock()
        async def send(data):
            async with send_lock:
                await ws.send_bytes(data)
            tap.frame("server_to_client", True, data)
            context.record({"original": data, "sent": data}, kind="ws", direction="server_to_client", binary=True, size=len(data), emulated=True,
                           device_sn=context.device_sn, user_id=context.user_id)
        session_id = uuid.uuid4().hex
        trace_context = {'session': session_id, 'sn': None, 'device_id': None}
        scoped_runtime = copy.copy(self)
        scoped_runtime.settings = self.identities.settings_for(self.settings, None)
        scoped_runtime.pending_calls, scoped_runtime.recent = [], []
        self.sessions[session_id] = {'connection_id':session_id, 'host':request.host.split(':',1)[0].lower(), 'ip':self.client_ip(request), 'device_sn': None, 'user_id': None, 'runtime': scoped_runtime, 'send':send, 'ws':ws}
        def delivered(message_id):  # Ack колонки: журнал действий корневого runtime (копия его не видит)
            for item in self.action_log:
                if item['connection_id'] == session_id and item['message_id'] == message_id:
                    item['delivery'] = 'acknowledged'
        conversation = Conversation(scoped_runtime, server_type, send, trace_context=trace_context,
                                    delivered=delivered)
        device = None

        def adopt(device_id, observed):
            # Идентичность соединения может только уточняться, но не подменяться.
            nonlocal context
            context = RequestContext(device_id=device_id, connection_id=session_id, recorder=record,
                                      device_sn=observed['sn'] if observed else None,
                                      user_id=observed.get('user_id') if observed else None)
            self.sessions[session_id].update(device_id=device_id, device_sn=context.device_sn, user_id=context.user_id)
            trace_context.update(sn=context.device_sn, device_id=device_id)  # трасса видит актуальную идентичность
            scoped_runtime.settings = self.identities.settings_for(self.settings, observed)
            tap.identify(context.device_sn, device_id)

        if header_device_id:
            # Заголовок WS-хендшейка — представление устройства: неизвестный
            # DeviceId сразу попадает в реестр (без владельца).
            device = self.identities.observe(header_device_id, source='header.x-uprx-device-id', ip=self.client_ip(request),
                                             register=True)
            adopt(header_device_id, device)
        tap.start()
        try:
            async for packet in ws:
                if packet.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
                    continue
                data = packet.data if packet.type == WSMsgType.BINARY else packet.data.encode()
                tap.frame("client_to_server", packet.type == WSMsgType.BINARY, data)
                context.record({"original": data, "sent": data}, kind="ws", direction="client_to_server", binary=packet.type == WSMsgType.BINARY, size=len(data), emulated=True,
                           device_sn=device['sn'] if device else None, user_id=device.get('user_id') if device else None)
                try:
                    if not data.startswith(b"AAPI"):
                        await conversation.receive_audio(data)
                        continue
                    incoming = client_type.FromString(data[4:])
                    trigger = incoming.Event.WhichOneof("Event") if incoming.WhichOneof("Message") == "Event" else "StreamControl"
                    if trigger == 'SynchronizeStateRequest':
                        # Re-identification requires a new connection: don't mix histories or channels.
                        # B03: sync — авторитетный источник, header — транспортная подсказка;
                        # расхождение независимых источников закрывает соединение (1008).
                        resolved, id_report = collect_device_id(
                            ('sync.Application.DeviceId', incoming.Event.SynchronizeStateRequest.Application.DeviceId),
                            ('header.x-uprx-device-id', header_device_id))
                        current = self.sessions[session_id].get('device_id')
                        if id_report['mismatch'] or (current not in (None, '', resolved)):
                            self.recent = (self.recent + [{'kind': 'ws', 'device_id_report': id_report}])[-30:]
                            await ws.close(code=1008, message=b'Device identity changed')
                            break
                        # sync — авторитетное представление устройства: как и header,
                        # регистрирует неизвестный DeviceId (без владельца)
                        device = self.identities.observe(resolved, source=id_report['resolved_from'],
                                                         ip=self.client_ip(request), register=True) if resolved else None
                        adopt(resolved, device)
                    # Обработчики событий — реестр aapi_handlers (Conversation и служебные ответы)
                    if not await conversation.receive(incoming, trigger):
                        if trigger not in AAPI_NO_REPLY:  # «Эмулятор → Без ответа»: сообщение без обработчика
                            self.aapi_misses.note(trigger, sn=(self.sessions.get(session_id) or {}).get('device_sn') or '',
                                                  example=text_format.MessageToString(incoming, as_one_line=True))
                except Exception as exc:
                    self.recent = (self.recent + [{"kind": "ws", "error": type(exc).__name__ + ": " + str(exc)[:120]}])[-30:]
                    await ws.close(code=1011, message=b"Emulator handler error")
        finally:
            await conversation.shutdown()
            self.sessions.pop(session_id, None)
            tap.close(ws.close_code)
            context.record({}, kind="ws", direction="lifecycle", event="close", close_code=ws.close_code, size=0, emulated=True)
        return ws


def mount_ui(app, runtime, prefix="/emulator"):
    # RF-02: домены админ-API переезжают в реестр ui/api; реестр спрашивается
    # ДО legacy-цепочки, поверхность контракта фиксирует tests/test_admin_api_surface.py.
    from .ui.api import dispatch as registry_dispatch, get as registry_get, guarded

    async def api(request):
        # RF-03: все домены админ-API живут в реестре ui/api; неизвестный action → 404.
        if registry_get(request.match_info["action"]) is None:
            raise web.HTTPNotFound()
        return await registry_dispatch(request, runtime, app)

    # Каталог картинок устройств (модель по SN): только существующие
    # PNG/WebP из каталога картинок (device_images.images_dir) — путь проверяется на выход за пределы папки.
    ASSETS = ("index.html", "app.js", "style.css", "users.js", "backend.js", "updates.js", "catalog.js", "replies.js", "animations.js", "endpoints.js", "misses.js", "endpoints.css", "devices_common.js", "device_cards.js", "mcp.js", "agent.js", "memory.js", "routines.js", "scenario_drafts.js", "params.js", "voice_stack.js", "sound_cache.js", "extensions.js", "setup.js", "server_config.js", "traffic.js")
    GENERIC_IMAGES = ROOT / "ui" / "img" / "generic"
    VENDOR = ROOT / "ui" / "vendor"

    async def static(request):
        filename = request.match_info.get("file", "") or "index.html"
        path = None
        if filename in ASSETS:
            path = ROOT / "ui" / filename
        elif filename.startswith("vendor/"):  # готовые библиотеки (CodeMirror): без выхода за папку
            resolved = (ROOT / "ui" / filename).resolve()
            if VENDOR in resolved.parents and resolved.is_file():
                path = resolved
        elif filename.startswith("img/devices/") and filename.endswith((".png", ".webp")):
            # картинки с веб-версии умного дома: каталог задаёт images_dir(), его может ещё не быть
            devices = images_dir().resolve()
            resolved = (devices / filename[len("img/devices/"):]).resolve()
            if devices in resolved.parents and resolved.is_file():
                path = resolved
        elif filename.startswith("img/generic/") and filename.endswith(".svg"):  # силуэты — часть репозитория
            resolved = (ROOT / "ui" / filename).resolve()
            if GENERIC_IMAGES in resolved.parents and resolved.is_file():
                path = resolved
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    from .ui.api import setup as setup_api
    app.router.add_post(prefix + "/api/catalog/apk", guarded(runtime.catalog.upload))
    app.router.add_post(prefix + "/api/setup/protocol", guarded(lambda request: setup_api.upload(request, runtime)))
    app.router.add_post(prefix + "/api/setup/images", guarded(lambda request: setup_api.refresh_images(request, runtime)))
    app.router.add_route("PUT", prefix + "/api/catalog/apps/{id}", guarded(runtime.catalog.edit))
    app.router.add_route("DELETE", prefix + "/api/catalog/apps/{id}", guarded(runtime.catalog.delete))
    app.router.add_route("*", prefix + "/api/{action}", api)
    app.router.add_get(prefix + "/{file:.*}", static)
