"""Проверка релизов через GitHub Releases API (TASK-040): версия и заметки, без скачивания кода.

stable — GET /releases/latest (пререлизы GitHub сам не отдаёт); beta — GET /releases (первый
не-черновик). Заголовки — по документации REST API: Accept, X-GitHub-Api-Version, User-Agent;
ETag/If-None-Match — 304 Not Modified вместо повторной выдачи. Приватный репозиторий без токена
отвечает 404 — это «репозиторий недоступен», а не ошибка сети.
"""
from aiohttp import ClientSession, ClientTimeout

API = "https://api.github.com"  # тесты подменяют на локальный сервер
TIMEOUT = ClientTimeout(total=20)


def headers(token="", etag=None):
    result = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
              "User-Agent": "alica-emule"}
    if token:
        result["Authorization"] = "Bearer " + token
    if etag:
        result["If-None-Match"] = etag
    return result


def _release(data):
    """Запись API → короткая сводка релиза: tag_name без «v», заметки как есть (markdown не исполняется)."""
    return {"version": (data.get("tag_name") or "").removeprefix("v"),
            "notes": data.get("body") or "", "prerelease": bool(data.get("prerelease")),
            "published_at": data.get("published_at") or "", "url": data.get("html_url") or ""}


async def latest(repo, token="", channel="stable", etag=None):
    """Последний релиз: {"release": …|None, "etag": …}. None — 304 (не менялся с прошлого раза).
    LookupError — репозиторий недоступен (приватный) или релизов нет; остальное — ошибка сети."""
    url = f"{API}/repos/{repo}/releases/latest" if channel == "stable" else f"{API}/repos/{repo}/releases"
    async with ClientSession(timeout=TIMEOUT) as client:
        async with client.get(url, headers=headers(token, etag)) as response:
            if response.status == 304:
                return {"release": None, "etag": etag}
            if response.status == 404:
                raise LookupError("репозиторий недоступен (приватный? задайте токен в настройках)")
            response.raise_for_status()
            data = await response.json()
            if channel != "stable":
                data = next((item for item in data if not item.get("draft")), None)
                if data is None:
                    raise LookupError("релизов пока нет")
            return {"release": _release(data), "etag": response.headers.get("ETag")}
