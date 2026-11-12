"""Типизированные разделы настроек: pydantic-модели для валидации входящих форм.

Настройки в Runtime остаются dict (прямое чтение работает на время перехода):
модель проверяет раздел присланной формы и возвращает dict с умолчаниями модели
(model_dump) — неизвестный ключ или неверный тип дают ValueError с именем поля,
API отвечает 4xx. Источник настроек — БД, а не переменные окружения:
pydantic-settings не нужен, достаточно BaseModel.
"""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, RootModel, SecretStr, ValidationError, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _http_url(value: str) -> str:
    """Пусто — умолчание (источник можно не задавать); непустое — http(s)
    без credentials в адресе."""
    if value and (urlsplit(value).scheme not in ("http", "https")
                  or not urlsplit(value).hostname or urlsplit(value).username):
        raise ValueError("нужен http(s) URL без credentials в адресе")
    return value


class OAuthSettings(_Strict):
    """OAuth/Passport: auto — по маршруту устройства (emulate/hybrid — заглушки,
    proxy — туннель), emulate — всегда заглушки, upstream — всегда туннель."""

    mode: Literal["auto", "emulate", "upstream"] = "auto"


class FetchStateSettings(_Strict):
    """Опрос состояния колонки (кнопка «Опросить»): минимальный интервал между
    опросами одного устройства в секундах, 0 — без лимита."""

    min_interval_s: float = Field(default=10, ge=0)

    @field_validator("min_interval_s", mode="before")
    @classmethod
    def _none_is_default(cls, value):
        return 10 if value is None else value


class _Provider(BaseModel):
    """Источник модели/ASR/TTS/эмбеддингов: http(s)-адрес без credentials.
    api_key — SecretStr: не утекает в repr/логи; в настройки возвращается
    реальным значением (checked_section раскрывает).

    Форма открыта (extra="allow"): пресеты voice_stack пишут в секцию
    конфигурацию адаптера — adapter и его поля из packages/*.json; это
    текущая функциональность, не легаси — строгий запрет лишнего здесь
    не работает."""

    model_config = ConfigDict(extra="allow")

    url: str = ""
    model: str = ""
    api_key: SecretStr = SecretStr("")

    @field_validator("url")
    @classmethod
    def _url_ok(cls, value):
        return _http_url(value)


class ModelSettings(_Provider):
    """Источник ответов модели; stub_text — ответ заглушки (пусто — использовать AI)."""

    temperature: float = 0.3
    stub_text: str = ""


class SttSettings(_Provider):
    """Источник распознавания."""

    language: str = "ru"


class TtsSettings(_Provider):
    """Источник синтеза."""

    voice: str = ""
    response_format: str = "opus"


class EmbedSettings(_Provider):
    """Эмбеддинги долговременной памяти (OpenAI-совместимый /v1/embeddings)."""


class MemorySettings(_Strict):
    """Долговременная память агента (agent/memory.py)."""

    enabled: bool = True
    embed: EmbedSettings = EmbedSettings()
    recall_limit: int = 6
    reflect_after_s: int = 120


class AgentSettings(_Strict):
    """Движок ответа модели и отложенные ответы. Поля по читателям: agent/service.py
    DEFAULTS (max_model_calls, summarize_after, keep_messages, confirm_*),
    async_reply.py (defer_mode, async_reply_s, async_wait_text), agent/ops.py
    (daily_model_calls, budgets, pii, suggest_*), webhook_token; пишет ui/agent.js."""

    engine: Literal["graph", "legacy"] = "graph"
    max_model_calls: int = 6
    summarize_after: int = 40
    keep_messages: int = 20
    confirm_yes: list[str] = Field(default_factory=lambda: [
        "да", "ага", "угу", "подтверждаю", "выполняй", "давай", "конечно", "хорошо", "ок", "окей"])
    confirm_no: list[str] = Field(default_factory=lambda: ["нет", "не", "отмена", "отмени", "стоп", "не надо", "не нужно"])
    confirm_prompt: str = "Подтвердите: сказать «да» или «нет»."
    defer_mode: Literal["push", "continue_session"] = "push"
    async_reply_s: float = Field(default=3, ge=0, le=15)  # 0 — отложенный ответ выключен
    async_wait_text: str = ""
    webhook_token: SecretStr = SecretStr("")
    daily_model_calls: int = 0
    budgets: dict[str, int] = Field(default_factory=dict)  # пользователь → дневной лимит
    pii: list[str] = Field(default_factory=lambda: ["credit_card", "secret"])
    suggest_cron: str = "30 4 * * *"
    suggest_min_count: int = 3
    suggest_days: int = 7


