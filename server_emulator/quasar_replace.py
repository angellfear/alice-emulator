"""Замены regexp в ответе Яндекса для режима «Напрямую».

Правила своего у каждого устройства: state.quasar_direct_replace = [{enabled, pattern, replace}],
порядок важен. Сверху к ним всегда применяются поставляемые правила защиты
(quasar_defaults/rules/direct_replace.json — чистка полей нового поколения конфига Яндекса;
ALICA_QUASAR_RULES_DIR подменяет каталог, effective_direct_replace ставит их перед правилами
устройства — его запись сильнее). validate_rules проверяет правила при сохранении
(PUT quasar_source); apply_replacements применяет включённые правила к тексту ответа
get_sync_info до отправки колонке; preview_replacements прогоняет правила по сохранённому
оригиналу (превью «После замен» в UI без запроса колонки). Python `re`: группы \\1, \\g<name>.
config_generation определяет поколение конфига в разобранном ответе upstream. Ничего не знает
про aiohttp и runtime — чистые функции над байтами и разобранным JSON.
"""
import json
import os
import re
from pathlib import Path

from .upstream.forward import decoded_body

MAX_RULES = 50
MAX_TEXT = 2000
RULES_DIR = Path(__file__).parent / "quasar_defaults" / "rules"  # поставляемый набор (в пакете)
SHIPPED_REPLACE_FILE = "direct_replace.json"
# поколение конфига, которое обслуживает эмулятор; меняется вместе с набором правил
SUPPORTED_CONFIG_GENERATION = "old"


def shipped_direct_replace(directory=None):
    """Поставляемые правила защиты от конфига нового поколения: файл direct_replace.json
    в каталоге поставляемого набора (по умолчанию — в пакете; ALICA_QUASAR_RULES_DIR
    подменяет каталог для тестов, по образцу слоя структурированных правил). Читается на
    каждый ответ — правки файла действуют сразу. Файла нет или он не разбирается — пустой
    список (защиты нет, ответы проходят как есть)."""
    base = directory or os.environ.get("ALICA_QUASAR_RULES_DIR") or RULES_DIR
    try:
        data = json.loads((Path(base) / SHIPPED_REPLACE_FILE).read_text(encoding="utf-8"))
        return validate_rules(data["rules"])
    except (OSError, ValueError, KeyError, TypeError):
        return []


def effective_direct_replace(device_rules, directory=None):
    """Полный список замен ответа «Напрямую»: поставляемая защита, затем правила
    устройства (его запись сильнее — идёт позже и перебивает)."""
    return shipped_direct_replace(directory) + list(device_rules or [])


def config_generation(body):
    """Поколение конфига в разобранном ответе upstream: "new" / "old"; None — ответ без
    узла system_config.centaur (форма неожиданна, состояние устройства не пишется).

    Маркеры нового поколения (любой): runtime_owner.requested_owner непуст (Яндекс
    передаёт владение рантаймом внешнему компоненту); is_io_client_features_enabled_v1
    включён; switchStrategy == "UPDATER_LIKE_REBOOT" (переключение рантайма перезагрузкой).
    Не ловит по версиям приложения — колонка свою версию в get_sync_info не несёт."""
    config = body.get("config") if isinstance(body, dict) else None
    system = config.get("system_config") if isinstance(config, dict) else None
    centaur = system.get("centaur") if isinstance(system, dict) else None
    if not isinstance(centaur, dict):
        return None
    owner = (centaur.get("runtime_owner") or {}).get("requested_owner")
    if owner:
        return "new"
    if _find_key(centaur, "is_io_client_features_enabled_v1") is True:
        return "new"
    if _find_key(centaur, "switchStrategy") == "UPDATER_LIKE_REBOOT":
        return "new"
    return "old"


