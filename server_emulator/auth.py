"""B04: принципалы и сессии панели (admin/user), локальный bootstrap.

Файла auth.json нет — панель ОТКРЫТА (локальная лаборатория, прежнее поведение,
все автотесты работают без входа). `python -m server_emulator.auth` создаёт
первого администратора со сгенерированным паролем (печатается один раз) и
ВКЛЮЧАЕТ обязательный вход для /api, /emulator/api и /__catalog. Фиксированных
паролей нет; хэш PBKDF2-SHA256. Сессии — в памяти (рестарт → повторный вход),
cookie HttpOnly + SameSite=Strict (CSRF), отзыв logout/ревоком всех.

Роли: admin — всё; user — только auth-эндпоинты (пользовательский кабинет
/F01 — будущая работа; старые admin-маршруты для user закрыты 403).
Трафик УСТРОЙСТВ (прокси-порты, standalone device-маршруты) входа не требует.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

from aiohttp import web

COOKIE = "alica_session"
SESSION_TTL = 12 * 3600
PBKDF2_ITERATIONS = 120_000
# Локальные управляющие вызовы трея/лаунчера: без сессии, но с маркером
LOCAL_CONTROL_HEADER = "X-Local-Control"


def hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": iterations}


def verify_password(password, record):
    try:
        candidate = hash_password(password, bytes.fromhex(record["salt"]), int(record["iterations"]))
        return hmac.compare_digest(candidate["hash"], record["hash"])
    except (KeyError, ValueError, TypeError):
        return False


def secret(value):
    """B04: секрет может быть ссылкой env:VAR — значение живёт в окружении
    процесса и не хранится в настройках/экспортах."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value if isinstance(value, str) else ""


