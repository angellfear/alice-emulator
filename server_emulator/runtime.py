"""Standalone emulator state, providers and management API."""
import asyncio
import copy
import datetime
import json
import logging
import os
import time
import traceback
import uuid
from pathlib import Path

from aiohttp import ClientTimeout, WSMsgType, web
from google.protobuf import text_format
from google.protobuf.message import DecodeError

from .dispatcher import Dispatcher
from .provider_manager import ProviderManager
from .lifecycle import Lifecycle, RequestContext
from .settings_service import DEFAULT, RECORD_MODES, ROUTE_MODES, SettingsService, masked_secret, validate_url
from .schema import SetupRequired, load_type, schemas_ready
from . import station_state
from .device_images import DeviceImages, images_dir
from .state_store import StateStore
from .endpoints import EndpointCatalog
from .params import ParamStore
from .quasar_rules import QUASAR_RULES_DIR, RuleStore
from . import aapi_handlers
from . import providers as providers_registry
from .aapi_handlers import has_handler as aapi_has_handler
from . import smart_home  # noqa: F401  (реестр умного дома и обработчик TextInput-действия)
from .voice import Conversation
from .identity import IdentityRegistry, collect_device_id
from .quasar import QuasarBackend
from . import actions
from .actions import push_message, execute_message
from .updates import Updates
from .auth import AuthStore, secret
from .ws_close import SocketCloser

# AAPI-сообщения, на которые облако не отвечает (или выключенные настройками aapi_model/aapi_voice):
# в очередь «Без ответа» не попадают.
AAPI_NO_REPLY = {'Ack', 'StreamControl', 'TextInput', 'VoiceInput'}

ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)
STANDALONE_KEY = web.AppKey("emulator_standalone", bool)

# событие SpeechTrace → микросостояние текущего хода для строки состояния в шапке
# (GET api/live). Событий нет в списке — состояние не меняется; «вызывает инструмент»
# пишет agent/tools.py (инструменты исполняются внутри графа, трассы там нет).
ACTIVITY_STATES = {
    "voice_input_start": "listening", "asr_partial": "listening",
    "run_start": "thinking", "asr_final": "thinking", "endpoint": "thinking", "async_wait": "thinking",
    "model_done": "thinking",
    "tts_start": "responding", "tts_done": "responding", "tts_audio": "responding", "async_answer": "responding",
    "scenario_handled": "idle", "alice_response": "idle", "spoken_reply": "idle", "no_speech_retry": "idle",
    "async_wait_reply": "idle", "async_push": "idle", "standard_reply": "idle", "server_action_handled": "idle",
    "callback_default_ack": "idle",
    "stream_close": "idle", "client_close": "idle", "run_cancelled": "idle", "cancel": "idle",
    "no_speech": "idle", "asr_abandoned": "idle",
    "pipeline_error": "error", "tts_failed": "error", "async_push_failed": "error", "failure_reply": "error",
}
# этап сбоя конвейера (stage в pipeline_error) → речевой сервис красной строки шапки
SERVICE_BY_STAGE = {"asr": "asr", "model": "llm", "tts": "tts"}
# Прежний дефолт удалённого параметра server.weather.url — значения с ним переносить
# не нужно (настройка плагина пуста = тот же URL).

PHRASE_LIST_MAX_AGE = 60.0  # свежесть (с) записи датасета для закрытых списков справочника фраз
PHRASE_LIST_TIMEOUT = 5.0   # ожидание плагина датасета в фоновом опросе (голос не ждём)
PHRASE_LIST_POLL = 5.0      # не чаще (с): проверка свежести списков при обращении к справочнику

# Опрос состояния колонки: сервер шлёт SyncPush{GetDeviceState}, колонка отвечает
# SyncPushResponse (протокол — quasar-protos sync_push/**). Ожидание ответа — не дольше
# FETCH_STATE_TIMEOUT_S; частота опросов одного устройства ограничена настройкой
# settings["fetch_state"]["min_interval_s"] (секунды, 0 — без лимита).
FETCH_STATE_TIMEOUT_S = 5.0
EXECUTE_TIMEOUT_S = 5.0  # ожидание квитанции SyncPushResponse у ExecuteDirective
ASSISTANT_NAMES = {"alisa": "Алиса", "yandex": "Яндекс", "yasmina": "Ясмина"}


class FetchStateThrottled(Exception):
    """Опрос состояния чаще settings["fetch_state"]["min_interval_s"]. API отвечает 429;
    программный вызов ловит исключение и пропускает (тихий отказ)."""

    def __init__(self, retry_after_s):
        super().__init__("Опрос состояния: повторите через %d с" % max(1, int(retry_after_s)))
        self.retry_after_s = float(retry_after_s)


class FetchStateTimeout(Exception):
    """Колонка не ответила SyncPushResponse за отведённое время."""


class FetchStateSuperseded(Exception):
    """Ожидание ответа отменено: по устройству пришёл новый опрос."""


