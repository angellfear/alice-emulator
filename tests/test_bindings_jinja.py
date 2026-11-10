"""Шаблоны bindings на Jinja2 SandboxedEnvironment: строгое подмножество.

Движок — ImmutableSandboxedEnvironment (StrictUndefined); фильтры закрытого реестра
FILTERS подключены обёрткой (value, *args) → FILTERS[name](строка-аргумент, ctx).
Подстановка — путь контекста и не больше одного реестрового фильтра с позиционными
литеральными аргументами. Строка целиком из одного {{…}} — compile_expression того же
окружения: значение без приведения к строке. Операторы ({% %}), комментарии ({# #}),
вызовы, арифметика и прочие выражения отвергаются; легаси-синтаксиса нет —
|имя:арг и .-N дают обычную ошибку разбора.
"""
import datetime

import pytest

from server_emulator.scenario_engine.bindings import BindingError, check_template, expand

NOW = datetime.datetime(2026, 10, 23, 12, 5)

CTX = {
    "clock": {"now": NOW, "hour": 12},
    "slots": {"seconds": 90, "time": "07:30", "count": 5, "n": 5, "level": 7,
              "weather": {"current": {"temperature_2m": 3.4,
                                      "apparent_temperature": -2.6}}},
    "state": {"label": "чай", "condition": "ясно", "animation": "http://alica.lan/a.gif",
              "title": "Погода",
              "hours": [{"time": "2026-10-27T15:00", "temperature_2m": 3.4, "t": "01:00"}]},
    "input": {"text": "привет", "frame_text": "текст"},
    "user": {"name": "Оля"},
    "device": {"fields": {"room": "Кухня"}},
    "a": {"b": "1"},
    "h": {"time": "2026-10-27T15:00", "temperature_2m": 3.4, "code": "clear", "is_day": 1},
    "tables": {"icons": {"clear": "ясно", "*": "?"},
               "kind": {"1": {"clear": "день-ясно", "*": "день-?"},
                        "*": {"*": "любой"}},
               "nested": {"clear": {" icon": 1}}},
}

# шаблоны инвентаризации — путь и один фильтр, строки в интерполяции
SAME_AS_BEFORE = [
    "Сейчас {{clock|time_ru}}.",
    "Таймер на {{slots.seconds|duration}}.",
    "Будильник на {{slots.time|hhmm}}.",
    "Привет, {{user.name}}!",
    "Свет: {{device.fields.room}}.",
    "Таймер «{{state.label}}» на {{slots.seconds|duration}} готов.",
    "{{a.b}} и {not_a_token}",
    "{{slots.weather.current.temperature_2m|signed}}°",
    "Ощущается как {{slots.weather.current.apparent_temperature|signed}}°, {{state.condition}}",
    "{{h.time|hour_label}}",
    "{{h.temperature_2m|signed}}°",
    "{{h.temperature_2m|round}}",
    "{{input.text}}",
    "{{input.frame_text}}",
    "{{state.animation}}",
    "{{state.title}}",
    "{{state.hours.0.t}}",
    "{{a|lower}}",
]


@pytest.mark.parametrize("template", SAME_AS_BEFORE)
def test_existing_templates_expand_the_same(template):
    result = expand(template, CTX)
    assert isinstance(result, str) and "{{" not in result


CALL_SYNTAX = [
    ("{{slots.count|plural('минуту','минуты','минут')}}", "5 минут"),
    ("{{h.code|lookup('icons')}}", "ясно"),
    ("{{h.code|lookup('kind', 'h.is_day')}}", "день-ясно"),
    ("{{slots.level|div(10)}}", "0"),
    ("{{slots.level|mul(3600)}}", 25200),
    ("{{h.temperature_2m|round(1)}}", "3.4"),
    ("{{slots.seconds|default(45)}}", 90),
    ("{{state.hours[-1].t}}", "01:00"),
]


@pytest.mark.parametrize("template, expected", CALL_SYNTAX)
def test_call_syntax_results(template, expected):
    assert expand(template, CTX) == expected


def test_whole_template_preserves_type():
    """Строка целиком из одного {{…}} — значение как есть (compile_expression):
    и без фильтра, и с фильтром."""
    assert expand("{{slots.n}}", CTX) == 5 and isinstance(expand("{{slots.n}}", CTX), int)
    assert expand("{{tables.icons}}", CTX) == CTX["tables"]["icons"]
    assert expand("{{h.code|lookup('nested')}}", CTX) == {" icon": 1}
    assert expand("{{input.text}}", CTX) == "привет"


def test_mixed_template_is_string():
    """Шаблон с текстом вокруг токенов — строка (значения приводятся к str)."""
    assert expand("N={{slots.n}}", CTX) == "N=5"
    assert expand("Таймер на {{slots.seconds|duration}}.", CTX) == "Таймер на 1 минуту 30 секунд."


ERRORS = [
    ("{{nowhere.x}}", "неизвестный корень 'nowhere'"),
    ("{{slots.x}}", "slots.x отсутствует в контексте"),
    ("{{slots.seconds|nope}}", "неизвестный фильтр nope"),
    ("{{slots.state.deep}}", "slots.state.deep отсутствует в контексте"),
]


@pytest.mark.parametrize("template, message", ERRORS)
def test_errors_are_binding_error_with_old_text(template, message):
    with pytest.raises(BindingError, match=message):
        expand(template, CTX)


