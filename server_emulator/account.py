"""Клиент аккаунта Яндекса для первичного конфигуратора («Подтянуть из аккаунта»).

Авторизация и iot-API — по образцу Home Assistant-интеграции AlexxIT/YandexStation
(MIT, https://github.com/AlexxIT/YandexStation, файлы core/yandex_session.py и
core/yandex_quasar.py): QR-вход (pwl-yandex magic-код), обмен cookies на x_token
(mobileproxy token_by_sessionid), проверка токена (short_info), вход по x_token
(x_token → сессия), список устройств iot. Лицензия и происхождение —
docs/LICENSE-NOTICE.md.

Секреты (x_token) живут в state/account/sessions/<пользователь>.json — вход свой у
каждого пользователя, наружу API отдаёт только статус; там же кешируются токены
сервисов (service_token — обмен x_token на oauth.mobile.yandex.net). В журналы и
тексты ошибок значения не попадают. Пароля клиент не спрашивает и не хранит.

Второй источник значений — журнал трафика (get_sync_info настоящего облака, ответы
записаны при проходе наверх): то, что колонка уже сообщила аккаунту, — wifi_hash,
координаты, имя, glagol-ключи. Работает без входа в аккаунт.
"""
import asyncio
import json
import logging
import re
import time

import aiohttp

log = logging.getLogger(__name__)

PASSPORT = "https://passport.yandex.ru"
MOBILEPROXY = "https://mobileproxy.passport.yandex.net/1/bundle"
MOBILE_OAUTH = "https://oauth.mobile.yandex.net/1/token"
IOT = "https://iot.quasar.yandex.ru"
IOT_PAGE = "https://yandex.ru/iot"  # страница iot — источник CSRF браузерной сессии
# Публичные идентификаторы клиента Яндекса из YandexStation (MIT) — обмен cookies на токен.
COOKIE_CLIENT_ID = "c0ebe342af7d48fbbbfcf2d2eedb8f9e"
COOKIE_CLIENT_SECRET = "ad0a908f0aa341a182a37ecd75bc319e"
# Публичные идентификаторы приложений сервисов (YandexStation, MIT —
# core/yandex_session.py::get_music_token; они же ссылаются на MarshalX/yandex-music-api;
# происхождение — docs/LICENSE-NOTICE.md): обмен x_token на токен сервиса.
SERVICE_CLIENTS = {"music": ("23cabbbdc6cd418abb4b39c32c41195d", "53bc75238f0c4d08a118e51fe9203300")}
QR_POLL_TIMEOUT_S = 120

# Пути значений в ответе get_sync_info настоящего облака (журнал upstream).
SYNC_WIFI_PATH = "/config/account_config/user_wifi_config/wifi_hash"
SYNC_LOCATION = "/config/device_config/location"
SYNC_NAME = "/config/device_config/name"
SYNC_GLAGOL = "/glagol/security"


class AccountError(ValueError):
    """Сбой входа/обращения к аккаунту (текст — для UI, без секретов)."""