class SelfUpdateSettings(_Strict):
    """Самообновление: проверка релизов GitHub и обновление по кнопке; token —
    строка или env:ИМЯ (маскируется), restart — кто запускает сервер после обновления."""

    enabled: bool = True
    repo: str = "angellfear/alice-emulator"
    channel: Literal["stable", "beta"] = "stable"
    interval_hours: int = Field(default=12, ge=1)
    token: SecretStr = SecretStr("")
    skipped: str = ""
    restart: Literal["self", "manager"] = "self"

    @field_validator("repo")
    @classmethod
    def _repo_ok(cls, value):
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("нужен owner/name")
        return value


class JournalSettings(_Strict):
    """Лимиты журнала трафика."""

    max_body_kb: int = 256
    max_size_mb: int = 100
    max_age_days: int = 14
    group_limit: int = Field(default=200, ge=1)  # записей на пару (устройство, тип сообщения)


class UpstreamSettings(_Strict):
    """Проход наверх: внешние DNS-серверы, закреплённые адреса хостов, лимиты журнала."""

    dns_servers: list[str] = Field(default_factory=lambda: ["77.88.8.8", "1.1.1.1"])
    upstream_ip: dict[str, str] = Field(default_factory=dict)
    journal: JournalSettings = JournalSettings()


class WeatherSettings(_Strict):
    """Гео для погоды (строки — как их отдаёт форма)."""

    latitude: str = ""
    longitude: str = ""


class AliasesSettings(RootModel):
    """Алиасы доменов: свои upstream и тесты (колонка ходит на настоящие домены)."""

    root: dict[str, str]


class ProvidersSettings(RootModel):
    """Вид → выбранный id поставщика: значения приводятся к непустой строке."""

    root: dict[str, str]

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, value):
        return {str(key): str(item or "").strip() for key, item in (value or {}).items()}


class PhraseOrderSettings(_Strict):
    """Ручной порядок справочника фраз: порядок видов и плагинов внутри вида."""

    kinds: list[str] = Field(default_factory=list)
    plugins: dict[str, list[str]] = Field(default_factory=dict)


class ExtensionsSettings(_Strict):
    """Состояние расширений: источники, включённость, установленное (запись-объект
    на расширение — поля ведёт менеджер), значения настроек."""

    sources: list[str] = Field(default_factory=list)
    enabled: dict[str, bool] = Field(default_factory=dict)
    installed: dict[str, dict] = Field(default_factory=dict)
    settings: dict[str, dict[str, object]] = Field(default_factory=dict)

    @field_validator("sources")
    @classmethod
    def _sources_ok(cls, value):
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("нужен список URL/папок")
        return value


class VoiceStackSettings(_Strict):
    """Режимы вывода ответа (карточка «Синтез (TTS)»): озвучка выключена делает
    карточку принудительно включённой — ответа совсем без текста нет.
    asr/tts/llm — выбранный источник (пакет или пресет «p:<id>»)."""

    speak: bool = True
    card: bool = True
    asr: str = ""
    tts: str = ""
    llm: str = ""

    @model_validator(mode="after")
    def _no_speech_means_card(self):
        if not self.speak:
            self.card = True
        return self


def _reveal(value):
    """SecretStr → реальное значение (в настройки и БД): вне модели секрет
    живёт как обычная строка — читатели runtime.settings не меняются."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {key: _reveal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_reveal(item) for item in value]
    return value


def _field_of(exc):
    """Имя поля первой ошибки разбора: loc через точку, без входных значений —
    секрет в текст ошибки не попадает."""
    return ".".join(str(part) for part in exc.errors()[0]["loc"]) or "value"


def _message_of(exc):
    return exc.errors()[0]["msg"]


def checked_section(value, model, name):
    """Проверка раздела присланной формы моделью → dict с умолчаниями модели
    (то же, что раньше делали ручные setdefault и приведения типов). Неизвестный
    ключ или неверный тип — ValueError `раздел.поле: сообщение` → 4xx API;
    значения секретов в текст ошибки и repr модели не попадают (SecretStr)."""
    try:
        checked = model.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"{name}.{_field_of(exc)}: {_message_of(exc)}") from None
    return _reveal(checked.model_dump(mode="python"))
