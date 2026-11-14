"""Заглушки oauth.yandex.ru и mobileproxy.passport.yandex.net (режим-зависимые builtin-ответы).

Образец — код quasar-server (oauth.yandex.ru.ts, mobileproxy.passport.yandex.net.ts):
токены детерминированные («VAT.» + sha256-hex от 'access'/'refresh' + code + device_id;
token_by_sessionid — «VAT.» + случайный hex 32 байта), нигде не хранятся и не проверяются.
Персональное в short_info — из реестра эмулятора: владелец устройства, иначе первый
владелец users (имя/логин — из users); остальной каркас полей — из образца.
Неизвестный grant_type — 400 unsupported_grant_type (RFC 6749 §5.2),
не 500 переписанного upstream. Токены заглушек note_token музыки не захватывает
(префиксы VAT./VRT. — только у заглушек, у настоящего токена их нет). uid в ответах —
число: SDK паспорта колонки разбирает его как long, строковый идентификатор реестра
роняет разбор ответа (Failed to parse literal … as a long value) — колонка не
восстанавливает сессию аккаунта и после перезагрузки остаётся на сплэше загрузки.
Если у владельца устройства через «Подтянуть из аккаунта» (account.AccountSession)
привязан настоящий Яндекс-аккаунт — uid берётся настоящий (resolve_uid), иначе
детерминированный синтетический от id пользователя реестра (numeric_uid).
"""
import hashlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

HOSTS = ('oauth.yandex.ru', 'mobileproxy.passport.yandex.net')
TOKEN_TTL = 315360000  # 365 * 10 * 24 * 60 * 60 — срок из образца
STUB_PREFIXES = ('VAT.', 'VRT.')  # детерминированные префиксы заглушек
STATIC_DIR = Path(__file__).parent / "oauth_static"
_STATIC_CACHE: dict[str, dict] = {}
# каркас образца quasar-server (short_info): id клиента x-токена — константа их кода,
# не ключ реального сервиса; вынесена, чтобы не выглядеть встроенным секретом
_SAMPLE_CLIENT_ID = "ef0e663c4b4d38debd41f76ed511e73f"


def is_stub_token(token):
    """Токен в формате заглушек (VAT./VRT.): note_token музыки такие не захватывает —
    в кеше поставщика остаётся настоящий токен аккаунта."""
    return str(token or "").startswith(STUB_PREFIXES)


def _static(name):
    if name not in _STATIC_CACHE:
        _STATIC_CACHE[name] = json.loads(
            (STATIC_DIR / f"mobileproxy.passport.yandex.net_1_{name}.json").read_text(encoding="utf-8"))
    return _STATIC_CACHE[name]


def _form(body):
    try:
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except (ValueError, UnicodeError):
        return {}


def _owner(runtime, device):
    """Пользователь для uid/профиля: владелец устройства, иначе первый владелец users."""
    users = runtime.identities.data["users"]
    user_id = (device or {}).get("user_id")
    return next((u for u in users if u["id"] == user_id), None) or next(iter(users), None)


def _uid(runtime, device):
    user = _owner(runtime, device)
    return (user or {}).get("id") or ""


def numeric_uid(user_id):
    """Числовой uid для ответов заглушек: SDK паспорта колонки разбирает uid как long
    (образец настоящего ответа — число), строковый id реестра туда не годится.
    Число детерминированное — стабильное для одного пользователя между перезапусками."""
    if not user_id:
        return 0
    return int.from_bytes(hashlib.sha256(str(user_id).encode()).digest()[:4], "big")


def resolve_uid(runtime, user_id):
    """Настоящий uid привязанного через «Подтянуть из аккаунта» Яндекс-аккаунта
    владельца устройства (AccountSession.uid()) приоритетнее синтетического: колонка
    в «Эмуляции» видит тот же uid, что дал бы реальный Яндекс этому владельцу.
    Нет привязки — numeric_uid."""
    from .account import AccountSession
    real = AccountSession(runtime, user_id).uid()
    return real if real is not None else numeric_uid(user_id)