@pytest.mark.parametrize("template, message", [
    ("{{ range(10) }}", "путь контекста"),           # вызовы функций
    ("{{ 'a'.upper() }}", "путь контекста"),         # вызовы методов
    ("{{slots.n + 1}}", "путь контекста"),           # арифметика
    ("{{'да' if x else 'нет'}}", "путь контекста"),  # условное выражение
    ("{{a[b]}}", "путь контекста"),                  # вычисляемый индекс
    ("{{5|duration}}", "путь контекста"),            # литерал-значение
    ("{{x|default(y)}}", "путь контекста"),          # не литеральный аргумент
    ("{{a|lower(y=1)}}", "именованные аргументы фильтра"),
    ("{{h.code|lookup(name='icons')}}", "именованные аргументы фильтра"),
    ("{{a|lower|lower}}", "цепочки фильтров"),
    ("{{slots.level|round(1, 'x')}}", "один аргумент"),
])
def test_outside_subset_rejected(template, message):
    with pytest.raises(BindingError, match=message):
        expand(template, CTX)


@pytest.mark.parametrize("template", ["{{ ''.__class__ }}", "{{ a.__dict__ }}"])
def test_sandbox_rejects_python_attributes(template):
    with pytest.raises(BindingError):
        expand(template, CTX)


def test_dict_method_named_keys_are_values():
    """Точка — только ключ словаря: items/keys/values/get — ключи контекста,
    не методы Python (getattr словаря берёт значение по ключу)."""
    ctx = {"slots": {"items": [1, 2, 3], "keys": ["a"], "values": [7], "get": "ряд"}}
    assert expand("{{slots.items}}", ctx) == [1, 2, 3]  # целиком — тип сохранён
    assert expand("Список: {{slots.items}}.", ctx) == "Список: [1, 2, 3]."
    assert expand("{{slots.items[-1]}}", ctx) == 3
    assert expand("{{slots.keys.0}}", ctx) == "a"
    assert expand("{{slots.get}}", ctx) == "ряд"


@pytest.mark.parametrize("template", ["{{state.title.upper}}", "{{input.text.strip}}"])
def test_attributes_of_non_dict_are_rejected(template):
    with pytest.raises(BindingError):
        expand(template, CTX)


@pytest.mark.parametrize("template", [
    "{{slots.count|plural:минуту,минуты,минут}}",
    "{{h.code|lookup:icons}}",
    "{{state.hours.-1.t}}",
])
def test_legacy_syntax_is_parse_error(template):
    """Легаси-синтаксиса нет: двоеточие-аргументы и .-N — обычная ошибка разбора."""
    with pytest.raises(BindingError):
        expand(template, CTX)


@pytest.mark.parametrize("template", [
    "{% if x %}да{% endif %}",
    "{# заметка #}",
    "текст {% for x in state.hours %}{{x}}{% endfor %}",
])
def test_operators_and_comments_are_forbidden(template):
    with pytest.raises(BindingError, match="только подстановки"):
        expand(template, CTX)


def test_whole_template_with_braces_inside_literal():
    """Скобки внутри строкового литерала вызова фильтра не разрывают
    целиком-шаблон — тип сохраняется."""
    assert expand('{{lst|default("}}")}}', {"lst": [10, 20, 30]}) == [10, 20, 30]


# --- проверка синтаксиса для валидатора (без контекста) ----------------------


def test_check_template_accepts_subset():
    assert check_template("Сейчас {{clock|time_ru}}.") is None
    assert check_template("{{slots.count|plural('минуту','минуты','минут')}}") is None
    assert check_template("{{state.hours[-1].t}}") is None


@pytest.mark.parametrize("template", [
    "{% if x %}да{% endif %}",
    "{# заметка #}",
    "{{slots.seconds|nope}}",
    "{{broken",
    "{{}}",
    "{{slots.count|plural:минуту,минуты,минут}}",  # легаси — ошибка разбора
    "{{state.hours.-1.t}}",
    "{{slots.n + 1}}",
])
def test_check_template_rejects(template):
    with pytest.raises(BindingError):
        check_template(template)


def test_validate_scenario_rejects_bad_template():
    from server_emulator.scenario_packages.validate import PackageError, validate_scenario
    scenario = {
        "format_version": 2,
        "id": "custom.template-check",
        "version": "1.0.0",
        "category": "custom",
        "triggers": [],
        "flow": [
            {"id": "say", "op": "compose", "reply": {"text": "Таймер на {{slots.seconds|duration}}."}},
            {"id": "done", "op": "finish"},
        ],
    }
    assert validate_scenario(scenario) == scenario

    legacy = {"format_version": 2, "id": "custom.template-check", "version": "1.0.0",
              "category": "custom", "triggers": [],
              "flow": [
                  {"id": "say", "op": "compose",
                   "reply": {"text": "{{slots.count|plural:минуту,минуты,минут}}"}},
                  {"id": "done", "op": "finish"},
              ]}
    with pytest.raises(PackageError, match="шаблон"):
        validate_scenario(legacy)

    broken = {"format_version": 2, "id": "custom.template-check", "version": "1.0.0",
              "category": "custom", "triggers": [],
              "flow": [
                  {"id": "say", "op": "compose", "reply": {"text": "{% if x %}да{% endif %}"}},
                  {"id": "done", "op": "finish"},
              ]}
    with pytest.raises(PackageError, match="только подстановки"):
        validate_scenario(broken)
