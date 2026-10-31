"""TASK-022: триггеры-шаблоны на hassil — таблица фраз карточки, приоритет над
contains, ошибка шаблона при сохранении, skip_words из lexicons.asr."""
import pytest

from server_emulator.scenario_engine.dialog import DialogManager
from server_emulator.scenario_engine.executor import Executor
from server_emulator.scenario_engine.router import with_templates
from server_emulator.scenario_engine.templates import (TemplateIndex, builtin_source,
                                                       merge_lexicons)
from server_emulator.scenario_packages.validate import PackageError, validate_scenario
from server_emulator.schema import load_type
from tests.conftest import CTX

SCHEMAS = "server_emulator/schemas"


def server_type():
    return load_type(SCHEMAS, "alice-api.pb", "NAlice.NAliceApi.TServerMessage")


def scenario(sid, triggers, text="Готово.", category="custom"):
    """Минимальный валидный сценарий v2 с template/phrase триггером."""
    return {
        "format_version": 2, "id": sid, "version": "1.0.0", "category": category,
        "title": sid, "triggers": triggers,
        "flow": [{"id": "say", "op": "compose", "reply": {"text": text}},
                 {"id": "done", "op": "finish"}],
    }


POWER = {"values": [{"in": "включи", "out": "on"}, {"in": "потуши", "out": "off"},
                    {"in": "выключи", "out": "off"}]}


def catalog():
    """Сценарии с template-триггерами: списки — общие lexicons.json (room, minutes,
    hour, minute, app) + свой список power у света (перекрывает общие по имени)."""
    return [
        scenario("timers.timer.set", [{"kind": "template", "sentences": [
            "[поставь] [таймер] (на|в) {minutes} [минут]", "таймер на {minutes} минут"]}],
                 category="timers"),
        scenario("timers.alarm.set", [{"kind": "template", "sentences": [
            "[поставь] будильник на {hour} [часов] {minute} [минут]"]}], category="timers"),
        scenario("smart_home.light", [{"kind": "template",
                                       "sentences": ["({power}; свет) [(в|на) {room}]"],
                                       "lists": {"power": POWER}}]),
        scenario("device.app.open", [{"kind": "template", "sentences": ["(открой|запусти) {app}"]}],
                 category="device"),
    ]


# Таблица фраз из карточки TASK-022
TABLE = [
    ("поставь таймер на пять минут", "timers.timer.set", {"minutes": 5}),
    ("поставь таймер на 5 минут", "timers.timer.set", {"minutes": 5}),
    ("таймер на двадцать пять минут", "timers.timer.set", {"minutes": 25}),
    ("потуши свет в спальне", "smart_home.light", {"power": "off", "room": "спальня"}),
    ("поставь будильник на семь часов тридцать минут", "timers.alarm.set", {"hour": 7, "minute": 30}),
    ("открой ютуб", "device.app.open", {"app": "ютуб"}),
    ("алиса включи пожалуйста свет", "smart_home.light", {"power": "on"}),
    ("свет включи на кухне", "smart_home.light", {"power": "on", "room": "кухня"}),
]


def test_card_phrase_table():
    """Таблица фраз карточки: полное совпадение, слоты, перестановки, wildcard."""
    index = TemplateIndex(catalog())
    for text, sid, slots in TABLE:
        assert index.match(text) == (sid, slots), text


def test_template_scenarios_are_valid_v2():
    """Форма template-триггера проходит валидатор формата v2."""
    for item in catalog():
        assert validate_scenario(item)["id"] == item["id"]


def test_numbers_coerced_to_int_by_list_type():
    """Цифры hassil приходят float (5.0), слова — int: range-список приводит к int."""
    index = TemplateIndex(catalog())
    found = index.match("поставь таймер на 5 минут")
    assert isinstance(found[1]["minutes"], int) and found[1]["minutes"] == 5
    found = index.match("таймер на двадцать пять минут")
    assert isinstance(found[1]["minutes"], int) and found[1]["minutes"] == 25


def test_skip_words_from_builtin_asr():
    """skip_words собираются из lexicons.asr (wake_words, fillers): шум не мешает."""
    _lists, skip = merge_lexicons([builtin_source()])
    assert "алиса" in skip and "пожалуйста" in skip
    index = TemplateIndex(catalog())
    assert index.match("алиса включи пожалуйста свет") == ("smart_home.light", {"power": "on"})


def test_merge_sources_later_overrides_list_name():
    """Слияние источников: поздний перекрывает ранний по имени списка, skip_words
    объединяются (пользовательский > расширение > встроенный)."""
    builtin = {"lists": {"room": {"values": [{"in": "кухне", "out": "кухня"}]}},
               "skip_words": ["пожалуйста"]}
    extension = {"lists": {"room": {"values": [{"in": "зале", "out": "зал"}]}},
                 "skip_words": ["ну"]}
    user = {"asr": {"wake_words": ["алиса"]}}
    lists, skip = merge_lexicons([builtin, extension, user])
    assert [value["out"] for value in lists["room"]["values"]] == ["зал"]
    assert skip == ["пожалуйста", "ну", "алиса"]


def test_trigger_lists_override_shared():
    """Свой список триггера перекрывает общий список источников с тем же именем."""
    index = TemplateIndex(catalog(), sources=[{"lists": {"power": {"values": [
        {"in": "включи", "out": "bogus"}]}}}])
    assert index.match("включи свет") == ("smart_home.light", {"power": "on"})


def test_with_templates_puts_template_first():
    phrase, template = {"id": "a.phrase"}, {"id": "b.template"}
    assert with_templates([(phrase, "phrase:contains")], [template]) == \
        [(template, "template"), (phrase, "phrase:contains")]
    assert with_templates([(phrase, "phrase:exact")], []) == [(phrase, "phrase:exact")]


