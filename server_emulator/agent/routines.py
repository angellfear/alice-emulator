"""Задания по расписанию: агент или сценарий по cron/интервалу/разово, доставка на колонку.

Источник истины — таблица routines в state/routines.sqlite3 (отдельный файл: синхронные записи
не ждут транзакций асинхронного checkpointer'а agent.sqlite3); задачи APScheduler (AsyncIOScheduler,
MemoryJobStore) пересобираются из неё при старте и при каждой правке. Пропуски во время
простоя не догоняются (как таймеры): помечаются missed в журнале routine_runs.

Задание:
    {id, title, owner (user_id), sn, kind: agent|scenario, prompt | scenario_id + text,
     schedule: {kind: cron|interval|once, expr}, tz, delivery: speak|inbox|silent,
     quiet: {from: "HH:MM", to: "HH:MM"} | null, enabled, created_by: user|agent,
     max_runs_per_day, created, last_run}
* agent    — прогон агента в треде routine:<id> с пометкой «фоновое задание»; вызовы,
             требующие подтверждения, не выполняются, а ждут решения в панели;
* scenario — сценарий без модели (scenario_run), дёшево и предсказуемо.

Доставка: speak — служебное событие @@agent_say в живую сессию колонки (как срабатывание
таймера, сценарий system.agent_say); колонка не в сети или тихие часы — во входящие
(inbox): агент сообщит их при следующем обращении пользователя. silent — только журнал.
"""
import copy
import datetime
import json
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

KINDS = ("agent", "scenario")
SCHEDULES = ("cron", "interval", "once")
DELIVERY = ("speak", "inbox", "silent")
MISSED_GRACE_S = 120
NAMES = {"routine_list", "routine_create", "routine_update", "routine_delete"}
RISKS = {"routine_list": "read", "routine_create": "config", "routine_update": "config", "routine_delete": "destructive"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL,
    created REAL NOT NULL, last_run REAL);
CREATE TABLE IF NOT EXISTS routine_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, routine_id TEXT NOT NULL,
    ts REAL NOT NULL, status TEXT NOT NULL, text TEXT, error TEXT, delivered TEXT);
CREATE INDEX IF NOT EXISTS routine_runs_by_routine ON routine_runs (routine_id, ts);
CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, sn TEXT,
    routine_id TEXT, title TEXT, text TEXT NOT NULL, ts REAL NOT NULL, delivered REAL);