class AccountSession:
    """Состояние входа аккаунта одного пользователя: x_token в
    state/account/sessions/<пользователь>.json, наружу — статус."""

    def __init__(self, runtime, user_id):
        self.runtime = runtime
        self.user_id = user_id
        # в user_id допустим любой текст (не-ASCII, пробелы) — имя файла безопасное,
        # сам user_id пишется в JSON: коллизии имён файлов видно по содержимому
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", user_id) or "_"
        self.path = runtime.directory / "account" / "sessions" / f"{name}.json"
        self._qr = None  # csrf/track от начала QR-входа (в памяти, не на диск)

    def _session(self):
        return self.runtime.lifecycle.client_session()

    # --- состояние -------------------------------------------------------------
    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) and data.get("x_token") else {}

    def _save(self, x_token, display_login):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"user_id": self.user_id, "x_token": x_token,
                                         "display_login": display_login},
                                        ensure_ascii=False), encoding="utf-8")

    def forget(self):
        """Выход: файл сессии удаляется целиком — x_token и кеш токенов сервисов
        уходят вместе с ним."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        self._qr = None

    async def service_token(self, service, refresh=False):
        """Токен сервиса Яндекса из x_token сессии (обмен grant_type=x-token на
        oauth.mobile.yandex.net с client_id/secret приложения сервиса). Кеш — в файле
        сессии (service_tokens), повторный обмен — только без кеша (refresh=False) или
        когда сервис ответил 401 (вызывающий запрашивает refresh=True); неудачный
        обмен кеш не трогает. Ошибки — AccountError без секретов."""
        client = SERVICE_CLIENTS.get(service)
        if client is None:
            raise AccountError(f"Неизвестный сервис: {service}")
        data = self.load()
        if not data:
            raise AccountError("Вход в аккаунт не выполнен")
        cached = (data.get("service_tokens") or {}).get(service)
        if not refresh and isinstance(cached, dict) and cached.get("token"):
            return str(cached["token"])
        client_id, client_secret = client
        try:
            async with self._session().post(
                    MOBILE_OAUTH,
                    data={"client_id": client_id, "client_secret": client_secret,
                          "grant_type": "x-token", "access_token": data["x_token"]},
                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                body = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AccountError(f"Яндекс недоступен ({type(exc).__name__})") from exc
        token = body.get("access_token")
        if not token:
            raise AccountError("Яндекс не выдал токен сервиса — проверьте вход аккаунта")
        self._cache_service_token(service, token)
        return str(token)

    def _cache_service_token(self, service, token):
        """Кеш токена сервиса в файле сессии (рядом с x_token); сбой записи не роняет
        вызов — токен обменяется заново при следующем запросе без кеша."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            tokens = data.get("service_tokens")
            tokens = tokens if isinstance(tokens, dict) else {}
            tokens[service] = {"token": token, "ts": time.time()}
            data["service_tokens"] = tokens
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def status(self):
        data = self.load()
        return {"login": bool(data), "login_name": data.get("display_login") or None}

    # --- вход ------------------------------------------------------------------
    async def login_token(self, x_token):
        """Вход готовым x_token: проверка short_info, токен — в state/."""
        info = await self._validate_token(x_token)
        self._save(x_token, info.get("display_login") or "")
        return self.status()

    async def login_cookies(self, cookies=""):
        """Cookies → x_token (YandexStation login_cookies): строка «k=v; …», JSON
        расширения Copy Cookies или пусто (cookies уже в сессии — после QR)."""
        host = "passport.yandex.ru"
        text = str(cookies or "").strip()
        if not text:
            jar = "; ".join(f"{c.key}={c.value}" for c in self._session().cookie_jar
                            if c["domain"].endswith("yandex.ru"))
            if not jar:
                raise AccountError("Cookies в сессии нет — передайте строку cookies")
            cookies, text = jar, jar
        elif text[0] == "[":
            try:
                raw = json.loads(text)
            except ValueError as exc:
                raise AccountError("Cookies: не JSON") from exc
            domains = [item for item in raw if str(item.get("domain", "")).startswith(".yandex.")]
            if not domains:
                raise AccountError("Cookies: нет домена .yandex.*")
            host = domains[0]["domain"]
            text = "; ".join(f"{p['name']}={p['value']}" for p in raw)
        try:
            async with self._session().post(
                    f"{MOBILEPROXY}/oauth/token_by_sessionid",
                    data={"client_id": COOKIE_CLIENT_ID, "client_secret": COOKIE_CLIENT_SECRET},
                    headers={"Ya-Client-Host": host, "Ya-Client-Cookie": text},
                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                body = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AccountError(f"Яндекс недоступен ({type(exc).__name__})") from exc
        x_token = body.get("access_token")
        if not x_token:
            raise AccountError("Яндекс не выдал токен по cookies (проверьте срок сессии)")
        info = await self._validate_token(x_token)
        self._save(x_token, info.get("display_login") or "")
        return self.status()

    async def _validate_token(self, x_token):
        try:
            async with self._session().get(
                    f"{MOBILEPROXY}/account/short_info/?avatar_size=islands-300",
                    headers={"Authorization": f"OAuth {x_token}"},
                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AccountError(f"Яндекс недоступен ({type(exc).__name__})") from exc

    async def qr_start(self):
        """Начало QR-входа (YandexStation get_qr): страница pwl-yandex → CSRF →
        трек → ссылка QR-кода. Ссылка пользователь подтверждает в приложении Яндекса."""
        session = self._session()
        try:
            async with session.get(f"{PASSPORT}/pwl-yandex",
                                   timeout=aiohttp.ClientTimeout(total=30)) as response:
                page = await response.text()
            found = re.search(r'__CSRF__ = "([^"]+)', page)
            if not found:
                raise AccountError("Страница входа без CSRF — попробуйте вход по cookies/token")
            headers = {"X-CSRF-Token": found.group(1)}
            async with session.post(f"{PASSPORT}/pwl-yandex/api/passport/auth/password/submit",
                                    json={"retpath": f"{PASSPORT}/"}, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                track = await response.json()
            async with session.post(f"{PASSPORT}/pwl-yandex/api/passport/auth/magic/code",
                                    data={"location_id": "0", "magic_track_id": track.get("track_id", ""),
                                          "track_id": ""},
                                    headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                magic = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AccountError(f"Страница входа недоступна ({type(exc).__name__})") from exc
        link = magic.get("link")
        if not link:
            raise AccountError("Яндекс не отдал ссылку QR-кода")
        self._qr = {"headers": headers, "track": track, "started": time.monotonic()}
        return {"link": link}

    async def qr_poll(self):
        """Ожидание подтверждения QR в приложении: state waiting | done | error.
        Готово — сессия из трека → cookies → x_token в state/."""
        if not self._qr:
            raise AccountError("QR-вход не начат")
        if time.monotonic() - self._qr["started"] > QR_POLL_TIMEOUT_S:
            self._qr = None
            raise AccountError("Время подтверждения QR истекло — начните заново")
        session = self._session()
        headers, track = self._qr["headers"], self._qr["track"]
        try:
            async with session.post(f"{PASSPORT}/pwl-yandex/api/passport/auth/magic/code/status",
                                    json=track, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                state = await response.json()
            if state.get("state") != "otp_auth_finished":
                return {"state": "waiting"}
            async with session.post(f"{PASSPORT}/pwl-yandex/api/passport/sessions/get_session",
                                    data={"track_id": state.get("trackId", "")}, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AccountError(f"Страница входа недоступна ({type(exc).__name__})") from exc
        self._qr = None
        await self.login_cookies()  # cookies сессии от get_session → x_token
        return {"state": "done", **self.status()}

    # --- iot-API аккаунта ------------------------------------------------------
    async def _iot_headers(self):
        """Сессия по x_token (YandexStation login_token) — cookies браузерной сессии;
        для iot-запросов они нужны вместе с CSRF страницы iot."""
        data = self.load()
        if not data:
            raise AccountError("Вход в аккаунт не выполнен")
        session = self._session()
        try:
            async with session.post(f"{MOBILEPROXY}/auth/x_token/",
                                    data={"type": "x-token", "retpath": "https://www.yandex.ru"},
                                    headers={"Ya-Consumer-Authorization": f"OAuth {data['x_token']}"},
                                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                auth = await response.json()
            if auth.get("status") != "ok":
                raise AccountError("Токен аккаунта истёк — войдите заново")
            host = auth.get("passport_host") or PASSPORT
            async with session.get(f"{host}/auth/session/",
                                   params={"track_id": auth.get("track_id", "")},
                                   allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=30)) as response:
                await response.read()
            async with session.get(IOT_PAGE,
                                   timeout=aiohttp.ClientTimeout(total=30)) as response:
                page = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AccountError(f"Яндекс недоступен ({type(exc).__name__})") from exc
        found = re.search(r'"csrfToken2":"(.+?)"', page)
        return {"x-csrf-token": found.group(1)} if found else {}

    async def _iot_get(self, path):
        try:
            async with self._session().get(f"{IOT}{path}", headers=await self._iot_headers(),
                                           timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status in (401, 403):
                    raise AccountError("Сессия аккаунта не принята — войдите заново")
                return await response.json()
        except AccountError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise AccountError(f"iot-API недоступен ({type(exc).__name__})") from exc

    async def iot_devices(self):
        """Список устройств аккаунта (iot /m/v3/user/devices): дом и колонки с
        именами, комнатами и quasar_info (device_id/platform)."""
        data = await self._iot_get("/m/v3/user/devices")
        if data.get("status") != "ok":
            raise AccountError("iot-API вернул отказ")
        devices = []
        for house in data.get("households") or []:
            for item in house.get("all") or []:
                if not isinstance(item, dict):
                    continue
                info = item.get("quasar_info") if isinstance(item.get("quasar_info"), dict) else {}
                devices.append({"id": str(item.get("id") or ""),
                                "name": str(item.get("name") or ""),
                                "room": str(item.get("room") or item.get("room_name") or ""),
                                "device_id": str(info.get("device_id") or ""),
                                "platform": str(info.get("platform") or ""),
                                "house": str(house.get("name") or "")})
        return devices

    async def iot_config(self, iot_id):
        """Конфигурация колонки из аккаунта (iot /m/v2/user/devices/<id>/configuration):
        quasar_config — тот же каркас, что у get_sync_info (location, имя)."""
        data = await self._iot_get(f"/m/v2/user/devices/{iot_id}/configuration")
        config = data.get("quasar_config") if isinstance(data.get("quasar_config"), dict) else {}
        return config


# --- импорт значений в каталог «Параметры» -------------------------------------

# Обязательные параметры новой установки (шаг 1 — сверка): без них поставляемые
# замены пропускаются с закреплённым уведомлением.
REQUIRED_KEYS = ("user.wifi_hash", "user.location.latitude", "user.location.longitude")

# Ключи glagol-сертификата колонки: определения секретов создаёт импорт, если их
# ещё нет (quasar.py читает их из каталога, сильнее автогена).
GLAGOL_PARAMS = (("server_certificate", "device.glagol_cert", "Сертификат Glagol"),
                 ("server_private_key", "device.glagol_key", "Ключ Glagol"))

def _dig(data, path):
    """Значение по пути «/a/b/c» из разобранного JSON или None."""
    node = data
    for part in path.strip("/").split("/"):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return node


async def import_sync_info(runtime):
    """Значения из ответов get_sync_info настоящего облака (журнал upstream) →
    каталог «Параметры»: wifi_hash и координаты — владельцу-пользователю колонки,
    имя и glagol-ключи — колонке. Отчёт по полям без значений секретов."""
    params = runtime.quasar_params
    _ensure_glagol_defs(params)
    answers = await _sync_answers(runtime)
    report, filled = [], {}
    for answer in answers:
        sn = answer["sn"]
        user_id = _user_of(runtime, sn)
        targets = [
            ("user.wifi_hash", f"user:{user_id}" if user_id else None, _dig(answer["data"], SYNC_WIFI_PATH)),
            ("user.location.latitude", f"user:{user_id}" if user_id else None,
             (_dig(answer["data"], SYNC_LOCATION) or {}).get("latitude")),
            ("user.location.longitude", f"user:{user_id}" if user_id else None,
             (_dig(answer["data"], SYNC_LOCATION) or {}).get("longitude")),
            ("device.name", f"device:{sn}", _dig(answer["data"], SYNC_NAME)),
        ]
        security = _dig(answer["data"], SYNC_GLAGOL) or {}
        for field, key, _ in GLAGOL_PARAMS:
            value = security.get(field)
            if value:
                targets.append((key, f"device:{sn}", value))
        for key, owner, value in targets:
            if not value or not owner:
                continue
            if _apply(params, owner, key, value):
                filled.setdefault(key, {"key": key, "owner": owner, "source": "get_sync_info",
                                        "secret": params.def_get(key)["type"] == "secret"})
    for key, item in filled.items():
        report.append({**item, "status": "filled"})
    return {"items": report, "answers": len(answers)}


def linked_sessions(runtime):
    """Сессии всех пользователей с выполненным входом (в порядке реестра):
    импорт «Подтянуть из аккаунта» идёт по каждой из них по очереди."""
    return [AccountSession(runtime, user["id"])
            for user in runtime.identities.data["users"]
            if AccountSession(runtime, user["id"]).status()["login"]]


async def import_iot(runtime):
    """Значения из iot-API аккаунта → каталог: комнаты и имена колонок, координаты
    из конфигурации колонки. Импорт по каждой сессии из linked_sessions() по очереди,
    результаты объединяются. Отчёт по полям без значений секретов."""
    sessions = linked_sessions(runtime)
    if not sessions:
        raise AccountError("Вход в аккаунт не выполнен")
    filled, devices = [], 0
    for session in sessions:
        devices += await _import_iot_session(runtime, session, filled)
    return {"items": filled, "devices": devices}


async def _import_iot_session(runtime, session, filled):
    """Импорт одной сессии аккаунта; items дописывает в общий filled,
    возвращает число колонок этой сессии."""
    params = runtime.quasar_params
    devices = await session.iot_devices()
    for device in devices:
        if not device["device_id"]:
            continue
        sn = device["device_id"]
        owner = f"device:{sn}" if _device(runtime, sn) else None
        if owner is None:
            continue  # колонки нет в реестре — её параметры не трогаем
        for key, value in (("device.room", device["room"]), ("device.name", device["name"])):
            if value and _apply(params, owner, key, value):
                filled.append({"key": key, "owner": owner, "source": "iot",
                               "secret": False, "status": "filled"})
        try:
            config = await session.iot_config(device["id"])
        except AccountError:
            config = {}
        location = (config.get("device_config") or {}).get("location") or {}
        user_id = _user_of(runtime, sn)
        user_owner = f"user:{user_id}" if user_id else None
        for key, value in (("user.location.latitude", location.get("latitude")),
                           ("user.location.longitude", location.get("longitude"))):
            if value is not None and user_owner and _apply(params, user_owner, key, value):
                filled.append({"key": key, "owner": user_owner, "source": "iot",
                               "secret": False, "status": "filled"})
    return len(devices)


def required_unfilled(runtime):
    """Обязательные незаполненные: REQUIRED_KEYS без значения ни на одном уровне
    цепочки каждого контекста устройства (device → user → platform → server → default).
    missing — владельцы, в которые конфигуратор должен записать значение."""
    params = runtime.quasar_params
    contexts, seen = [], set()
    for device in runtime.identities.data["devices"]:
        user_id = device.get("user_id")
        mark = (device.get("sn"), user_id)
        if mark in seen:
            continue
        seen.add(mark)
        contexts.append({"sn": device.get("sn"), "user_id": user_id,
                         "platform": (device.get("profile") or {}).get("platform") or ""})
    result = []
    for key in REQUIRED_KEYS:
        definition = params.def_get(key)
        if definition is None:
            continue
        missing = []
        for context in contexts:
            try:
                params.resolve(key, user_id=context["user_id"], sn=context["sn"],
                               platform=context["platform"])
            except KeyError:
                owner = f"user:{context['user_id']}" if context["user_id"] else None
                if owner and owner not in missing:
                    missing.append(owner)
        if missing:
            result.append({"key": key, "title": definition["title"], "hint": definition["hint"],
                           "type": definition["type"], "missing": missing})
    return result


def _ensure_glagol_defs(params):
    """Определения glagol-ключей: секреты в разделе «служебное» колонки; idемпотентно."""
    for _field, key, title in GLAGOL_PARAMS:
        if params.def_get(key) is None:
            params.def_upsert({"key": key, "scope": "device", "category": "service",
                               "user_editable": False, "type": "secret", "title": title,
                               "hint": "Из get_sync_info настоящего облака (импорт конфигуратора)"})


async def _sync_answers(runtime):
    """Разобранные ответы get_sync_info настоящего облака из журнала (новейшие),
    по колонке: [{sn, data}] без повторов SN."""
    journal = runtime.upstream.journal
    listing = await journal.list(host="quasar.yandex.net", source="upstream", limit=50)
    answers, seen = [], set()
    for item in listing.get("items") or []:
        if not str(item.get("path") or "").startswith("/get_sync_info") or item["id"] in seen:
            continue
        seen.add(item["id"])
        record = await journal.item(item["id"])
        body = record.get("response_body") or b""
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            continue
        query = str(item.get("path") or "")
        sn = _query_param(query, "device_id") or ""
        if sn and data:
            answers.append({"sn": sn, "data": data})
        if len(answers) >= 8:
            break
    return answers


def _query_param(path, name):
    from urllib.parse import parse_qs, urlsplit
    values = parse_qs(urlsplit(path).query).get(name) or []
    return values[0] if values else ""


def _apply(params, owner, key, value):
    """Запись значения в каталог (числа — числами); False — параметр не определён."""
    definition = params.def_get(key)
    if definition is None:
        return False
    if definition["type"] == "number":
        try:
            value = float(value) if not isinstance(value, bool) else value
        except (TypeError, ValueError):
            return False
        value = int(value) if float(value).is_integer() else value
    try:
        params.set_value(owner, key, value)
    except Exception as exc:  # noqa: BLE001 — неверное значение — пропуск поля, не сбой импорта
        log.warning("Импорт аккаунта: %s не записан: %s", key, exc)
        return False
    return True


def _user_of(runtime, sn):
    device = _device(runtime, sn)
    return (device or {}).get("user_id") or ""


def _device(runtime, sn):
    return next((d for d in runtime.identities.data["devices"] if d.get("sn") == sn), None)
