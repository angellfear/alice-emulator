"""Демо-данные для скриншотов README: папка state с нуля, наполнение через API и подключения.

Запуск: python tools/demo_state.py <папка>
Папка создаётся с нуля (существующая непустая — ошибка). Эмулятор поднимается на временном
порте, пользователи/устройства/маршруты/сценарии/правила вносятся HTTP-запросами, трафик —
подключениями WS и запросами от имени устройств (как это делали бы колонка и пользователь).
Внутренние файлы эмулятора скрипт не пишет. Upstream для «Прозрачно» и гибрида — локальная
заглушка (как в тестах), не Яндекс.

В демо нет настоящих значений: серийники — вымышленные с T1000 (allowlist .gitleaks.toml),
device-id demo-…, IP документационные 192.0.2.x, e-mail *@example.test, ключей нет.
После наполнения рядом с папкой создаётся demo.json — момент генерации для съёмки скриншотов.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import sys
import time
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import ClientSession, WSMsgType, web  # noqa: E402

from server_emulator.runtime import Runtime  # noqa: E402
from server_emulator.server import create_app  # noqa: E402

CLIENT = __import__("server_emulator.schema", fromlist=["load_type"]).load_type(
    ROOT / "server_emulator" / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def demo_sn(type_char):
    """Валидный по CRC серийник демо-устройства: тип (X/C/M/U) + 00T1000 + контроль,
    по алгоритму DeviceKit.parseSn; значения фиктивные (allowlist .gitleaks.toml)."""
    import zlib
    head = type_char + "00T10000"
    stored = zlib.crc32((head + "K").encode()) & 0xFFFFF
    body = "".join(CROCKFORD[(stored >> shift) & 31] for shift in (0, 5, 10, 15))
    return head + body + "K"


# Устройства: (название, модель/платформа, тип серийника, device-id, владелец, маршрут, запись)
DEVICES = [
    ("Гостиная", "yandexstation_2", "X", "demo-station", "home", "emulate", "on"),
    ("Кухня", "yandexmini_2", "M", "demo-mini2", "home", "hybrid", "off"),
    ("Детская", "chiron", "C", "demo-duo", "dacha", "proxy", "off"),
    ("Кабинет", "magritte", "T", "demo-tv", "dacha", "emulate", "off"),
]

SYNCHRONIZE = {
    "Event": {"Header": {"MessageId": "demo-sync"},
              "SynchronizeStateRequest": {
                  "Application": {"AppId": "ru.yandex.centaur", "AppVersion": "3.368.53",
                                  "Platform": "android", "DeviceId": "", "Lang": "ru-RU",
                                  "Timezone": "Asia/Yekaterinburg", "DeviceModel": "",
                                  "DeviceManufacturer": "Yandex", "IntegrationType": "QUARK",
                                  "ClientType": "IOSDK"}}}

}


def demo_apk(path):
    """Минимальный APK: AndroidManifest.xml в текстовом виде (его читает apk_metadata.read_apk)."""
    manifest = ("""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
          package="com.example.palette" android:versionCode="7" android:versionName="1.7">
  <application android:label="Палитра" android:icon="@mipmap/ic">
    <activity android:name=".Main"/>
  </application>
