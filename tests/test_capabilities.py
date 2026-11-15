"""Возможности колонки: разбор объявления, правило записи, чтение, наблюдатель.

Объявление собирается в тесте из дескрипторов (обезличенный EnvironmentStateRaw:
фиктивный серийник с префиксом модели). Без протокола тесты, которым нужны
дескрипторы, пропускаются (conftest по SetupRequired); правило записи и чтение
от схем не зависят и идут всегда.
"""
import asyncio
from types import SimpleNamespace

from server_emulator import capabilities
from server_emulator.identity import IdentityRegistry

SN = "C00T1000TESTK"


def _schema(message_type):
    from server_emulator.schema import load_type, schemas_dir, station_file
    return load_type(schemas_dir(), station_file() or "alica-missing.pb", message_type)


def _env_client(video_player=True, animation=True, smart_home=False, local_iot=False,
                extra_holder=None, trigger="TextInput", sn=None):
    """TClientMessage с событием trigger (TextInput/VoiceInput): Endpoints[0] —
    Animation (AnyCapabilities) и VideoPlayer (Capabilities/Any), по флагам;
    smart_home — SmartHomeClientView (Any); local_iot — LocalIoT (Any).
    sn — серийник в Endpoints[0].Id (по умолчанию SN)."""
    from server_emulator.schema import load_type, schemas_dir
    client = load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TClientMessage")()
    client.Event.Header.MessageId = "m1"
    env_type = load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TEnvironmentState")
    # EnvironmentState и EnvironmentStateRaw — oneof: собираем EnvironmentState отдельно
    env = env_type()
    enum = env_type.DESCRIPTOR.file.pool.FindEnumTypeByName("NAlice.TCapability.ECapabilityType")
    num_anim = next(v.number for v in enum.values if v.name == "AnimationCapabilityType")
    endpoint = env.Endpoints.add()
    endpoint.Id = sn or SN
    if animation:
        anim = _schema("NAlice.TAnimationCapability")()
        anim.Meta.SupportedDirectives.append("DrawAnimationDirectiveType")
        anim.Parameters.SupportedFormats.append(0)
        holder = endpoint.AnyCapabilities.add()
        holder.CapabilityType = num_anim
        holder.RawCapability = anim.SerializeToString()
    if video_player:
        video = _schema("NAlice.TVideoPlayerCapability")()
        video.Meta.SupportedDirectives.append("PlayVideoDirectiveType")
        any_cap = endpoint.Capabilities.add()
        any_cap.type_url = "type.googleapis.com/NAlice.TVideoPlayerCapability"
        any_cap.value = video.SerializeToString()
    if smart_home:
        view = _schema("NAlice.TSmartHomeClientViewCapability")()
        view.Meta.SupportedDirectives.append("OpenSmartHomeDirectiveType")
        view.Parameters.HasApplication = True
        any_cap = endpoint.Capabilities.add()
        any_cap.type_url = "type.googleapis.com/NAlice.TSmartHomeClientViewCapability"
        any_cap.value = view.SerializeToString()
    if local_iot:
        iot = _schema("NAlice.TLocalIoTCapability")()
        # SupportedDirectives — enum EDirectiveType: 154 SetIoTUserInfo,
        # 153 SyncIoTUserInfo, 203 SetIoTAccountSettings
        for num in (154, 153, 203):
            iot.Meta.SupportedDirectives.append(num)
        any_cap = endpoint.Capabilities.add()
        any_cap.type_url = "type.googleapis.com/NAlice.TLocalIoTCapability"
        any_cap.value = iot.SerializeToString()
    if extra_holder is not None:
        holder = endpoint.AnyCapabilities.add()
        holder.CapabilityType = extra_holder
        holder.RawCapability = b"\x18\x01"
    client.Event.TextInput.Request.EnvironmentStateRaw = env.SerializeToString()
    if trigger != "TextInput":
        getattr(client.Event, trigger).Request.EnvironmentStateRaw = \
            client.Event.TextInput.Request.EnvironmentStateRaw
    return client