"""


def _hhmm(value):
    hour, minute = map(int, str(value).split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError
    return hour * 60 + minute


def validate(routine, users):
    """Нормализованное задание или ValueError с причиной."""
    r = copy.deepcopy(routine)
    if not isinstance(r.get("title"), str) or not r["title"].strip():
        raise ValueError("title: название задания")
    if r.get("owner") not in users:
        raise ValueError("owner: пользователь реестра")
    r.setdefault("kind", "agent")
    if r["kind"] not in KINDS:
        raise ValueError("kind ∈ " + ", ".join(KINDS))
    if r["kind"] == "agent" and not str(r.get("prompt") or "").strip():
        raise ValueError("prompt: что сделать агенту")
    if r["kind"] == "scenario" and not (r.get("scenario_id") or r.get("text")):
        raise ValueError("scenario_id или text: какой сценарий запускать")
    schedule = r.get("schedule") or {}
    if schedule.get("kind") not in SCHEDULES:
        raise ValueError("schedule.kind ∈ " + ", ".join(SCHEDULES))
    r.setdefault("tz", "Europe/Moscow")
    try:
        ZoneInfo(r["tz"])
    except Exception:
        raise ValueError("tz: таймзона IANA, например Europe/Moscow")
    trigger(r)  # проверка выражения
    r.setdefault("delivery", "speak")
    if r["delivery"] not in DELIVERY:
        raise ValueError("delivery ∈ " + ", ".join(DELIVERY))
    if r.get("quiet"):
        try:
            _hhmm(r["quiet"]["from"]), _hhmm(r["quiet"]["to"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("quiet: {from: 'HH:MM', to: 'HH:MM'}")
    r["max_runs_per_day"] = max(1, min(1440, int(r.get("max_runs_per_day") or 24)))
    r["enabled"] = bool(r.get("enabled", True))
    r.setdefault("created_by", "user")
    return r


def trigger(routine):
    schedule, tz = routine["schedule"], ZoneInfo(routine.get("tz") or "Europe/Moscow")
    expr = str(schedule.get("expr") or "").strip()
    try:
        if schedule["kind"] == "cron":
            return CronTrigger.from_crontab(expr, timezone=tz)
        if schedule["kind"] == "interval":
            seconds = int(expr)
            if seconds < 60:
                raise ValueError
            start = datetime.datetime.fromtimestamp(routine.get("created") or time.time(), tz)
            return IntervalTrigger(seconds=seconds, start_date=start, timezone=tz)
        moment = datetime.datetime.fromisoformat(expr)
        return DateTrigger(run_date=moment if moment.tzinfo else moment.replace(tzinfo=tz), timezone=tz)
    except (ValueError, TypeError):
        raise ValueError({"cron": "cron: пять полей, например «0 8 * * 1-5»",
                          "interval": "interval: секунды, не меньше 60",
                          "once": "once: дата-время ISO, например 2026-11-01T08:00"}[schedule["kind"]])


def quiet_now(routine, now=None):
    quiet = routine.get("quiet")
    if not quiet:
        return False
    local = datetime.datetime.fromtimestamp(now or time.time(), ZoneInfo(routine.get("tz") or "Europe/Moscow"))
    minute, start, end = local.hour * 60 + local.minute, _hhmm(quiet["from"]), _hhmm(quiet["to"])
    return start <= minute < end if start <= end else (minute >= start or minute < end)


class Routines:
    def __init__(self, runtime, path):
        self.runtime, self.path = runtime, str(path)
        self.scheduler = None
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # --- хранилище -------------------------------------------------------------------

    def list(self, owner=None):
        with self._db() as db:
            rows = db.execute("SELECT * FROM routines" + (" WHERE owner = ?" if owner else "") + " ORDER BY created",
                              (owner,) if owner else ()).fetchall()
        return [self._routine(row) for row in rows]

    def get(self, routine_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchone()
        if row is None:
            raise ValueError("Нет задания: " + str(routine_id))
        return self._routine(row)

    def _routine(self, row):
        routine = json.loads(row["data"])
        routine.update(id=row["id"], owner=row["owner"], created=row["created"], last_run=row["last_run"])
        if self.scheduler and routine["enabled"]:
            job = self.scheduler.get_job(routine["id"])
            routine["next_run"] = job.next_run_time.timestamp() if job and job.next_run_time else None
        return routine

    def save(self, routine):
        users = {u["id"] for u in self.runtime.identities.data["users"]}
        existing = None
        if routine.get("id"):
            existing = self.get(routine["id"])
            routine = {**existing, **routine}
        routine = validate({**routine, "created": (existing or {}).get("created") or time.time()}, users)
        routine_id = routine.get("id") or "r" + uuid.uuid4().hex[:10]
        data = {k: v for k, v in routine.items() if k not in ("id", "owner", "created", "last_run", "next_run")}
        with self._db() as db:
            db.execute("INSERT INTO routines (id, owner, data, created, last_run) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT(id) DO UPDATE SET owner = excluded.owner, data = excluded.data",
                       (routine_id, routine["owner"], json.dumps(data, ensure_ascii=False), routine["created"],
                        (existing or {}).get("last_run")))
        self._schedule(self.get(routine_id))
        return self.get(routine_id)

    def delete(self, routine_id):
        self.get(routine_id)
        with self._db() as db:
            db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
        if self.scheduler and self.scheduler.get_job(routine_id):
            self.scheduler.remove_job(routine_id)

    def runs(self, routine_id=None, limit=50):
        with self._db() as db:
            rows = db.execute("SELECT * FROM routine_runs" + (" WHERE routine_id = ?" if routine_id else "")
                              + " ORDER BY id DESC LIMIT ?", ((routine_id, limit) if routine_id else (limit,))).fetchall()
        return [dict(row) for row in rows]

    def _record(self, routine_id, status, text=None, error=None, delivered=None, ts=None):
        with self._db() as db:
            db.execute("INSERT INTO routine_runs (routine_id, ts, status, text, error, delivered) VALUES (?, ?, ?, ?, ?, ?)",
                       (routine_id, ts or time.time(), status, text, error, delivered))

    def inbox(self, user_id=None, unread=False):
        query, args = "SELECT * FROM inbox WHERE 1=1", []
        if user_id:
            query, args = query + " AND user_id = ?", args + [user_id]
        if unread:
            query += " AND delivered IS NULL"
        with self._db() as db:
            return [dict(row) for row in db.execute(query + " ORDER BY id DESC LIMIT 100", args).fetchall()]

    def mark_read(self, ids):
        with self._db() as db:
            db.executemany("UPDATE inbox SET delivered = ? WHERE id = ?", [(time.time(), i) for i in ids])

    # --- планировщик -----------------------------------------------------------------

    def start(self):
        """Планировщик в текущем event loop; пропуски простоя — missed, без догоняющего запуска."""
        if self.scheduler is not None:
            return
        self.scheduler = AsyncIOScheduler(timezone=datetime.timezone.utc)
        self.scheduler.start()
        self._schedule_suggest()
        now = time.time()
        for routine in self.list():
            if not routine["enabled"]:
                continue
            reference = datetime.datetime.fromtimestamp(routine["last_run"] or routine["created"], datetime.timezone.utc)
            due = trigger(routine).get_next_fire_time(None, reference + datetime.timedelta(seconds=1))
            if due is not None and due.timestamp() < now - MISSED_GRACE_S:
                self._record(routine["id"], "missed", ts=due.timestamp(),
                             error="сервер был выключен в момент запуска")
                if routine["schedule"]["kind"] == "once":
                    self.save({"id": routine["id"], "enabled": False})
                    continue
            self._schedule(routine)

    def _schedule_suggest(self):
        """Системное задание: частые промахи → черновики сценариев (agent/ops.py)."""
        from . import ops
        cron = ops.options(self.runtime)["suggest_cron"]
        agent = getattr(self.runtime, "agent", None)
        if self.scheduler.get_job("__suggest"):
            self.scheduler.remove_job("__suggest")
        if not cron or agent is None:
            return
        from .. import mcp_hub
        tz = mcp_hub._timezone(self.runtime, {}) or "Europe/Moscow"
        self.scheduler.add_job(agent.ops.suggest, CronTrigger.from_crontab(cron, timezone=ZoneInfo(tz)),
                               args=[self.runtime], id="__suggest", replace_existing=True,
                               misfire_grace_time=3600, coalesce=True)

    async def event(self, text, sn=None, user_id=None, delivery="speak", title="Событие"):
        """Внешнее событие (вебхук, Home Assistant) → фоновый прогон агента с доставкой итога."""
        if delivery not in DELIVERY:
            raise ValueError("delivery: " + ", ".join(DELIVERY))
        device = next((d for d in self.runtime.identities.data["devices"] if d["sn"] == sn), None) if sn else None
        owner = user_id or (device or {}).get("user_id")
        if not owner:
            raise ValueError("Нужен user_id или sn колонки с владельцем")
        routine = {"id": "event", "owner": owner, "sn": sn, "delivery": delivery, "title": title, "quiet": None}
        scoped, sn = self._scoped(routine)
        answer = await self.runtime.agent.chat(scoped, text, thread="event:" + (sn or owner), sn=sn, user_id=owner,
                                               background=title, kind="event")
        if answer["tool_calls"]:
            self.runtime.pending_calls = (self.runtime.pending_calls + answer["tool_calls"])[-30:]
            return {"status": "needs_confirmation", "text": answer["text"],
                    "delivered": self._to_inbox(routine, sn, f"«{title}» ждёт подтверждения в панели.")}
        delivered = await self.deliver(routine, sn, answer["text"]) if answer["text"] else None
        return {"status": "finished", "text": answer["text"], "delivered": delivered}

    def shutdown(self):
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None

    def _schedule(self, routine):
        if self.scheduler is None:
            return
        if self.scheduler.get_job(routine["id"]):
            self.scheduler.remove_job(routine["id"])
        if routine["enabled"]:
            self.scheduler.add_job(self.fire, trigger(routine), args=[routine["id"]], id=routine["id"],
                                   coalesce=True, misfire_grace_time=MISSED_GRACE_S, max_instances=1)

    # --- запуск ----------------------------------------------------------------------

    def _scoped(self, routine):
        """Runtime с настройками профиля колонки задания (как у WS-сессии)."""
        device = next((d for d in self.runtime.identities.data["devices"] if d["sn"] == routine.get("sn")), None) \
            or next((d for d in self.runtime.identities.data["devices"] if d.get("user_id") == routine["owner"]), None)
        scoped = copy.copy(self.runtime)
        scoped.settings = self.runtime.identities.settings_for(self.runtime.settings, device) if device \
            else copy.deepcopy(self.runtime.settings)
        scoped.settings["device_context"] = {**scoped.settings.get("device_context", {}),
                                             "sn": (device or {}).get("sn"), "user_id": routine["owner"]}
        return scoped, (device or {}).get("sn") or ""

    async def fire(self, routine_id, manual=False):
        try:
            routine = self.get(routine_id)
        except ValueError:
            return None
        if not routine["enabled"] and not manual:
            return None
        from . import engine
        if engine(self.runtime) != "graph":  # AI агент выключен во вкладке — задания стоят
            self._record(routine_id, "skipped_disabled", error="AI агент выключен")
            return {"status": "skipped_disabled"}
        day_start = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
        today = [r for r in self.runs(routine_id, limit=routine["max_runs_per_day"] + 1)
                 if r["ts"] >= day_start and r["status"] not in ("missed", "skipped_budget", "skipped_disabled")]
        if len(today) >= routine["max_runs_per_day"]:
            self._record(routine_id, "skipped_budget", error=f"лимит {routine['max_runs_per_day']} запусков в сутки")
            return {"status": "skipped_budget"}
        with self._db() as db:
            db.execute("UPDATE routines SET last_run = ? WHERE id = ?", (time.time(), routine_id))
        scoped, sn = self._scoped(routine)
        try:
            if routine["kind"] == "scenario":
                from . import scenarios
                context = {"sn": sn, "user_id": routine["owner"], "scenario_messages": []}
                result = await scenarios.run(scoped, {"id": routine.get("scenario_id"), "text": routine.get("text") or ""},
                                             context)
                text, status = result.get("speech") or "", result.get("status") or "finished"
            else:
                answer = await self.runtime.agent.chat(scoped, routine["prompt"], thread="routine:" + routine_id,
                                                       sn=sn, user_id=routine["owner"], background=routine["title"])
                text, status = answer["text"], "finished"
                if answer["tool_calls"]:  # подтверждать некому — решение в панели
                    status = "needs_confirmation"
                    self.runtime.pending_calls = (self.runtime.pending_calls + answer["tool_calls"])[-30:]
        except Exception as exc:
            self._record(routine_id, "error", error=type(exc).__name__ + ": " + str(exc)[:300])
            return {"status": "error"}
        delivered = await self.deliver(routine, sn, text) if text and status != "needs_confirmation" else None
        if status == "needs_confirmation":
            delivered = self._to_inbox(routine, sn, f"Задание «{routine['title']}» ждёт подтверждения в панели.")
        self._record(routine_id, status, text=text, delivered=delivered)
        if routine["schedule"]["kind"] == "once" and not manual:
            self.save({"id": routine_id, "enabled": False})
        return {"status": status, "text": text, "delivered": delivered}

    async def deliver(self, routine, sn, text):
        if routine["delivery"] == "silent":
            return "silent"
        session = next((s for s in self.runtime.sessions.values() if s.get("device_sn") == sn and s.get("send")), None)
        if routine["delivery"] == "speak" and session and not quiet_now(routine):
            # как напоминание облака: Push server_action → колонка сама запрашивает реплику и
            # получает её обычным ответом с голосом и карточкой (async_reply.py, проверено на колонке)
            from .. import async_reply
            from ..schema import load_type
            from ..runtime import ROOT
            from ..voice import Conversation
            server_type = load_type(ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
            answer = {"text": text}
            # звук — до Push: колонка запросит реплику и получит её сразу
            await Conversation(self.runtime, server_type, session["send"], {"sn": sn})._prerender(answer)
            answer_id = async_reply.keep(answer, routine["title"])
            try:
                await session["send"](b"AAPI" + async_reply.push(server_type, answer_id).SerializeToString())
            except Exception:  # noqa: BLE001 — соединение умерло: сообщение во входящие
                async_reply.take(answer_id)
            else:
                await self._note_voice(sn, text, routine["title"])
                return "spoken"
        return self._to_inbox(routine, sn, text)

    async def _note_voice(self, sn, text, title):
        """Сказанное — в голосовой тред колонки: ответ на вопрос задания попадёт туда (voice:<SN>)."""
        agent = getattr(self.runtime, "agent", None)
        if agent is None:
            return
        try:
            await agent.note(self.runtime, "voice:" + sn, text, f"Фоновое задание «{title}» сказало на колонке")
        except Exception as exc:  # noqa: BLE001 — тред не обязателен: реплика уже доставлена
            self.runtime.recent = (self.runtime.recent + [{"kind": "routine", "error": type(exc).__name__ + ": " + str(exc)[:160]}])[-30:]

    def _to_inbox(self, routine, sn, text):
        with self._db() as db:
            db.execute("INSERT INTO inbox (user_id, sn, routine_id, title, text, ts) VALUES (?, ?, ?, ?, ?, ?)",
                       (routine["owner"], sn, routine["id"], routine["title"], text, time.time()))
        return "inbox"


def tool_specs(routines, user_id, sn, tz="Europe/Moscow"):
    """Инструменты агента для своих заданий пользователя: [(name, description, schema, executor)]."""
    schedule = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(SCHEDULES)},
        "expr": {"type": "string", "description": "cron «мин час день месяц день_недели» | секунды | ISO дата-время"}},
        "required": ["kind", "expr"]}
    fields = {"title": {"type": "string"}, "prompt": {"type": "string", "description": "что сделать агенту при запуске"},
              "kind": {"type": "string", "enum": list(KINDS)}, "scenario_id": {"type": "string"},
              "text": {"type": "string", "description": "фраза для сценария"}, "schedule": schedule,
              "delivery": {"type": "string", "enum": list(DELIVERY)}, "enabled": {"type": "boolean"},
              "quiet": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}}}}

    def own(routine_id):
        routine = routines.get(routine_id)
        if routine["owner"] != user_id:
            raise ValueError("Задание другого пользователя")
        return routine

    def brief(r):
        return {k: r.get(k) for k in ("id", "title", "kind", "prompt", "scenario_id", "schedule", "delivery", "enabled",
                                       "next_run", "tz")}

    async def routine_list():
        return json.dumps([brief(r) for r in routines.list(user_id)], ensure_ascii=False)

    async def routine_create(**values):
        values = {k: v for k, v in values.items() if k in fields}
        routine = routines.save({"tz": tz, **values, "owner": user_id, "sn": sn, "created_by": "agent"})
        return json.dumps(brief(routine), ensure_ascii=False)

    async def routine_update(id, **values):
        own(id)
        routine = routines.save({**{k: v for k, v in values.items() if k in fields}, "id": id})
        return json.dumps(brief(routine), ensure_ascii=False)

    async def routine_delete(id):
        own(id)
        routines.delete(id)
        return "Задание удалено."

    return [
        ("routine_list", "Задания пользователя по расписанию (напоминания, утренние сводки).",
         {"type": "object", "properties": {}}, routine_list),
        ("routine_create", "Создать задание по расписанию. kind=scenario — запустить сценарий (scenario_id или text), "
         "без модели; kind=agent — выполнить prompt агентом. delivery: speak — сказать на колонке, inbox — сообщить "
         "при следующем обращении, silent — только выполнить. Таймзона — пользователя.",
         {"type": "object", "properties": fields, "required": ["title", "schedule"]}, routine_create),
        ("routine_update", "Изменить своё задание по id (только переданные поля).",
         {"type": "object", "properties": {"id": {"type": "string"}, **fields}, "required": ["id"]}, routine_update),
        ("routine_delete", "Удалить своё задание по id.",
         {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, routine_delete),
    ]


def inbox_prompt(items):
    lines = "\n".join(f"- {datetime.datetime.fromtimestamp(i['ts']).strftime('%d.%m %H:%M')} · {i['title']}: {i['text']}"
                      for i in reversed(items))
    return ("Непрочитанные сообщения фоновых заданий (пользователь их ещё не слышал). В начале ответа коротко "
            "передай их, затем ответь на реплику:\n" + lines)
