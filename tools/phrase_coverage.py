"""Покрытие команд справки Яндекса сценариями эмулятора (корпус 076).

Для каждой фразы tests/fixtures/phrases/station_help.json — dry-run маршрутизатора
(тот же вход, что у вкладки «Проверка»: dialog.handle_text без доставки и без
«Интеллекта») на временном экземпляре: server_emulator/state/ не трогается, сети нет
(плагин погоды подменён тихим отказом). Итог по фразе: ok (ответил ожидаемый сценарий),
other (ответил другой), miss (никто). --json — машинный отчёт (он же формат baseline),
--baseline — сравнение с прошлым прогоном: фраза, которая раньше отвечала ожидаемым
сценарием, а теперь отвечает другой или промахивается (и раньше отвечала хоть чем-то,
а теперь промах) — регрессия, код выхода 1.

Использование:
    python tools/phrase_coverage.py                 # сводка по группам + промахи
    python tools/phrase_coverage.py --group media   # только одна группа
    python tools/phrase_coverage.py --json          # машинный отчёт в stdout
    python tools/phrase_coverage.py --baseline reports.json
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "tests" / "fixtures" / "phrases" / "station_help.json"
GROUPS = ("media", "time", "chat", "knowledge", "unavailable", "device")


def load_corpus(path=CORPUS):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def make_runtime(directory):
    """Runtime на временной папке: маршрутизация как на живом экземпляре, без сети
    (плагин погоды отвечает отказом — сценарий даёт честную ошибку данных). Библиотека
    — демо-файлы с названиями из корпуса: голые запросы («включи майкла джексона»)
    должны находить музыку, иначе честный отказ не отличить от промаха."""
    from server_emulator.runtime import Runtime

    runtime = Runtime(directory)

    async def _no_weather(key, args):
        return None

    runtime.scenario_plugins["weather"] = lambda fetch, session: _no_weather
    media_root = runtime.directory / "media"
    for relative in ("music/Майкл Джексон/Beat It.mp3", "music/Наутилус Помпилиус/Химера.mp3",
                     "music/Кино/Группа крови.mp3", "music/Ромашки/Ромашки.mp3",
                     "music/Вечно молодой.mp3", "music/Белые розы.mp3", "music/Мое сердце.mp3",
                     "music/Элвис Пресли/Hound Dog.mp3", "music/Metallica/Enter Sandman.mp3",
                     "music/Аудиокниги/Дюна.mp3", "music/Аудиокниги/Книга Война и мир.mp3",
                     "music/Диско/Диско.mp3",
                     "music/Индастриал/Индастриал.mp3", "music/Настроение/Радостное.mp3",
                     "music/Настроение/Грустное.mp3", "music/Настроение/Осеннее.mp3",
                     "music/Хиты/Вечные хиты.mp3", "music/Новая музыка/Новая музыка.mp3",
                     "music/Популярное/Популярная музыка.mp3", "music/Жанры/Русская поп музыка.mp3",
                     "music/Тренировка/Веселый рок.mp3", "music/Фон/Фоновая музыка.mp3",
                     "music/Кельтская музыка.mp3", "music/Чарт/Самое популярное.mp3",
                     "tales/Пряничный домик.mp3",
                     "tales/Сергей Лазарев/Сказка.mp3",
                     "sounds/Шум дождя.mp3", "sounds/Пение птиц.mp3", "sounds/Шум океана.mp3"):
        path = media_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3demo" * 8)
    (media_root / "likes.json").write_text(
        json.dumps({"": {"music": {"like": ["Ромашки/Ромашки.mp3"], "dislike": []}}}, ensure_ascii=False),
        encoding="utf-8")
    runtime.settings.setdefault("media", {})["radio"] = [
        {"name": "Европа Плюс", "url": "http://demo.example/europa"}]
    return runtime


async def run_corpus(corpus, runtime=None):
    """Прогон всех фраз → [(group, section, text, expect, verdict, scenario, reason)].
    verdict: ok | other | miss. Чужой runtime не закрывается."""
    own = runtime is None
    if own:
        runtime = make_runtime(tempfile.mkdtemp(prefix="phrase_coverage_"))
    try:
        results = []
        for section in corpus["sections"]:
            for index, phrase in enumerate(section["phrases"]):
                text = phrase["text"]
                ctx = {"input": {"message_id": "coverage", "request_id": "coverage",
                                 "dialog_id": f"coverage-{len(results)}-{index}", "sn": "",
                                 "text": text},
                       "clock": {"now": datetime.datetime.now()}, "dry_run": True}
                ctx.update(runtime._scenario_context(sn=""))
                # свой dialog_id на фразу: сценарий в ожидании уточнения (awaiting)
                # не должен продолжаться на следующей фразе
                report = await runtime.dialog.handle_text(text, ctx, ctx["input"]["dialog_id"],
                                                          None, deliver=False)
                scenario = report.get("scenario") if report.get("handled") else None
                expect = phrase.get("expect")
                verdict = "miss" if scenario is None else ("ok" if scenario == expect else "other")
                results.append({"group": section["group"], "section": section["slug"],
                                "text": text, "expect": expect, "verdict": verdict,
                                "scenario": scenario, "reason": report.get("reason")})
        return results
    finally:
        if own:
            await runtime.close()


def summarize(results, group=None):
    """Сводка по группам: [(group, всего, ok, other, miss)] и список промахов."""
    groups = {}
    misses = []
    for item in results:
        if group and item["group"] != group:
            continue
        stats = groups.setdefault(item["group"], [0, 0, 0, 0])
        stats[0] += 1
        stats[{"ok": 1, "other": 2, "miss": 3}[item["verdict"]]] += 1
        if item["verdict"] == "miss":
            misses.append(item)
    return sorted(groups.items()), misses


def regressions(results, baseline):
    """Фразы, которые раньше отвечали (ok — ожидаемым, other — хоть каким-то сценарием),
    а теперь ответили хуже: ok→other/miss, other→miss. Появление ответа (miss→ok/other)
    и смена «другого» сценария на другой «другой» — не регрессия. Аргументы — отчёт
    --json (объект с results) или список результатов."""
    if isinstance(results, dict):
        results = results.get("results") or []
    if isinstance(baseline, dict):
        baseline = baseline.get("results") or []
    before = {item["text"]: item for item in baseline}
    found = []
    for item in results:
        old = before.get(item["text"])
        if old is None:
            continue
        if old["verdict"] == "ok" and item["verdict"] != "ok":
            found.append((item, old))
        elif old["verdict"] == "other" and item["verdict"] == "miss":
            found.append((item, old))
    return found


def print_report(results, group=None):
    groups, misses = summarize(results, group)
    print("группа       всего    ok  другой  промах")
    for name, (total, ok, other, miss) in groups:
        print(f"{name:12} {total:5} {ok:5} {other:7} {miss:6}")
    if misses:
        print(f"\nПромахи ({len(misses)}):")
        for item in misses:
            print(f"  [{item['group']}/{item['section']}] {item['text']}")
    matched = sum(1 for item in results if item["verdict"] == "ok")
    total = sum(count[0] for _name, count in groups)
    print(f"\nИтого: {matched}/{total} фраз отвечают ожидаемым сценарием.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Покрытие команд справки сценариями")
    parser.add_argument("--group", choices=GROUPS, help="только одна группа")
    parser.add_argument("--json", action="store_true", help="машинный отчёт")
    parser.add_argument("--baseline", help="сравнить с прошлым отчётом (--json)")
    args = parser.parse_args(argv)

    corpus = load_corpus()
    results = asyncio.run(run_corpus(corpus))
    if args.json or args.baseline:
        payload = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "results": results}
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        failed = regressions(results, baseline)
        for item, old in failed:
            print(f"РЕГРЕССИЯ [{item['group']}/{item['section']}] {item['text']}: "
                  f"было {old['verdict']} ({old['scenario']}), стало "
                  f"{item['verdict']} ({item['scenario']})")
        print(f"Регрессий: {len(failed)}")
        if not args.json:
            print_report(results, args.group)
        return 1 if failed else 0
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0
    print_report(results, args.group)
    return 0


if __name__ == "__main__":
    sys.exit(main())