# --- правило записи и чтение (без протокола) -------------------------------------


def test_merge_subset_does_not_overwrite():
    stored = {"ts": 1, "items": {"Animation": {}, "VideoPlayer": {}}}
    announcement, changed = capabilities.merge(stored, {"Animation"})
    assert not changed and announcement is stored  # подмножество не затирает полный набор


def test_merge_identical_no_write():
    stored = {"ts": 1, "items": {"Animation": {}}}
    announcement, changed = capabilities.merge(stored, {"Animation"})
    assert not changed and announcement is stored


def test_merge_new_names_replace():
    stored = {"ts": 1, "items": {"Animation": {}}}
    announcement, changed = capabilities.merge(stored, {"Animation": {}, "SmartHomeClientView": {}})
    assert changed and set(announcement["items"]) == {"Animation", "SmartHomeClientView"}
    assert announcement["ts"] >= 1


def test_merge_empty_both_no_write():
    announcement, changed = capabilities.merge(None, {})
    assert not changed  # запрос без объявления ничего не пишет


def test_read_points():
    device = {"state": {"capabilities": {"ts": 42, "items": {
        "Animation": {"Meta": {"SupportedDirectives": ["DrawAnimationDirectiveType"]}},
        "LocalIoT": {}}}}}
    assert capabilities.names(device) == ["Animation", "LocalIoT"]
    assert capabilities.has(device, "Animation") and not capabilities.has(device, "VideoPlayer")
    assert capabilities.params(device, "Animation") == {
        "Meta": {"SupportedDirectives": ["DrawAnimationDirectiveType"]}}
    assert capabilities.params(device, "VideoPlayer") == {}
    assert capabilities.announced_at(device) == 42
    assert capabilities.names({"state": {}}) == []
    assert capabilities.announced_at({"state": {}}) == 0


# --- разбор объявления и наблюдатель (нужен протокол) -----------------------------


def test_collect_names_and_params():
    from server_emulator import station_state
    raw = bytes(_env_client().Event.TextInput.Request.EnvironmentStateRaw)
    assert station_state.endpoint_sn(raw) == SN
    assert station_state.capability_names(raw) == ["Animation", "VideoPlayer"]
    caps = station_state.capabilities(raw)
    assert caps["Animation"]["Meta"]["SupportedDirectives"] == ["DrawAnimationDirectiveType"]
    assert caps["Animation"]["Parameters"]["SupportedFormats"]
    assert caps["VideoPlayer"]["Meta"]["SupportedDirectives"] == ["PlayVideoDirectiveType"]
    # тип не из схем: имя по номеру, без параметров, разбор не падает
    raw2 = bytes(_env_client(video_player=False, extra_holder=312)
                 .Event.TextInput.Request.EnvironmentStateRaw)
    caps2 = station_state.capabilities(raw2)
    assert caps2.get("#312") == {}


