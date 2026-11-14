"""Настройки эмулятора: дефолты, дозаполнение, валидация, сохранение.

SettingsService владеет жизненным циклом настроек: загрузка из StateStore
с досевом отсутствующих значений по умолчанию, валидация входящих значений,
маскирование секретов для UI, сохранение ревизией в БД: тела сценариев живут
в файлах state/scenarios, ревизия хранит заглушки {id, enabled}.

Runtime держит текущий dict в runtime.settings и оркестрирует сохранение: замок,
профили устройств (refresh_profiles), звуковой кеш — это состояние и заботы runtime.
"""
import copy
import json
import os
from urllib.parse import urlsplit

from .settings_models import (AgentSettings, AliasesSettings, ExtensionsSettings,
                              FetchStateSettings, MemorySettings, ModelSettings,
                              PhraseOrderSettings, ProvidersSettings,
                              SelfUpdateSettings, SttSettings, TtsSettings, UpstreamSettings,
                              VoiceStackSettings, WeatherSettings, checked_section)

# Режимы маршрута (решение вместо двух режимов 2026-10-24): у каждого
# устройства свой route_mode; без него (и для неизвестных устройств) — emulate,
# маршрута по умолчанию нет. «Запись» — не маршрут, а опция устройства record:
# off | on | misses, пишется поверх любого маршрута.
ROUTE_MODES = ("emulate", "hybrid", "proxy")
RECORD_MODES = ("off", "on", "misses")

DEFAULT = {
    "intelligence": "agent",  # режим «Интеллект»: scenarios | model | agent (intelligence.py)
    "system_prompt": "Ты домашний голосовой помощник. Отвечай кратко по-русски.",
    "model": {"url": "", "model": "", "api_key": "", "temperature": 0.3},
    "stt": {"url": "", "model": "", "api_key": "", "language": "ru"},
    "tts": {"url": "", "model": "", "api_key": "", "voice": "", "response_format": "opus"},
    "tools": [], "device_context": {}, "aapi_model": False,
    "aapi_voice": False, "aapi_tts": False, "alice2_settings": False,
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
        {"id": "simple_unavailable", "name": "Функция в простом режиме", "case": "simple_unavailable",
         "enabled": True, "text": "В простом режиме это недоступно."},
        {"id": "no_data_source", "name": "Нет источника данных", "case": "no_data_source",
         "enabled": True, "text": "Для этого нет источника данных."},
    ],
    "weather": {"latitude": "", "longitude": ""},
    "scenarios": [],
    # Движок ответа модели (server_emulator/agent): graph — LangGraph-агент с тредами и
    # подтверждениями, legacy — прежний цикл. Прочие ключи — agent/service.py DEFAULTS.
    "agent": {"engine": "graph"},
    # Долговременная память агента (agent/memory.py): эмбеддинги — OpenAI-совместимый /v1/embeddings
    "memory": {"enabled": True, "embed": {"url": "", "model": "", "api_key": ""}, "recall_limit": 6, "reflect_after_s": 120},
    "aliases": {},  # колонка ходит на настоящие домены Яндекса; алиасы — для своих upstream и тестов
    # Расширения: источники (URL/локальные папки), состояние включённости,
    # установленное (версия/предыдущая/источник/самопроверка) и значения настроек.
    # Файлы живут в state/extensions/<id>/<version>/; детали — extensions/manager.py.
    "extensions": {"sources": []},
    # Проход наверх: внешние DNS-серверы, закреплённые адреса хостов
    # и лимиты журнала трафика. Какие запросы пересылать — режимы маршрута.
    "upstream": {"dns_servers": ["77.88.8.8", "1.1.1.1"], "upstream_ip": {},
                 "journal": {"max_body_kb": 256, "max_size_mb": 100, "max_age_days": 14,
                             "group_limit": 200, "asr_group_limit": 1000}},
    # Самообновление: проверка релизов GitHub и обновление по кнопке,
    # автоматически ничего не ставится. token — строка или env:ИМЯ, по умолчанию пуст
    # (API публичного репозитория работает без него); restart — кто запускает сервер после обновления.
    "self_update": {"enabled": True, "repo": "angellfear/alice-emulator", "channel": "stable",
                    "interval_hours": 12, "token": "", "skipped": "", "restart": "self"},
    # Поставщики данных: выбранный поставщик по видам — settings["providers"][<вид>]
    # (id; пусто — первый включённый по алфавиту). Реестр видов —
    # server_emulator/providers.py.
    "providers": {},
    # Ручной порядок справочника фраз (равная точность — выше в списке): порядок
    # видов для споров между видами и плагинов внутри вида; плагины без вида —
    # группа «Прочее» (_other). id вне списков — после перечисленных по алфавиту.
    "phrase_order": {"kinds": [], "plugins": {}},
    # Опрос состояния колонки (кнопка «Опросить»): минимальный интервал между опросами
    # одного устройства в секундах, 0 — без лимита. Форма в UI — карточка «Состояние (опрос)».
    "fetch_state": {"min_interval_s": 10},
}


