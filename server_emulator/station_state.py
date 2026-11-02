"""Состояние колонки из её запроса (TASK-079): таймеры, будильники, экраны «Часов».

Колонка сообщает своё состояние в КАЖДОМ запросе — `Request.EnvironmentStateRaw`
(NAlice.NAliceApi.TEnvironmentState, образец: журнал 2026-10-22…2026-10-27):
  · Endpoints[].AnyCapabilities[] CapabilityType=21 (AlarmCapabilityType) —
    RawCapability = NAlice.TAlarmCapability, State.Timers / State.Alarms;
  · CapabilityType=144 (ClockAppCapabilityType) — NAlice.TClockAppCapability,
    State.*ScreenState — какие экраны приложения «Часы» открыты у колонки;
  · CapabilityType=46 (AudioPlayerCapabilityType) — NAlice.TAudioPlayerCapability,
    State.StateType (Idle/Playing/Paused…) и CommonPlayerState — играет ли колонка
    сейчас (cap 46 есть в каждом запросе, образец — журнал 2026-10-21…2026-10-26);
  · CapabilityType=23 (VolumeCapabilityType) — NAlice.TVolumeCapability,
    State.Level/MaxLevel/PercentLevel/IsMuted — текущая громкость колонки
    (образцы: журнал 2026-10-24…29, cap 23 есть в каждом запросе).
CapabilityUpdateState колонка этими типами ничего не слала — состояние живёт
только в запросах. Эмулятор свои таймеры и свою очередь плеера НЕ хранит
(главное правило tasks/PLAN-station-skills.md): что колонка не сообщает — того нет.

Остаток таймера — арифметика из состояния колонки (сверено с ответом настоящего
сервера «Осталось 47 минут 48 секунд», 2026-10-25 16:13):
    remaining = Duration − (now − StartTimestamp)
Активный таймер — Disabled=false и remaining>0; сигнал (ringing) — CurrentlyPlaying
у будильника/таймера (образец 2026-10-27 19:05) либо истёкший активный таймер.
"""
from __future__ import annotations

# capability-типы из ECapabilityType (alice-api.pb), образец — журнал захвата
ALARM_CAPABILITY = 21
CLOCK_APP_CAPABILITY = 144
PLAYER_CAPABILITY = 46
VOLUME_CAPABILITY = 23

_ALARM_CAP = "NAlice.TAlarmCapability"
_CLOCK_CAP = "NAlice.TClockAppCapability"
_PLAYER_CAP = "NAlice.TAudioPlayerCapability"
_VOLUME_CAP = "NAlice.TVolumeCapability"
_ENV_STATE = "NAlice.NAliceApi.TEnvironmentState"

# EStateType вложенного TState (alica-*.pb): номер → имя
_PLAYER_STATES = {0: "unknown", 1: "idle", 2: "playing", 3: "paused",
                  4: "buffering", 5: "stopped", 6: "finished"}


def _load(full_name):
    from .schema import SetupRequired, load_type, schemas_dir, station_file
    try:
        return load_type(schemas_dir(), station_file() or "alica-missing.pb", full_name)
    except (SetupRequired, KeyError):
        return None


def _time_to_dict(local_time):
    return {"hours": local_time.Hours or 0, "minutes": local_time.Minutes or 0,
            "seconds": local_time.Seconds or 0}


def _alarm_to_dict(alarm):
    dates = []
    weekdays = []
    for day in alarm.ActivationDays:
        if day.HasField("Date"):
            dates.append((day.Date.Year, day.Date.Month, day.Date.Day))
        elif day.Weekday:
            weekdays.append(int(day.Weekday))
    return {"alarm_id": alarm.AlarmId,
            "time": _time_to_dict(alarm.LocalTime),
            "dates": dates, "weekdays": weekdays,
            "disabled": bool(alarm.Disabled),
            "currently_playing": bool(alarm.CurrentlyPlaying),
            "snooze_time": int(alarm.SnoozeTime or 0),
            "timezone": alarm.Timezone.TimezoneName if alarm.HasField("Timezone") else ""}


def _timer_to_dict(timer):
    return {"timer_id": timer.TimerId,
            "start_ts": int(timer.StartTimestamp or 0),
            "duration": int(timer.Duration or 0),
            "paused": bool(timer.Paused),
            "disabled": bool(timer.Disabled),
            "currently_playing": bool(timer.CurrentlyPlaying),
            "title": timer.Title if timer.Title else ""}


def _screens_to_dict(state):
    """Какие экраны «Часов» открыты: {timer_active, alarm_active, timer_list, alarm_list}."""
    screens = {}
    if state.HasField("TimerActiveScreenState"):
        screens["timer_active"] = state.TimerActiveScreenState.TimerId
    if state.HasField("AlarmActiveScreenState"):
        screens["alarm_active"] = state.AlarmActiveScreenState.AlarmId
    if state.HasField("TimerListScreenState"):
        screens["timer_list"] = True
    if state.HasField("TimerInfoScreenState"):
        screens["timer_info"] = state.TimerInfoScreenState.TimerId
    if state.HasField("AlarmListScreenState"):
        screens["alarm_list"] = True
    if state.HasField("AlarmEditScreenState"):
        screens["alarm_edit"] = True
    return screens


