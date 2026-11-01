"""TASK-032: точки подключения данных разделов — анимации, стандартные ответы, параметры,
каталог приложений, правила обновлений, MCP-серверы.

Расширение-папка (режим разработки, make_extension из test_extension_catalogs) кладёт записи
в разделы: только чтение, origin «extension:<id>», пользовательская запись с тем же id побеждает,
выключение расширения убирает вклад, некорректная запись — warning без падения раздела.
"""
import json

from server_emulator import mcp_hub
from server_emulator.behavior import standard_for
from server_emulator.runtime import Runtime
from tests.test_extension_catalogs import make_extension


def dump(value):
    return json.dumps(value, ensure_ascii=False)


# --- animations/ -------------------------------------------------------------------------

GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


async def test_extension_animations(tmp_path):
    runtime = Runtime(tmp_path)
    root = make_extension(runtime, files={
        "animations/blink.json": dump({"id": "blink", "title": "Моргание",
                                       "url": "http://example.com/blink.gif", "file": "blink.gif"}),
        "animations/bad.json": dump({"id": "bad", "title": "Без url"}),  # некорректная — warning
        "animations/broken.json": "{не json",
    })
    (root / "animations" / "blink.gif").write_bytes(GIF)
    runtime.extensions.load_enabled()
    try:
        def from_extension():
            return [a for a in runtime.animations.list() if a["origin"].startswith("extension:")]

        items = {a["id"]: a for a in runtime.animations.list()}
        assert [a["id"] for a in from_extension()] == ["coffee.blink"]  # префикс расширения; битые пропущены
        assert items["coffee.blink"]["origin"] == "extension:coffee"
        assert runtime.animations.url_of("coffee.blink") == "http://example.com/blink.gif"
        assert runtime.animations.media_path("blink.gif").read_bytes() == GIF  # GIF из папки расширения
        # в catalog.json расширение ничего не пишет
        assert not any(a["id"].startswith("coffee.") for a in runtime.animations.data["animations"])
        # пользовательская запись с тем же id перекрывает
        runtime.animations.put({"id": "coffee.blink", "title": "Моя", "url": "http://example.com/my.gif"})
        items = {a["id"]: a for a in runtime.animations.list()}
        assert items["coffee.blink"]["title"] == "Моя" and items["coffee.blink"]["origin"] == "user"
        runtime.animations.delete("coffee.blink")
        assert [a["id"] for a in from_extension()] == ["coffee.blink"]  # снова запись расширения
        await runtime.extensions.disable("coffee")
        assert from_extension() == []
        assert runtime.animations.get("coffee.blink") is None
    finally:
        await runtime.close()


# --- replies/ ----------------------------------------------------------------------------

async def test_extension_standard_replies(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime, files={
        "replies/hint.json": dump({"case": "not_understood", "name": "Кофе", "text": "Кофе не понял."}),
        "replies/alien.json": dump({"case": "nonexistent_case", "text": "Чужой случай"}),  # только известные
        "replies/empty.json": dump({"case": "unclaimed", "text": " "}),
    })
    runtime.extensions.load_enabled()
    try:
        assert standard_for([], "not_understood")["text"] == "Кофе не понял."
        assert standard_for([], "not_understood")["origin"] == "extension:coffee"
        assert standard_for([], "nonexistent_case") is None and standard_for([], "unclaimed") is None
        # пользовательская запись из настроек сильнее; settings не мутируются
        settings_replies = runtime.settings["standard_replies"]
        before = dump(settings_replies)
        assert standard_for(settings_replies, "not_understood")["text"] == "Я пока этого не умею."
        assert dump(settings_replies) == before
        # выключенная пользовательская запись с тем же id всё равно перекрывает запись расширения
        override = [{"id": "coffee.hint", "name": "x", "case": "not_understood", "text": "t", "enabled": False}]
        assert standard_for(override, "not_understood") is None
        # звуковой кеш видит фразу расширения
        assert "Кофе не понял." in runtime._static_phrases()
        await runtime.extensions.disable("coffee")
        assert standard_for([], "not_understood") is None
        assert "Кофе не понял." not in runtime._static_phrases()
    finally:
        await runtime.close()


