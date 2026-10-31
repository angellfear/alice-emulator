"""Замены regexp в ответе Яндекса для режима «Напрямую» (TASK-063).

Правила своего у каждого устройства: state.quasar_direct_replace = [{enabled, pattern, replace}],
порядок важен. validate_rules проверяет их при сохранении (PUT quasar_source); apply_replacements
применяет включённые правила к тексту ответа get_sync_info до отправки колонке. Python `re`:
группы \\1, \\g<name>. Ничего не знает про aiohttp и runtime — чистые функции над байтами.
"""
import json
import re

from .upstream.forward import decoded_body

MAX_RULES = 50
MAX_TEXT = 2000


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


def apply_replacements(rules, headers, payload):
    """Применить включённые правила к ответу Яндекса.

    Возвращает None, если применять нечего или ответ не изменился (колонке уходит оригинал
    байт-в-байт). Иначе словарь: либо {error} — замены сломали JSON или regex упал (колонке уходит
    оригинал), либо {payload, text, applied} — новое несжатое тело и число сработавших правил.
    """
    active = [(number, rule) for number, rule in enumerate(rules or [], 1) if rule.get('enabled')]
    if not active:
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
    text, applied, last = original, 0, 0
    for number, rule in active:
        try:
            changed = re.sub(rule['pattern'], rule['replace'], text)
        except (re.error, IndexError, RecursionError) as exc:
            return {'error': f'правило {number}: {type(exc).__name__}: {str(exc)[:120]}'}
        if changed != text:
            applied, last = applied + 1, number
        text = changed
    if text == original:
        return None
    if was_json:
        try:
            ok = isinstance(json.loads(text), dict)
        except ValueError:
            ok = False
        if not ok:
            return {'error': f'замены сломали JSON (последнее применённое правило {last})'}
    return {'payload': text.encode('utf-8'), 'text': text, 'applied': applied}


def replaced_headers(headers):
    """Заголовки ответа для изменённого тела: без Content-Encoding и Content-Length
    (длину выставляет aiohttp по телу — оно несжатое)."""
    return [(k, v) for k, v in headers if str(k).lower() not in ('content-encoding', 'content-length')]