def _token_pair(form):
    code = str(form.get("code") or "").encode()
    device_id = str(form.get("device_id") or "").encode()
    return {"access_token": "VAT." + hashlib.sha256(b"access" + code + device_id).hexdigest(),
            "expires_in": TOKEN_TTL,
            "refresh_token": "VRT." + hashlib.sha256(b"refresh" + code + device_id).hexdigest(),
            "token_type": "bearer"}


def _x_token(form, uid):
    return {"access_token": str(form.get("access_token") or ""),
            "expires_in": TOKEN_TTL,
            "token_type": "bearer",
            "uid": uid}


def _short_info(runtime, device):
    """Профиль аккаунта: каркас полей из образца, персональное — из реестра.
    uid/master_uid и в master_members — число (resolve_uid): настоящий uid привязанного
    аккаунта, иначе детерминированный синтетический — SDK паспорта колонки разбирает
    их как long, строковый id реестра роняет разбор ответа."""
    user = _owner(runtime, device) or {}
    uid = resolve_uid(runtime, user.get("id"))
    name = str(user.get("name") or "Пользователь")
    return {
        "avatar_url": "https://avatars.mds.yandex.net/get-yapic/53031/0s-5/islands-300",
        "birthday": "1984-01-01",
        "display_login": name,
        "display_name": name,
        "filter_params": {"subtype": ["portal"]},
        "firstname": name,
        "gender": "m",
        "has_cards": True,
        "has_family": True,
        "has_password": True,
        "has_plus": True,
        "has_telemost": True,
        "have_webauthn_credential": True,
        "is_browser_account": True,
        "is_rfc_2fa_enabled": True,
        "lastname": "",
        "location_id": 0,
        "machine_readable_login": name,
        "master_members": [{
            "avatar_url": "https://avatars.mds.yandex.net/get-yapic/53031/0s-5/islands-300",
            "display_login": name,
            "has_cards": True,
            "has_family": True,
            "has_plus": True,
            "has_telemost": True,
            "is_browser_account": True,
            "plus_card": True,
            "primary_alias_type": 1,
            "public_name": name,
            "secure_phone_number": "+78005553535",
            "uid": uid,
        }],
        "master_uid": uid,
        "native_default_email": "owner@example.test",
        "normalized_display_login": name,
        "partitions": ["default"],
        "plus_card": True,
        "primary_alias_type": 1,
        "public_id": "oj4bx3cwbowdyz4eb5lt1v0z7v",
        "public_name": name,
        "secure_phone_number": "+78005553535",
        "status": "ok",
        "uid": uid,
        "x_token_client_id": _SAMPLE_CLIENT_ID,
        "x_token_issued_at": int(time.time()),
        "xtoken_has_muid": True,
    }


def _oauth_token(form, runtime, device):
    grant = str(form.get("grant_type") or "")
    if grant == "authorization_code":
        return 200, _token_pair(form)
    if grant == "x-token":
        return 200, _x_token(form, resolve_uid(runtime, _uid(runtime, device)))
    return 400, {"error": "unsupported_grant_type"}  # RFC 6749 §5.2


def handle(runtime, host, method, path, body, device):
    """(status, ответ-объект) builtin-заглушки хоста | None — маршрута заглушек нет."""
    if host not in HOSTS:
        return None
    route = (method, path)
    if route == ("POST", "/token"):
        return _oauth_token(_form(body), runtime, device)
    if host != "mobileproxy.passport.yandex.net":
        return None
    if route == ("POST", "/1/token"):
        return _oauth_token(_form(body), runtime, device)
    if route == ("POST", "/1/bundle/oauth/token_by_sessionid"):
        return 200, {"access_token": "VAT." + secrets.token_hex(32),
                     "expires_in": TOKEN_TTL, "status": "ok", "token_type": "bearer"}
    if route == ("GET", "/1/bundle/account/short_info"):
        return 200, _short_info(runtime, device)
    if route == ("GET", "/1/am/sso/config.json"):
        return 200, _static("am_sso_config")
    if route == ("GET", "/1/am/config.json"):
        return 200, _static("am_config")
    if route == ("GET", "/1/badges-specification.json"):
        return 200, _static("badges-specification")
    return None