# --- params/ -----------------------------------------------------------------------------

async def test_extension_param_definitions(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime, files={
        "params/theme.json": dump({"key": "user.theme", "scope": "user", "type": "select",
                                   "options": ["light", "dark"], "title": "Тема", "user_editable": True,
                                   "default_value": "dark"}),
        "params/bad.json": dump({"key": "user.bad", "scope": "nowhere", "type": "string"}),
    })
    runtime.extensions.load_enabled()
    store = runtime.quasar_params
    try:
        keys = [d["key"] for d in store.defs()]
        assert "user.coffee.theme" in keys and not any(k.endswith("bad") for k in keys)
        item = store.def_get("user.coffee.theme")
        assert item["origin"] == "extension:coffee" and item["default_value"] == "dark"
        assert "user.coffee.theme" in [d["key"] for d in store.defs(editable_only=True)]
        assert "user.coffee.theme" not in [d["key"] for d in store.defs(scopes=["server"])]
        # определения расширения в БД не копируются
        row = store.db.execute("SELECT 1 FROM param_defs WHERE key=?", ("user.coffee.theme",)).fetchone()
        assert row is None
        # пользовательское определение с тем же key побеждает
        store.def_upsert({"key": "user.coffee.theme", "scope": "user", "type": "string", "title": "Моя тема"})
        item = store.def_get("user.coffee.theme")
        assert item["title"] == "Моя тема" and "origin" not in item
        with store.db:
            store.db.execute("DELETE FROM param_defs WHERE key=?", ("user.coffee.theme",))
        assert store.def_get("user.coffee.theme")["origin"] == "extension:coffee"
        await runtime.extensions.disable("coffee")
        assert store.def_get("user.coffee.theme") is None
        assert "user.coffee.theme" not in [d["key"] for d in store.defs()]
    finally:
        await runtime.close()


# --- catalog/ ----------------------------------------------------------------------------

async def test_extension_catalog_apps(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime, files={
        "catalog/maps.json": dump({"name": "Карты", "package": "com.coffee.maps", "version": "1.0",
                                   "version_code": 5, "apk_url": "https://cdn.example.com/maps.apk"}),
        "catalog/local.json": dump({"name": "Локальный", "package": "com.coffee.test",
                                    "version_code": 1, "apk_url": "/__catalog/apk/abc"}),  # только http(s)
        "catalog/nopkg.json": dump({"name": "Без package", "version_code": 1,
                                    "apk_url": "https://cdn.example.com/x.apk"}),
    })
    runtime.extensions.load_enabled()
    catalog = runtime.catalog
    try:
        apps = {a["id"]: a for a in catalog.apps()}
        assert set(apps) == {"coffee.maps"}
        assert apps["coffee.maps"]["origin"] == "extension:coffee"
        assert apps["coffee.maps"]["package"] == "com.coffee.maps" and apps["coffee.maps"]["version_code"] == 5
        assert catalog.value["apps"] == []  # в catalog.json расширение ничего не пишет
        # пользовательская запись с тем же id побеждает
        catalog.value["apps"].append({"id": "coffee.maps", "name": "Мои карты", "package": "com.my.maps",
                                      "version": "2", "version_code": 9, "sha256": "a" * 64,
                                      "description": "", "icon": ""})
        apps = {a["id"]: a for a in catalog.apps()}
        assert apps["coffee.maps"]["name"] == "Мои карты" and "origin" not in apps["coffee.maps"]
        catalog.value["apps"].clear()
        # запись расширения попадает в wire-ответ каталога: ссылка на APK — прямая
        catalog.value["enabled"] = True
        patched = catalog.patch(b"", base_url="http://rpc.alice.yandex.net")
        assert b"https://cdn.example.com/maps.apk" in patched and b"com.coffee.maps" in patched
        await runtime.extensions.disable("coffee")
        assert catalog.apps() == []
    finally:
        await runtime.close()