def _player_to_dict(state):
    """Состояние плеера из TAudioPlayerCapability.State: играет ли, что играет, прогресс."""
    common = state.CommonPlayerState if state.HasField("CommonPlayerState") else None
    return {"state": _PLAYER_STATES.get(int(state.StateType), "unknown"),
            "playing": bool(common.IsPlaying) if common is not None else False,
            "title": state.Title or "",
            "subtitle": state.SubTitle or "",
            "played_ms": int(common.PlayedMs or 0) if common is not None else 0,
            "duration_ms": int(common.DurationMs or 0) if common is not None else 0}


def _volume_to_dict(state):
    """Громкость из TVolumeCapability.State: шкала колонки (Level/MaxLevel), процент, мьют.
    PercentLevel на wire — float32 (8.7 приходит как 8.699999809…), округляем до 0.1."""
    return {"muted": bool(state.IsMuted),
            "level": int(state.Level or 0),
            "max_level": int(state.MaxLevel or 0),
            "percent_level": round(float(state.PercentLevel or 0.0), 1)}


def parse(raw):
    """EnvironmentStateRaw (байты) → {"timers": [...], "alarms": [...], "screens": {...},
    "player": {...}, "volume": {...}}.

    Пустой/битый raw, отсутствие дескрипторов протокола — пустое состояние, не ошибка:
    запрос без состояния честен («таймеров нет»), сбой разбора не должен ломать ответ.
    """
    state = {"timers": [], "alarms": [], "screens": {}, "player": {}, "volume": {}}
    if not raw:
        return state
    env_type = _load_env()
    if env_type is None:
        return state
    env = env_type()
    try:
        env.ParseFromString(bytes(raw))
    except Exception:  # noqa: BLE001 — чужая сериализация: битый raw = состояние отсутствует
        return state
    alarm_cap = _load(_ALARM_CAP)
    clock_cap = _load(_CLOCK_CAP)
    player_cap = _load(_PLAYER_CAP)
    volume_cap = _load(_VOLUME_CAP)
    for endpoint in env.Endpoints:
        for holder in endpoint.AnyCapabilities:
            kind = int(holder.CapabilityType)
            raw_cap = bytes(holder.RawCapability)
            if not raw_cap:
                continue
            if kind == ALARM_CAPABILITY and alarm_cap is not None:
                cap = alarm_cap()
                try:
                    cap.ParseFromString(raw_cap)
                except Exception:  # noqa: BLE001
                    continue
                state["timers"] += [_timer_to_dict(t) for t in cap.State.Timers]
                state["alarms"] += [_alarm_to_dict(a) for a in cap.State.Alarms]
            elif kind == CLOCK_APP_CAPABILITY and clock_cap is not None:
                cap = clock_cap()
                try:
                    cap.ParseFromString(raw_cap)
                except Exception:  # noqa: BLE001
                    continue
                state["screens"].update(_screens_to_dict(cap.State))
            elif kind == PLAYER_CAPABILITY and player_cap is not None:
                cap = player_cap()
                try:
                    cap.ParseFromString(raw_cap)
                except Exception:  # noqa: BLE001
                    continue
                state["player"] = _player_to_dict(cap.State)
            elif kind == VOLUME_CAPABILITY and volume_cap is not None:
                cap = volume_cap()
                try:
                    cap.ParseFromString(raw_cap)
                except Exception:  # noqa: BLE001
                    continue
                state["volume"] = _volume_to_dict(cap.State)
    return state


def _load_env():
    from .schema import SetupRequired, load_type, schemas_dir
    try:
        return load_type(schemas_dir(), "alice-api.pb", _ENV_STATE)
    except (SetupRequired, KeyError):
        return None


def from_request(request):
    """Состояние из запроса колонки (TTextInput/TVoiceInput): Request.EnvironmentStateRaw."""
    raw = getattr(getattr(request, "Request", None), "EnvironmentStateRaw", b"") or b""
    return parse(raw)


# --- хелперы сценариев: остаток, активные, сигнал -------------------------------


def remaining_seconds(timer, now_ts):
    """Секунды до сигнала таймера: Duration − (now − StartTimestamp), из состояния колонки."""
    return int(timer["duration"] - (int(now_ts) - timer["start_ts"]))


def active_timers(timers, now_ts):
    """Активные таймеры (не отключены и ещё не истекли), ближайшие — первыми."""
    live = [dict(timer, remaining=remaining_seconds(timer, now_ts)) for timer in timers if not timer["disabled"]]
    return sorted((timer for timer in live if timer["remaining"] > 0), key=lambda timer: timer["remaining"])


def ringing(state, now_ts):
    """Звенит ли у колонки сейчас: ("alarm", alarm_id) | ("timer", timer_id) | None.

    Образцы: 2026-10-27 19:05:07 — будильник CurrentlyPlaying=true; истёкший
    активный таймер (remaining<=0, Disabled=false) — сигнал тоже звенит
    (колонка ждёт «хватит»)."""
    for alarm in state.get("alarms") or []:
        if alarm.get("currently_playing") and not alarm.get("disabled"):
            return "alarm", alarm["alarm_id"]
    for timer in state.get("timers") or []:
        if timer.get("disabled"):
            continue
        if timer.get("currently_playing") or remaining_seconds(timer, now_ts) <= 0:
            return "timer", timer["timer_id"]
    return None
