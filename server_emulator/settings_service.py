"""Настройки эмулятора: дефолты, миграции, валидация, сохранение (шаг декомпозиции DEBT-01).

SettingsService владеет жизненным циклом настроек: загрузка из StateStore с миграцией
режимной модели и аддитивной миграцией дефолтов, валидация входящих значений,
маскирование секретов для UI, сохранение ревизией в БД (RF-08: тела сценариев живут
в файлах state/scenarios, ревизия хранит заглушки {id, enabled}).

Runtime держит текущий dict в runtime.settings и оркестрирует сохранение: замок,
профили устройств (refresh_profiles), звуковой кеш — это состояние и заботы runtime.
"""
import copy
import json
import logging
import os
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Режимы маршрута (TASK-044, решение вместо двух режимов 2026-10-24): у каждого
# устройства свой route_mode; без него (и для неизвестных устройств) — emulate (TASK-064,
# маршрута по умолчанию нет). «Запись» — не маршрут, а опция устройства record (TASK-054):
# off | on | misses, пишется поверх любого маршрута.
ROUTE_MODES = ("emulate", "hybrid", "proxy")
RECORD_MODES = ("off", "on", "misses")
ROUTE_MIGRATION_KEY = "route_migration"  # meta StateStore: {"excluded": [sn]} — очередь миграции
RECORD_MIGRATION_KEY = "record_migration"  # meta: маршрут record → proxy + опция record (TASK-054), однократно
DEFAULT = {
    "intelligence": "agent",  # режим «Интеллект»: scenarios | model | agent (intelligence.py)
    "system_prompt": "Ты домашний голосовой помощник. Отвечай кратко по-русски.",
    "model": {"url": "", "model": "", "api_key": "", "temperature": 0.3},
    "stt": {"url": "", "model": "", "api_key": "", "language": "ru"},
    "tts": {"url": "", "model": "", "api_key": "", "voice": "", "response_format": "opus"},
    "mcp_servers": [], "tools": [], "device_context": {}, "aapi_model": False,
    "aapi_voice": False, "aapi_tts": False,
    "builtin_scenarios": True,  # простые команды (время/таймер/будильник/свет/громкость) без AI
    "builtin_disabled": [],  # id встроенных сценариев, выключенных по одному (каталог builtin)
    # Стандартные голосовые ответы: реплики на особые состояния (вне модели). Первый случай —
    # непривязанное устройство: вместо Vins-таймаута колонка озвучит подсказку о привязке.
    "standard_replies": [
        {"id": "unclaimed", "name": "Устройство не привязано", "case": "unclaimed", "enabled": True,
         "text": "Извините, я пока не могу ответить: эта колонка не привязана к пользователю. "
                 "Привяжите её в панели эмулятора, раздел «Устройства», и я заработаю."},
        {"id": "not_understood", "name": "Нет сценария", "case": "not_understood", "enabled": True,
         "text": "Я пока этого не умею."},
    ],
    "weather": {"url": "", "latitude": "", "longitude": "", "ttl_min": 10},
    "scenarios": [],
    # Движок ответа модели (server_emulator/agent): graph — LangGraph-агент с тредами и
    # подтверждениями, legacy — прежний цикл. Прочие ключи — agent/service.py DEFAULTS.
    "agent": {"engine": "graph"},
    # Долговременная память агента (agent/memory.py): эмбеддинги — OpenAI-совместимый /v1/embeddings
    "memory": {"enabled": True, "embed": {"url": "", "model": "", "api_key": ""}, "recall_limit": 6, "reflect_after_s": 120},
    "aliases": {"quasar.local": "quasar.yandex.net", "rpc.local": "rpc.alice.yandex.net", "uniproxy.local": "uniproxy.alice.yandex.net"},
    # Расширения (TASK-029): источники (URL/локальные папки), состояние включённости,
    # установленное (версия/предыдущая/источник/самопроверка) и значения настроек.
    # Файлы живут в state/extensions/<id>/<version>/; детали — extensions/manager.py.
    "extensions": {"sources": []},
    # Проход наверх (TASK-043): внешние DNS-серверы, закреплённые адреса хостов
    # и лимиты журнала трафика. Какие запросы пересылать — режимы маршрута (TASK-044).
    "upstream": {"dns_servers": ["77.88.8.8", "1.1.1.1"], "upstream_ip": {},
                 "journal": {"max_body_kb": 256, "max_size_mb": 100, "max_age_days": 14}},
    # Самообновление (TASK-040): проверка релизов GitHub и обновление по кнопке,
    # автоматически ничего не ставится. token — строка или env:ИМЯ (приватный репозиторий
    # без токена отдаёт 404); restart — кто запускает сервер после обновления.
    "self_update": {"enabled": True, "repo": "angellfear/alica_emule", "channel": "stable",
                    "interval_hours": 12, "token": "", "skipped": "", "restart": "self"},
}