# --- updates/ ----------------------------------------------------------------------------

async def test_extension_update_rules(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime, files={
        "updates/rule.json": dump({
            "devices": {"SN-EXT": "allow"},
            "platforms": {"yandexmini_2": "custom"},
            "custom_platforms": {"yandexmini_2": {"updates": [{"url": "https://ota.example.com/mini.zip"}]}}}),
        "updates/bad.json": dump({"devices": {"SN-BAD": "teleport"}}),  # неизвестная политика — warning
        "updates/unknown.json": dump({"surprise": 1}),
    })
    runtime.extensions.load_enabled()
    updates = runtime.updates
    try:
        rules = updates.extension_rules()
        assert [r["origin"] for r in rules] == ["extension:coffee"]
        settings = updates.settings()
        merged = updates.with_extension_rules(settings)
        assert updates.effective(merged, {"sn": "SN-EXT"})[0] == "allow"
        policy, custom = updates.effective(merged, {"sn": "SN-X", "model": "yandexmini_2"})
        assert policy == "custom" and custom["updates"][0]["url"] == "https://ota.example.com/mini.zip"
        assert updates.effective(merged, {"sn": "SN-BAD"})[0] == settings["policy"]  # битое правило не применено
        # settings не мутируются и в БД правила не копируются
        assert "SN-EXT" not in updates.settings()["devices"] and "platforms" not in updates.settings()
        # пользовательская запись побеждает
        user = {**settings, "devices": {"SN-EXT": "deny"}}
        assert updates.effective(updates.with_extension_rules(user), {"sn": "SN-EXT"})[0] == "deny"
        await runtime.extensions.disable("coffee")
        assert updates.extension_rules() == []
        assert updates.with_extension_rules(settings) == settings
    finally:
        await runtime.close()


# --- mcp/ --------------------------------------------------------------------------------

async def test_extension_mcp_servers(tmp_path):
    runtime = Runtime(tmp_path)
    make_extension(runtime, files={
        "mcp/tools.json": dump({"mcpServers": {"shell": {"command": "python", "args": ["srv.py"]},
                                                "off": {"url": "http://127.0.0.1:9/mcp", "disabled": True}}}),
        "mcp/remote.json": dump({"url": "http://127.0.0.1:9/mcp"}),  # одно описание: имя — по файлу
        "mcp/bad.json": dump({"mcpServers": {"broken": {"type": "sse"}}}),  # некорректное — warning
    })
    runtime.extensions.load_enabled()
    try:
        servers = mcp_hub.servers(runtime)
        assert {"coffee.shell", "coffee.off", "coffee.remote"} <= set(servers)
        assert "coffee.broken" not in servers
        assert servers["coffee.shell"]["origin"] == "extension:coffee"
        assert mcp_hub.transport(servers["coffee.shell"]) == "stdio"
        assert mcp_hub.transport(servers["coffee.remote"]) == "http"
        assert servers["coffee.off"]["disabled"] is True
        assert mcp_hub.server_of(runtime, "coffee.shell")["command"] == "python"
        # mcp.json расширение не пишет
        assert not any(k.startswith("coffee.") for k in mcp_hub.load_config(runtime)["mcpServers"])
        # пользовательская запись mcp.json с тем же id побеждает
        mcp_hub.save_config(runtime, {"mcpServers": {"coffee.shell": {"url": "http://127.0.0.1:9/own"}}})
        own = mcp_hub.servers(runtime)["coffee.shell"]
        assert own["url"] == "http://127.0.0.1:9/own" and "origin" not in own
        await runtime.extensions.disable("coffee")
        assert not any(k.startswith("coffee.") and k != "coffee.shell" for k in mcp_hub.servers(runtime))
        assert mcp_hub.servers(runtime)["coffee.shell"]["url"] == "http://127.0.0.1:9/own"
    finally:
        await runtime.close()
