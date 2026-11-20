"""Реестр сервисных аккаунтов («Настройки → Авторизация в сервисах»): мультиаккаунты
с выбором основного, повторный вход тем же логином обновляет запись, перенос старых
сессий входа, копилка токенов колонок (note_token по SN + гидратация) и приоритет
токена колонки над токеном реестра при выборе для поставщика музыки. Обмен x_token
на токен сервиса — против локальной заглушки oauth.mobile.yandex.net; настоящий
Яндекс из тестов не дёргается, секреты в ответах не светятся.
"""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import unused_port

from server_emulator import account, music
from server_emulator.runtime import Runtime

TOKEN = "y0_" + "t" * 30


@pytest.fixture(autouse=True)
def _clean_captured():
    music._CAPTURED.clear()
    music._CAPT_DETAILS.clear()
    yield
    music._CAPTURED.clear()
    music._CAPT_DETAILS.clear()


def provider_yandex_music():
    return {"id": "builtin.yandex-music", "service": "yandex-music"}


def headers_write():
    return {"X-Emulator": "1", "Content-Type": "application/json"}


class Ctx:
    """Минимальный контекст вызова: sn колонки и доступ к runtime."""

    def __init__(self, runtime, sn=""):
        self.sn = sn
        self._runtime = runtime

    def _services_runtime(self):
        return self._runtime


# --- реестр: мультиаккаунты и основной ------------------------------------------------

async def test_registry_add_default_remove(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        first = runtime.service_accounts.add("yandex", {"x_token": "t1"}, title="Первый")
        assert first["default"] is True and first["title"] == "Первый"  # первый — основной
        second = runtime.service_accounts.add("yandex", {"x_token": "t2"}, title="Второй")
        assert second["default"] is False
        # повторный вход тем же логином обновляет запись, а не плодит
        renewed = runtime.service_accounts.add("yandex", {"x_token": "t2-new"}, title="Второй")
        assert renewed["id"] == second["id"]
        assert len(runtime.service_accounts.list("yandex")) == 2
        assert runtime.service_accounts.secret(second["id"])["x_token"] == "t2-new"
        # основной переключается, у прочих снимается
        runtime.service_accounts.set_default(second["id"])
        assert runtime.service_accounts.default("yandex")["id"] == second["id"]
        assert first["default"] is False or runtime.service_accounts.default("yandex")["id"] != first["id"]
        # удаление основного — основной переходит преемнику того же сервиса
        runtime.service_accounts.remove(second["id"])
        assert runtime.service_accounts.default("yandex")["id"] == first["id"]
        assert not runtime.service_accounts.secret_path(second["id"]).exists()
        # список — только статусы, секретов нет; чужой сервис пуст
        assert "x_token" not in json.dumps(runtime.service_accounts.list())
        assert runtime.service_accounts.tokens("spotify") is None
        assert runtime.service_accounts.tokens("yandex") == {"x_token": "t1"}
    finally:
        await runtime.close()


async def test_registry_note_of_login_limitation(tmp_path):
    """note — ограничение входа, видимое в карточке: сохраняется с записью,
    повторный вход без причины снимает заметку."""
    runtime = Runtime(tmp_path)
    try:
        record = runtime.service_accounts.add("spotify", {"access_token": "t"},
                                              title="Аккаунт", note="HTTP 403: premium")
        assert record["note"] == "HTTP 403: premium"
        assert runtime.service_accounts.list("spotify")[0]["note"] == "HTTP 403: premium"
        runtime.service_accounts.add("spotify", {"access_token": "t2"}, title="Аккаунт")
        assert "note" not in runtime.service_accounts.list("spotify")[0]
    finally:
        await runtime.close()


async def test_registry_services_do_not_mix(tmp_path):
    """Аккаунты разных сервисов не влияют друг на друга (основной у каждого свой)."""
    runtime = Runtime(tmp_path)
    try:
        yandex = runtime.service_accounts.add("yandex", {"x_token": "t1"})
        spotify = runtime.service_accounts.add("spotify", {"access_token": "s1"})
        assert yandex["default"] and spotify["default"]
        runtime.service_accounts.remove(yandex["id"])
        assert runtime.service_accounts.default("spotify")["id"] == spotify["id"]
        assert runtime.service_accounts.default("yandex") is None
        with pytest.raises(ValueError):
            runtime.service_accounts.remove("yandex-ghost")  # неизвестный аккаунт
    finally:
        await runtime.close()


# --- перенос старых сессий входа --------------------------------------------------------

async def test_old_sessions_migrate_into_registry(tmp_path):
    sessions = tmp_path / "account" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "u1.json").write_text(
        json.dumps({"x_token": "t1", "display_login": "tester-1"}), encoding="utf-8")
    (sessions / "u2.json").write_text(
        json.dumps({"x_token": "t2", "display_login": "tester-2"}), encoding="utf-8")
    runtime = Runtime(tmp_path)  # перенос — при создании реестра
    try:
        accounts = runtime.service_accounts.list("yandex")
        assert [item["title"] for item in accounts] == ["tester-1", "tester-2"]
        assert all(item["login"] for item in accounts)
        assert accounts[0]["default"] is True and accounts[1]["default"] is False
        assert not (sessions / "u1.json").exists() and not (sessions / "u2.json").exists()
        assert not (tmp_path / "account").exists()  # папка старых сессий убрана
    finally:
        await runtime.close()


