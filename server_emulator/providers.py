"""Поставщики данных: общий реестр видов (kind = папка в плагине + обязательные функции).

Точка подключения расширений по видам — обобщение реестра музыки (music/*.py): вид
объявлен в KINDS — папка расширения и обязательные функции модуля PROVIDER. Реестр
собирает runtime.rebuild_extension_registries; конфликт id поставщиков в пределах
вида или битый модуль отключает расширение целиком (как у actions/datasets).
Встроенные плагины (id с приставкой builtin.) и внешние расширения живут в одном
реестре — приставка разводит их id.

    KINDS = {"music": {"title": "Музыка", "folder": "music",
                       "required": ("search", "neighbor"), "optional": ("rate",)}}

    PROVIDER = {"id": "builtin.yandex-music", "title": "Яндекс Музыка",
                "service": "yandex-music"}   # service — признак сервиса вместо id

    async def search(ctx, query, kind): ...      # обязательные функции вида
    async def neighbor(ctx, track_id, context, step): ...

Выбранный поставщик вида — настройка settings["providers"][<вид>] (id; пусто — первый
включённый по алфавиту). Прежний ключ settings["music"]["provider"] переносится в
providers.music при загрузке настроек (settings_service). Вызов selected()/kinds() —
после apply() пересборки; записи содержат callables вида и метаданные {id, title,
service, kind, ext_id, origin, file}.
"""
from __future__ import annotations

import inspect
from pathlib import Path

BUILTIN_PREFIX = "builtin."

KINDS = {
    "music": {"title": "Музыка", "folder": "music",
              "required": ("search", "neighbor"), "optional": ("rate",)},
    # forecast(ctx, location) → прогноз в форме Open-Meteo (current/hourly/daily) —
    # форма, которую уже читают informers.weather и виджет погоды; location —
    # {"latitude", "longitude", "timezone"?}. Датасет weather — кеш записей,
    # обновляет его диспетчер (scenario_plugins.weather_plugin) вызовом forecast.
    "weather": {"title": "Погода", "folder": "weather", "required": ("forecast",)},
    # headlines(ctx, kind) → {"items": [заголовок, ...], "note"?} — заголовки репликой
    # («что нового»); kind — раздел новостей ("top"), пусто — общий.
    "news": {"title": "Новости", "folder": "news", "required": ("headlines",)},
}

PROVIDERS: dict[str, dict[str, dict]] = {}  # вид → id → запись (реестр пересобирает Runtime)
_COLLECTED: dict[str, list] = {}  # накопитель за пересборку (как drop_extensions у aapi_handlers)
DECLARED_TITLES: dict[str, str] = {}  # вид → название из объявления (новые виды без правки ядра)


def reset():
    """Начать пересборку реестра (runtime.rebuild_extension_registries)."""
    _COLLECTED.clear()
    DECLARED_TITLES.clear()


def collect(kind, ext_id, paths, runner):
    """Модули <folder>/*.py одного расширения → накопитель вида. Битый модуль, PROVIDER
    без id или без обязательных функций вида — ValueError: расширение не включается
    (как у actions). Одинаковый id в пределах вида — тот же отказ."""
    spec = KINDS.get(kind)
    if spec is None:
        raise ValueError(f"Неизвестный вид поставщиков: {kind}")
    for path in paths:
        for file in sorted(Path(path).glob("*.py")):
            try:
                module = runner._module(file)
            except Exception as exc:  # noqa: BLE001 — битый модуль отключает расширение
                raise ValueError(f"{file.name}: модуль не загружается "
                                 f"({type(exc).__name__}: {str(exc)[:150]})") from exc
            provider = getattr(module, "PROVIDER", None)
            if provider is None:
                continue  # вспомогательный модуль без PROVIDER — не участвует
            if not isinstance(provider, dict) or not str(provider.get("id") or "").strip():
                raise ValueError(f"{file.name}: PROVIDER — объект с непустым id")
            pid = str(provider["id"]).strip()
            record = {"id": pid, "title": str(provider.get("title") or pid),
                      "service": str(provider.get("service") or "").strip(),
                      "kind": kind, "ext_id": str(ext_id), "file": str(file),
                      "origin": "builtin" if str(ext_id).startswith(BUILTIN_PREFIX) else "extension"}
            for name in spec["required"]:
                function = getattr(module, name, None)
                if not callable(function):
                    raise ValueError(f"{file.name}: нужна async {name} "
                                     f"({', '.join(spec['required'])} — обязательные функции вида)")
                record[name] = function
            for name in spec.get("optional") or ():
                function = getattr(module, name, None)
                record[name] = function if callable(function) else None
            if any(item["id"] == pid for item in _COLLECTED.setdefault(kind, [])):
                raise ValueError(f"Имя поставщика {pid!r} уже занято другим расширением")
            _COLLECTED[kind].append(record)


