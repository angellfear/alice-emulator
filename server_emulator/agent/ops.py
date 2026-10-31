"""Эксплуатация агента: журнал прогонов (трассировка), дневной бюджет, PII-фильтр, промахи.

Каждый прогон AgentService пишется в state/agent_ops.sqlite3: тред, источник, реплика, ответ,
инструменты, число вызовов модели и токены, длительность. По журналу считается дневной бюджет
пользователя (agent.daily_model_calls; 0 — без лимита) и ищутся промахи — голосовые просьбы,
которые не взял ни один сценарий и которые ушли агенту. Частые промахи раз в сутки
(agent.suggest_cron) отдаются агенту фоновым прогоном: он готовит черновики сценариев,
администратор включает их во вкладке «Сценарии».
"""
import datetime
import json
import re
import sqlite3
import time
from pathlib import Path

DEFAULTS = {"daily_model_calls": 0, "budgets": {}, "pii": ["credit_card", "secret"],
            "suggest_cron": "30 4 * * *", "suggest_min_count": 3, "suggest_days": 7, "webhook_token": ""}
BUDGET_TEXT = "Дневной лимит запросов к модели исчерпан. Продолжим завтра."
# Ключи и токены в тексте: sk-…/ghp_…/xoxb-…, Bearer …, «пароль: …», api_key=…
SECRET = (r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"
          r"|bearer\s+[A-Za-z0-9._~+/-]{16,}=*"
          r"|(?:api[_-]?key|token|secret|password|пароль|токен|ключ)\s*[:=]\s*\S{6,})")
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, thread TEXT, kind TEXT, user_id TEXT, sn TEXT,
    text TEXT, answer TEXT, tools TEXT, model_calls INTEGER, tokens INTEGER, ms INTEGER, status TEXT);