def revision(value):
    """Ревизия настроек в БД хранит сценарии как заглушки {id, enabled}: тела
    живут в файлах state/scenarios , история настроек не растёт от
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
    # В UI возвращается только configured/masked; ссылка env:VAR — не
    # секрет и видна как есть (значение живёт в окружении процесса)
    if isinstance(value, str) and value.startswith("env:"):
        return value
    return "__KEEP__" if value else ""


class SettingsService:
    def __init__(self, store, directory):
        self.store = store
        self.scenario_dir = directory / "scenarios"  # один JSON-файл на сценарий

    def load(self):
        """Текущие настройки из БД: досев отсутствующих значений по умолчанию,
        файлы сценариев как источник правды."""
        stored = self.store.load_settings()
        settings = stored or copy.deepcopy(DEFAULT)
        # Аддитивный досев дефолтов: настройки сохранены раньше, чем появились
        # новые ключи — доходим их значениями по умолчанию, не трогая существующие.
        missing = {key: copy.deepcopy(value) for key, value in DEFAULT.items()
                   if key not in settings}
        if missing or stored is None:
            settings.update(missing)
            self.store.save_settings(settings)
        journal = (settings.get("upstream") or {}).get("journal")
        if isinstance(journal, dict):  # новые лимиты журнала у старых настроек — по умолчанию
            for key, value in DEFAULT["upstream"]["journal"].items():
                journal.setdefault(key, value)
        self._migrate_standard_replies(settings)
        self.load_scenario_files(settings)
        return settings

    def _migrate_standard_replies(self, settings):
        """Новые стандартные ответы доходим по id: изменённые пользователем тексты не трогаем."""
        replies = settings.get("standard_replies")
        if not isinstance(replies, list):
            return
        known = {r.get("id") for r in replies if isinstance(r, dict)}
        added = [copy.deepcopy(r) for r in DEFAULT["standard_replies"] if r["id"] not in known]
        if added:
            settings["standard_replies"] = replies + added
            self.store.save_settings(settings)

    def persist(self, settings):
        """Сохранение: ревизия в БД  + сверка каталога файлов сценариев."""
        self.store.save_settings(revision(settings))
        if isinstance(settings, dict):
            self.sync_scenario_files(settings.get("scenarios", []))

    def public(self, settings):
        value = copy.deepcopy(settings)
        for name in ("model", "stt", "tts"):
            value[name]["api_key"] = masked_secret(value[name].get("api_key"))
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
        # служебное поле ревизии (сверка устаревшей копии формы) — не настройка
        value.pop("_rev", None)
        from .intelligence import LEVELS, ext_id
        # Встроенный уровень или "ext:<id>" (режим расширения). Валидатор
        # проверяет только формат: включённость расширения с modes/ знает менеджер
        # расширений, её проверяет intelligence.extension_mode при ответе — недоступный
        # режим откатывается на scenarios с записью в recent.
        if value.setdefault("intelligence", current.get("intelligence", "agent")) not in LEVELS \
                and ext_id(value["intelligence"]) is None:
            raise ValueError("intelligence: " + " | ".join(LEVELS) + " или ext:<id>")
        if not isinstance(value.get("tools"), list):
            raise ValueError("tools: нужен массив")
        if not isinstance(value.get("device_context"), dict):
            raise ValueError("device_context: нужен объект")
        value = copy.deepcopy(value)
        # Режимы вывода ответа (карточка «Синтез (TTS)»): speak/card — булевы; озвучка
        # выключена делает карточку принудительно включённой — ответа совсем без текста нет.
        value["voice_stack"] = checked_section(value.get("voice_stack") or {},
                                               VoiceStackSettings, "voice_stack")
        from .behavior import validate_standard
        validate_standard(value.get('standard_replies', []))
        for name, section_model in (("model", ModelSettings), ("stt", SttSettings), ("tts", TtsSettings)):
            cfg = value[name]
            if cfg.get("api_key") == "__KEEP__":
                cfg["api_key"] = current[name].get("api_key", "")
            value[name] = checked_section(cfg, section_model, name)
        if "weather" in value:
            value["weather"] = checked_section(value["weather"], WeatherSettings, "weather")
        if "aliases" in value:
            value["aliases"] = checked_section(value["aliases"], AliasesSettings, "aliases")
        if "upstream" in value:
            value["upstream"] = checked_section(value["upstream"], UpstreamSettings, "upstream")
        memory = value.get("memory")
        embed = (memory or {}).get("embed")
        if isinstance(embed, dict) and embed.get("api_key") == "__KEEP__":
            embed["api_key"] = ((current.get("memory") or {}).get("embed") or {}).get("api_key", "")
        if isinstance(memory, dict):
            value["memory"] = checked_section(memory, MemorySettings, "memory")
        agent_cfg = value.get("agent")
        if isinstance(agent_cfg, dict):
            if agent_cfg.get("webhook_token") == "__KEEP__":
                agent_cfg["webhook_token"] = (current.get("agent") or {}).get("webhook_token", "")
            value["agent"] = checked_section(agent_cfg, AgentSettings, "agent")
        # Самообновление: форма без ключа — восстановить из current; присланное — по форме.
        if "self_update" not in value:
            value["self_update"] = copy.deepcopy(current.get("self_update") or DEFAULT["self_update"])
        else:
            su = value["self_update"]
            if su.get("token") == "__KEEP__":
                su["token"] = (current.get("self_update") or {}).get("token", "")
            value["self_update"] = checked_section(su, SelfUpdateSettings, "self_update")
        # Медиа: список радио «имя → URL»; присланной формы нет — из current.
        # Поставщики данных: выбранный id по видам (проверку реестра делает
        # providers.selected — выключенное расширение просто не выбирается);
        # присланной формы UI нет — из current.
        if "providers" not in value:
            value["providers"] = copy.deepcopy(current.get("providers") or DEFAULT["providers"])
        else:
            value["providers"] = checked_section(value["providers"], ProvidersSettings, "providers")
        # Ручной порядок справочника фраз: формы UI пока нет — из current (появится
        # с редактором порядка); присланное — проверяем по форме.
        if "phrase_order" not in value:
            value["phrase_order"] = copy.deepcopy(current.get("phrase_order") or DEFAULT["phrase_order"])
        else:
            value["phrase_order"] = checked_section(value["phrase_order"], PhraseOrderSettings, "phrase_order")
        # Опрос состояния колонки: UI хранит только min_interval_s (карточка «Состояние
        # (опрос)»); присланной формы нет — из current.
        if "fetch_state" not in value:
            value["fetch_state"] = copy.deepcopy(current.get("fetch_state") or DEFAULT["fetch_state"])
        else:
            value["fetch_state"] = checked_section(value["fetch_state"], FetchStateSettings, "fetch_state")
        # Расширения: устаревшая форма UI присылает настройки без ключа —
        # восстанавливаем из current (состояние расширений UI не редактирует);
        # присланное — проверяем по форме.
        if "extensions" not in value:
            value["extensions"] = copy.deepcopy(current.get("extensions") or DEFAULT["extensions"])
        else:
            value["extensions"] = checked_section(value["extensions"], ExtensionsSettings, "extensions")
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
        Ревизии БД хранят сценарии как заглушки {id, enabled}  — запись без
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
