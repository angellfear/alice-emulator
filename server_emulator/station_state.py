"""Состояние колонки из её запроса: таймеры, будильники, экраны «Часов», приложения.
Тот же разбор — для ответа на SyncPush{GetDeviceState} (fetch_state_result).

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
    (образцы: журнал 2026-10-24…29, cap 23 есть в каждом запросе);
  · CapabilityType=39 (AppLaunchCapabilityType) — NAlice.TAppLaunchCapability,
    State.InstalledAppsList[{AppName, PackageName, VersionCode}] — установленные
    приложения колонки, единственный штатный источник списка (образец: журнал
    2026-11-12, колонки chiron присылают его в каждом VoiceInput/TextInput;
    у TV-устройств Station_2 возможностей в состоянии нет).
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

import time

# capability-типы из ECapabilityType (alice-api.pb), образец — журнал захвата
ALARM_CAPABILITY = 21
CLOCK_APP_CAPABILITY = 144
PLAYER_CAPABILITY = 46
VOLUME_CAPABILITY = 23
APP_LAUNCH_CAPABILITY = 39

_ALARM_CAP = "NAlice.TAlarmCapability"
_CLOCK_CAP = "NAlice.TClockAppCapability"
_PLAYER_CAP = "NAlice.TAudioPlayerCapability"
_VOLUME_CAP = "NAlice.TVolumeCapability"
_APPS_CAP = "NAlice.TAppLaunchCapability"
_ENV_STATE = "NAlice.NAliceApi.TEnvironmentState"

# Пакеты, которые не показываются как приложения: системная оболочка Android и
# служебные сервисы колонки. Правило одно на весь эмулятор (ответ «какие приложения
# установлены», запуск приложения, промпт модели): колонка присылает сырой список
# пакетов устройства — на .85 из 87 записей осмысленных приложений около 30.
_NOT_APPS = ("android", "com.android.", "com.droidlogic", "com.yandex.centaur.overlay.",
             "com.yandex.io.", "com.yandex.prebuiltstub", "com.yandex.server.",
             "com.yandex.vqe_service.", "com.yandex.devices.farm.", "com.yandex.tv.qc",
             "com.stardust", "com.topjohnwu.")

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


def _apps_to_list(state):
    """TAppLaunchCapability.State.InstalledAppsList → [{name, package, version_code}]."""
    return [{"name": str(app.AppName or ""), "package": str(app.PackageName or ""),
             "version_code": int(app.VersionCode or 0)} for app in state.InstalledAppsList]


def is_user_app(app):
    """Приложение для человека, а не система/служебный пакет: пакет не из системных
    (_NOT_APPS) и имя не повторяет имя пакета (у служебных AppName == PackageName)."""
    package = str(app.get("package") or "").strip()
    name = str(app.get("name") or "").strip()
    if not package or not name:
        return False
    return not package.lower().startswith(_NOT_APPS) and name.lower() != package.lower()


def user_apps(apps):
    """Приложения для ответа и запуска: [{name, package}] без системных пакетов.
    Названия — AppName колонки (человеческие имена она присылает сама)."""
    return [{"name": app["name"], "package": app["package"]}
            for app in apps or [] if is_user_app(app)]


def _env(raw):
    """EnvironmentStateRaw (байты) → TEnvironmentState; пусто/битые байты — None."""
    if not raw:
        return None
    env_type = _load_env()
    if env_type is None:
        return None
    env = env_type()
    try:
        env.ParseFromString(bytes(raw))
    except Exception:  # noqa: BLE001 — чужая сериализация: битый raw = состояние отсутствует
        return None
    return env


def _apps_from_env(env, apps_cap):
    """Установленные приложения из состояния: cap 39 → State.InstalledAppsList."""
    if env is None or apps_cap is None:
        return []
    apps = []
    for endpoint in env.Endpoints:
        for holder in endpoint.AnyCapabilities:
            if int(holder.CapabilityType) != APP_LAUNCH_CAPABILITY:
                continue
            raw_cap = bytes(holder.RawCapability)
            if not raw_cap:
                continue
            cap = apps_cap()
            try:
                cap.ParseFromString(raw_cap)
            except Exception:  # noqa: BLE001
                continue
            apps += _apps_to_list(cap.State)
    return apps


def _raw_state(request):
    """Байты EnvironmentStateRaw из TRequest (VoiceInput/TextInput)."""
    return getattr(getattr(request, "Request", None), "EnvironmentStateRaw", b"") or b""


def parse(raw):
    """EnvironmentStateRaw (байты) → {"timers": [...], "alarms": [...], "screens": {...},
    "player": {...}, "volume": {...}, "apps": [...]}.

    Пустой/битый raw, отсутствие дескрипторов протокола — пустое состояние, не ошибка:
    запрос без состояния честен («таймеров нет», «список приложений не получен»),
    сбой разбора не должен ломать ответ.
    """
    state = {"timers": [], "alarms": [], "screens": {}, "player": {}, "volume": {}, "apps": []}
    env = _env(raw)
    if env is None:
        return state
    alarm_cap = _load(_ALARM_CAP)
    clock_cap = _load(_CLOCK_CAP)
    player_cap = _load(_PLAYER_CAP)
    volume_cap = _load(_VOLUME_CAP)
    state["apps"] = _apps_from_env(env, _load(_APPS_CAP))
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
    return parse(_raw_state(request))


def installed_apps(request):
    """Установленные приложения колонки из её запроса (cap 39):
    [{name, package, version_code}] — сырой список пакетов, как прислала колонка.
    Пусто, если состояние в запросе без возможностей (TV-устройства список не шлют)."""
    return _apps_from_env(_env(_raw_state(request)), _load(_APPS_CAP))


# --- объявление возможностей колонки ---------------------------------------------


def _capability_type_names():
    """ECapabilityType (вложен в NAlice.TCapability, alice-api.pb): номер → имя значения."""
    env_type = _load_env()
    if env_type is None:
        return {}
    try:
        enum = env_type.DESCRIPTOR.file.pool.FindEnumTypeByName("NAlice.TCapability.ECapabilityType")
    except KeyError:
        return {}
    return {value.number: value.name for value in enum.values}


def _enum_capability(value_name):
    """Значение ECapabilityType (…CapabilityType) → (короткое имя, класс или None).
    Имена вне шаблона …CapabilityType (ThermostatType, AccessibilityVoiceControlType)
    пробуют вариант без хвоста Type — тело возможности находится по схеме."""
    base = value_name[:-len("CapabilityType")] if value_name.endswith("CapabilityType") else value_name
    candidates = [base]
    if base.endswith("Type"):
        candidates.append(base[:-len("Type")])
    for short in candidates:
        cls = _load("NAlice.T" + short + "Capability")
        if cls is not None:
            return short, cls
    return base, None


def _capability_params(cls, body):
    """Тело возможности (Meta/Parameters/State) как dict; пустое или неразбираемое — {}."""
    if cls is None or not body:
        return {}
    cap = cls()
    try:
        cap.ParseFromString(bytes(body))
    except Exception:  # noqa: BLE001 — чужая сериализация: тело не отдаём
        return {}
    from .schema import message_dict
    try:
        return message_dict(cap)
    except Exception:  # noqa: BLE001 — нестрогое тело не должно ломать разбор
        return {}


def endpoint_sn(raw):
    """Серийник из самого объявления (Endpoints[0].Id); состояния в запросе нет — пусто."""
    env = _env(raw)
    if env is None or not env.Endpoints:
        return ""
    return env.Endpoints[0].Id or ""


def capability_names(raw):
    """Имена объявленных возможностей без разбора тел — быстрый путь наблюдателя
    (вызывается на каждый запрос колонки). Состояния нет — []."""
    env = _env(raw)
    if env is None:
        return []
    numbers = _capability_type_names()
    out = []
    for endpoint in env.Endpoints:
        for holder in endpoint.AnyCapabilities:
            number = int(holder.CapabilityType)
            value_name = numbers.get(number) or "#" + str(number)
            short, _cls = _enum_capability(value_name)
            out.append(short)
        for any_cap in endpoint.Capabilities:
            full = (any_cap.type_url or "").rsplit("/", 1)[-1]
            if full.startswith("NAlice.T") and full.endswith("Capability"):
                out.append(full[len("NAlice.T"):-len("Capability")])
            else:
                out.append(full)
    return out


def capabilities(raw):
    """EnvironmentStateRaw → {короткое имя: {Meta/Parameters/State}} — объявление колонки.
    AnyCapabilities (номер + тело) и Capabilities (google.protobuf.Any, type_url) дают
    одинаковые короткие имена (Alarm, Animation, SmartHomeClientView); типа нет в схемах —
    имя по номеру (#312) без параметров. Состояния нет — {} (запрос без объявления честен)."""
    env = _env(raw)
    if env is None:
        return {}
    numbers = _capability_type_names()
    caps = {}
    for endpoint in env.Endpoints:
        for holder in endpoint.AnyCapabilities:
            number = int(holder.CapabilityType)
            value_name = numbers.get(number) or "#" + str(number)
            short, cls = _enum_capability(value_name)
            caps[short] = _capability_params(cls, holder.RawCapability)
        for any_cap in endpoint.Capabilities:
            full = (any_cap.type_url or "").rsplit("/", 1)[-1]
            if full.startswith("NAlice.T") and full.endswith("Capability"):
                short = full[len("NAlice.T"):-len("Capability")]
            else:
                short = full
            caps[short] = _capability_params(_load(full), any_cap.value)
    return caps


def fetch_state_result(response, assistant_name=""):
    """Ответ колонки на SyncPush{GetDeviceState} (TClientMessage.Event.SyncPushResponse)
    → {ts, alarms, timers, volume, player, assistant_name}: будильники/таймеры/плеер —
    из EnvironmentState (parse), у таймеров — remaining на момент опроса; громкость —
    DeviceState (SoundLevel/SoundMaxLevel/SoundMuted), в DeviceState пусто —
    TVolumeCapability из parse. Битый ответ — пустое состояние, не исключение."""
    inner = response.Event.SyncPushResponse.SyncResponse.GetDeviceStateResponse
    state = parse(bytes(inner.EnvironmentState.SerializeToString())) \
        if inner.HasField("EnvironmentState") else {}
    now = time.time()
    timers = []
    for timer in state.get("timers") or []:
        item = dict(timer)
        if item.get("start_ts"):
            item["remaining"] = remaining_seconds(item, int(now))
        timers.append(item)
    device_state = inner.DeviceState
    if device_state.SoundLevel or device_state.SoundMaxLevel or device_state.SoundMuted:
        volume = {"level": int(device_state.SoundLevel or 0),
                  "max_level": int(device_state.SoundMaxLevel or 0),
                  "muted": bool(device_state.SoundMuted)}
        if volume["max_level"]:
            volume["percent"] = round(volume["level"] / volume["max_level"] * 100, 1)
    else:
        volume = state.get("volume") or {}
    return {"ts": now, "alarms": state.get("alarms") or [], "timers": timers,
            "volume": volume, "player": state.get("player") or {},
            "assistant_name": assistant_name}


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