def test_observer_writes_registry(tmp_path):
    from server_emulator import station_state
    registry = IdentityRegistry(tmp_path)
    registry.mutate({"operation": "device", "sn": SN, "name": "Тест",
                     "user_id": None, "device_ids": [SN]})
    conversation = SimpleNamespace(
        runtime=SimpleNamespace(identities=registry, lock=asyncio.Lock()),
        trace_context={"sn": SN})
    client = _env_client()

    async def run():
        assert await capabilities.observe(conversation, client, "TextInput") is False
        device = next(d for d in registry.data["devices"] if d["sn"] == SN)
        first = (device["state"]["capabilities"]["ts"],
                 tuple(sorted(device["state"]["capabilities"]["items"])))
        assert first[1] == ("Animation", "VideoPlayer")
        assert capabilities.names(device) == ["Animation", "VideoPlayer"]
        assert capabilities.announced_at(device) == first[0] > 0

        # тот же набор, другой канал кадра (VoiceInput) — запись не повторяется
        await asyncio.sleep(0.01)
        await capabilities.observe(conversation, _env_client(trigger="VoiceInput"), "VoiceInput")
        device = next(d for d in registry.data["devices"] if d["sn"] == SN)
        assert device["state"]["capabilities"]["ts"] == first[0]

        # сокращённое объявление — подмножество: полный набор остался
        await capabilities.observe(conversation, _env_client(video_player=False), "TextInput")
        device = next(d for d in registry.data["devices"] if d["sn"] == SN)
        assert capabilities.names(device) == ["Animation", "VideoPlayer"]

        # состав изменился: SmartHomeClientView добавилась — набор заменяется
        await capabilities.observe(conversation, _env_client(smart_home=True), "TextInput")
        device = next(d for d in registry.data["devices"] if d["sn"] == SN)
        assert capabilities.names(device) == ["Animation", "SmartHomeClientView", "VideoPlayer"]
        assert capabilities.has(device, "SmartHomeClientView")

        # запрос без состояния — ничего не меняет
        empty = _env_client(video_player=False, animation=False)
        empty.Event.TextInput.Request.EnvironmentStateRaw = b""
        await capabilities.observe(conversation, empty, "TextInput")
        device = next(d for d in registry.data["devices"] if d["sn"] == SN)
        assert capabilities.names(device) == ["Animation", "SmartHomeClientView", "VideoPlayer"]

        # неизвестная колонка — записи нет и падения нет
        stranger = _env_client()
        from server_emulator.schema import load_type, schemas_dir
        env = load_type(schemas_dir(), "alice-api.pb", "NAlice.NAliceApi.TEnvironmentState").FromString(
            bytes(stranger.Event.TextInput.Request.EnvironmentStateRaw))
        env.Endpoints[0].Id = "C00T1000OTHERK"
        stranger.Event.TextInput.Request.EnvironmentStateRaw = env.SerializeToString()
        assert await capabilities.observe(conversation, stranger, "TextInput") is False

    asyncio.run(run())


def test_observer_max_path_any_only_voiceinput(tmp_path):
    """Путь Макса (без экрана): VoiceInput с EnvironmentStateRaw, все возможности —
    только через Capabilities (google.protobuf.Any, type_url); AnyCapabilities станция
    не использует. Наблюдатель пишет набор в реестр."""
    registry = IdentityRegistry(tmp_path)
    registry.mutate({"operation": "device", "sn": SN, "name": "Тест",
                     "user_id": None, "device_ids": [SN]})
    conversation = SimpleNamespace(
        runtime=SimpleNamespace(identities=registry, lock=asyncio.Lock()),
        trace_context={"sn": SN})
    client = _env_client(video_player=False, animation=False, smart_home=True,
                         trigger="VoiceInput")

    async def run():
        assert await capabilities.observe(conversation, client, "VoiceInput") is False
        device = next(d for d in registry.data["devices"] if d["sn"] == SN)
        assert capabilities.names(device) == ["SmartHomeClientView"]
        assert capabilities.params(device, "SmartHomeClientView") == {
            "Meta": {"SupportedDirectives": ["OpenSmartHomeDirectiveType"]},
            "Parameters": {"HasApplication": True}}

    asyncio.run(run())


def test_observer_broken_frame_does_not_raise(tmp_path):
    registry = IdentityRegistry(tmp_path)
    registry.mutate({"operation": "device", "sn": SN, "name": "Тест",
                     "user_id": None, "device_ids": [SN]})
    conversation = SimpleNamespace(
        runtime=SimpleNamespace(identities=registry, lock=asyncio.Lock()),
        trace_context={"sn": SN})
    client = _env_client()
    client.Event.TextInput.Request.EnvironmentStateRaw = b"\xff\xffbroken"

    async def run():
        assert await capabilities.observe(conversation, client, "TextInput") is False
        device = next(d for d in registry.data["devices"] if d["sn"] == SN)
        assert "capabilities" not in device["state"]

    asyncio.run(run())