def revision(value):
    """Ревизия настроек в БД хранит сценарии как заглушки {id, enabled}: тела
    живут в файлах state/scenarios (RF-08), история настроек не растёт от
    правок сценариев. Legacy-миграция пишет текст в БД напрямую и не
    прореживается; в памяти Runtime тела сохраняются как были."""
    if not isinstance(value, dict) or not isinstance(value.get("scenarios"), list):
        return value
    stubs = [{"id": s.get("id"), "enabled": bool(s.get("enabled"))}
             for s in value["scenarios"] if isinstance(s, dict) and s.get("id")]
    return {**value, "scenarios": stubs}


def validate_url(value):
    if value and (urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname or urlsplit(value).username):
        raise ValueError("Нужен http(s) URL без credentials в адресе")


def masked_secret(value):
    # B04: в UI возвращается только configured/masked; ссылка env:VAR — не
    # секрет и видна как есть (значение живёт в окружении процесса)
    if isinstance(value, str) and value.startswith("env:"):
        return value
    return "__KEEP__" if value else ""


class SettingsService:
    def __init__(self, store, directory):
        self.store = store
        self.scenario_dir = directory / "scenarios"  # один JSON-файл на сценарий

    def load(self):
        """Текущие настройки из БД: миграция режимной модели (partial больше не
        существует), аддитивная миграция дефолтов, файлы сценариев как источник правды."""
        stored = self.store.load_settings()
        settings = stored or copy.deepcopy(DEFAULT)
        # Миграция режимной модели: partial больше не существует — стоявшие в нём
        # переводятся в эмуляцию; список emulation_ws_local (H05) упразднён. Блок
        # касается только настроек, где legacy-ключ ещё есть (TASK-044: без этого
        # каждая загрузка добавляла mode обратно и перезатирала прежний маршрут по умолчанию).
        legacy_mode = ("mode" in settings and settings["mode"] not in ("proxy", "emulate"))             or "emulation_ws_local" in settings
        if "mode" in settings and settings["mode"] not in ("proxy", "emulate"):
            settings["mode"] = "emulate"
        settings.pop("emulation_ws_local", None)
        from .intelligence import migrate
        legacy_mode = migrate(settings) or legacy_mode  # до аддитивной миграции: выводится из agent.engine
        # Аддитивная миграция дефолтов: настройки production засеяны раньше, чем
        # появились новые ключи (builtin_scenarios и пр.) — доходим их значениями
        # по умолчанию и фиксируем ревизией, не трогая существующие значения.
        missing = {key: copy.deepcopy(value) for key, value in DEFAULT.items()
                   if key not in settings}
        if missing or legacy_mode or stored is None:
            settings.update(missing)
            self.store.save_settings(settings)
        self._migrate_route_modes(settings)
        self._migrate_record_route(settings)
        self._migrate_default_route(settings)
        self.load_scenario_files(settings)
        return settings

    def _migrate_route_modes(self, settings):
        """TASK-044: старая режимная модель (settings.mode + emulation_excluded) →
        маршруты по устройствам. mode: emulate → default emulate; mode: proxy →
        default record. Каждый SN из emulation_excluded попадает в очередь миграции
        (meta route_migration): известным устройствам реестра сразу ставится
        route_mode=proxy (Runtime), подключившиеся позже — при регистрации
        (IdentityRegistry). Сами ключи из настроек удаляются."""
        if "mode" not in settings and "emulation_excluded" not in settings:
            return False
        default = "record" if settings.get("mode") == "proxy" else "emulate"
        excluded = [sn for sn in (settings.pop("emulation_excluded", None) or [])
                    if isinstance(sn, str) and sn.strip()]
        settings.pop("mode", None)
        settings["default_route_mode"] = default  # легаси-режим определяет прежний default (его уберёт _migrate_default_route)
        if excluded:
            pending = self.store.meta(ROUTE_MIGRATION_KEY)
            try:
                known = json.loads(pending).get("excluded", []) if pending else []
            except ValueError:
                known = []
            merged = list(dict.fromkeys([*known, *excluded]))
            self.store.set_meta(ROUTE_MIGRATION_KEY, json.dumps({"excluded": merged}))
            log.info("Миграция маршрутов: %d SN из emulation_excluded переведены на proxy", len(merged))
        self.store.save_settings(settings)
        return True

    def _migrate_record_route(self, settings):
        """TASK-054: «Запись» перестала быть маршрутом. Однократно (отметка в meta):
        устройство с route_mode=record → proxy + record=on; default_route_mode=record →
        proxy, и устройства без своего маршрута получают record=on (прежнее поведение).
        SN из очереди миграции TASK-044 (emulation_excluded) — «прокси без журнала» —
        запись не получают."""
        default_record = settings.get("default_route_mode") == "record"
        if self.store.meta(RECORD_MIGRATION_KEY) and not default_record:
            return False  # остаток прежнего default (режимная миграция TASK-044 после отметки) — переносим снова
        data = self.store.load_identity_data()
        changed = 0
        pending = self.store.meta(ROUTE_MIGRATION_KEY)
        try:
            excluded = set(json.loads(pending).get("excluded", [])) if pending else set()
        except ValueError:
            excluded = set()
        for device in data["devices"]:
            if device.get("route_mode") == "record":
                device["route_mode"], device["record"] = "proxy", "on"
                changed += 1
            elif default_record and not device.get("route_mode") and device["sn"] not in excluded:
                device["record"] = "on"
                changed += 1
        if changed:
            self.store.save_identity_data(data)
        if default_record:
            settings["default_route_mode"] = "proxy"
            self.store.save_settings(settings)
        self.store.set_meta(RECORD_MIGRATION_KEY, "1")
        if changed or default_record:
            log.info("Миграция записи: маршрут record → proxy + опция record (устройств: %d)", changed)
        return True

    def _migrate_default_route(self, settings):
        """TASK-064: маршрута по умолчанию больше нет. Прежний default_route_mode ≠ emulate
        переносится устройствам реестра без своего route_mode (поведение колонок не меняется
        молча), ключ удаляется из настроек. Идёт после legacy-миграций (mode, record):
        они приводят default к emulate | hybrid | proxy."""
        if "default_route_mode" not in settings:
            return False
        default = settings.pop("default_route_mode")
        changed = 0
        if default in ROUTE_MODES and default != "emulate":
            data = self.store.load_identity_data()
            for device in data["devices"]:
                if not device.get("route_mode"):
                    device["route_mode"] = default
                    changed += 1
            if changed:
                self.store.save_identity_data(data)
        self.store.save_settings(settings)
        log.info("Миграция маршрута по умолчанию: «%s» перенесён на устройства без своего маршрута (%d)", default, changed)
        return True

    def persist(self, settings):
        """Сохранение: ревизия в БД (RF-08) + сверка каталога файлов сценариев."""
        self.store.save_settings(revision(settings))
        if isinstance(settings, dict):
            self.sync_scenario_files(settings.get("scenarios", []))

    def public(self, settings):
        value = copy.deepcopy(settings)
        for name in ("model", "stt", "tts"):
            value[name]["api_key"] = masked_secret(value[name].get("api_key"))
        for server in value["mcp_servers"]:
            server["api_key"] = masked_secret(server.get("api_key"))
        embed = (value.get("memory") or {}).get("embed")
        if isinstance(embed, dict):
            embed["api_key"] = masked_secret(embed.get("api_key"))
        if (value.get("agent") or {}).get("webhook_token"):
            value["agent"]["webhook_token"] = masked_secret(value["agent"].get("webhook_token"))
        su = value.get("self_update") or {}
        if su.get("token"):
            su["token"] = masked_secret(su["token"])
        return value

    def validate(self, value, current):
        """Проверка присланных UI настроек → копия, готовая к persist.
        current — прежние настройки для восстановления секретов __KEEP__."""
        # TASK-044/064: устаревшая форма UI присылает старые ключи режимов — не сохраняем их
        value.pop("default_route_mode", None)
        value.pop("mode", None)
        value.pop("emulation_excluded", None)
        from .intelligence import LEVELS, ext_id
        # Встроенный уровень или "ext:<id>" (режим расширения, TASK-034). Валидатор
        # проверяет только формат: включённость расширения с modes/ знает менеджер
        # расширений, её проверяет intelligence.extension_mode при ответе — недоступный
        # режим откатывается на scenarios с записью в recent.
        if value.setdefault("intelligence", current.get("intelligence", "agent")) not in LEVELS \
                and ext_id(value["intelligence"]) is None:
            raise ValueError("intelligence: " + " | ".join(LEVELS) + " или ext:<id>")
        for key in ("tools", "mcp_servers"):
            if not isinstance(value.get(key), list):
                raise ValueError(key + ": нужен массив")
        if not isinstance(value.get("device_context"), dict):
            raise ValueError("device_context: нужен объект")
        value = copy.deepcopy(value)
        from .behavior import validate_standard
        value.pop("behaviors", None)  # раздел «Поведение» удалён: правила из старых settings вычищаются
        validate_standard(value.get('standard_replies', []))
        for name in ("model", "stt", "tts"):
            cfg = value[name]
            validate_url(cfg.get("url", ""))
            if cfg.get("api_key") == "__KEEP__":
                cfg["api_key"] = current[name].get("api_key", "")
        embed = (value.get("memory") or {}).get("embed")
        if isinstance(embed, dict) and embed.get("api_key") == "__KEEP__":
            embed["api_key"] = ((current.get("memory") or {}).get("embed") or {}).get("api_key", "")
        agent_cfg = value.get("agent")
        if isinstance(agent_cfg, dict) and agent_cfg.get("webhook_token") == "__KEEP__":
            agent_cfg["webhook_token"] = (current.get("agent") or {}).get("webhook_token", "")
        # Самообновление (TASK-040): форма без ключа — восстановить из current; присланное — по форме.
        if "self_update" not in value:
            value["self_update"] = copy.deepcopy(current.get("self_update") or DEFAULT["self_update"])
        else:
            su = value["self_update"]
            if not isinstance(su, dict):
                raise ValueError("self_update: нужен объект")
            if not isinstance(su.setdefault("enabled", True), bool):
                raise ValueError("self_update.enabled: нужен булев")
            repo = su.setdefault("repo", DEFAULT["self_update"]["repo"])
            if not (isinstance(repo, str) and len(repo.split("/")) == 2 and all(repo.split("/"))):
                raise ValueError("self_update.repo: нужен owner/name")
            if su.setdefault("channel", "stable") not in ("stable", "beta"):
                raise ValueError("self_update.channel: stable | beta")
            su["interval_hours"] = max(1, int(su.get("interval_hours") or 12))
            if not isinstance(su.setdefault("token", ""), str):
                raise ValueError("self_update.token: нужна строка или env:ИМЯ")
            if su.get("token") == "__KEEP__":
                su["token"] = (current.get("self_update") or {}).get("token", "")
            if not isinstance(su.setdefault("skipped", ""), str):
                raise ValueError("self_update.skipped: нужна строка")
            if su.setdefault("restart", "self") not in ("self", "manager"):
                raise ValueError("self_update.restart: self | manager")
        # Расширения (TASK-029): устаревшая форма UI присылает настройки без ключа —
        # восстанавливаем из current (состояние расширений UI не редактирует);
        # присланное — проверяем по форме.
        if "extensions" not in value:
            value["extensions"] = copy.deepcopy(current.get("extensions") or DEFAULT["extensions"])
        else:
            extensions = value["extensions"]
            if not isinstance(extensions, dict):
                raise ValueError("extensions: нужен объект")
            sources = extensions.setdefault("sources", [])
            if not isinstance(sources, list) or not all(isinstance(s, str) and s.strip() for s in sources):
                raise ValueError("extensions.sources: нужен список URL/папок")
            for key in ("enabled", "installed", "settings"):
                bucket = extensions.setdefault(key, {})
                if not isinstance(bucket, dict):
                    raise ValueError(f"extensions.{key}: нужен объект")
            if not all(isinstance(item, dict) for item in extensions["installed"].values()):
                raise ValueError("extensions.installed: запись на расширение — объект")
        old_servers = {s["id"]: s for s in current["mcp_servers"]}
        for server in value["mcp_servers"]:
            validate_url(server.get("url", ""))
            if server.get("api_key") == "__KEEP__":
                server["api_key"] = old_servers.get(server["id"], {}).get("api_key", "")
        # Сценарии: файлы state/scenarios — источник правды. Устаревшая форма UI
        # (открыта до добавления сценариев) присылает старый список — сервер
        # восстанавливает сценарии, чьи файлы на диске есть, а в присланном
        # списке их нет. Легитимное удаление идёт через DELETE /scenarios и
        # файл убирает, поэтому воскрешения удалённого не происходит.
        if isinstance(value.get("scenarios"), list):
            incoming = {s.get("id") for s in value["scenarios"] if isinstance(s, dict)}
            try:
                self.scenario_dir.mkdir(parents=True, exist_ok=True)
                for path in sorted(self.scenario_dir.glob("*.json")):
                    if path.stem in incoming:
                        continue
                    try:
                        item = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if isinstance(item, dict) and item.get("id"):
                        value["scenarios"].append(item)
            except OSError:
                pass
        return value

    def load_scenario_files(self, settings):
        """Файлы state/scenarios/*.json — определение сценариев по одному на файл.
        При пустом каталоге существующие сценарии из settings выгружаются в файлы
        (миграция); при непустом — файлы источник правды (можно править при остановке).
        Ревизии БД хранят сценарии как заглушки {id, enabled} (RF-08) — запись без
        тела (triggers/flow) сценарием не считается и в файлы не выгружается."""
        try:
            self.scenario_dir.mkdir(parents=True, exist_ok=True)
            files = sorted(self.scenario_dir.glob("*.json"))
            if files:
                loaded = []
                for path in files:
                    try:
                        item = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue  # битый файл пропускаем, остальные работают
                    if isinstance(item, dict) and item.get("id"):
                        loaded.append(item)
                settings["scenarios"] = loaded
            else:
                full = [s for s in settings.get("scenarios", [])
                        if isinstance(s, dict) and ("triggers" in s or "flow" in s)]
                if full:
                    self.sync_scenario_files(full)
                elif settings.get("scenarios"):
                    # только заглушки: тел в ревизии нет и файлов нет — сценариев нет
                    settings["scenarios"] = []
        except OSError:
            pass  # каталог недоступен — работаем только из settings

    def sync_scenario_files(self, scenarios):
        """Сверка каталога файлов с settings.scenarios: запись изменённых, удаление лишних."""
        try:
            self.scenario_dir.mkdir(parents=True, exist_ok=True)
            wanted = {}
            for scenario in scenarios:
                if isinstance(scenario, dict) and scenario.get("id"):
                    wanted[f"{scenario['id']}.json"] = scenario
            for path in self.scenario_dir.glob("*.json"):
                if path.name not in wanted:
                    path.unlink()
            for name, scenario in wanted.items():
                target = self.scenario_dir / name
                payload = json.dumps(scenario, ensure_ascii=False, indent=2)
                try:
                    if target.read_text(encoding="utf-8") == payload:
                        continue
                except OSError:
                    pass
                temporary = target.with_suffix(".tmp")
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, target)
        except OSError:
            pass  # файловый кеш не должен ломать сохранение настроек
