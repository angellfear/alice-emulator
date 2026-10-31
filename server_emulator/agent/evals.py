"""Оценка агента на наборе фраз (evals): роутинг сценариев без модели и прогоны агента с моделью.

    python -m server_emulator.agent.evals --state <папка state> [--cases файл.json] [--llm]

Случай: {"phrase", "scenario"} — фразу должен взять этот сценарий (null — ни один, уходит
агенту); {"phrase", "tools": [...], "answer": "подстрока", "llm": true} — прогон агента:
вызваны эти инструменты (scenario_run сверяется с полем "scenario_run": id), ответ содержит
подстроку. Случаи llm запускаются только с --llm (нужна настроенная модель в state). Каждый
llm-случай — свой тред eval:<n>; вызовы, ждущие подтверждения, засчитываются как вызванные и
не выполняются. Набор по умолчанию — evals/cases.json рядом с модулем.
"""
import argparse
import asyncio
import datetime
import json
import sys
import uuid
from pathlib import Path

CASES = Path(__file__).parent / "evals" / "cases.json"


async def routed(runtime, phrase, sn=""):
    ctx = {"input": {"message_id": uuid.uuid4().hex, "request_id": uuid.uuid4().hex, "dialog_id": "", "sn": sn,
                     "text": phrase}, "clock": {"now": datetime.datetime.now()}, "sequence": 1}
    ctx.update(runtime._scenario_context(sn=sn))
    report = await runtime.dialog.handle_text(phrase, ctx, "eval-" + uuid.uuid4().hex, None, deliver=False)
    return report.get("scenario") if report.get("handled") else None


async def check(runtime, case, n):
    """→ (ok, подробность)."""
    if not case.get("llm"):
        got = await routed(runtime, case["phrase"], case.get("sn") or "")
        return got == case.get("scenario"), f"сценарий {got}"
    thread = f"eval:{n}"
    await runtime.agent.reset(runtime, thread)
    answer = await runtime.chat(case["phrase"], thread=thread)
    called = [r["name"] for r in answer["tool_runs"]] + [c["function"]["name"] for c in answer["tool_calls"]]
    runs = [json.loads(c["function"]["arguments"]) for c in answer["tool_calls"] if c["function"]["name"] == "scenario_run"]
    runs += [r.get("arguments") or {} for r in answer["tool_runs"] if r["name"] == "scenario_run"]
    problems = [f"не вызван {t}" for t in case.get("tools") or [] if t not in called]
    if case.get("scenario_run") and case["scenario_run"] not in [r.get("id") for r in runs]:
        problems.append("scenario_run не " + case["scenario_run"])
    if case.get("answer") and case["answer"].lower() not in answer["text"].lower():
        problems.append("в ответе нет «" + case["answer"] + "»")
    await runtime.agent.reset(runtime, thread)
    return not problems, "; ".join(problems) or f"инструменты {called}"


async def run(state, cases_path=CASES, llm=False, out=sys.stdout):
    from ..runtime import Runtime
    runtime = Runtime(Path(state))
    cases = [c for c in json.loads(Path(cases_path).read_text(encoding="utf-8")) if llm or not c.get("llm")]
    passed = 0
    try:
        for n, case in enumerate(cases, 1):
            try:
                ok, detail = await check(runtime, case, n)
            except Exception as exc:
                ok, detail = False, type(exc).__name__ + ": " + str(exc)[:200]
            passed += ok
            print(f"{'OK  ' if ok else 'FAIL'} «{case['phrase']}» — {detail}", file=out)
    finally:
        await runtime.close()
    print(f"{passed}/{len(cases)}", file=out)
    return passed, len(cases)


def main():
    parser = argparse.ArgumentParser(description="Оценка агента и роутинга сценариев на наборе фраз")
    parser.add_argument("--state", required=True)
    parser.add_argument("--cases", default=str(CASES))
    parser.add_argument("--llm", action="store_true", help="запускать и случаи с моделью")
    args = parser.parse_args()
    passed, total = asyncio.run(run(args.state, args.cases, args.llm))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
