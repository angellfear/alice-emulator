"""Декларативные условия сценариев (guard): право отказаться от выполнения.

Условие — дерево {all/any/not} с листьями exists/empty/eq/ne/in/contains/gt/ge/lt/le над bindings и шаблонами {{…}}
(включая контекст user/device). Сценарий с guard, которому условие не подошло
(например, у пользователя нет гео для погоды), ОТКАЗЫВАЕТСЯ: реплика уходит
дальше по цепочке кандидатов или в LLM — решение принимает сценарий сам,
без хардкода в роутере. Кода в условиях нет: только сверка значений.
"""
from __future__ import annotations

from .bindings import BindingError, expand

COMBINATORS = ("all", "any", "not")
LEAF_OPS = ("exists", "eq", "ne", "in", "gt", "ge", "lt", "le", "contains", "empty")


def check(condition, ctx):
    """True/False. Битое условие (нет поля и т.п.) = отказ — безопасный дефолт."""
    try:
        return bool(_node(condition, ctx))
    except (BindingError, KeyError, TypeError, ValueError):
        return False


def _node(condition, ctx):
    if not isinstance(condition, dict) or not condition:
        raise TypeError("условие должно быть непустым объектом")
    keys = [k for k in COMBINATORS if k in condition]
    if keys:
        if len(keys) > 1 or len(condition) > 1:
            raise TypeError(f"комбинатор один: {sorted(condition)}")
        return _combinator(keys[0], condition[keys[0]], ctx)
    return _leaf(condition, ctx)


def _combinator(kind, value, ctx):
    if kind == "not":
        return not _node(value, ctx)
    if not isinstance(value, list):
        raise TypeError(f"{kind}: список условий")
    results = [_node(item, ctx) for item in value]
    return all(results) if kind == "all" else any(results)


def _leaf(condition, ctx):
    ops = [k for k in LEAF_OPS if k in condition]
    if len(ops) != 1 or len(condition) != 1:
        raise TypeError(f"лист условия — одна операция из {LEAF_OPS}: {sorted(condition)}")
    op = ops[0]
    args = condition[op]
    if not isinstance(args, list):
        raise TypeError(f"{op}: список [binding, значение]")
    if op in ("exists", "empty"):
        try:
            value = expand(args[0], ctx)
        except BindingError:
            value = None
        present = value is not None
        if op == "exists":
            return present
        return not present or value in ("", [], {}, 0)
    if op in ("eq", "ne"):
        left, right = _value(args[0], ctx), _value(args[1], ctx)
        return (left == right) if op == "eq" else (left != right)
    if op == "in":
        haystack = _value(args[1], ctx)
        if isinstance(haystack, dict):
            haystack = list(haystack)
        if not isinstance(haystack, (list, tuple, set)):
            raise TypeError("in: второй аргумент — список")
        return _value(args[0], ctx) in haystack
    if op == "contains":
        return str(_value(args[1], ctx)).lower() in str(_value(args[0], ctx)).lower()
    left, right = float(_value(args[0], ctx)), float(_value(args[1], ctx))
    return {"gt": left > right, "ge": left >= right, "lt": left < right, "le": left <= right}[op]


def _value(item, ctx):
    return expand(item, ctx)
