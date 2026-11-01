"""076: корпус команд справки и отчёт покрытия.

Схема корпуса, отсутствие дублей, ссылки expect на существующие сценарии; фразы
с expect ≠ null совпадают (регрессия); --baseline ловит ухудшение (проверено
подменой отчёта). Прогон — на временном Runtime, без сети и без state/."""
import copy
import json

import pytest

from server_emulator.scenario_engine.library import SCENARIOS
from tools.phrase_coverage import GROUPS, load_corpus, regressions, run_corpus

CORPUS_PATH = "tests/fixtures/phrases/station_help.json"
BASELINE_PATH = "tests/fixtures/phrases/phrase_coverage_baseline.json"


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS_PATH)


def test_corpus_schema_and_scale(corpus):
    """Схема корпуса, группы, масштаб (≥130 фраз из ≥30 разделов), без дублей."""
    assert corpus["source"].startswith("https://alice.yandex.ru/support/")
    sections = corpus["sections"]
    texts = [p["text"] for s in sections for p in s["phrases"]]
    assert len(texts) >= 130
    assert len({s["slug"] for s in sections if s["phrases"]}) >= 30
    assert len(texts) == len(set(texts)), "дубли фраз в корпусе"
    for section in sections:
        assert section["group"] in GROUPS, section["slug"]
        assert section["title"] and section["slug"]
        for phrase in section["phrases"]:
            text = phrase["text"]
            assert text == text.lower() and text.strip() == text, text
            assert "алиса" not in text.split(" ", 1)[0], text  # без обращения в начале
            assert "«" not in text and "»" not in text and "**" not in text, text


def test_corpus_expect_refs_existing_scenarios(corpus):
    """expect — id встроенного сценария или null; фраз с expect достаточно для регрессии."""
    known = set(SCENARIOS)
    filled = 0
    for section in corpus["sections"]:
        for phrase in section["phrases"]:
            expect = phrase["expect"]
            if expect is not None:
                assert expect in known, (phrase["text"], expect)
                filled += 1
    assert filled >= 40  # 077–083 будут добавлять свои


async def test_expected_phrases_still_match(corpus):
    """Регрессия: каждая фраза с expect ≠ null отвечает ожидаемым сценарием."""
    results = await run_corpus(corpus)
    by_text = {item["text"]: item for item in results}
    bad = [(item["text"], item["scenario"])
           for item in results
           if item["expect"] is not None and item["verdict"] != "ok"]
    assert not bad, bad
    assert len(by_text) == sum(len(s["phrases"]) for s in corpus["sections"])


def test_baseline_regression_detection(corpus):
    """--baseline ловит ухудшение: ok→other/miss и other→miss; улучшения — нет."""
    baseline = json.loads(open(BASELINE_PATH, encoding="utf-8").read())
    current = copy.deepcopy(baseline)
    assert regressions(current, baseline) == []  # идентичный отчёт — без регрессий
    # подмена: первая ok-фраза отвечает другим сценарием → регрессия
    tampered = copy.deepcopy(baseline)
    item = next(x for x in tampered["results"] if x["verdict"] == "ok")
    item["verdict"], item["scenario"] = "other", "device.app.open"
    failed = regressions(tampered, baseline)
    assert len(failed) == 1 and failed[0][0]["text"] == item["text"]
    # подмена: промах вместо ответа → регрессия; появление ответа — нет
    miss = copy.deepcopy(baseline)
    for x in miss["results"]:
        if x["verdict"] == "other":
            x["verdict"], x["scenario"] = "miss", None
    assert regressions(miss, baseline)  # other→miss
    improved = copy.deepcopy(baseline)
    for x in improved["results"]:
        if x["verdict"] == "miss":
            x["verdict"], x["scenario"] = "other", "device.app.open"
    assert regressions(improved, baseline) == []  # miss→other — не регрессия
    # эталон в репозитории согласован с корпусом (без регрессий против текущего прогона)