</manifest>
""")
    icon = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff610000001d49444154"
        "789c63f8cfc0f01f622618a8b810ac0855c41c1cd0f00e3f00d9f1b7cefc0000000049454e44ae426082")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", manifest)
        archive.writestr("res/mipmap/ic.png", icon)
        archive.writestr("classes.dex", b"dex\n demo")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def stub_upstream():
    """Локальная заглушка Яндекса для «Прозрачно» и гибрида (как в тестах)."""
    async def handler(request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            socket = web.WebSocketResponse(autoping=False)
            await socket.prepare(request)
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    await socket.send_str("up:" + message.data)
            return socket
        return web.json_response({"status": "ok", "from": "upstream-stub"})

    app = web.Application()
    app.router.add_route("*", "/{p:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def client_message(frame):
    from google.protobuf import json_format
    frame = json.loads(json.dumps(frame)) if not isinstance(frame, dict) else frame
    return json_format.ParseDict(frame, CLIENT())


async def device_session(client, base_url, device_id, model, frames):
    """Подключение устройства: sync → кадры → ожидание ответов (сценарии и промахи
    регистрируются асинхронно — закрывать соединение раньше нельзя)."""
    sync = json.loads(json.dumps(SYNCHRONIZE))
    sync["Event"]["SynchronizeStateRequest"]["Application"]["DeviceId"] = device_id
    sync["Event"]["SynchronizeStateRequest"]["Application"]["DeviceModel"] = model
    async with client.ws_connect(base_url + "/uni.ws") as ws:
        await ws.send_bytes(b"AAPI" + client_message(sync).SerializeToString())
        for frame in frames:
            await ws.send_bytes(b"AAPI" + client_message(frame).SerializeToString())
        for _ in range(int(2.5 / 0.25)):
            try:
                got = await ws.receive(timeout=0.25)
                if got.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                    break
            except asyncio.TimeoutError:
                pass


def text_input(text, request_id):
    return {"Event": {"Header": {"MessageId": request_id},
                      "TextInput": {"Header": {"RequestId": request_id, "DialogId": "demo-dialog"},
                                    "Request": {"Event": {"Type": "text_input", "Text": text}}}}}


async def populate(base_url, stub_port, state_dir):
    """Наполнение через API и подключения: пользователи → устройства → трафик → правила."""
    from google.protobuf import json_format

    async with ClientSession() as client:
        async def api(method, path, value=None, headers=None):
            response = await client.request(method, base_url + path,
                                            headers=headers or {"X-Emulator": "1",
                                                                "Content-Type": "application/json"},
                                            data=None if value is None else json.dumps(value))
            if response.status >= 400:
                raise SystemExit(f"API {method} {path} → {response.status}: {await response.text()}")
            return response
        # upstream-заглушка для устройств в «Прозрачно» и гибриде
        settings = await (await api("GET", "/emulator/api/settings")).json()
        settings["aliases"] = {host: f"http://127.0.0.1:{stub_port}" for host in (
            "quasar.yandex.net", "rpc.alice.yandex.net", "uniproxy.alice.yandex.net")}
        settings["aliases"]["avatars.yandex.net"] = "http://127.0.0.1:1"  # обложка не получена — для «Лога»
        settings["aapi_model"] = True  # фразы обрабатываются сценариями и промахами
        await api("PUT", "/emulator/api/settings", settings)

        # пользователи: «Дом» и «Дача»
        await api("POST", "/emulator/api/identities", {"operation": "user", "id": "home", "name": "Дом"})
        await api("POST", "/emulator/api/identities", {"operation": "user", "id": "dacha", "name": "Дача"})

        # устройства: реестр + модель/название; маршруты и «Запись»
        for name, model, type_char, device_id, user, route_mode, record in DEVICES:
            sn = demo_sn(type_char)
            await api("POST", "/emulator/api/identities",
                      {"operation": "device", "sn": sn, "name": name, "model": model,
                       "user_id": user, "device_ids": [device_id]})
            await api("PUT", "/emulator/api/route_mode", {"sn": sn, "mode": route_mode})
            await api("PUT", "/emulator/api/record_mode", {"sn": sn, "mode": record})

        headers_of = lambda device_id: {"Host": "quasar.yandex.net", "X-YaQuasarDeviceId": device_id,
                                        "Content-Type": "application/json"}

        # трафик: голосовые фразы, HTTP-запросы, промахи (от имени устройств)
        frames_home = [text_input("сколько времени", "demo-rq-clock"),
                       text_input("сделай громкость пять", "demo-rq-volume"),
                       text_input("включи мультики", "demo-rq-cartoon")]  # промах фразы
        frames_kitchen = [text_input("сколько времени", "demo-rq-clock-2")]
        frames_dacha = [text_input("включи музыку", "demo-rq-music")]
        for device_id, model, frames in ((DEVICES[0][3], DEVICES[0][1], frames_home),
                                         (DEVICES[1][3], DEVICES[1][1], frames_kitchen),
                                         (DEVICES[2][3], DEVICES[2][1], frames_dacha)):
            await device_session(client, base_url, device_id, model, frames)

        # HTTP от имени устройств: sync-конфиг, метрики (встроенный ответ), промах эндпоинта
        for device_id, path, method in ((DEVICES[0][3], "/get_sync_info?platform=yandexstation_2&uuid=6205001", "GET"),
                                        (DEVICES[0][3], "/quasar_metrics/write/batch", "POST"),
                                        (DEVICES[1][3], "/quasar/write/json/batch", "POST"),
                                        (DEVICES[1][3], "/v1.0/devices/state?extra=1", "GET"),
                                        (DEVICES[2][3], "/get_sync_info?platform=yandexmini&uuid=6205003", "GET")):
            body = json.dumps({"metrics": []}) if "batch" in path else None
            await client.request(method, base_url + path, headers=headers_of(device_id),
                                 data=body)

        # служебные события без ответа («Эмулятор → WS», по 1–2): ConfirmRequest-подобных нет,
        # берём Ping — обработчика нет, попадает в «AAPI без ответа»
        ping = {"Event": {"Header": {"MessageId": "demo-ping"}, "Ping": {}}}
        await device_session(client, base_url, DEVICES[0][3], DEVICES[0][1], [ping])

        # сценарии: 2 своих (JSON и py) — уже после трафика, чтобы «включи мультики» остался промахом
        cartoon = {"format_version": 2, "id": "user.cartoons", "version": "1.0.0", "category": "custom",
                   "title": {"ru": "Включи мультики", "en": "Cartoons"},
                   "triggers": [{"kind": "phrase", "phrases": ["включи мультики", "включи мультфильм"]}],
                   "flow": [{"id": "reply", "op": "compose",
                             "reply": {"text": "Включаю мультики на детской."}},
                            {"id": "done", "op": "finish"}]}
        await api("PUT", "/emulator/api/scenarios", {"scenario": cartoon})
        bedtime = {"format_version": 2, "id": "user.bedtime", "version": "1.0.0", "category": "custom",
                   "title": {"ru": "Режим сна", "en": "Bedtime"},
                   "triggers": [{"kind": "phrase", "phrases": ["спокойной ночи"]}],
                   "executor": {"kind": "script"}}
        script = '''"""Отвечает пожеланием доброй ночи."""
from server_emulator.sdk import say


async def handle(ctx):
    return say("Спокойной ночи! Свет в детской скоро погаснет.")
'''
        await api("PUT", "/emulator/api/scenarios", {"scenario": bedtime, "script": script})

        # Quasar: «Напрямую» с 1–2 правилами замен (значения вымышленные)
        station = demo_sn(DEVICES[0][2])
        await api("PUT", "/emulator/api/quasar_source",
                  {"sn": station, "mode": "direct",
                   "replace": [{"enabled": True, "pattern": '"subscription_type":\\s*"[a-z]+"',
                                "replace": '"subscription_type": "demo"'},
                               {"enabled": True, "pattern": '"quasar_app_version":\\s*"\\d+"',
                                "replace": '"quasar_app_version": "0"'}]})
        # у «Детской» — свой конфиг (custom) с минимальным config
        await api("PUT", "/emulator/api/quasar_source",
                  {"sn": demo_sn(DEVICES[2][2]), "mode": "custom",
                   "custom": {"config": {"name": "Детская", "update_period": 300,
                                          "capabilities": [], "device_config": {}}}})

        # обновления: у станции — запрет (демо-правило)
        # GET отдаёт inventory, а не настройки: собираем настройки из дефолтов через POST save
        await api("POST", "/emulator/api/updates",
                  {"policy": "allow", "custom": {"updates": []}, "devices": {station: "deny"},
                   "platforms": {}, "custom_devices": {}, "custom_platforms": {},
                   "endpoints": [{"id": "ota-list", "host": "quasar.yandex.net",
                                  "path": "/tv/api/v4/updates", "method": "POST", "kind": "list"},
                                 {"id": "ota-check", "host": "quasar.yandex.net",
                                  "path": "/check_updates", "method": "GET", "kind": "check"}]})

        # каталог приложений: своё приложение из мини-APK + раздел
        demo_dir = Path(state_dir).parent / "demo-build"
        demo_dir.mkdir(parents=True, exist_ok=True)
        apk_path = demo_dir / "palette.apk"
        demo_apk(apk_path)
        await api("PUT", "/emulator/api/catalog", {"mode": "merged",
                                                   "sections": [{"id": "demo-section", "title": "Мои приложения", "order": 0}]})
        async with client.post(base_url + "/emulator/api/catalog/apk",
                               headers={"X-Emulator": "1",
                                        "Content-Type": "application/vnd.android.package-archive"},
                               data=apk_path.read_bytes()) as response:
            app = await response.json()
        await client.put(base_url + "/emulator/api/catalog/apps/" + app["id"],
                         headers={"X-Emulator": "1", "Content-Type": "application/json"},
                         data=json.dumps({"section": "demo-section"}))
        (demo_dir / "palette.apk").unlink()

        # лог сервера: событие самообновления (проверка релизов — как в фоновом цикле)
        await api("POST", "/emulator/api/self_update", {"action": "check"})

        # лог сервера: события «Обложка не получена» и LineTooLong (аутентичные источники 068/070)
        try:
            await client.get(base_url + "/get-music-content/demo/700x700",
                             headers={"Host": "avatars.yandex.net"})
            reader, writer = await asyncio.open_connection("127.0.0.1", int(base_url.rsplit(":", 1)[1]))
            writer.write(b"GET /emulator/ HTTP/1.1\r\nHost: quasar.yandex.net\r\nX-Big: "
                         + b"A" * 20000 + b"\r\n\r\n")
            await writer.drain()
            await reader.read(256)
            writer.close()
        except OSError:
            pass


async def main(folder):
    state_dir = Path(folder).resolve()
    if state_dir.exists() and any(state_dir.iterdir()):
        raise SystemExit(f"Папка {state_dir} не пуста — укажите новую для демо-данных")
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(state_dir)
    runner = web.AppRunner(create_app(runtime=runtime))
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    base_url = f"http://127.0.0.1:{port}"
    stub, stub_port = await stub_upstream()
    try:
        await populate(base_url, stub_port, state_dir)
        await asyncio.sleep(0.3)
        # артефакт in-process прогона (незавершённая задача при остановке) — не для скриншота
        with runtime.server_log.db:
            runtime.server_log.db.execute("DELETE FROM entries WHERE message LIKE 'Task was destroyed%'")
        marker = {"generated": time.time(), "base_url": base_url}
        (ROOT / "tools" / "demo.json").write_text(json.dumps(marker), encoding="utf-8")
        print(f"Демо-данные готовы: {state_dir}")
    finally:
        await stub.cleanup()
        await runner.cleanup()
        await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Демо-данные для скриншотов (TASK-075)")
    parser.add_argument("folder", help="папка state для демо (создаётся, должна быть пуста)")
    args = parser.parse_args()
    asyncio.run(main(args.folder))