def _find_key(node, key):
    """Первое значение ключа key в поддереве (поиск в глубину); None — не найдено."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def validate_rules(value):
    """Список правил из PUT → нормализованный [{enabled, pattern, replace}] или ValueError
    с номером строки (с 1) и текстом ошибки. Ничего не сохраняет."""
    if not isinstance(value, list):
        raise ValueError('Замены: список правил')
    if len(value) > MAX_RULES:
        raise ValueError(f'Замены: не больше {MAX_RULES} правил')
    rules = []
    for number, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ValueError(f'Строка {number}: правило — объект {{enabled, pattern, replace}}')
        pattern, replace = item.get('pattern', ''), item.get('replace', '')
        if not isinstance(pattern, str) or not isinstance(replace, str):
            raise ValueError(f'Строка {number}: pattern и replace — строки')
        if not pattern:
            raise ValueError(f'Строка {number}: pattern не может быть пустым')
        if len(pattern) > MAX_TEXT or len(replace) > MAX_TEXT:
            raise ValueError(f'Строка {number}: pattern и replace — не длиннее {MAX_TEXT} символов')
        try:
            re.compile(pattern).sub(replace, '')  # sub разбирает и шаблон замены (\1, \g<name>)
        except (re.error, IndexError) as exc:
            raise ValueError(f'Строка {number}: {exc}') from exc
        rules.append({'enabled': bool(item.get('enabled', True)), 'pattern': pattern, 'replace': replace})
    return rules


def _apply(rules, text):
    """Применить включённые правила к тексту сверху вниз: → (text, applied, last, error)."""
    applied, last = 0, 0
    for number, rule in enumerate(rules or [], 1):
        if not rule.get('enabled'):
            continue
        try:
            changed = re.sub(rule['pattern'], rule['replace'], text)
        except (re.error, IndexError, RecursionError) as exc:
            return text, applied, last, f'правило {number}: {type(exc).__name__}: {str(exc)[:120]}'
        if changed != text:
            applied, last = applied + 1, number
        text = changed
    return text, applied, last, None


def _json_broken(text, last):
    """Текст после замен — валидный JSON-объект? Нет → сообщение об ошибке."""
    try:
        ok = isinstance(json.loads(text), dict)
    except ValueError:
        ok = False
    return None if ok else f'замены сломали JSON (последнее применённое правило {last})'


def apply_replacements(rules, headers, payload):
    """Применить включённые правила к ответу Яндекса.

    Возвращает None, если применять нечего или ответ не изменился (колонке уходит оригинал
    байт-в-байт). Иначе словарь: либо {error} — замены сломали JSON или regex упал (колонке уходит
    оригинал), либо {payload, text, applied} — новое несжатое тело и число сработавших правил.
    """
    if not any(rule.get('enabled') for rule in rules or []):
        return None
    raw = decoded_body(headers, payload)
    if raw is None:
        return None
    try:
        original = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None
    try:
        was_json = isinstance(json.loads(original), dict)
    except ValueError:
        was_json = False
    text, applied, last, error = _apply(rules, original)
    if error:
        return {'error': error}
    if text == original:
        return None
    if was_json:
        error = _json_broken(text, last)
        if error:
            return {'error': error}
    return {'payload': text.encode('utf-8'), 'text': text, 'applied': applied}


def apply_local_urls(urls, headers, payload, replaced=None):
    """Локальные адреса «Эмуляции»/«Вместе» (quasar_rules.local_urls: [(name, path, url)])
    поверх ответа «Напрямую» после замен regexp: каналы, которые в этих режимах ведёт эмулятор
    (подпись push_subscribe — локальная), не остаются на настоящих хостах. Путь — от корня
    ответа, недостающие объекты создаются. Результат — в форме apply_replacements; ничего не
    изменилось или ответ не JSON-объект — replaced как есть."""
    if not urls:
        return replaced
    try:
        text = replaced['text'] if replaced else decoded_body(headers, payload).decode('utf-8')
        body = json.loads(text)
    except (AttributeError, UnicodeDecodeError, ValueError):
        return replaced
    if not isinstance(body, dict):
        return replaced
    changed = 0
    for _name, path, url in urls:
        parts = [part for part in path.split('/') if part]
        node = body
        for key in parts[:-1]:
            node = node.setdefault(key, {}) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(parts[-1]) != url:
            node[parts[-1]] = url
            changed += 1
    if not changed:
        return replaced
    text = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
    return {'payload': text.encode('utf-8'), 'text': text,
            'applied': (replaced or {}).get('applied', 0) + changed}


def preview_replacements(rules, body):
    """Прогнать правила по сохранённому оригинальному ответу (превью «После замен»).

    body — разобранный JSON из state.quasar_direct_last; текст — компактная пере-сериализация
    (как на проводе). Правила с пробелами после «:»/«,» на превью могут не совпасть — на
    реальном ответе совпадут. → None (правил нет или ничего не меняют) |
    {'error': …} | {'body': dict, 'applied': n}."""
    if not isinstance(body, dict) or not any(rule.get('enabled') for rule in rules or []):
        return None
    original = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
    text, applied, last, error = _apply(rules, original)
    if error:
        return {'error': error}
    if text == original:
        return None
    try:
        return {'body': json.loads(text), 'applied': applied}
    except ValueError:
        return {'error': _json_broken(text, last)}


def replaced_headers(headers):
    """Заголовки ответа для изменённого тела: без Content-Encoding и Content-Length
    (длину выставляет aiohttp по телу — оно несжатое)."""
    return [(k, v) for k, v in headers if str(k).lower() not in ('content-encoding', 'content-length')]