def collect_declaration(ext_id, decl, base_dir, runner):
    """Поставщик из объявления contributes.providers: точку подключения называет сам
    плагин (decl["module"]), папка вида не нужна. Обязательные функции вида объявляет
    потребитель: для известных видов это таблица KINDS; вид, объявленный плагином
    впервые, регистрируется без проверки функций — их проверит его потребитель, когда
    появится (сценарий ядра или общий исполнитель). Битый модуль или PROVIDER без
    id — ValueError: расширение не включается (как у folder-путей)."""
    kind = str(decl["kind"]).strip()
    file = Path(base_dir) / str(decl["module"]).strip()
    try:
        module = runner._module(file)
    except Exception as exc:  # noqa: BLE001 — битый модуль отключает расширение
        raise ValueError(f"{file.name}: модуль не загружается "
                         f"({type(exc).__name__}: {str(exc)[:150]})") from exc
    provider = getattr(module, "PROVIDER", None)
    if not isinstance(provider, dict) or not str(provider.get("id") or "").strip():
        raise ValueError(f"{file.name}: PROVIDER — объект с непустым id")
    pid = str(provider["id"]).strip()
    spec = KINDS.get(kind)
    record = {"id": pid, "title": str(provider.get("title") or pid),
              "service": str(provider.get("service") or "").strip(),
              "kind": kind, "ext_id": str(ext_id), "file": str(file),
              "origin": "builtin" if str(ext_id).startswith(BUILTIN_PREFIX) else "extension",
              "capabilities": [str(c).strip() for c in decl.get("capabilities") or []
                               if str(c).strip()]}
    for name in (spec or {}).get("required") or ():
        function = getattr(module, name, None)
        if not callable(function):
            raise ValueError(f"{file.name}: нужна async {name} "
                             f"({', '.join(spec['required'])} — требования потребителя вида)")
        record[name] = function
    for name in (spec or {}).get("optional") or ():
        function = getattr(module, name, None)
        record[name] = function if callable(function) else None
    if any(item["id"] == pid for item in _COLLECTED.setdefault(kind, [])):
        raise ValueError(f"Имя поставщика {pid!r} уже занято другим расширением")
    _COLLECTED.setdefault(kind, []).append(record)
    DECLARED_TITLES.setdefault(kind, str(decl["title"]).strip())


def apply():
    """Завершить пересборку: реестр = накопленное за успешный проход."""
    PROVIDERS.clear()
    for kind, items in _COLLECTED.items():
        PROVIDERS[kind] = {item["id"]: item for item in items}
    _COLLECTED.clear()


def kinds():
    """Виды, у которых есть поставщики включённых плагинов (объявленные или из папок):
    разделы «Поставщики данных» строятся по ним — нет вида, нет вкладки."""
    known = sorted(set(KINDS) | set(DECLARED_TITLES))
    return [kind for kind in known if PROVIDERS.get(kind)]


def kind_title(kind):
    """Название вида для вкладки: таблица ядра для известных, иначе — из объявления
    плагина (новый вид), без обеих — сам вид."""
    return KINDS.get(kind, {}).get("title") or DECLARED_TITLES.get(kind) or kind


def extension_context(ctx, provider):
    """ExtensionContext плагина-поставщика для вызова (настройки свежие). Менеджера нет —
    None; запись плагина не читается (например, заглушка тестов) — пустые настройки:
    поставщик в реестре собран, значит его код работоспособен и без манифеста."""
    runtime = ctx._services_runtime()
    manager = getattr(runtime, "extensions", None)
    build = getattr(manager, "context", None)
    if not callable(build):
        return None
    try:
        return build(provider["ext_id"])
    except Exception:  # noqa: BLE001 — см. докстринг
        from .extensions.context import ExtensionContext
        return ExtensionContext(str(provider.get("ext_id") or ""), {})


async def invoke(ctx, provider, function, *args):
    """Вызов функции поставщика вида: его ExtensionContext — первым аргументом
    (настройки свежие), остальные — как требует контракт вида. Синхронная или
    async функция; None — контекста нет (плагин выключен между выбором и вызовом)."""
    context = extension_context(ctx, provider)
    if context is None:
        return None
    result = function(context, *args)
    if inspect.isawaitable(result):
        result = await result
    return result


def selected(kind, settings):
    """Выбранный поставщик вида: settings["providers"][<вид>] (id); пусто/нет — первый
    включённый по алфавиту id. Выбранный id не в реестре (расширение выключили) — None."""
    want = str(((settings or {}).get("providers") or {}).get(kind) or "").strip()
    bucket = PROVIDERS.get(kind) or {}
    if want:
        return bucket.get(want)
    for pid in sorted(bucket):
        return bucket[pid]
    return None


def for_ext(kind, ext_id):
    """Поставщик плагина-владельца записи справочника: первая запись вида с этим
    ext_id. Нет (плагин выключили между матчингом и вызовом) — None."""
    for record in (PROVIDERS.get(kind) or {}).values():
        if record.get("ext_id") == str(ext_id or ""):
            return record
    return None


def migrate(settings):
    """Прежний выбор поставщика музыки — в общий реестр видов:
    settings["music"]["provider"] → settings["providers"]["music"] (старый ключ
    читается один раз и удаляется). True — настройки изменились, их нужно сохранить."""
    legacy = settings.get("music")
    if not isinstance(legacy, dict):
        return False
    value = str(legacy.get("provider") or "").strip()
    providers_state = settings.setdefault("providers", {})
    if value and not str(providers_state.get("music") or "").strip():
        providers_state["music"] = value
    settings.pop("music", None)  # ключ больше не используется
    return True
