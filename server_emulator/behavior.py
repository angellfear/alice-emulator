"""Стандартные голосовые ответы на особые состояния сервера (вне модели).

Расширения (TASK-032): папка replies/ включённого расширения добавляет записи
(только чтение): *.json — ответ {id, name, case, text, enabled}; case — только
известные STANDARD_CASES (новые случаи расширением не вводятся), id без префикса
получает <ext-id>.; пользовательская запись settings с тем же id побеждает,
settings не мутируются — слой применяется при чтении (standard_for); выключение
расширения убирает записи; некорректная запись — warning без падения раздела.
Слои — extensions/layers (source_layers/layer_records); провайдер ставит runtime
(set_extension_layers, как scenario_templates.set_extension_sources).
"""
import logging

log = logging.getLogger(__name__)

# Реплики на особые состояния сервера, вне модели и правил распознавания.
# Непривязанное устройство; фраза без сценария в режиме «Интеллект: сценарии».
STANDARD_CASES = {'unclaimed': 'Устройство не привязано к пользователю',
                  'not_understood': 'Нет сценария (режим «Сценарии»)'}

_extension_layers = None  # callable → [(origin, Path)] — слои replies/ расширений (TASK-032)


def set_extension_layers(provider):
    """Провайдер слоёв replies/ включённых расширений: callable → [(origin, Path)]
    (extensions/layers.source_layers); ставит runtime; None — слой пуст."""
    global _extension_layers
    _extension_layers = provider


def extension_replies(layers=None):
    """Ответы replies/ включённых расширений: [(origin, запись)] — файл = одна запись
    или список записей; id без префикса получает <ext-id>.; case — только известные
    STANDARD_CASES, text непустой (иначе warning, запись пропущена); enabled по
    умолчанию True; запись с тем же id у позднего слоя перекрывает раннюю. settings
    не мутируются, в settings записи не копируются."""
    from .extensions.layers import layer_records

    def warn(message):
        log.warning("Расширения (стандартные ответы): %s", message)

    if layers is None:
        if _extension_layers is None:
            return []
        try:
            layers = _extension_layers() or []
        except Exception:  # noqa: BLE001 — расширения не должны ломать реплики
            return []
    found = {}
    for origin, path, value in layer_records(layers, warn=warn):
        ext_id = origin.split(":", 1)[1] if str(origin).startswith("extension:") else ""
        items = value if isinstance(value, list) else [value]
        for index, item in enumerate(items):
            where = path if len(items) == 1 else f"{path}[{index}]"
            if not isinstance(item, dict):
                warn(f"{where}: нужен JSON-объект с ответом")
                continue
            case = item.get('case')
            if case not in STANDARD_CASES:
                warn(f"{where}: неизвестный случай стандартного ответа: {case}")
                continue
            text = item.get('text')
            if not isinstance(text, str) or not text.strip():
                warn(f"{where}: укажите текст стандартного ответа")
                continue
            identifier = item.get('id') or path.stem
            if ext_id and not str(identifier).startswith(ext_id + "."):
                identifier = f"{ext_id}.{identifier}"
            found[identifier] = (origin, {'id': identifier, 'name': str(item.get('name') or path.stem),
                                          'case': case, 'text': text,
                                          'enabled': bool(item.get('enabled', True)),
                                          'origin': origin})
    return list(found.values())


def validate_standard(replies):
    if not isinstance(replies, list) or len(replies)>50:
        raise ValueError('Стандартные ответы: не более 50 записей')
    identifiers=set()
    for reply in replies:
        if not isinstance(reply, dict):raise ValueError('Стандартный ответ должен быть объектом')
        for key in ('id','name','text'):
            if not isinstance(reply.get(key,''),str) or len(reply.get(key,''))>2000:
                raise ValueError('Некорректное поле стандартного ответа: '+key)
        if not reply.get('id') or reply['id'] in identifiers:raise ValueError('ID стандартного ответа отсутствует или повторяется')
        identifiers.add(reply['id'])
        if reply.get('case') not in STANDARD_CASES:raise ValueError('Неизвестный случай стандартного ответа: '+str(reply.get('case')))
        if not reply.get('text','').strip():raise ValueError('Укажите текст стандартного ответа')
        if not isinstance(reply.get('enabled'),bool):raise ValueError('enabled должен быть bool')


def standard_for(replies,case,layers=None):
    """Реплика для случая: первая включённая из settings (пользовательская побеждает),
    иначе — из включённых расширений (TASK-032; layers — слои replies/, иначе
    провайдер set_extension_layers). Пользовательская запись с тем же id — даже
    выключенная — перекрывает запись расширения. Settings не мутируются."""
    for reply in replies or []:
        if reply.get('enabled') and reply.get('case')==case:return reply
    known={reply.get('id') for reply in replies or [] if isinstance(reply,dict)}
    for origin, reply in extension_replies(layers):
        if reply['id'] not in known and reply.get('enabled') and reply.get('case')==case:return reply
    return None