async def test_old_sessions_junk_is_left_behind(tmp_path):
    sessions = tmp_path / "account" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "broken.json").write_text("{не json", encoding="utf-8")
    (sessions / "nologin.json").write_text(json.dumps({"display_login": "x"}), encoding="utf-8")
    runtime = Runtime(tmp_path)
    try:
        assert runtime.service_accounts.list("yandex") == []  # без x_token — не аккаунт
        assert (sessions / "broken.json").exists() and (sessions / "nologin.json").exists()
    finally:
        await runtime.close()


# --- копилка токенов колонок -------------------------------------------------------------

async def test_column_tokens_kept_per_sn_and_hydrated(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        assert music.note_token(runtime, TOKEN, sn="SN-1") is True
        assert music.note_token(runtime, "y0_" + "a" * 30, sn="SN-2") is True
        assert music.captured_oauth("SN-1") == TOKEN          # свой токен колонки
        assert music.captured_oauth("SN-2") == "y0_" + "a" * 30
        assert music.captured_oauth() == "y0_" + "a" * 30     # последний захваченный
        assert music.captured_oauth("SN-9") == "y0_" + "a" * 30  # без своего — запасной
        assert music.captured_columns() == ["SN-1", "SN-2"]   # наружу SN, не токены
        assert music.note_token(runtime, "junk with spaces", sn="SN-3") is False  # не токен
        assert music.captured_columns() == ["SN-1", "SN-2"]
    finally:
        await runtime.close()
    music._CAPTURED.clear()
    revived = Runtime(tmp_path)
    try:
        music.load_token(revived.store)  # старт сервера: копилка из БД состояния
        assert music.captured_oauth("SN-1") == TOKEN
        assert music.captured_oauth("SN-9") == "y0_" + "a" * 30
        assert music.captured_columns() == ["SN-1", "SN-2"]
    finally:
        await revived.close()


async def test_column_details_ip_and_forget(tmp_path):
    """Детали захвата (ip, время) для панели; «Удалить» убирает и запись колонки,
    и запасной токен, если это тот же; повторное удаление — честный отказ."""
    runtime = Runtime(tmp_path)
    try:
        music.note_token(runtime, TOKEN, sn="SN-1", ip="192.0.2.10")
        music.note_token(runtime, "y0_" + "a" * 30, sn="SN-2")
        music.note_token(runtime, TOKEN, sn="SN-1", ip="192.0.2.10")  # SN-1 — последний
        details = {item["sn"]: item for item in music.captured_column_details()}
        assert details["SN-1"]["ip"] == "192.0.2.10" and details["SN-1"]["ts"]
        assert details["SN-2"]["ip"] == "" and details["SN-2"]["ts"]
        assert music.forget_token(runtime, "SN-1") is True
        assert music.captured_oauth("SN-1") == ""
        assert music.captured_oauth("SN-2") == "y0_" + "a" * 30  # чужая колонка не тронута
        assert music.captured_oauth() == ""  # запасной был тем же токеном — удалён тоже
        assert music.captured_column_details() == [{"sn": "SN-2", "ts": details["SN-2"]["ts"], "ip": ""}]
        with pytest.raises(ValueError):
            music.forget_token(runtime, "SN-1")
    finally:
        await runtime.close()


async def test_column_details_survive_restart(tmp_path):
    """Копилка с деталями переживает перезапуск (meta yandex_oauth_columns)."""
    runtime = Runtime(tmp_path)
    try:
        music.note_token(runtime, TOKEN, sn="SN-1", ip="192.0.2.10")
    finally:
        await runtime.close()
    music._CAPTURED.clear()
    music._CAPT_DETAILS.clear()
    revived = Runtime(tmp_path)
    try:
        music.load_token(revived.store)
        details = {item["sn"]: item for item in music.captured_column_details()}
        assert details["SN-1"]["ip"] == "192.0.2.10" and details["SN-1"]["ts"]
        assert music.captured_oauth("SN-1") == TOKEN
    finally:
        await revived.close()


async def test_columns_api_rows_and_forget(tmp_path):
    """API панели: строки копилки с платформой из реестра устройств и адресом захвата,
    без токенов; op forget удаляет и повторно честно отказывает."""
    runtime = Runtime(tmp_path)
    runtime.identities.mutate({"operation": "user", "id": "owner", "name": "O"})
    runtime.identities.mutate({"operation": "device", "sn": "SN-1", "user_id": "owner",
                               "profile": {"platform": "chiron"}, "device_ids": ["id-1"]})
    from server_emulator.server import create_app
    from tests.conftest import listen
    runner, url = await listen(create_app(runtime=runtime))
    try:
        music.note_token(runtime, TOKEN, sn="SN-1", ip="192.0.2.10")
        async with runtime.lifecycle.client_session().get(
                f"{url}/emulator/api/service-accounts") as response:
            data = await response.json()
        [column] = data["columns"]
        assert column["sn"] == "SN-1" and column["platform"] == "chiron"
        assert column["ip"] == "192.0.2.10" and column["ts"]
        assert "token" not in json.dumps(data["columns"])  # токены наружу не идут
        async with runtime.lifecycle.client_session().post(
                f"{url}/emulator/api/service-accounts", headers=headers_write(),
                json={"op": "forget", "sn": "SN-1"}) as response:
            assert await response.json() == {"ok": True}
        assert music.captured_columns() == []
        async with runtime.lifecycle.client_session().post(
                f"{url}/emulator/api/service-accounts", headers=headers_write(),
                json={"op": "forget", "sn": "SN-1"}) as response:
            assert response.status == 400 and "не захвачен" in (await response.json())["error"]
    finally:
        await runner.cleanup()  # закрывает и runtime (create_app.on_cleanup)


async def test_empty_state_drops_foreign_captured(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        music.note_token(runtime, TOKEN)
        assert music.captured_oauth() == TOKEN
    finally:
        await runtime.close()
    music._CAPTURED.clear()
    music._CAPTURED["oauth"] = TOKEN
    fresh = Runtime(tmp_path / "other-state")  # другая БД состояния
    try:
        music.load_token(fresh.store)  # пустая БД — чужой токен из кеша убрать
        assert music.captured_oauth() == ""
    finally:
        await fresh.close()


# --- выбор токена для поставщика музыки ---------------------------------------------------

async def test_oauth_token_prefers_column_token(tmp_path):
    """Токен колонки из копилки — приоритет: реестр даже не опрашивается."""
    runtime = Runtime(tmp_path)
    try:
        runtime.service_accounts.add("yandex", {"x_token": "test-x-token"})
        music.note_token(runtime, TOKEN, sn="SN-C")
        token, refresh = await music._oauth_token(Ctx(runtime, "SN-C"), provider_yandex_music())
        assert token == TOKEN and callable(refresh)
        # колонка без своего токена — запасной (последний захваченный)
        token, _ = await music._oauth_token(Ctx(runtime, "SN-OTHER"), provider_yandex_music())
        assert token == TOKEN
        # не Яндекс.Музыке токен не положен
        assert await music._oauth_token(Ctx(runtime, "SN-C"),
                                        {"id": "other", "service": "spotify"}) == (None, None)
    finally:
        await runtime.close()


async def test_oauth_token_falls_back_to_registry(tmp_path, monkeypatch):
    """Колонка без токена — токен сервиса основного аккаунта реестра (обмен x_token
    на заглушке oauth); refresh — обмен заново; входа в реестре нет — пусто."""
    runtime = Runtime(tmp_path)
    forms = []

    async def token(request):
        form = dict(await request.post())
        forms.append(form)
        return web.json_response({"access_token": f"music-token-{len(forms)}"})

    app = web.Application()
    app.router.add_post("/1/token", token)
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    monkeypatch.setattr(account, "MOBILE_OAUTH", f"http://127.0.0.1:{port}/1/token")
    try:
        # входа нет — пустой токен без обращения к Яндексу
        token, _ = await music._oauth_token(Ctx(runtime, "SN-C"), provider_yandex_music())
        assert token == "" and forms == []
        # аккаунт в реестре — обмен x_token
        runtime.service_accounts.add("yandex", {"x_token": "test-x-token", "display_login": "tester"})
        token, refresh = await music._oauth_token(Ctx(runtime, "SN-C"), provider_yandex_music())
        assert token == "music-token-1" and callable(refresh)
        assert forms[0]["grant_type"] == "x-token" and forms[0]["access_token"] == "test-x-token"
        assert await refresh() == "music-token-2"  # сервис ответил 401 — обмен заново
        assert forms[1]["grant_type"] == "x-token"
    finally:
        await runner.cleanup()
        await runtime.close()