def sync_push_message(schemas_dir):
    """SyncPush{GetDeviceState} на проводе: TServerMessage{Event{Header{MessageId: uuid,
    Ack: now_ms}, SyncPush{PushId=1, DeduplicationPushId=2, SyncRequest=3{GetDeviceState=1{
    GetDeviceStateOptions=1{}, GetEnvironmentStateOptions=2{}}}}}} — пустые опции в кадре
    есть (SetInParent), как у настоящего сервера."""
    cls = load_type(schemas_dir, "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    message = cls()
    message.Event.Header.MessageId = uuid.uuid4().hex
    message.Event.Header.Ack = int(time.time() * 1000)
    push = message.Event.SyncPush
    push.PushId = uuid.uuid4().hex
    push.DeduplicationPushId = uuid.uuid4().hex
    push.SyncRequest.GetDeviceState.GetDeviceStateOptions.SetInParent()
    push.SyncRequest.GetDeviceState.GetEnvironmentStateOptions.SetInParent()
    message.Timings.SendingTime.seconds = int(time.time())
    return message


@aapi_handlers.aapi_handler('SyncPushResponse')
async def _on_sync_push_response(conversation, incoming, trigger):
    """Ответ колонки на наш SyncPush — ожидающему этого устройства (по сессии,
    в runtime.fetch_waiters / runtime.execute_waiters): GetDeviceStateResponse —
    fetch_state, ExecuteDirectiveResponse — квитанция push-действия. Событие
    обработано всегда — это ответ на НАШ push, в «Без ответа» ему не место,
    наверх в «Вместе» он не релеится (hybrid.relay пересылает только StreamControl
    и Ack)."""
    waiters = conversation.runtime.fetch_waiters \
        if incoming.Event.SyncPushResponse.SyncResponse.WhichOneof('Response') == 'GetDeviceStateResponse' \
        else conversation.runtime.execute_waiters
    waiter = waiters.get(conversation.trace_context.get('sn') or '')
    if waiter is not None and not waiter.done():
        waiter.set_result(incoming)
    return True


class Runtime:
    def __init__(self, directory=None, catalog_root=None):
        self.directory = Path(directory or ROOT / "state")
        self.directory.mkdir(parents=True, exist_ok=True)
        from . import proto_schemas
        proto_schemas.DIRECTORY = self.directory / "proto"  # схемы, импортированные из libquasar_daemons.so
        # Миграции схем SQLite до открытия соединений хранилищ. Ветка журнала сервера
        # первой: его соединение открывается сразу после — причина сбоя миграций
        # попадает в журнал, сервер не стартует.
        from .migrations import upgrade_all, upgrade_database
        upgrade_database(self.directory / "server_log.sqlite3", "server_log")
        from .server_log import ServerLog, install_logging  # журнал «Дополнительно → Лог»
        self.server_log = install_logging(ServerLog(self.directory / "server_log.sqlite3"))
        upgrade_all(self.directory, skip="server_log", log=self.server_log.log)
        # Единое SQLite-хранилище; legacy JSON мигрируется один раз с backup.
        self.store = StateStore(self.directory / "emulator.sqlite3")
        self.store.on_scenario_data = self._scenario_data_changed  # справочник фраз: lists_from
        from . import music
        music.load_token(self.store)  # захваченный OAuth аккаунта — из БД в кеш поставщика
        # (DEBT-01): дефолты, миграции, валидация и сохранение настроек —
        # в settings_service.SettingsService; runtime держит текущий dict и оркестрацию.
        self.settings_service = SettingsService(self.store, self.directory)
        self.settings = self.settings_service.load()
        self.scenario_dir = self.settings_service.scenario_dir  # один JSON-файл на сценарий
        self.provider_manager = ProviderManager(self)  # (DEBT-01): ASR/TTS/LLM-резолвинг и legacy-цикл
        self.recent = []
        self.pending_calls = []
        self.speech_log = []
        # состояние для строки панели — микросостояние текущего хода («ожидает/
        # слушает/думает/вызывает инструмент/отвечает/ошибка») и последний сбой речевого
        # сервиса. Оба — общие изменяемые объекты: scoped-копии runtime (copy.copy) делят их
        # с корнем и пишут in-place, как в speech_log/tool_log.
        self.agent_activity = {"state": "idle", "tool": None, "error": None, "ts": 0.0}
        self.voice_errors = {}  # asr | tts | llm → {"error", "ts"} — красный значок в шапке
        # SN → ошибки последней сборки конфига колонки (quasar_rules._record_errors):
        # нескрываемое уведомление UI, снимается следующей сборкой без ошибок
        self.quasar_errors = {}
        self.settings_rev = time.time()  # ревизия настроек: сервер отвергает устаревшую копию формы 
        import itertools
        self.sequence = itertools.count(1)  # SequenceNumber ответов (реальный счётчик сервера)
        self.lock = asyncio.Lock()
        self.identities = IdentityRegistry(self.directory, self.store)
        # установленные приложения колонок: SN → [{name, package, version_code}] из
        # состояния запроса (station_state, cap 39); колонка присылает список в каждом
        # запросе, своего ввода у эмулятора нет
        self.device_apps = {}
        self.quasar_params = ParamStore(self.store.db,  # params/ расширений поверх БД
                                        extension_dirs=lambda: self._extension_layers("params"))
        self.quasar_params.seed()  # каталог параметров по умолчанию, идемпотентно
        self.sessions = {}
        self._phrase_lists_checked = 0.0  # последняя проверка свежести списков справочника фраз
        # fetch_state: sn → Future(SyncPushResponse) — ответ маршрутизирует обработчик
        # SyncPushResponse; словарь общий с корнем (scoped-копии runtime его делят)
        self.fetch_waiters = {}
        # квитанции ExecuteDirective: sn → Future(SyncPushResponse) — тот же обработчик
        # SyncPushResponse; на устройстве одно ждущее действие (кнопка панели),
        # корреляция — RefMessageId нашего кадра
        self.execute_waiters = {}
        self.closer = SocketCloser()  # закрытие WS колонок в фоне, без ожидания ответа клиента
        self.quasar_sync_headers = {}  # последние заголовки get_sync_info по SN, только память
        self.quasar = QuasarBackend(self)
        self.action_log = []
        self.tool_log = []  # вызовы инструментов моделью (MCP-хаб), последние 100
        from . import agent
        self.agent = agent.create(self.directory)  # None — LangChain не установлен, только legacy
        self.routines = agent.create_routines(self)  # задания по расписанию (agent/routines.py) или None
        self.mcp_errors = {}  # последняя ошибка tools/list по серверу mcp.json (вкладка MCP)
        from .catalog import Catalog
        self.catalog = Catalog(catalog_root or self.directory,  # catalog/ расширений
                               extension_dirs=lambda: self._extension_layers("catalog"))
        self.updates = Updates(self.directory,  # updates/ расширений
                               extension_dirs=lambda: self._extension_layers("updates"))
        self.endpoints = EndpointCatalog(self.directory / "endpoints",  # каталог захваченных ответов
                                         extension_dirs=lambda: self._extension_layers("endpoints"))
        from .self_update import Service  # проверка релизов и статус для кнопки обновления
        self.self_update = Service(self)
        from .miss_journal import MissJournal, exact  # очередь «Эмулятор → Без ответа»
        self.phrase_misses = MissJournal(self.directory / "phrase_misses.json")  # фразы без сценария
        self.aapi_misses = MissJournal(self.directory / "aapi_misses.json", key=exact)  # AAPI без ответа
        self.callback_misses = MissJournal(self.directory / "callback_misses.json", key=exact)  # колбэки без сценария
        from .endpoint_scripts import ScriptRunner
        self.endpoint_scripts = ScriptRunner()  # обработчики <id>.py (режим script)
        from .voice_stack import VoiceStack
        self.voice_stack = VoiceStack(self)  # варианты ASR/TTS/LLM + локальные речевые серверы
        # замены sync-конфига: общие и по платформам; поставляемый набор — из пакета,
        # ALICA_QUASAR_RULES_DIR подменяет каталог набора (тесты, по образцу ALICA_SCHEMAS_DIR)
        shipped_rules = os.environ.get("ALICA_QUASAR_RULES_DIR") or QUASAR_RULES_DIR
        self.quasar_rules = RuleStore(self.directory / "quasar",
                                      extension_dirs=lambda: self._extension_layers("quasar"),
                                      shipped_dir=shipped_rules)
        self.device_images = DeviceImages()  # картинки с веб-версии умного дома — только по согласию (api/setup/images)
        self.auth = AuthStore(self.directory)  # Принципалы/сессии панели (нет файла — открытый режим)
        # менеджер расширений; включённые загружаются сразу (без сети и хуков),
        # точки подключения берут каталоги из runtime.extensions.contribution_dirs().
        from .extensions.manager import ExtensionManager
        self.extensions = ExtensionManager(self)
        self.extensions.load_enabled()
        # требования потребителей видов (providers.require) — при старте ядра:
        # потребитель импортируется лениво, записи реестра дообогащаются его функциями
        from .scenarios.informers import news as _news_consumer  # noqa: F401
        from .scenarios.media import video as _video_consumer  # noqa: F401
        from .sdk import bind_runtime  # sdk.send_push/send_directive без ctx
        bind_runtime(self)
        # реестр умного дома: устройства/комнаты расширений + директивы колонке
        from . import smart_home
        self.smart_home = smart_home.SmartHomeRegistry(self)
        self.smart_home.subscribe(lambda: self._smart_home_push())
        # check_template (validate.py про runtime не знает) разрешает {ссылки}
        # сценариев на списки включённых расширений — как при матчинге шаблонов.
        from .scenario_engine import templates as scenario_templates
        scenario_templates.set_extension_sources(self._extension_lexicon_sources)
        self.client_peer = None  # внедряет встроенный прокси: резолвер реального пира фронта
        self.front_observed = None  # внедряет фронт TLS: домены вне сертификата (имя → {count, first}),
        self.front_sans = None  # внедряет фронт TLS: DNS-SAN сертификата, который он видит (api/server-front)
        self.update_fetch = None
        # Скрипты-расширения UI от хоста (прокси подключает свои кнопки);
        # standalone-эмулятор их не имеет — список пуст.
        self.ui_extensions = []
        self.lifecycle = Lifecycle()
        from .animations import AnimationStore
        self.animations = AnimationStore(self.directory / 'animations',  # animations/ расширений
                                         extension_dirs=lambda: self._extension_layers("animations"))
        from .animation_storage import AnimationProjectStore
        self.animation_projects = AnimationProjectStore(self.directory / 'animations')
        from . import screen_templates
        screen_templates.configure(self.directory / screen_templates.STATE_DIRNAME)  # шаблоны директив: пользовательские и переопределения
        from .behavior import set_extension_layers  # replies/ расширений
        set_extension_layers(lambda: self._extension_layers("replies"))
        # Диалоговый слой сценариев; пуст по умолчанию — заглушки не меняются
        from .scenario_engine.dialog import DialogManager
        from .scenario_engine.executor import Executor
        from .scenario_engine import library
        from .scenario_engine.library import LEXICONS
        # серверных таймеров больше нет — состояние колонки в её запросе,
        # директивы timer/alarm уходят колонке штатно (station_state, clock_directives)
        self.fetch_cache = {}  # (url, query) → (ts, json): кеш GET JSON (ctx.fetch_json, плагины)

        # --- эффекты-примитивы сценариев: без текстов, словарей и выбора веток ---
        def _input(execution):
            return (getattr(execution, "ctx", None) or {}).get("input") or {}

        async def timer_start(arguments, execution):
            """timer.start — не серверное расписание, а штатные директивы
            колонке (open_screen + timer_set_directive): таймер живёт у колонки."""
            from . import clock_directives
            source = _input(execution)
            sn = arguments.get("sn") or source.get("sn") or ""
            seconds = int(arguments["seconds"])
            screens = ((getattr(execution, "ctx", None) or {}).get("station") or {}).get("screens") or {}
            timer_id = clock_directives.new_clock_id()
            items = clock_directives.timer_set_items(timer_id, seconds, screen_open=bool(screens))
            message = clock_directives.push_message(ROOT / "schemas", items)
            sent = await self.send_message(sn, message)
            return {"id": timer_id, "seconds": seconds, "label": arguments.get("label"),
                    "delivery": "sent" if sent else "no_session"}

        async def alarm_start(arguments, execution):
            """alarm.start — директива alarm_add_directive колонке (образец
            2026-10-22 05:47:24); ближайшая дата — сегодня/завтра по времени колонки."""
            import zoneinfo
            from . import clock_directives
            source = _input(execution)
            sn = arguments.get("sn") or source.get("sn") or ""
            hhmm = str(arguments["time"])
            hours, minutes = (int(part) for part in hhmm.split(":"))
            now = datetime.datetime.now()
            tz_name = str(arguments.get("timezone") or "")
            if tz_name:
                try:
                    now = now.astimezone(zoneinfo.ZoneInfo(tz_name))
                except ValueError:
                    tz_name = ""
            when = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
            if when <= now:
                when += datetime.timedelta(days=1)
            offset = int(now.utcoffset().total_seconds()) if now.utcoffset() is not None else 0
            alarm_id = clock_directives.new_clock_id()
            items = clock_directives.alarm_add_items(
                alarm_id, when, hours, minutes, tz_name or str(now.tzname() or ""), offset)
            message = clock_directives.push_message(ROOT / "schemas", items)
            sent = await self.send_message(sn, message)
            return {"id": alarm_id, "time": hhmm, "delivery": "sent" if sent else "no_session"}

        async def timer_cancel(arguments, execution):
            """Отмена — по состоянию колонки из контекста запроса (своего
            хранилища нет): активный таймер → timer_cancel_directive, else 0.
            Будильники — 0: образца TAlarmCancelDirective в журнале нет (БЛОКЕР)."""
            from . import clock_directives, station_state
            if (arguments.get("kind") or "timer") != "timer":
                return 0
            station = (getattr(execution, "ctx", None) or {}).get("station") or {}
            timers = station_state.active_timers(station.get("timers") or [],
                                                 datetime.datetime.now().timestamp())
            if not timers:
                return 0
            sn = arguments.get("sn") or _input(execution).get("sn") or ""
            items = clock_directives.timer_cancel_items(timers[0]["timer_id"])
            message = clock_directives.push_message(ROOT / "schemas", items)
            return 1 if await self.send_message(sn, message) else 0

        async def device_push(arguments, execution):
            item = await self.push_action(arguments.get("sn") or _input(execution).get("sn") or "", arguments["action"],
                                          arguments.get("arguments") or {})
            return item["delivery"]

        async def device_set_room(arguments, execution):
            """Запомнить комнату колонки (та же правка, что «Комната» в кабинете)."""
            device = (getattr(execution, "ctx", None) or {}).get("device") or {}
            sn = arguments.get("sn") or _input(execution).get("sn") or device.get("sn") or ""
            saved = await self.rename_device(sn, device.get("user_id"), room=str(arguments["room"]))
            return saved.get("room") or ""

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

        from .scenario_plugins import weather_plugin  # плагины датасетов (weather — диспетчер поставщика)
        # встроенные реестры — база пересборки (включённые расширения добавляются
        # поверх при каждом load_enabled); executor.plugins — тот же объект, что scenario_plugins
        # (заглушки тестов и tools подменяют weather на месте).
        self._builtin_actions = {"timer.start": timer_start, "alarm.start": alarm_start,
                                 "timer.cancel": timer_cancel, "device.push": device_push,
                                 "light.set": light_set,
                                 "device.set_room": device_set_room}
        self._builtin_plugins = {"weather": weather_plugin(self)}
        self.scenario_plugins = dict(self._builtin_plugins)
        from .tts_cache import TtsCache
        self.tts_cache = TtsCache(self.directory / "tts_cache")
        self.tts_cache.is_static = lambda text: text in self._static_phrases()

        def scenario_script_path(scenario):
            """Файл .py py-сценария. Встроенный — рядом с его JSON в
            библиотеке (_file — «<раздел>/<id>.json» относительно library.ROOT);
            сценарий расширения (_origin «extension:<id>») — рядом с его
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
        self.rebuild_extension_registries()  # действия/датасеты включённых расширений

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
            # биометрия говорящего и состояние возможностей колонки — из WS-сессии
            session = next((s for s in self.sessions.values() if sn and s.get("device_sn") == sn), None)
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
            from .spoken import voice_output
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
                               "apps": copy.deepcopy(self.device_apps.get((device or {}).get("sn")) or [])
                               if device else [],
                               "platform": platform,
                               "fields": copy.deepcopy((device or {}).get("fields") or {}),
                               "capabilities": {name: (entry or {}).get("preview") or ""
                                                for name, entry in ((session or {}).get("capabilities") or {}).items()}},
                    "params": params, "home": {"devices": home},
                    "speaker": copy.deepcopy((session or {}).get("speaker")),
                    "settings": dict(zip(("tts_speak", "tts_card"), voice_output(self)),
                                     weather=copy.deepcopy(self.settings.get("weather") or {}),
                                     alice2_settings=bool(self.settings.get("alice2_settings")))}
        self._scenario_context = _scenario_context
        self.dialog = DialogManager(self.scenario_executor, self._scenarios,
                                    lambda: load_type(ROOT / "schemas", "alice-api.pb",
                                                      "NAlice.NAliceApi.TServerMessage")(),
                                    lexicon_sources=self._lexicon_sources())
        # справочник фраз плагинов и его окружение — сразу при создании диалога
        # (полная пересборка реестров в старте ранняя, обновление — в rebuild)
        self.rebuild_phrase_directory()
        self.dialog.phrase_order = lambda: {**(self.settings.get("phrase_order")
                                             if isinstance(self.settings.get("phrase_order"), dict)
                                             else {}), "selected": {}}
        self.dialog.extension_context = lambda ext_id: (
            self.extensions.context(ext_id) if ext_id in self.extensions.loaded else None)

        def _search_timeout():
            try:
                return float(self.quasar_params.resolve("server.providers.search_timeout") or 3)
            except Exception:  # noqa: BLE001 — параметра нет/битый: умолчание
                return 3.0
        self.dialog.search_timeout = _search_timeout
        # (DEBT-01): HTTP-маршрутизация (dispatch) вынесена в Dispatcher;
        # runtime остаётся владельцем WS-сессий и состояния, к которому dispatcher обращается.
        self.dispatcher = Dispatcher(self)
        # проход наверх — резолвер внешнего DNS, пересылка HTTP и мост WS
        # + журнал трафика state/traffic.sqlite3. Правило пересылки — режимы маршрута
        # устройств: dispatcher.route_mode_for + upstream.target_for.
        from .upstream import Upstream
        self.upstream = Upstream(self)

    async def close(self):
        """Идемпотентная остановка общих ресурсов (клиент, фоновые задачи)."""
        for variant in list(self.voice_stack.local.processes):  # локальные речевые серверы
            self.voice_stack.local.stop_key(variant, wait=False)  # убить сразу, выхода не ждать
        if self.routines is not None:
            self.routines.shutdown()
        await self.self_update.close()  # цикл проверки релизов и его клиент
        await self.server_log.close()  # доработать очередь журнала сервера
        if self.agent is not None:
            await self.agent.ops.close()  # соединение журнала прогонов агента
        if self.routines is not None:
            await self.routines.close()
        await self.updates.close()  # соединение базы обновлений и суточный опрос
        await self.closer.drain()
        await self.upstream.close()  # доработать очередь журнала, закрыть клиента прохода
        await self.lifecycle.close()
        await self.store.close()  # дописать очередь записи в файл
        # доработать отменённые фоновые задачи (батчи stора памяти langgraph
        # отменяются в __del__ без ожидания) — иначе «Task was destroyed but it is pending!»
        await asyncio.sleep(0)

    # --- каталоги точек подключения расширений ---------------------------

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
        """Источники словарей шаблонов для DialogManager: встроенный
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
        включённых расширений, их каталог tts_cache не видит. Пользовательская
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
        """Запись в журнал recent (сбои сценариев — Executor вызывает
        этот колбэк); тот же формат и лимит, что у остальных записей."""
        self.recent = (self.recent + [entry])[-30:]
        if entry.get("kind") == "scenario":  # сбои сценариев — и в журнал сервера
            self.server_log.log("error", "scenarios",
                                f"Ошибка сценария {entry.get('id', '')}: {entry.get('error', '')}")

    # реестры действий и датасетов включённых расширений -------------------
    def rebuild_extension_registries(self):
        """Пересборка реестров Executor из включённых расширений (старт и каждое
        включение/выключение — хук в extensions.load_enabled): actions/*.py объявляют
        ACTIONS = {"имя": fn}, datasets/*.py — DATASETS = {"имя": async (key, args) →
        значение}. Модули грузит общий ScriptRunner (mtime-кеш — правка файла в режиме
        разработки без рестарта, как у режимов); записи обновляются на месте —
        объекты реестров (и алиас scenario_plugins) не подменяются.

        Сверка разрешения (правило): имя действия обязано начинаться с префикса
        расширения (contributes.actions.prefix или id, manager.action_prefix) ИЛИ быть
        объявленным в permissions.actions; имя вне префикса без объявления — противоречие.
        Имя датасета — всегда с префикса (контракт extensions.md, «Имена элементов»).
        Конфликт со встроенным реестром (timer.start…, weather) или другим расширением
        (владеет тот, чей id раньше по сортировке) → расширение НЕ включается целиком:
        запись уходит из extensions.loaded с понятной ошибкой в extensions.warnings
        (как у битого манифеста); битый модуль — та же реакция. Остальные
        расширения и встроенные примитивы работают как раньше."""
        executor = getattr(self, "scenario_executor", None)
        extensions = getattr(self, "extensions", None)
        if executor is None or extensions is None or not hasattr(extensions, "loaded"):
            return  # старт: Executor ещё не собран — соберёт вызов в конце __init__
        runners = getattr(self, "_extension_runners", None)
        if runners is None:
            from .scripting import ScriptRunner
            runners = self._extension_runners = (ScriptRunner(prefix="ext_actions"),
                                                 ScriptRunner(prefix="ext_datasets"),
                                                 ScriptRunner(prefix="ext_aapi"),
                                                 ScriptRunner(prefix="ext_providers"))

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
        aapi_dirs = folders("aapi")  # обработчики событий колонки из расширений
        # поставщики данных: объявления contributes.providers (модуль называет точку
        # подключения сам)
        declarations = {}
        for ext_id_d, decl, base in extensions.provider_declarations():
            declarations.setdefault(ext_id_d, []).append((decl, base))
        actions, plugins = dict(self._builtin_actions), dict(self._builtin_plugins)
        action_owner = {name: "" for name in self._builtin_actions}  # "" — встроенное
        dataset_owner = {name: "" for name in self._builtin_plugins}
        bad = {}
        aapi_handlers.drop_extensions()  # пересборка — расширения регистрируются заново
        providers_registry.reset()  # реестр поставщиков данных собирается заново
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
            if error is None:
                # aapi/*.py — обработчики событий колонки (sdk.aapi_handler/on_event);
                # событие обязано быть в permissions.aapi — иначе не регистрируется (warning)
                from . import sdk as sdk_module
                try:
                    with sdk_module.loading_extension(ext_id):
                        for path in aapi_dirs.get(ext_id, []):
                            for file in sorted(path.glob("*.py")):
                                try:
                                    runners[2]._module(file)
                                except Exception as exc:  # noqa: BLE001 — битый модуль отключает расширение
                                    raise ValueError(f"{file.name}: модуль не загружается "
                                                     f"({type(exc).__name__}: {str(exc)[:150]})") from exc
                except ValueError as exc:
                    error = str(exc)
                allowed = [a for a in (manifest["permissions"].get("aapi") or []) if isinstance(a, str)]
                for registration in sdk_module._pending():
                    for trigger in registration["triggers"]:
                        if str(trigger) not in allowed:
                            extensions.warnings.append(
                                {"id": ext_id, "error": f"aapi {trigger!r}: события нет в "
                                 f"permissions.aapi — обработчик не зарегистрирован"})
                            continue
                        aapi_handlers.register_extension(str(trigger), registration["handler"],
                                                         registration["enabled"], ext_id,
                                                         observer=registration["observer"])
            if error is None:
                # поставщики данных: объявления contributes.providers (модуль называет
                # точку подключения сам); ошибка модуля или конфликт id в пределах
                # вида — расширение не включается, как у actions
                try:
                    for decl, base in declarations.get(ext_id, []):
                        providers_registry.collect_declaration(ext_id, decl, base, runners[3])
                except ValueError as exc:
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
        providers_registry.apply()  # реестр поставщиков = успешный проход
        executor.actions.clear()
        executor.actions.update(actions)
        executor.plugins.clear()
        executor.plugins.update(plugins)
        # набор включённых расширений сменился — пересобрать каталоги точек
        # подключения (эндпоинты) и источники словарей шаблонов. На старте Executor ещё
        # без dialog (источники придут при его создании), endpoints.reload просто перечитает.
        endpoints = getattr(self, "endpoints", None)
        if endpoints is not None and hasattr(endpoints, "reload"):
            endpoints.reload()
        dialog = getattr(self, "dialog", None)
        if dialog is not None and hasattr(dialog, "lexicon_sources"):
            dialog.lexicon_sources = self._lexicon_sources()  # новый список — кеш шаблонов пересоберётся
            # справочник фраз плагинов (каркас «от плагинов»): файлы из объявлений;
            # битый файл — warning, остальные работают
            self.rebuild_phrase_directory()
            dialog.phrase_order = lambda: {**(self.settings.get("phrase_order")
                                            if isinstance(self.settings.get("phrase_order"), dict)
                                            else {}),
                                           "selected": {kind: (providers_registry.selected(kind, self.settings) or {}).get("ext_id", "")
                                                        for kind in providers_registry.PROVIDERS}}
            dialog.extension_context = lambda ext_id: (
                self.extensions.context(ext_id) if ext_id in self.extensions.loaded else None)
            dialog.search_timeout = lambda: _search_timeout()

            def _search_timeout():
                try:
                    return float(self.quasar_params.resolve("server.providers.search_timeout") or 3)
                except Exception:  # noqa: BLE001 — параметра нет/битый: умолчание
                    return 3.0

    # --- справочник фраз: закрытые списки из записей датасета плагинов ------------
    def rebuild_phrase_directory(self):
        """Скомпилированный справочник фраз плагинов в диалог (файлы из объявлений,
        manager.phrase_files: contributes.providers[].phrases и contributes.phrases,
        готовность файла по настройкам `requires`, закрытые списки `lists_from`);
        битый файл — warning в журнал, остальные работают. Прочитанные записи
        датасета запоминаются: их изменение пересобирает справочник
        (_scenario_data_changed), свежесть списков поддерживает фоновый опрос
        (_refresh_phrase_lists)."""
        if getattr(self, "dialog", None) is None:
            return
        from .scenario_engine.phrases import PhraseDirectory
        directory = PhraseDirectory(self.extensions.phrase_files(), self.dialog.lexicon_sources)
        for message in directory.warnings:
            log.warning("Справочник фраз: %s", message)
        def phrase_directory():
            # обращение реплики к справочнику — повод проверить свежесть закрытых списков:
            # на старте цикла ещё нет, сбой плагина (неверный токен) не должен оставлять
            # фразы плагина выключенными до следующей пересборки реестров
            if time.time() - self._phrase_lists_checked >= PHRASE_LIST_POLL:
                self._refresh_phrase_lists()
            return directory
        self.dialog.phrase_directory = phrase_directory
        self._refresh_phrase_lists()

    def _refresh_phrase_lists(self):
        """Фоновая свежесть закрытых списков файлов фраз (lists_from): записи старше
        PHRASE_LIST_MAX_AGE (или нет записи) — обновить плагином датасета; запись
        проходит scenario_data → извещение → пересборка справочника. Голосовой путь
        сети не ждёт: реплика ранжируется по уже собранному списку, свежий придёт
        со следующей сборкой. Нет запущенного цикла (старт, скрипты tools) — ничего:
        записи обновятся при первом обращении реплики к справочнику (не чаще
        PHRASE_LIST_POLL) или пересборке в рантайме."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._phrase_lists_checked = time.time()
        loop.create_task(self._update_phrase_lists())

    async def _update_phrase_lists(self):
        executor = self.scenario_executor
        now = time.time()
        for dataset, key in self.extensions.watched_phrase_lists():
            record = executor.dataset_record(dataset, key)
            if record is not None and now - record[1] < PHRASE_LIST_MAX_AGE:
                continue
            try:
                await executor.refresh_dataset(dataset, key, timeout=PHRASE_LIST_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 — работает старый список, ждём следующий опрос
                log.warning("Обновление датасета %s для справочника фраз не удалось: %s",
                            dataset, str(exc)[:200] or type(exc).__name__)

    def _scenario_data_changed(self, scenario_id, key):
        """Записана запись scenario_data (извещает state_store.on_scenario_data):
        если сборка справочника фраз читала этот датасет (lists_from) — пересобрать
        справочник, закрытый список попадёт в hassil."""
        sid = str(scenario_id)
        manager = getattr(self, "extensions", None)
        if not sid.startswith("dataset:") or manager is None:
            return
        if manager.note_dataset_change(sid[len("dataset:"):], key):
            self.rebuild_phrase_directory()

    def warm_sound_cache(self, delay=0):
        """Фразы раздела «AI агент» звукового кеша — синтез заранее, в фоне (PCM-движок)."""
        from .tts_cache import agent_phrases
        from .spoken import voice_output

        async def run():
            await asyncio.sleep(delay)
            if not voice_output(self)[0]:  # озвучка выключена — TTS-сервер и кеш не нужны
                return
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

    def _bridge_session(self, sn):
        """Мостовое соединение устройства («Прозрачно», upstream/forward.py connections):
        {connection_id, send} — кадр уходит колонке как инъекция эмулятора, журнал
        помечает его source=emulator; None — мостового соединения нет."""
        upstream = getattr(self, "upstream", None)
        if upstream is None:
            return None
        for connection in list(upstream.forwarder.connections.values()):
            if connection.get("send") and self._bridge_sn(connection) == sn:
                return connection
        return None

    def _no_session_reason(self, sn):
        """Причина no_session для панели: устройство в «Прозрачно» — нет мостового
        соединения (колонка не на связи); иначе — сессии нет вовсе."""
        device = next((d for d in self.identities.data["devices"] if d["sn"] == sn), None)
        return "transparent" if (device or {}).get("route_mode") == "proxy" else "offline"

    async def push_action(self, sn, action, arguments):
        """Отправка push-действия в живую сессию устройства по SN: важные действия —
        квитируемый SyncPush{ExecuteDirective} (EXECUTE_ACTIONS), косметика и push_tts —
        обычный Push. Сессия эмулятора или мостовое соединение («Прозрачно» — через
        upstream/forward.py, кадр-инъекция source=emulator). Возвращает запись
        action_log: delivery 'sent'|'failed'|'no_session' (reason 'transparent'/'offline'),
        execution 'unconfirmed' (нет квитанции) или статус 'started'|'completed'|'failed'.
        Квитанция — SyncPushResponse.ExecuteDirectiveResponse (сессия: обработчик
        SyncPushResponse; мост: наблюдатель кадров upstream._bridge_receipt), ждётся
        не дольше 5 с."""
        cls = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        session = next((x for x in self.sessions.values()
                        if x.get("device_sn") == sn and x.get("send")), None)
        bridge = None if session is not None else self._bridge_session(sn)
        if session is None and bridge is None:
            return {"action": action, "delivery": "no_session", "execution": "unconfirmed",
                    "reason": self._no_session_reason(sn)}
        connection = session or bridge
        execute = action in actions.EXECUTE_ACTIONS
        if execute:
            message = execute_message(cls, action, arguments)
        else:
            message = push_message(cls, action, arguments)
        item = {"message_id": message.Event.Header.MessageId, "connection_id": connection["connection_id"],
                "device_sn": sn, "user_id": connection.get("user_id"), "action": action,
                "delivery": "sending", "execution": "unconfirmed"}
        self.action_log = (self.action_log + [item])[-100:]
        try:
            await connection["send"](b"AAPI" + message.SerializeToString())
        except (ConnectionError, RuntimeError):
            item["delivery"] = "failed"
            return item
        item["delivery"] = "sent"
        if not execute:
            return item
        receipt = asyncio.get_running_loop().create_future()
        previous = self.execute_waiters.get(sn)
        if previous is not None and not previous.done():  # новое действие — прежнее ожидание отменяется
            previous.cancel()
        self.execute_waiters[sn] = receipt
        try:
            try:
                response = await asyncio.wait_for(receipt, EXECUTE_TIMEOUT_S)
            except asyncio.TimeoutError:
                return item  # execution остаётся 'unconfirmed' — колонка не ответила
            status = response.Event.SyncPushResponse.SyncResponse.ExecuteDirectiveResponse \
                .ExecutionStatus.WhichOneof("Status")
            item["execution"] = {"ExecutionStarted": "started", "ExecutionCompleted": "completed",
                                 "ExecutionFailed": "failed"}.get(status, "unconfirmed")
        finally:
            if self.execute_waiters.get(sn) is receipt:
                self.execute_waiters.pop(sn, None)
        return item

    def _smart_home_push(self, sn=None):
        """Реестр умного дома изменился: set_iot_user_info живым сессиям (smart_home.py);
        sn — только этой колонке (сразу после опознания — ей нужен текущий список).
        Вызывается из записи реестра — сбой сборки/отправки не должен ломать запись."""
        from . import smart_home
        sn_set = {str(sn)} if sn else {s.get("device_sn") for s in self.sessions.values()
                                       if s.get("send") and s.get("device_sn")}
        if not sn_set:
            return
        if sn and not (self.smart_home.devices() or self.smart_home.rooms()):
            return  # новой колонке показывать нечего; при изменении реестра пустой
                    # список уходит — карточки должны сниматься
        try:
            raw = smart_home.user_info_raw(self.smart_home)
            server_type = load_type(ROOT / "schemas", "alice-api.pb",
                                    "NAlice.NAliceApi.TServerMessage")
        except Exception as exc:  # noqa: BLE001 — без схем/реестра пуша нет
            log.warning("Пуш умного дома не собран: %s", exc)
            return
        for sn in sn_set:
            message = smart_home.iot_push(server_type, sn, "set_iot_user_info", raw)
            self.lifecycle.spawn(self.send_message(sn, message))

    async def send_message(self, sn, message):
        """Отправить готовое сообщение живой WS-сессии устройства по SN — sdk.send_directive.
        True — отправлено; False — сессии нет или сокет упал."""
        session = next((x for x in self.sessions.values()
                        if x.get("device_sn") == str(sn) and x.get("send")), None)
        if session is None:
            return False
        try:
            await session["send"](b"AAPI" + message.SerializeToString())
        except (ConnectionError, RuntimeError):
            return False
        return True

    def _assistant_name(self, device):
        """Имя ассистента из sync-конфига устройства (config.account_config.spotter):
        alisa→Алиса, yandex→Яндекс, yasmina→Ясмина; иное значение — как есть. Битый
        источник конфига не мешает опросу состояния — пустое имя."""
        if device is None:
            return ""
        try:
            from .quasar_config import device_platform
            from .quasar_rules import compose_config
            body = compose_config(self, device_platform(device), device)
            spotter = str((((body or {}).get("config") or {}).get("account_config") or {}).get("spotter") or "")
        except Exception:  # noqa: BLE001 — конфиг не собирается, но состояние опросить можно
            return ""
        return ASSISTANT_NAMES.get(spotter, spotter)

    async def fetch_state(self, sn, timeout=FETCH_STATE_TIMEOUT_S):
        """Опрос состояния колонки: SyncPush{GetDeviceState} в её живую WS-сессию,
        ожидание SyncPushResponse (не дольше timeout, ответ маршрутизирует обработчик
        _on_sync_push_response). Итог — {ts, alarms, timers, volume, player,
        assistant_name} и он же сохраняется в devices.state.fetch_state_last.
        Повторный опрос того же устройства отменяет предыдущее ожидание
        (FetchStateSuperseded); чаще settings["fetch_state"]["min_interval_s"] —
        FetchStateThrottled (0 — без лимита); нет сессии — ValueError, нет ответа
        вовремя — FetchStateTimeout."""
        interval = (self.settings.get("fetch_state") or {}).get("min_interval_s")
        min_interval = float(10 if interval is None else interval)
        device = next((d for d in self.identities.data["devices"] if d["sn"] == sn), None)
        last = ((device or {}).get("state") or {}).get("fetch_state_last") or {}
        elapsed = time.time() - float(last.get("ts") or 0)
        if min_interval > 0 and last.get("ts") and elapsed < min_interval:
            raise FetchStateThrottled(min_interval - elapsed)
        session = next((x for x in self.sessions.values()
                        if x.get("device_sn") == sn and x.get("send")), None)
        if session is None:
            raise ValueError("Колонка не в сети: нет активной WS-сессии " + sn)
        message = sync_push_message(ROOT / "schemas")
        previous = self.fetch_waiters.get(sn)
        if previous is not None and not previous.done():  # повторный опрос — прежнее ожидание отменяется
            previous.set_exception(FetchStateSuperseded("По устройству пришёл новый опрос состояния"))
        future = asyncio.get_running_loop().create_future()
        self.fetch_waiters[sn] = future
        try:
            try:
                await session["send"](b"AAPI" + message.SerializeToString())
            except (ConnectionError, RuntimeError):
                raise ValueError("Сессия закрылась до отправки SyncPush")
            try:
                response = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise FetchStateTimeout(f"Колонка не ответила за {timeout:.0f} с") from None
        finally:
            if self.fetch_waiters.get(sn) is future:
                self.fetch_waiters.pop(sn, None)
        result = station_state.fetch_state_result(response, self._assistant_name(device))
        async with self.lock:  # чтение-запись состояния устройства — под общим замком
            fresh = next((d for d in self.identities.data["devices"] if d["sn"] == sn), None)
            if fresh is not None:
                state = copy.deepcopy(fresh.get("state") or {})
                state["fetch_state_last"] = result
                self.identities.mutate({"operation": "state", "sn": sn, "state": state})
        return result

    def speech_sink(self, record):
        """Приёмник событий SpeechTrace. In-place: scoped-копии runtime в WS-сессиях
        делят этот список с основным — трасса видна из API корневого runtime."""
        from .speech_trace import SPEECH_LOG_LIMIT
        self.speech_log.append(record)
        del self.speech_log[:-SPEECH_LOG_LIMIT]
        self._note_activity(record)  # из тех же событий — состояние строки шапки

    def _note_activity(self, record):
        """Микросостояние хода и сбои речевых сервисов — в одной точке, на существующем
        механизме событий (SpeechTrace). Состояние и флаги ошибок — общие dict'ы,
        scoped-копии runtime пишут их in-place."""
        name = record.get("name")
        if name in ("asr_final", "tts_done", "model_done"):  # сервис ответил — флаг ошибки снимаем
            self.voice_errors.pop({"asr_final": "asr", "tts_done": "tts", "model_done": "llm"}[name], None)
        elif name == "pipeline_error":
            service = SERVICE_BY_STAGE.get(record.get("stage"))
            if service:
                self.voice_errors[service] = {"error": str(record.get("error") or ""), "ts": time.time()}
        elif name == "tts_failed":
            self.voice_errors["tts"] = {"error": str(record.get("error") or ""), "ts": time.time()}
        state = ACTIVITY_STATES.get(name)
        if state:
            error = record.get("error") or (self.agent_activity.get("error") if state == "error" else None)
            self.agent_activity.update(state=state, tool=None, error=error or None, ts=time.time())

    def note_tool_call(self, name):
        """Ход агента вызвал инструмент (agent/tools.py) — имя в микросостоянии."""
        self.agent_activity.update(state="tool", tool=str(name), error=None, ts=time.time())

    def note_tool_done(self):
        """Инструмент вернул результат — агент продолжает думать (следующий вызов или ответ)."""
        if self.agent_activity.get("state") == "tool":
            self.agent_activity.update(state="thinking", tool=None, ts=time.time())

    def start_background(self):
        """Фоновые задачи runtime (задания агента, голосовой стек); идемпотентно.
        планировщика таймеров больше нет — сигнал отдаёт сама колонка."""
        if not getattr(self, "_background_started", None):
            self._background_started = True
            self.lifecycle.spawn(self.voice_stack.autostart())
            if not self.self_update.task:
                self.self_update.start()  # проверка релизов с задержкой и по интервалу
            if self.routines is not None:
                self.lifecycle.spawn(self.routines.start())  # планировщик и пропуски простоя
            self.warm_sound_cache(delay=20)  # локальный TTS поднимается autostart'ом

    def public(self):
        value = self.settings_service.public(self.settings)
        value["_rev"] = self.settings_rev  # форма возвращает её же — сервер сверит при сохранении
        return value

    @staticmethod
    def masked_secret(value):
        return masked_secret(value)  # В UI — только configured/masked, env:VAR открыт

    def refresh_profiles(self):
        for session in self.sessions.values():
            device=self.identities.resolve(session.get('device_id',''))
            session['runtime'].settings=self.identities.settings_for(self.settings,device)

    async def save_settings(self, value, warm=True):
        """Сохранение настроек. warm=False — не прогревать звуковой кеш (смена режима
        «Интеллект» не должна нагружать локальный TTS-сервер)."""
        value = self.settings_service.validate(value, self.settings)
        tts_changed = value.get("tts") != self.settings.get("tts")
        upstream_changed = value.get("upstream") != self.settings.get("upstream")
        async with self.lock:
            self.write("settings.json", value)
            self.settings = value
            self.settings_rev = time.time()  # устаревшие копии форм (с открытой вкладкой) отклоняются
            self.refresh_profiles()
        if upstream_changed:  # смена DNS/пинов резолвинга — кеш не ждёт TTL (PLAN-code-audit)
            resolver = getattr(getattr(self, "upstream", None), "resolver", None)
            if resolver is not None:
                resolver.invalidate()
        self.prune_sound_cache()
        if warm:
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

    def _bridge_sn(self, connection):
        """SN устройства моста прохода наверх: прямой или через DeviceId."""
        sn = connection.get('device_sn')
        if sn:
            return sn
        device_id = connection.get('device_id')
        device = self.identities.resolve(device_id) if device_id else None
        return device.get('sn') if device else None

    def device_sockets(self, match):
        """[(ws, transport)] соединений устройств, чей SN подходит под match(sn): WS-сессии
        эмулятора и мосты прохода наверх (режим «Прозрачно» — их в sessions нет)."""
        entries = [(s['ws'], s.get('transport')) for s in self.sessions.values()
                   if s.get('ws') and match(self._session_sn(s))]
        upstream = getattr(self, 'upstream', None)
        if upstream is not None:
            entries += [(c['ws'], c.get('transport')) for c in list(upstream.forwarder.connections.values())
                        if match(self._bridge_sn(c))]
        return entries

    def disconnect_devices(self, match, message):
        """Закрыть соединения устройств (1012) в фоне — ответ не ждёт колонку.
        Возвращает число закрываемых соединений; колонка переподключается сама."""
        return self.closer.close(self.device_sockets(match), 1012, message)

    async def set_device_route_mode(self, sn, mode):
        """Маршрут одного устройства; mode=None — снять свой маршрут (Эмуляция).
        Соединения закрываются (колонка переподключается сама) только если меняется то, как
        обслуживается открытый WS: эмулятором (emulate, hybrid) или мостом в Яндекс (proxy).
        emulate ↔ hybrid WS не рвёт, HTTP маршрутизируется заново на каждом запросе."""
        if mode not in (None, *ROUTE_MODES):
            raise ValueError("mode: " + " | ".join(ROUTE_MODES) + " или null")
        async with self.lock:
            device = next((d for d in self.identities.data['devices'] if d['sn'] == sn), None)
            before = ((device or {}).get('route_mode') or 'emulate') == 'proxy'
            self.identities.mutate({"operation": "route_mode", "sn": sn, "mode": mode})
            closed = 0
            if before != ((mode or 'emulate') == 'proxy'):
                closed = self.disconnect_devices(lambda device_sn: device_sn == sn, b'Route changed')
        return {"sn": sn, "mode": mode, "disconnected": closed}
    async def rename_device(self, sn, user_id, name=None, room=None):
        """Название/комната колонки: реестр (как правка в «Устройствах») + параметры
        device.name / device.room, затем профили сессий. Под замком; запись устройства
        перечитывается под ним — привязку могли сменить, пока ждали замок."""
        async with self.lock:
            current = next((d for d in self.identities.data["devices"]
                            if d["sn"] == sn and d.get("user_id") == user_id), None)
            if current is None:
                raise ValueError("Устройство отвязано от пользователя")
            request = {"operation": "device", "sn": sn, "user_id": user_id,
                       "device_ids": current.get("device_ids") or [], "name": current.get("name")}
            if name is not None:
                request["name"] = name
            if room is not None:
                request["room"] = room
            registry = self.identities.mutate(request)
            saved = next(d for d in registry["devices"] if d["sn"] == sn)
            owner = "device:" + sn
            if name is not None and self.quasar_params.def_get("device.name"):
                self.quasar_params.set_value(owner, "device.name", saved["name"])
            if room is not None and self.quasar_params.def_get("device.room"):
                self.quasar_params.set_value(owner, "device.room", saved.get("room") or "")
            self.refresh_profiles()
        return saved


    async def set_device_record_mode(self, sn, mode):
        """Опция «Запись» устройства: off | on | misses. Соединения не закрываются:
        опция читается на каждом обмене и кадре, действует сразу."""
        if mode not in RECORD_MODES:
            raise ValueError("mode: " + " | ".join(RECORD_MODES))
        async with self.lock:
            self.identities.mutate({"operation": "record", "sn": sn, "mode": mode})
        return {"sn": sn, "mode": mode}

    @staticmethod
    def validate_url(value):
        validate_url(value)  # http(s) без credentials — реализация в settings_service

    # (DEBT-01): резолвинг ASR/TTS/LLM-адаптеров, потоковые режимы и legacy-цикл
    # инструментов — в provider_manager.ProviderManager. Вызовы идут с self-аргументом:
    # scoped-копии runtime в WS-сессиях несут настройки профиля устройства.
    # provider/chat — публичные имена (voice.py, ui/api, тесты подменяют их заглушками).
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

    async def chat(self, text, history=None, thread=None, first_sentence=None, **context):
        """Ответ модели: stub → заглушка, graph → LangGraph-агент, иначе legacy-цикл (provider_manager).
        first_sentence — колбэк первой фразы стримингового ответа (предпрогрев озвучки)."""
        return await self.provider_manager.chat(self, text, history, thread=thread,
                                                first_sentence=first_sentence, **context)

    async def _chat_legacy(self, text, history=None):
        return await self.provider_manager._chat_legacy(self, text, history)
    async def mcp(self, server, method, params):
        """MCP-запрос к серверу (streamable HTTP: JSON или SSE) — см. mcp_hub.request."""
        from . import mcp_hub
        return await mcp_hub.request(self, server, method, params)

    # (DEBT-01): маршрутизация HTTP — в dispatcher.Dispatcher; публичные
    # имена сохранены как делегации (их зовут server.py и тесты).
    async def handle_update(self, request, host, path, record=None):
        return await self.dispatcher.handle_update(request, host, path, record)

    def route_mode_for(self, request):
        """(sn, device_id, mode) маршрута запроса — (делегация в dispatcher)."""
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
        oauth_header = request.headers.get('x-uprx-oauth-token')
        if oauth_header and header_device_id:
            device = self.identities.resolve(header_device_id)
            if device and device.get('sn'):
                # OAuth аккаунта из WS-handshake: только от опознанной колонки — в БД для поставщика
                from . import music
                music.note_token(self, oauth_header)
        ws = web.WebSocketResponse(max_msg_size=16 * 1024**2, heartbeat=30)
        await ws.prepare(request)
        if not schemas_ready():  # протокол ещё не извлечён из APK станции
            await ws.close(code=1013, message=b"setup_required")
            return ws
        context = RequestContext(recorder=record)  # идентичность достраивается ниже
        context.record({}, kind="ws", direction="lifecycle", status=101, event="open", size=0, emulated=True)
        # Журнал трафика: опция «Запись» устройства читается на каждом кадре; звук не пишется
        tap = self.upstream.ws_tap("emulator", request.host.split(':', 1)[0].lower(), request.path,
                                   list(request.headers.items()),
                                   handled=lambda trigger: aapi_has_handler(trigger, scoped_runtime.settings))
        client_type = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
        server_type = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
        send_lock = asyncio.Lock()
        async def send(data):
            if data[:4] == b"AAPI":
                # Timings.SendingTime на каждом серверном кадре и InputStartAck.RequestStartTime
                # (микросекунды) — как у настоящего сервера (образец: все его кадры несут
                # Timings, InputStartAck несёт RequestStartTime).
                try:
                    reply = server_type.FromString(data[4:])
                    reply.Timings.SendingTime.GetCurrentTime()
                    if reply.Event.WhichOneof("Event") == "InputStartAck" \
                            and not reply.Event.InputStartAck.RequestStartTime:
                        reply.Event.InputStartAck.RequestStartTime = time.time_ns() // 1000
                    data = b"AAPI" + reply.SerializeToString()
                except DecodeError:
                    pass  # не серверное сообщение — кадр уходит как есть
            async with send_lock:
                await ws.send_bytes(data)
            tap.frame("server_to_client", True, data)
            try:  # «Вместе» : свои плеерные директивы снимают флаг контента Яндекса
                if data[:4] == b"AAPI" and hybrid.yandex_player:
                    reply = server_type.FromString(data[4:])
                    if reply.Event.WhichOneof("Event") == "AliceResponse":
                        names = [d.Name for d in reply.Event.AliceResponse.Response.Directives]
                        if "player_pause" in names or "clear_queue" in names or "audio_play" in names:
                            hybrid.yandex_player = False
            except Exception:
                pass
            context.record({"original": data, "sent": data}, kind="ws", direction="server_to_client", binary=True, size=len(data), emulated=True,
                           device_sn=context.device_sn, user_id=context.user_id)
        session_id = uuid.uuid4().hex
        trace_context = {'session': session_id, 'sn': None, 'device_id': None}
        scoped_runtime = copy.copy(self)
        scoped_runtime.settings = self.identities.settings_for(self.settings, None)
        scoped_runtime.pending_calls, scoped_runtime.recent = [], []
        self.sessions[session_id] = {'connection_id':session_id, 'host':request.host.split(':',1)[0].lower(), 'ip':self.client_ip(request), 'device_sn': None, 'user_id': None, 'runtime': scoped_runtime, 'send':send, 'ws':ws, 'transport': request.transport}
        def delivered(message_id):  # Ack колонки: журнал действий корневого runtime (копия его не видит)
            for item in self.action_log:
                if item['connection_id'] == session_id and item['message_id'] == message_id:
                    item['delivery'] = 'acknowledged'
        conversation = Conversation(scoped_runtime, server_type, send, trace_context=trace_context,
                                    delivered=delivered)
        # «Вместе», фаза 2 : параллельная сессия Яндекса на этот WS — открывается лениво
        # при первой пересылке промаха; режим читается при каждом использовании (меняется на лету)
        from .upstream.hybrid import HybridSession
        hybrid = HybridSession(self, request, send)
        scoped_runtime.hybrid_session = hybrid
        scoped_runtime.hybrid_mode = lambda: self.route_mode_for(request)[2]
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
            # список умного дома — сразу после опознания колонки (set_iot_user_info из реестра)
            if getattr(self, 'smart_home', None) is not None and context.device_sn:
                self._smart_home_push(context.device_sn)

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
                        hybrid.observe("audio", data)  # 098: аудио хода — в копилку ветки
                        await conversation.receive_audio(data)
                        continue
                    incoming = client_type.FromString(data[4:])
                    trigger = incoming.Event.WhichOneof("Event") if incoming.WhichOneof("Message") == "Event" else "StreamControl"
                    # список приложений колонки — из состояния запроса (cap 39), по серийнику;
                    # обновляем на каждом речевом и текстовом запросе
                    if trigger in ('VoiceInput', 'TextInput') and device:
                        event = incoming.Event.VoiceInput if trigger == 'VoiceInput' else incoming.Event.TextInput
                        self.device_apps[device['sn']] = station_state.installed_apps(event)
                    # «Вместе», фаза 3 : кадры голосового хода колонки — в копилку
                    # для релея ветке (VoiceInput открывает ход, аудио и StreamControl копятся)
                    hybrid.observe(trigger, data)
                    # «Вместе» : протокольные ответы колонки (StreamControl по потоку
                    # ветки, Ack, RequestStat) — копией в ветку Яндекса, иначе её диалог
                    # «подвисает» после первого обмена
                    hybrid.relay(incoming, data)
                    if trigger == 'SynchronizeStateRequest':
                        hybrid.keep(data)  # авторизация ветки «Вместе» — её кадр                        # Re-identification requires a new connection: don't mix histories or channels.
                        # sync — авторитетный источник, header — транспортная подсказка;
                        # расхождение независимых источников закрывает соединение (1008).
                        resolved, id_report = collect_device_id(
                            ('sync.Application.DeviceId', incoming.Event.SynchronizeStateRequest.Application.DeviceId),
                            ('header.x-uprx-device-id', header_device_id))
                        current = self.sessions[session_id].get('device_id')
                        if id_report['mismatch'] or (current not in (None, '', resolved)):
                            self.recent = (self.recent + [{'kind': 'ws', 'device_id_report': id_report}])[-30:]
                            self.server_log.log("warning", "ws", "Расхождение DeviceId соединения",
                                                device_id_report=id_report)
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
                    self.server_log.log("error", "ws", "Ошибка обработчика сообщения колонки: " + type(exc).__name__ + ": " + str(exc)[:200],
                                        device_sn=(self.sessions.get(session_id) or {}).get("device_sn") or "",
                                        trace="".join(traceback.format_exception(exc))[-1800:])
                    await ws.close(code=1011, message=b"Emulator handler error")
        finally:
            await conversation.shutdown()
            await hybrid.close()
            self.sessions.pop(session_id, None)
            tap.close(ws.close_code)
            context.record({}, kind="ws", direction="lifecycle", event="close", close_code=ws.close_code, size=0, emulated=True)
        return ws


def mount_ui(app, runtime, prefix="/emulator"):
    # Домены админ-API переезжают в реестр ui/api; реестр спрашивается
    # ДО legacy-цепочки, поверхность контракта фиксирует tests/test_admin_api_surface.py.
    from .ui.api import dispatch as registry_dispatch, get as registry_get, guarded

    async def api(request):
        # Все домены админ-API живут в реестре ui/api; неизвестный action → 404.
        if registry_get(request.match_info["action"]) is None:
            raise web.HTTPNotFound()
        return await registry_dispatch(request, runtime, app)

    # Каталог картинок устройств (модель по SN): только существующие
    # PNG/WebP из каталога картинок (device_images.images_dir) — путь проверяется на выход за пределы папки.
    ASSETS = ("index.html", "toast.js", "ws.js", "templates.js", "app.js", "style.css", "users.js", "backend.js", "updates.js", "catalog.js", "replies.js", "animations.js", "endpoints.js", "misses.js", "endpoints.css", "devices_common.js", "device_cards.js", "mcp.js", "agent.js", "memory.js", "routines.js", "scenario_drafts.js", "params.js", "voice_stack.js", "live_status.js", "sound_cache.js", "extensions.js", "setup.js", "server_config.js", "server_log.js", "smart_home.js", "traffic.js", "providers.js", "configurator.js")
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
    # иконка панели — запросы браузера к серверу не попадают в «HTTP без эндпоинта»
    favicon = ROOT.parent / "tools" / "assets" / "alica.ico"

    async def icon(request):
        if favicon.is_file():
            return web.FileResponse(favicon, headers={"Cache-Control": "max-age=86400"})
        raise web.HTTPNotFound()

    app.router.add_get("/favicon.ico", icon)
    app.router.add_post(prefix + "/api/setup/protocol", guarded(lambda request: setup_api.upload(request, runtime)))
    app.router.add_post(prefix + "/api/setup/images", guarded(lambda request: setup_api.refresh_images(request, runtime)))
    app.router.add_route("PUT", prefix + "/api/catalog/apps/{id}", guarded(runtime.catalog.edit))
    app.router.add_route("DELETE", prefix + "/api/catalog/apps/{id}", guarded(runtime.catalog.delete))
    from .ui.api import me as me_api  # кабинет пользователя: /me и /api/me/*
    me_api.mount(app, runtime, prefix)
    app.router.add_route("*", prefix + "/api/{action}", api)
    app.router.add_get(prefix + "/{file:.*}", static)