CREATE INDEX IF NOT EXISTS runs_day ON runs (day, user_id);
"""
KEEP_DAYS = 30


def options(runtime):
    return {**DEFAULTS, **(runtime.settings.get("agent") or {})}


def _day(ts=None):
    return datetime.date.fromtimestamp(ts or time.time()).isoformat()


def normalize(text):
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е")).split())


def middleware(runtime):
    """PIIMiddleware по agent.pii: карты маскируются, секреты вырезаются — и во вводе, и в
    результатах инструментов, до того как текст уйдёт провайдеру модели и в checkpoint."""
    from langchain.agents.middleware import PIIMiddleware
    result = []
    for kind in options(runtime)["pii"] or []:
        if kind == "secret":
            result.append(PIIMiddleware("secret", detector=SECRET, strategy="redact", apply_to_tool_results=True))
        else:
            result.append(PIIMiddleware(kind, strategy="mask" if kind == "credit_card" else "redact",
                                        apply_to_tool_results=True))
    return result


class Ops:
    def __init__(self, directory):
        self.path = str(Path(directory) / "agent_ops.sqlite3")
        with self._db() as db:
            db.executescript(SCHEMA)
            db.execute("DELETE FROM runs WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, run):
        hide = lambda value: re.sub(SECRET, "[REDACTED_SECRET]", value or "")[:500]  # noqa: E731
        tools = [{"name": r.get("name"), "status": r.get("status")} for r in run.get("tools") or []]
        with self._db() as db:
            db.execute("INSERT INTO runs (ts, day, thread, kind, user_id, sn, text, answer, tools, model_calls, tokens,"
                       " ms, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (run["ts"], _day(run["ts"]), run.get("thread"), run.get("kind"), run.get("user_id") or "",
                        run.get("sn") or "", hide(run.get("text")), hide(run.get("answer")),
                        json.dumps(tools, ensure_ascii=False), run.get("model_calls", 0), run.get("tokens", 0),
                        run.get("ms", 0), run.get("status") or "ok"))

    def runs(self, limit=100, user_id=None):
        query, args = "SELECT * FROM runs", []
        if user_id:
            query, args = query + " WHERE user_id = ?", [user_id]
        with self._db() as db:
            rows = db.execute(query + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        return [{**dict(r), "tools": json.loads(r["tools"] or "[]")} for r in rows]

    def usage(self, day=None):
        """Расход за день по пользователям: {user_id: {runs, model_calls, tokens}}."""
        with self._db() as db:
            rows = db.execute("SELECT user_id, COUNT(*) runs, SUM(model_calls) calls, SUM(tokens) tokens FROM runs"
                              " WHERE day = ? GROUP BY user_id", (day or _day(),)).fetchall()
        return {r["user_id"]: {"runs": r["runs"], "model_calls": r["calls"] or 0, "tokens": r["tokens"] or 0}
                for r in rows}

    def limit(self, runtime, user_id):
        cfg = options(runtime)
        return int((cfg["budgets"] or {}).get(user_id or "", cfg["daily_model_calls"]) or 0)

    def over_budget(self, runtime, user_id):
        limit = self.limit(runtime, user_id)
        return bool(limit) and self.usage().get(user_id or "", {}).get("model_calls", 0) >= limit

    def misses(self, days=7, min_count=1):
        """Голосовые просьбы, ушедшие агенту, сгруппированные по нормализованной фразе."""
        with self._db() as db:
            rows = db.execute("SELECT text, tools, ts FROM runs WHERE kind = 'request' AND thread LIKE 'voice:%' AND status != 'budget'"
                              " AND ts >= ? ORDER BY ts", (time.time() - days * 86400,)).fetchall()
        groups = {}
        for row in rows:
            key = normalize(row["text"])
            if not key:
                continue
            group = groups.setdefault(key, {"phrase": key, "count": 0, "examples": [], "tools": [], "last": 0})
            group["count"] += 1
            group["last"] = row["ts"]
            if row["text"] not in group["examples"] and len(group["examples"]) < 3:
                group["examples"].append(row["text"])
            for tool in json.loads(row["tools"] or "[]"):
                if tool["name"] not in group["tools"]:
                    group["tools"].append(tool["name"])
        return sorted((g for g in groups.values() if g["count"] >= min_count), key=lambda g: -g["count"])

    async def suggest(self, runtime):
        """Частые промахи → фоновый прогон агента, который готовит черновики сценариев."""
        cfg = options(runtime)
        from . import scenario_edit
        drafted = {normalize(p) for d in scenario_edit.drafts(runtime)
                   for t in d["scenario"].get("triggers") or [] for p in t.get("phrases") or []}
        misses = [m for m in self.misses(int(cfg["suggest_days"]), int(cfg["suggest_min_count"]))
                  if m["phrase"] not in drafted][:10]
        from . import engine
        if not misses or engine(runtime) != "graph":
            return {"status": "nothing", "misses": misses}
        lines = "\n".join(f"- «{m['examples'][0]}» — {m['count']} раз; агент вызывал: {', '.join(m['tools']) or 'ничего'}"
                          for m in misses)
        prompt = ("Эти голосовые просьбы не взял ни один сценарий, и их выполнял ты:\n" + lines + "\n\n"
                  "Для каждой, которую колонка может выполнять сама без модели (те же действия каждый раз), "
                  "сделай черновик сценария: scenario_draft (на основе подходящего сценария — from_id, "
                  "или новый), затем scenario_test с фразами-примерами. Не включай сценарии — их включит "
                  "администратор. Просьбы, где нужен разговор или каждый раз разный ответ, пропусти. "
                  "Итог — одной фразой: сколько черновиков сделано.")
        thread = "ops:suggest"
        await runtime.agent.reset(runtime, thread)
        answer = await runtime.agent.chat(runtime, prompt, thread=thread, background="предложения сценариев",
                                          user_id="", kind="suggest")
        await runtime.agent.reset(runtime, thread)  # недошедшие подтверждения не висят
        return {"status": "done", "misses": misses, "text": answer["text"]}