def test_broken_template_rejected_on_save():
    """Ошибка hassil при сохранении — понятный текст с номером фразы."""
    with pytest.raises(PackageError) as exc:
        validate_scenario(scenario("broken.template", [
            {"kind": "template", "sentences": ["включи свет", "(({broken"]}]))
    assert "фраза 2" in str(exc.value) and "hassil" in str(exc.value)


def test_unknown_list_reference_rejected_on_save():
    with pytest.raises(PackageError) as exc:
        validate_scenario(scenario("bad.reference", [
            {"kind": "template", "sentences": ["включи {nosuch}"]}]))
    assert "nosuch" in str(exc.value)


def test_template_trigger_unknown_keys_rejected():
    with pytest.raises(PackageError):
        validate_scenario(scenario("bad.keys", [
            {"kind": "template", "sentences": ["включи свет"], "junk": 1}]))


class CapturingExecutor(Executor):
    """Запоминает ctx, с которым сценарий ушёл в исполнение (доставка slots шаблона)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured = None

    async def execute(self, scenario, ctx, dialog_id):
        self.captured = dict(ctx.get("slots") or {})
        return await super().execute(scenario, ctx, dialog_id)


async def send_nothing(_data):
    """Отправка без эффектов: отчёт dialog.handle_text проверяется сам."""


async def test_template_route_and_slots_reach_ctx(tmp_path):
    """Полный путь: template-совпадение выбирает сценарий, сущности — в ctx.slots."""
    executor = CapturingExecutor(schemas_root=SCHEMAS)
    dialog = DialogManager(executor, lambda: catalog(), server_type())
    sent = []

    async def send(data):
        sent.append(data)
    report = await dialog.handle_text("потуши свет в спальне", dict(CTX), "dlg-t", send)
    assert report["handled"] and report["reason"] == "template" \
        and report["scenario"] == "smart_home.light"
    assert executor.captured == {"power": "off", "room": "спальня"}
    assert sent  # ответ сценария доставлен


async def test_template_priority_over_contains(tmp_path):
    """Шаблонное совпадение приоритетнее phrase-contains."""
    scenarios = catalog() + [scenario("phrase.light", [{"kind": "phrase", "phrases": ["свет"]}])]
    dialog = DialogManager(Executor(schemas_root=SCHEMAS), lambda: scenarios, server_type())
    report = await dialog.handle_text("включи свет", dict(CTX), "dlg-p", send_nothing)
    assert report["scenario"] == "smart_home.light" and report["reason"] == "template"


async def test_phrase_triggers_work_as_before(tmp_path):
    """Старые phrase-триггеры не сломаны: contains (по вхождению) и exact работают."""
    scenarios = catalog() + [
        scenario("phrase.tail", [{"kind": "phrase", "phrases": ["включи свет везде"]}]),
        scenario("phrase.exact", [{"kind": "phrase", "name": "погода", "match": "exact"}]),
    ]
    dialog = DialogManager(Executor(schemas_root=SCHEMAS), lambda: scenarios, server_type())
    report = await dialog.handle_text("ну включи свет везде", dict(CTX), "dlg-c", send_nothing)
    assert report["scenario"] == "phrase.tail" and report["reason"] == "phrase:contains"
    report = await dialog.handle_text("погода", dict(CTX), "dlg-e", send_nothing)
    assert report["scenario"] == "phrase.exact" and report["reason"] == "phrase:exact"


async def test_cache_recompiles_when_catalog_changes(tmp_path):
    """Компиляция один раз на версию каталога; сохранение сценария — перекомпиляция."""
    holder = {"catalog": [item for item in catalog() if item["id"] != "device.app.open"]}
    dialog = DialogManager(Executor(schemas_root=SCHEMAS), lambda: holder["catalog"], server_type())
    first = dialog._template_index(holder["catalog"])
    assert dialog._template_index(list(holder["catalog"])) is first  # кэш держится
    report = await dialog.handle_text("открой ютуб", dict(CTX), "dlg-1", send_nothing)
    assert report["handled"] is False  # сценария с шаблоном ещё нет
    # «сохранение сценария» (PUT /api/scenarios): каталог изменился → перекомпиляция
    holder["catalog"] = holder["catalog"] + [validate_scenario(scenario(
        "device.app.open", [{"kind": "template", "sentences": ["(открой|запусти) {app}"]}]))]
    assert dialog._template_index(holder["catalog"]) is not first
    report = await dialog.handle_text("открой ютуб", dict(CTX), "dlg-2", send_nothing)
    assert report["handled"] and report["scenario"] == "device.app.open" \
        and report["reason"] == "template"


async def test_template_slots_reach_json_compose(tmp_path):
    """Сущности template-триггера доступны JSON-шагам как {{slots.*}} (TASK-022)."""
    scripted = scenario("timers.timer.set", [{"kind": "template", "sentences": [
        "[поставь] [таймер] (на|в) {minutes} [минут]", "таймер на {minutes} минут"]}],
        text="Таймер на {{slots.minutes}} минут.")
    executor = Executor(schemas_root=SCHEMAS)
    dialog = DialogManager(executor, lambda: [scripted], server_type())
    sent = []

    async def send(data):
        sent.append(data)
    try:
        await dialog.handle_text("поставь таймер на 5 минут", dict(CTX), "dlg-s", send)
        cards = [m.Event.AliceResponse.Response.Cards[0].Text for raw in sent
                 if raw.startswith(b"AAPI")
                 for m in [server_type().FromString(raw[4:])]
                 if m.Event.WhichOneof("Event") == "AliceResponse"]
        assert cards and cards[0] == "Таймер на 5 минут.", cards
    finally:
        await executor.close() if hasattr(executor, "close") else None
