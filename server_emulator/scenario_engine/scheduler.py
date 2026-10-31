"""M08 (v9 §5), семейство «таймеры»: серверный планировщик.

Решение о владельце расписания: ТАЙМЕРЫ ведёт сервер (эмулятор владеет
диалогом и сессией; device-native таймеры не подтверждены протоколом).
Будильники (абсолютное время) потребуют timezone/DST — отдельная ветка.

Свойства (по v9 §5): персистентные IDs; dedupe одинакового активного таймера
(тот же SN+label+длительность в пределах окна); cancel; срабатывание по due;
после restart просроченные активные таймеры помечаются 'missed' — НЕ
догоняют огонь в мёртвый диалог, а видны в инвентаре.
Таймеры — относительные длительности, DST-безопасны.

Будильники: абсолютное время HH:MM + дни недели, таймзона устройства (zoneinfo,
DST учитывается), snooze после срабатывания, dedupe, отмена; после restart
пропущенное срабатывание помечается 'missed' отдельной строкой, будильник
продолжает со следующего запланированного времени (без догоняющего огня).
"""
from __future__ import annotations

import datetime
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo

DEDUPE_WINDOW_S = 3.0

def next_occurrence(hhmm, tz_name, after_ts, weekdays=None):
    """Следующий момент HH:MM в таймзоне (zoneinfo, DST корректен), с учётом дней недели."""
    tz = ZoneInfo(tz_name or "Europe/Moscow")
    hour, minute = map(int, hhmm.split(":"))
    local = datetime.datetime.fromtimestamp(after_ts, tz)
    for day in range(9):
        candidate = (local + datetime.timedelta(days=day)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            continue
        if weekdays and candidate.isoweekday() not in weekdays:
            continue
        return candidate.timestamp()
    return None


class TimerScheduler:
    """Планировщик поверх StateStore (таблица timers). Огонь — колбэк fire(timer)."""

    def __init__(self, store, fire=None, clock=time.time):
        self.store = store
        self.fire = fire  # callable(timer: dict) — доставка в сессию устройства
        self.clock = clock

    # --- CRUD -------------------------------------------------------------------
    def schedule(self, sn, seconds, label=None, user_id=None, dialog_id=None, now=None):
        if not isinstance(seconds, (int, float)) or seconds <= 0 or seconds > 24 * 3600:
            raise ValueError("длительность таймера: 1..86400 секунд")
        now = now if now is not None else self.clock()
        label = label or f"Таймер {int(seconds // 60)} мин"
        # dedupe: тот же SN+label+duration, активный, назначенный только что
        for timer in self.list_timers(status="active", sn=sn):
            if (timer["label"] == label and abs(timer["due_ts"] - (now + seconds)) <= DEDUPE_WINDOW_S):
                return timer, False
        timer = {"id": uuid.uuid4().hex, "kind": "timer", "sn": sn, "user_id": user_id,
                 "dialog_id": dialog_id, "label": label, "duration_s": int(seconds),
                 "created_ts": now, "due_ts": now + seconds, "status": "active", "fired_ts": None}
        self._save(timer)
        return timer, True

    def cancel(self, timer_id=None, sn=None, label=None, kind=None):
        """Отмена по ID или (sn, label, kind) активного таймера/будильника."""
        removed = 0
        for timer in self.list_timers(status="active"):
            if timer_id and timer["id"] != timer_id:
                continue
            if sn and timer["sn"] != sn:
                continue
            if label and timer["label"] != label:
                continue
            if kind and timer.get("kind") != kind:
                continue
            timer["status"] = "cancelled"
            self._save(timer)
            removed += 1
        return removed

    def list_timers(self, status=None, sn=None):
        result = [self._row(r) for r in self.store._live().execute("SELECT * FROM timers ORDER BY due_ts")]
        return [t for t in result
                if (status is None or t["status"] == status) and (sn is None or t["sn"] == sn)]

    # --- жизненный цикл -----------------------------------------------------------
    def recover_missed(self, now=None):
        """После рестарта: просроченные активные — 'missed', без догоняющего огня."""
        now = now if now is not None else self.clock()
        missed = []
        for timer in list(self.list_timers(status="active")):
            if timer["due_ts"] > now:
                continue
            if timer.get("kind") == "alarm":
                skipped = dict(timer)
                skipped.update(status="missed", fired_ts=now,
                               id=uuid.uuid4().hex)  # запись о пропуске отдельной строкой
                timer.update(status="cancelled")
                self._save(timer)
                self._save(skipped)
                missed.append(skipped)
                self._rollover_alarm(timer, now)  # будильник продолжает со следующего раза
            else:
                timer.update(status="missed", fired_ts=now)
                self._save(timer)
                missed.append(timer)
        return missed

    def poll(self, now=None):
        """Одна итерация: наставшие активные → fired + колбэк fire. Возвращает сработавшие."""
        now = now if now is not None else self.clock()
        fired = []
        for timer in self.list_timers(status="active"):
            if timer["due_ts"] <= now:
                timer.update(status="fired", fired_ts=now)
                self._save(timer)
                fired.append(timer)
                if timer.get("kind") != "alarm" and self.fire:
                    try:
                        self.fire(timer)
                    except Exception:  # доставка не должна ронять планировщик
                        pass
                if timer.get("kind") == "alarm":
                    if self.fire:
                        try:
                            self.fire(timer)
                        except Exception:  # доставка не должна ронять планировщик
                            pass
                    # журнал срабатываний отдельной строкой; сам будильник перекатывается
                    record = dict(timer)
                    record.update(status="fired", fired_ts=now, id=uuid.uuid4().hex)
                    self._save(record)
                    fired[-1] = record
                    self._rollover_alarm(timer, now)
        return fired

    # --- будильники ----------------------------------------------------------------
    def schedule_alarm(self, sn, hhmm, tz=None, weekdays=None, label=None,
                       user_id=None, dialog_id=None, now=None):
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(hhmm))
        hhmm = f"{int(match.group(1)):02d}:{match.group(2)}" if match else str(hhmm)
        if not re.fullmatch(r"\d{2}:\d{2}", hhmm):
            raise ValueError("время будильника: HH:MM")
        now = now if now is not None else self.clock()
        tz = tz or "Europe/Moscow"
        weekdays = sorted(set(weekdays)) if weekdays else []
        label = label or f"Будильник {hhmm}"
        for alarm in self.list_timers(status="active", sn=sn):
            if (alarm.get("kind") == "alarm" and alarm.get("time_hhmm") == hhmm
                    and (alarm.get("weekdays") or []) == weekdays):
                return alarm, False  # dedupe: такой будильник уже стоит
        due = next_occurrence(hhmm, tz, now, weekdays or None)
        if due is None:
            raise ValueError("не удалось вычислить время срабатывания")
        alarm = {"id": uuid.uuid4().hex, "kind": "alarm", "sn": sn, "user_id": user_id,
                 "dialog_id": dialog_id, "label": label, "duration_s": None,
                 "created_ts": now, "due_ts": due, "status": "active", "fired_ts": None,
                 "time_hhmm": hhmm, "weekdays": json.dumps(weekdays), "tz": tz, "next_ts": None}
        self._save(alarm)
        return alarm, True

    def snooze(self, sn, minutes=9, now=None):
        """Snooze последнего сработавшего будильника SN (окно 10 минут после огня).
        Fired-запись помечается 'snoozed'; повтор — one-shot активная запись
        (semantics таймера), основной будильник продолжает своё расписание."""
        now = now if now is not None else self.clock()
        recent = [t for t in self.list_timers()
                  if t.get("kind") == "alarm" and t["sn"] == sn and t["status"] == "fired"
                  and t.get("fired_ts") and now - t["fired_ts"] <= 600]
        if not recent:
            return None
        alarm = max(recent, key=lambda t: t["fired_ts"])
        alarm.update(status="snoozed")
        self._save(alarm)
        repeat = {"id": uuid.uuid4().hex, "kind": "alarm", "sn": sn,
                  "user_id": alarm.get("user_id"), "dialog_id": alarm.get("dialog_id"),
                  "label": alarm["label"] + " · повтор", "duration_s": int(minutes * 60),
                  "created_ts": now, "due_ts": now + minutes * 60, "status": "active",
                  "fired_ts": None, "time_hhmm": alarm.get("time_hhmm"),
                  "weekdays": [], "tz": alarm.get("tz"), "next_ts": None}
        self._save(repeat)
        return repeat

    def _rollover_alarm(self, alarm, now):
        """После срабатывания/пропуска — следующее запланированное время (или done)."""
        weekdays = json.loads(alarm.get("weekdays") or "[]")
        due = next_occurrence(alarm["time_hhmm"], alarm.get("tz"), now, weekdays or None)
        if due is None:
            alarm["status"] = "done"
        else:
            alarm["due_ts"] = due
            alarm["status"] = "active"
        self._save(alarm)

    # --- хранение -----------------------------------------------------------------
    def _save(self, timer):
        db = self.store._live()
        if isinstance(timer.get("weekdays"), list):
            timer = {**timer, "weekdays": json.dumps(timer["weekdays"])}  # sqlite не хранит списки
        with db:
            db.execute("INSERT INTO timers VALUES(:" + ",:".join(TIMER_COLUMNS) + ") "
                       "ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in TIMER_COLUMNS[1:]),
                       {c: timer.get(c) for c in TIMER_COLUMNS})

    @staticmethod
    def _row(row):
        result = {key: row[key] for key in TIMER_COLUMNS}
        if isinstance(result.get("weekdays"), str):
            try:
                result["weekdays"] = json.loads(result["weekdays"])
            except ValueError:
                result["weekdays"] = []
        return result


TIMER_COLUMNS = ("id", "kind", "sn", "user_id", "dialog_id", "label", "duration_s",
                 "created_ts", "due_ts", "status", "fired_ts",
                 "time_hhmm", "weekdays", "tz", "next_ts")
