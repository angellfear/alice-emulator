"""Источники расширений и общая механика скачивания/распаковки (TASK-029).

parse_source() различает четыре вида источников (docs/contracts/extensions.md):
GitHub-репозиторий (последний релиз или закреплённый тег/коммит через #ref),
прямая zip-ссылка, JSON-каталог {extensions: [...]} и локальная папка (режим
разработки). Сетевые источники — только https; http допускается для loopback
(локальная разработка и тесты на встроенном http-сервере).

download_bytes()/zip_entry_bytes()/extract_zip() — общие помощники сети и zip.
Ими пользуется и перенесённый в расширения реестр сценариев (legacy_registry.py,
TASK-036): правило traversal (safe_zip_name) перенесено дословно, чтобы не дублировать.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientTimeout

# hosts, для которых разрешён http (тесты и локальная разработка — без интернета)
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MAX_CATALOG_BYTES = 1024 * 1024  # лимит манифеста каталога — как у реестра сценариев
MAX_PACKAGE_BYTES = 4 * 1024 * 1024  # лимит пакета реестра сценариев (legacy_registry)


@dataclass(frozen=True)
class GithubSource:
    """https://github.com/<owner>/<repo>[#тег|sha] — релиз через api.github.com или archive/<ref>.zip."""

    url: str  # https://github.com/<owner>/<repo> без fragment
    ref: str | None = None  # закреплённый тег/коммит


@dataclass(frozen=True)
class ZipSource:
    url: str  # прямая ссылка на zip с extension.json в корне


@dataclass(frozen=True)
class CatalogSource:
    url: str  # JSON-индекс {extensions: [{id, url, version, sha256?, description}]}


@dataclass(frozen=True)
class LocalSource:
    path: Path  # режим разработки: без копирования, правки подхватываются


def parse_source(url):
    """URL/путь → конкретный вид источника; неподходящие — ValueError."""
    text = str(url or "").strip()
    if not text:
        raise ValueError("Источник не задан")
    if "://" not in text:  # локальная папка (режим разработки)
        path = Path(text).expanduser()
        if not path.is_dir():
            raise ValueError(f"Локальная папка не найдена: {text}")
        return LocalSource(path=path.resolve())
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
        raise ValueError("Нужен https-URL или локальная папка")
    if parts.scheme != "https" and parts.hostname not in LOOPBACK:
        raise ValueError("Сетевые источники — только https (http — только для localhost)")
    if parts.hostname.removeprefix("www.") == "github.com":
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2:
            raise ValueError("GitHub-источник: https://github.com/<owner>/<repo>[#тег-или-sha]")
        return GithubSource(url=f"https://github.com/{segments[0]}/{segments[1]}",
                            ref=parts.fragment or None)
    if parts.path.endswith(".zip"):
        return ZipSource(url=text)
    return CatalogSource(url=text)


def network_url_allowed(url):
    """https для сетевых источников; http — только loopback (tests/разработка)."""
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)


async def download_bytes(session, url, *, label, limit, timeout=30):
    """GET через общий клиент Lifecycle: статус 200, лимит размера; ошибки aiohttp
    не глотаются (диспетчер отдаёт 502). Схему URL проверяет вызывающий
    (parse_source/network_url_allowed): реестр сценариев разрешает http-реестры."""
    async with session.get(url, timeout=ClientTimeout(total=timeout)) as response:
        if response.status != 200:
            raise ValueError(f"{label} вернул {response.status}")
        payload = await response.read()
    if len(payload) > limit:
        raise ValueError(f"{label} больше {max(1, limit // (1024 * 1024))} МБ")
    return payload


def safe_zip_name(name):
    """Имя элемента архива без явного traversal: не абсолютное, не '..', без диска
    (правило реестра сценариев). Полная гарантия — проверка resolve в extract_zip."""
    return bool(name) and not name.startswith(("/", "..")) and ":" not in name


def zip_entry_bytes(bundle, wanted):
    """Байты файла из открытого zip по базовому имени с защитой от traversal."""
    try:
        entry = next(name for name in bundle.namelist()
                     if safe_zip_name(name) and name.rstrip("/").rsplit("/", 1)[-1] == wanted)
    except StopIteration as exc:
        raise ValueError(f"{wanted}: файл не найден или имя небезопасно") from exc
    return bundle.read(entry)


def extract_zip(payload, dest):
    """Распаковка zip в каталог с защитой от path traversal: небезопасное имя — отказ
    (не молчивый пропуск), цель дополнительно проверяется resolve'ом; общий корневой
    префикс zipball снимается. Возвращает снятый префикс ('' если нет)."""
    try:
        bundle = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Архив повреждён: {exc}") from exc
    names = [n for n in bundle.namelist() if not n.endswith("/")]
    if not names:
        raise ValueError("Архив пуст")
    for name in names:
        if not safe_zip_name(name) or "\\" in name:
            raise ValueError(f"Небезопасный путь в архиве: {name}")
    roots = {n.split("/", 1)[0] for n in names if "/" in n}
    prefix = ""
    if len(roots) == 1 and not any("/" not in n for n in names):
        prefix = next(iter(roots)) + "/"  # zipball/github archive: всё внутри <repo>-<ref>/
    dest = Path(dest).resolve()
    for name in names:
        relative = name[len(prefix):] if prefix and name.startswith(prefix) else name
        if not relative:
            continue
        target = (dest / relative).resolve()
        if dest not in target.parents:
            raise ValueError(f"Путь вне каталога установки: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundle.read(name))
    return prefix


async def github_zip_url(session, source):
    """zipball последнего релиза (api.github.com/releases/latest) или
    github.com/<o>/<r>/archive/<ref>.zip для закреплённого тега/коммита."""
    if source.ref:
        return f"{source.url}/archive/{source.ref}.zip"
    api = source.url.replace("://github.com/", "://api.github.com/repos/", 1) + "/releases/latest"
    async with session.get(api, timeout=ClientTimeout(total=20)) as response:
        if response.status != 200:
            raise ValueError(f"GitHub вернул {response.status} для {api}")
        try:
            data = json.loads(await response.read())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"Ответ GitHub не JSON: {exc}") from exc
    zipball = data.get("zipball_url") if isinstance(data, dict) else None
    if not zipball:
        raise ValueError("У репозитория нет релизов")
    return zipball


async def catalog_entries(session, url):
    """Записи каталога {extensions: [{id, url, version, sha256?, description}]}
    с лимитом 1 МБ; запись без id/url — ошибка (битый каталог не молчит)."""
    raw = await download_bytes(session, url, label="Каталог расширений",
                               limit=MAX_CATALOG_BYTES, timeout=20)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"Каталог не JSON: {exc}") from exc
    from .legacy_registry import is_registry_manifest, registry_entries
    if is_registry_manifest(data):  # прежний реестр сценариев (TASK-036): пакеты — записи каталога
        return registry_entries(data, url)
    entries = data.get("extensions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Каталог не {extensions: [...]}")
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("url"):
            raise ValueError("Запись каталога без id или url")
        result.append({"id": str(entry["id"]), "url": str(entry["url"]),
                       "version": entry.get("version"), "sha256": entry.get("sha256"),
                       "description": str(entry.get("description") or "")})
    return result
