"""Поставщики данных: реестр из объявлений плагинов (contributes.providers).

Точку подключения называет сам плагин (decl["module"]), папка вида не нужна; вид —
строка плагина (новый вид без правки ядра). Реестр собирает
runtime.rebuild_extension_registries; конфликт id поставщиков в пределах вида или
битый модуль отключает расширение целиком (как у actions/datasets). Встроенные
плагины (id с приставкой builtin.) и внешние расширения живут в одном реестре —
приставка разводит их id.

Требования к функциям вида объявляют ПОТРЕБИТЕЛИ (providers.require): погодный
диспетчер — forecast, новостной сценарий — headlines, исполнитель музыки —
search/neighbor. Вид без потребителя регистрируется без проверки функций.
"""
from __future__ import annotations

import inspect
from pathlib import Path

BUILTIN_PREFIX = "builtin."

# требования к функциям вида → заполняют потребители (providers.require)
REQUIREMENTS: dict[str, tuple] = {}  # вид → (обязательные, опциональные) функции

PROVIDERS: dict[str, dict[str, dict]] = {}  # вид → id → запись (реестр пересобирает Runtime)
_COLLECTED: dict[str, list] = {}  # накопитель за пересборку (как drop_extensions у aapi_handlers)
DECLARED_TITLES: dict[str, str] = {}  # вид → название из объявления плагина


def reset():
    """Начать пересборку реестра (runtime.rebuild_extension_registries)."""
    _COLLECTED.clear()
    DECLARED_TITLES.clear()


def collect_declaration(ext_id, decl, base_dir, runner):
    """Поставщик из объявления contributes.providers: точку подключения называет сам
    плагин (decl["module"]), папка вида не нужна. Обязательные функции вида объявил
    потребитель (providers.require); вид без потребителя регистрируется без проверки
    функций — их проверит его потребитель, когда появится. Битый модуль или PROVIDER
    без id — ValueError: расширение не включается."""
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
    required, optional = REQUIREMENTS.get(kind) or ((), ())
    record = {"id": pid, "title": str(provider.get("title") or pid),
              "service": str(provider.get("service") or "").strip(),
              "kind": kind, "ext_id": str(ext_id), "file": str(file),
              "origin": "builtin" if str(ext_id).startswith(BUILTIN_PREFIX) else "extension",
              "capabilities": [str(c).strip() for c in decl.get("capabilities") or []
                               if str(c).strip()],
              "_module": module}  # для дообогащения, когда потребитель объявит требования
    for name in required:
        function = getattr(module, name, None)
        if not callable(function):
            raise ValueError(f"{file.name}: нужна async {name} "
                             f"({', '.join(required)} — требования потребителя вида)")
        record[name] = function
    for name in optional:
        function = getattr(module, name, None)
        record[name] = function if callable(function) else None
    login = {name: getattr(module, name, None)
             for name in ("login_start", "login_callback", "login_status")}
    if all(callable(function) for function in login.values()):
        record["login"] = login  # кнопка входа в UI появляется только у объявивших
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
    """Виды, у которых есть поставщики включённых плагинов: разделы «Поставщики
    данных» строятся по ним — нет вида, нет вкладки."""
    return [kind for kind in sorted(set(DECLARED_TITLES)) if PROVIDERS.get(kind)]


def kind_title(kind):
    """Название вида для вкладки — из объявления плагина."""
    return DECLARED_TITLES.get(kind) or kind


def require(kind, required, optional=()):
    """Потребитель вида объявляет функции поставщика (вызывается из кода потребителя
    при импорте): диспетчер погоды — forecast, новости — headlines, исполнитель
    музыки — search/neighbor (+rate опционально). Несовместимый поставщик не попадает
    в реестр, причина — в карточке расширения. Потребитель импортируется позже
    пересборки — готовым записям вида функции дообогащаются здесь."""
    REQUIREMENTS[kind] = (tuple(required), tuple(optional))
    for record in PROVIDERS.get(kind, {}).values():
        module = record.get("_module")
        if module is None:
            continue
        for name in required:
            function = getattr(module, name, None)
            if callable(function):
                record[name] = function
        for name in optional:
            function = getattr(module, name, None)
            record[name] = function if callable(function) else None


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


# вызов функций поставщика по видам — заполняют потребители (providers.call_invoker)
CALL_INVOKERS: dict[str, callable] = {}


def call_invoker(kind, invoker):
    """Потребитель вида объявляет вызов функций поставщика: invoker(ctx, provider,
    function, *args) → результат (sync/async, первый аргумент function решает сам).
    Музыка — SDK-контекст вызова первым аргументом и OAuth-токен аккаунта Яндекса
    в ctx.extension: без него поиск «включи {X}» не находит ничего (сценарий зовёт
    тем же music._call). Без объявления — ExtensionContext плагина первым
    аргументом, как раньше. Потребитель импортируется позже пересборки реестра —
    объявление живёт в словаре, как REQUIREMENTS у require."""
    CALL_INVOKERS[kind] = invoker


async def invoke(ctx, provider, function, *args):
    """Вызов функции поставщика вида: контекст вызова — построением потребителя
    вида (providers.call_invoker), без объявления — ExtensionContext плагина
    (настройки свежие) первым аргументом, остальные — как требует контракт вида.
    Синхронная или async функция; None — контекста нет (плагин выключен между
    выбором и вызовом)."""
    invoker = CALL_INVOKERS.get(str(provider.get("kind") or ""))
    if invoker is None:
        context = extension_context(ctx, provider)
        if context is None:
            return None
        result = function(context, *args)
    else:
        result = invoker(ctx, provider, function, *args)
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