class AuthStore:
    def __init__(self, directory):
        self.path = directory / "auth.json"
        self.data = None  # None — файла нет: открытый режим
        self.sessions = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value.get("principals"), list):
                self.data = value
        except (ValueError, OSError):
            # повреждённый файл: вход закрыт, локальный bootstrap --reset чинит
            self.data = {"principals": [], "version": 1}

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    @property
    def enabled(self):
        return self.data is not None

    def bootstrap(self, name, password=None, reset=False):
        """Первый администратор локальной командой. Пароль генерируется и
        печатается ОДИН раз; без reset повторный запуск запрещён."""
        if self.data is not None and self.data.get("principals") and not reset:
            raise ValueError("Администратор уже существует; --reset для принудительной замены")
        password = password or secrets.token_urlsafe(12)
        principal = {"id": uuid.uuid4().hex, "name": (name or "admin").strip() or "admin",
                     "role": "admin", "disabled": False, "created": time.time(),
                     **hash_password(password)}
        self.data = {"version": 1, "principals": [principal]}
        self.sessions.clear()
        self._save()
        return principal, password

    def add_principal(self, name, password, role="user", identities_user=None):
        if not self.enabled:
            raise ValueError("Сначала bootstrap администратора")
        if role not in ("admin", "user"):
            raise ValueError("role: admin или user")
        if any(p["name"] == name for p in self.data["principals"]):
            raise ValueError("Имя уже занято")
        principal = {"id": uuid.uuid4().hex, "name": name, "role": role, "disabled": False,
                     "created": time.time(), **hash_password(password)}
        if role == "user":
            principal["identities_user"] = identities_user or None
        self.data["principals"].append(principal)
        self._save()
        return principal

    def user_login(self, identities_user):
        """Вход в кабинет пользователя реестра: user-принципал с этой привязкой."""
        return next((p for p in (self.data or {}).get("principals", [])
                     if p["role"] == "user" and p.get("identities_user") == identities_user), None)

    def set_user_login(self, identities_user, name, password=None):
        """Задать логин/пароль кабинета пользователю реестра (админ, из панели).
        Нет принципала — создаётся (пароль обязателен); есть — меняются имя и,
        если передан, пароль; его сессии отзываются."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Логин не может быть пустым")
        if password is not None and len(password) < 6:
            raise ValueError("Пароль — не короче 6 символов")
        principal = self.user_login(identities_user)
        if principal is None:
            if not password:
                raise ValueError("Задайте пароль")
            return self.add_principal(name, password, role="user", identities_user=identities_user)
        if any(p["name"] == name and p is not principal for p in self.data["principals"]):
            raise ValueError("Имя уже занято")
        principal["name"] = name
        if password:
            principal.update(hash_password(password))
        self._save()
        for token in [t for t, s in self.sessions.items() if s["id"] == principal["id"]]:
            self.sessions.pop(token)
        return principal

    def login(self, name, password):
        for principal in (self.data or {}).get("principals", []):
            if principal["name"] == name and not principal.get("disabled") \
                    and verify_password(password, principal):
                token = secrets.token_urlsafe(32)
                session = {"id": principal["id"], "name": principal["name"],
                           "role": principal["role"], "expires": time.time() + SESSION_TTL,
                           "identities_user": principal.get("identities_user")}
                self.sessions[token] = session
                return token, session
        return None

    def logout(self, token):
        self.sessions.pop(token, None)

    def revoke_all(self):
        self.sessions.clear()

    def session(self, token):
        session = self.sessions.get(token)
        if session is None:
            return None
        if session["expires"] < time.time():
            self.sessions.pop(token, None)
            return None
        return session

    def public(self):
        return {"enabled": self.enabled,
                "principals": [{"id": p["id"], "name": p["name"], "role": p["role"],
                                "disabled": bool(p.get("disabled"))}
                               for p in (self.data or {}).get("principals", [])]}


def auth_middleware(store):
    """Сессии для панели/API; открытый режим (нет auth.json) — всё как раньше.
    /api/status анонимно отдаёт минимум (здоровье трея), /api/server/stop —
    только с маркером локального управления."""
    user_allowed = {"/api/auth/logout", "/api/auth/whoami"}

    @web.middleware
    async def middleware(request, handler):
        path = request.path
        guarded = path.startswith("/api/") or path.startswith("/emulator/api/") \
            or path.startswith("/__catalog/")
        if not store.enabled or not guarded:
            return await handler(request)
        if path == "/api/server/stop":
            if request.headers.get(LOCAL_CONTROL_HEADER) == "1":
                return await handler(request)
            raise web.HTTPUnauthorized(text='{"error":"unauthorized"}',
                                       content_type="application/json")
        if path == "/api/auth/login" and request.method == "POST":
            return await handler(request)
        if path.endswith("/api/agent-event") and request.headers.get("X-Agent-Token"):
            return await handler(request)  # вебхук: токен проверяет сам обработчик (ui/api/agent_ops.py)
        token = request.cookies.get(COOKIE)
        session = store.session(token) if token else None
        if session is not None:
            request["auth"] = session
        if path == "/api/status":
            return await handler(request)  # анонимно — минимум (трей); с сессией — полный
        if session is None:
            raise web.HTTPUnauthorized(text='{"error":"unauthorized"}',
                                       content_type="application/json")
        if session["role"] != "admin" and path not in user_allowed                 and not path.startswith("/api/me/"):
            # B04: старые admin-маршруты закрыты от обычного пользователя;
            # кабинет пользователя (/api/me/*) — любая роль со своей сессией
            raise web.HTTPForbidden(text='{"error":"admin_only"}',
                                    content_type="application/json")
        return await handler(request)

    return middleware


def mount_auth(app, store):
    async def login(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            return web.json_response({"error": "expected_json"}, status=400)
        result = store.login(str(value.get("name", "")), str(value.get("password", "")))
        if result is None:
            await asyncio.sleep(0.3)  # грубый тормоз перебора
            return web.json_response({"error": "invalid_credentials"}, status=401)
        token, session = result
        response = web.json_response({"ok": True, "name": session["name"], "role": session["role"]})
        response.set_cookie(COOKIE, token, httponly=True, samesite="Strict",
                            max_age=SESSION_TTL, path="/")
        return response

    async def logout(request):
        token = request.cookies.get(COOKIE)
        if token:
            store.logout(token)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def whoami(request):
        if not store.enabled:
            # открытый режим локальной лаборатории: вход не требуется
            return web.json_response({"authenticated": True, "role": "admin", "open": True})
        token = request.cookies.get(COOKIE)
        session = store.session(token) if token else None
        if session is None:
            return web.json_response({"authenticated": False}, status=401)
        return web.json_response({"authenticated": True, "name": session["name"],
                                  "role": session["role"]})

    app.router.add_post("/api/auth/login", login)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_get("/api/auth/whoami", whoami)


def main():
    parser = argparse.ArgumentParser(description="B04: bootstrap администратора панели")
    parser.add_argument("--state", default=os.path.join(os.path.dirname(__file__), "state"),
                        help="каталог состояния эмулятора (auth.json)")
    parser.add_argument("--name", default="admin", help="имя администратора")
    parser.add_argument("--password", default=None,
                        help="задать пароль явно (по умолчанию генерируется и печатается один раз)")
    parser.add_argument("--reset", action="store_true",
                        help="принудительно заменить существующего администратора")
    arguments = parser.parse_args()
    from pathlib import Path
    store = AuthStore(Path(arguments.state))
    try:
        _principal, password = store.bootstrap(arguments.name, arguments.password,
                                               reset=arguments.reset)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print("Администратор создан. Имя:", arguments.name)
    print("Пароль (показан один раз):", password)
    print("Панель теперь требует входа:", store.path)


if __name__ == "__main__":
    main()
