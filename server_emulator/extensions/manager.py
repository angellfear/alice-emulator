"""Менеджер расширений (TASK-029): источники, установка, включение, обновление,
откат, хуки, python-зависимости. Контракт — docs/contracts/extensions.md, схема
манифеста — extensions/schema.json.

Валидация манифеста — ручная (validate_manifest): jsonschema в зависимостях
проекта нет (карточка TASK-028 не добавляла зависимость), структура повторяет
схему дословно, включая запрет неизвестных полей. Диапазоны совместимости —
satisfies(): ^X.Y.Z, ~X.Y.Z, операторы >=, <=, >, <, =, голая версия X.Y как
X.Y.x; условия через пробел/запятую — И.

Состояние живёт в settings["extensions"] (sources/enabled/installed/settings —
дефолт и валидация в settings_service); файлы — state/extensions/<id>/<version>/,
активная версия — атомарный файл current. Хуки hooks.py — через
scripting.ScriptRunner(prefix="extension_hook_<id>"), ошибка хука откатывает
операцию. Самопроверка tests/ — dry-run механизма TASK-023; пока py-сценарии
не подключены (TASK-019), каталоги с *.py в scenarios/ дают предупреждение.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..scripting import ScriptRunner, failure
from ..sdk import SDK_VERSION
from . import EMULATOR_VERSION
from . import legacy_registry
from .context import ExtensionContext, bind_manager
from .sources import (MAX_PACKAGE_BYTES, CatalogSource, GithubSource, LocalSource, catalog_entries,
                      download_bytes, extract_zip, github_zip_url, network_url_allowed,
                      parse_source)

log = logging.getLogger(__name__)

MANIFEST = "extension.json"
HOOK_TIMEOUT = 10.0  # таймаут хука hooks.py (ScriptRunner)
MAX_ZIP_BYTES = 50 * 1024 * 1024  # лимит архива расширения (модели тяжелее — это класс install)
FOLDERS = ("scenarios", "endpoints", "quasar", "voice", "lists", "modes", "mcp", "aapi", "actions",
           "datasets", "music", "animations", "params", "replies", "catalog", "updates", "sounds", "ui")

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
ID_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
FIELD_KEY_RE = re.compile(r"^[a-z0-9_]+$")
FIELD_TYPES = ("text", "password", "number", "select", "textarea", "file")
REQUIRED_FIELDS = ("id", "name", "version", "description", "author", "license", "requires", "permissions")
MANIFEST_FIELDS = set(REQUIRED_FIELDS) | {"homepage", "settings", "contributes"}


# --- версии и диапазоны совместимости -----------------------------------------

def version_key(version):
    """Ключ сравнения semver: пререлиз младше релиза того же triple."""
    match = SEMVER_RE.match(str(version or "").strip())
    if not match:
        raise ValueError(f"Версия {version!r} не semver")
    pre = match[4]
    return (int(match[1]), int(match[2]), int(match[3]), 0 if pre else 1, pre or "")


def _ref_key(text):
    """Операнд диапазона: X.Y достраивается до X.Y.0."""
    parts = str(text).strip().split(".")
    if len(parts) < 3:
        text = ".".join(parts + ["0"] * (3 - len(parts)))
    return version_key(text)


def satisfies(version, constraint):
    """Совместима ли версия с диапазоном: ^X.Y.Z (в рамках major; для 0.Y — в рамках
    minor), ~X.Y.Z (в рамках minor), >=, <=, >, <, =, голая X.Y как X.Y.x; условия
    через пробел или запятую — И; пустой диапазон — любая версия."""
    key = version_key(version)
    text = str(constraint or "").strip()
    if not text:
        return True
    for token in re.split(r"[,\s]+", text):
        if not token:
            continue
        match = re.match(r"^(\^|~|>=|<=|>|<|=)?(.*)$", token)
        op, ref = match[1], match[2].strip()  # op None — голая версия
        if op in ("^", "~"):
            base = _ref_key(ref)
            major, minor, patch = base[:3]
            if op == "^":
                top = (major + 1, 0, 0) if major else ((0, minor + 1, 0) if minor else (0, 0, patch + 1))
            else:
                top = (major, minor + 1, 0)
            if not (key >= base and key[:3] < top):
                return False
        elif op in (">", "<", ">=", "<="):
            ref_key = _ref_key(ref)
            if not {"<": key < ref_key, "<=": key <= ref_key,
                    ">": key > ref_key, ">=": key >= ref_key}[op]:
                return False
        else:  # "=" или голая версия: X.Y.Z — точная, X.Y — любые patch
            base = _ref_key(ref)
            if len(str(ref).split(".")) < 3:
                if not (key >= base and key[:3] < (base[0], base[1] + 1, 0)):
                    return False
            elif key != base:
                return False
    return True


# --- валидация манифеста (schema.json руками, TASK-028) ------------------------

def validate_manifest(data):
    """Ручная проверка extension.json по структуре extensions/schema.json:
    обязательные поля, паттерны, типы разрешений и настроек, запрет неизвестных
    полей (опечатка в permissions не должна превратить расширение в «без разрешений»)."""
    if not isinstance(data, dict):
        raise ValueError("extension.json: нужен JSON-объект")
    unknown = sorted(set(data) - MANIFEST_FIELDS)
    if unknown:
        raise ValueError(f"extension.json: неизвестные поля {unknown}")
    for field in REQUIRED_FIELDS:
        if field not in data:
            raise ValueError(f"extension.json: нет обязательного поля {field}")
    for field in ("id", "name", "version", "description", "author", "license"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"extension.json: {field} — нужна непустая строка")
    if not ID_RE.match(data["id"]):
        raise ValueError("extension.json: id — строчная латиница, цифры, точки, дефисы")
    if not SEMVER_RE.match(data["version"]):
        raise ValueError(f"extension.json: version {data['version']!r} не semver")
    if "homepage" in data and not isinstance(data["homepage"], str):
        raise ValueError("extension.json: homepage — строка")
    requires = data["requires"]
    if not isinstance(requires, dict) or set(requires) - {"emulator", "sdk", "python_packages"}:
        raise ValueError("extension.json: requires — объект emulator/sdk/python_packages")
    for key in ("emulator", "sdk"):
        if not isinstance(requires.get(key), str) or not requires[key].strip():
            raise ValueError(f"extension.json: requires.{key} — строка-диапазон semver")
    packages = requires.get("python_packages", [])
    if not isinstance(packages, list) or not all(isinstance(p, str) and p.strip() for p in packages):
        raise ValueError("extension.json: requires.python_packages — список строк")
    permissions = data["permissions"]
    if not isinstance(permissions, dict) or set(permissions) - {"network", "actions", "device_push", "install", "aapi"}:
        raise ValueError("extension.json: permissions — объект network/actions/device_push/install/aapi")
    if not all(isinstance(h, str) and HOST_RE.match(h) for h in permissions.get("network", [])):
        raise ValueError("extension.json: permissions.network — список хостов")
    if not all(isinstance(a, str) and a.strip() for a in permissions.get("actions", [])):
        raise ValueError("extension.json: permissions.actions — список строк")
    if not all(isinstance(a, str) and a.strip() for a in permissions.get("aapi", [])):
        raise ValueError("extension.json: permissions.aapi — список событий (TASK-059)")
    for key in ("device_push", "install"):
        if not isinstance(permissions.get(key, False), bool):
            raise ValueError(f"extension.json: permissions.{key} — boolean")
    if data.get("settings") is not None:
        validate_settings_schema(data["settings"])
    if data.get("contributes") is not None:
        validate_contributes(data["contributes"])
    return data


def validate_settings_schema(settings):
    """settings: {fields, defaults} — формат fields из voice_stack/packages/*.json."""
    if not isinstance(settings, dict) or set(settings) - {"fields", "defaults"}:
        raise ValueError("extension.json: settings — объект fields/defaults")
    fields = settings.get("fields", [])
    if not isinstance(fields, list) or not isinstance(settings.get("defaults", {}), dict):
        raise ValueError("extension.json: settings.fields — список, defaults — объект")
    for field in fields:
        if not isinstance(field, dict) or set(field) - {"key", "label", "type", "options", "options_from", "when", "step"}:
            raise ValueError("extension.json: поле settings — key/label/type/options/options_from/when/step")
        if not isinstance(field.get("key"), str) or not FIELD_KEY_RE.match(field["key"]) \
                or not isinstance(field.get("label"), str) or not field["label"] \
                or field.get("type") not in FIELD_TYPES:
            raise ValueError("extension.json: полю settings нужны key [a-z0-9_], label и type " + "|".join(FIELD_TYPES))
        if field["type"] == "select" and not isinstance(field.get("options"), list):
            raise ValueError("extension.json: select требует options")


def validate_contributes(contributes):
    """contributes: имя папки → путь или список путей; actions → {prefix}."""
    if not isinstance(contributes, dict) or not contributes:
        raise ValueError("extension.json: contributes — непустой объект")
    for key, value in contributes.items():
        if key == "actions":
            if not isinstance(value, dict) or set(value) - {"prefix"} \
                    or not isinstance(value.get("prefix", "x"), str):
                raise ValueError("extension.json: contributes.actions — объект {prefix}")
        elif not (isinstance(value, str) and value) and not (
                isinstance(value, list) and value and all(isinstance(v, str) and v for v in value)):
            raise ValueError(f"extension.json: contributes.{key} — путь или список путей")


# --- менеджер -------------------------------------------------------------------

class ExtensionManager:
    """Установка/состояние расширений. Сеть — общий клиент Lifecycle; файлы —
    state/extensions/<id>/<version>/ + атомарный файл current; состояние —
    settings["extensions"] (sources, enabled, installed, значения настроек)."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.directory) / "extensions"
        self.loaded = {}  # id → {dir, manifest} — включённые, после load_enabled()
        self.warnings = []  # проблемы включённых при старте (сервер не падает)
        self.source_cache = {}  # url каталога → {ts, entries}: список доступных без сети
        bind_manager(self)  # TASK-053: Executor/SDK находят владельца действия (context.action_context)

    # --- состояние ----------------------------------------------------------------
    def _state(self):
        state = self.runtime.settings.setdefault("extensions", {})
        state.setdefault("sources", [])
        state.setdefault("enabled", {})
        state.setdefault("installed", {})
        state.setdefault("settings", {})
        return state

    async def _save(self):
        await self.runtime.save_settings(dict(self.runtime.settings))

    def _installed(self, ext_id):
        entry = self._state()["installed"].get(ext_id)
        if entry is None:
            raise ValueError(f"Расширение {ext_id} не установлено")
        return entry

    # --- пути ----------------------------------------------------------------------
    def ext_root(self, ext_id):
        return self.root / ext_id

    def version_dir(self, ext_id, version):
        return self.ext_root(ext_id) / str(version)

    def current_file(self, ext_id):
        return self.ext_root(ext_id) / "current"

    def current_version(self, ext_id):
        try:
            version = self.current_file(ext_id).read_text(encoding="utf-8").strip()
            if version:
                return version
        except OSError:
            pass
        return (self._state()["installed"].get(ext_id) or {}).get("version")

    def _switch_current(self, ext_id, version):
        """Атомарное переключение активной версии (tmp + os.replace)."""
        root = self.ext_root(ext_id)
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / "current.tmp"
        temporary.write_text(str(version), encoding="utf-8")
        os.replace(temporary, self.current_file(ext_id))

    def _active_dir(self, ext_id):
        """Активный каталог: локальная папка (dev) или state/extensions/<id>/<version>."""
        entry = self._state()["installed"].get(ext_id) or {}
        if entry.get("dev"):
            path = Path(entry["dev"])
            if not path.is_dir():
                raise ValueError(f"Каталог локального расширения пропал: {path}")
            return path
        version = self.current_version(ext_id)
        if not version:
            raise ValueError(f"У расширения {ext_id} нет активной версии")
        return self.version_dir(ext_id, version)

    def _manifest_dir(self, staging):
        """Каталог с extension.json: корень распаковки или единственная подпапка."""
        if (staging / MANIFEST).is_file():
            return staging
        candidates = [p.parent for p in staging.glob(f"*/{MANIFEST}")]
        if len(candidates) == 1:
            return candidates[0]
        raise ValueError(f"В архиве нет {MANIFEST} в корне или в одной подпапке")

    def _load_manifest(self, ext_id):
        path = self._active_dir(ext_id) / MANIFEST
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"{path}: манифест не читается ({str(exc)[:120]})") from exc
        return validate_manifest(data)

    # --- содержимое -----------------------------------------------------------------
    def _content_info(self, ext_dir, manifest):
        """Класс (data < code < install), список .py, папки, python-зависимости, размер."""
        ext_dir = Path(ext_dir)
        py_files = sorted(p.relative_to(ext_dir).as_posix() for p in ext_dir.rglob("*.py")
                          if "tests" not in p.relative_to(ext_dir).parts)
        folders = sorted(f for f in FOLDERS if (ext_dir / f).is_dir())
        folders += sorted(k for k in (manifest.get("contributes") or {})
                          if k != "actions" and k not in folders)
        if manifest["permissions"].get("install") or (ext_dir / "voice" / "packages").is_dir():
            cls = "install"
        elif py_files or (ext_dir / "mcp").is_dir():
            cls = "code"
        else:
            cls = "data"
        size = sum(p.stat().st_size for p in ext_dir.rglob("*") if p.is_file())
        return {"class": cls, "py_files": py_files, "folders": folders,
                "python_packages": list(manifest["requires"].get("python_packages") or []),
                "size": size}

    def _check_compatibility(self, manifest):
        requires = manifest["requires"]
        for name, actual in (("emulator", EMULATOR_VERSION), ("sdk", SDK_VERSION)):
            constraint = requires.get(name, "")
            if not satisfies(actual, constraint):
                raise ValueError(f"requires.{name}: нужно {constraint}, установлено {actual}")

    def values(self, ext_id, manifest=None):
        """Значения настроек расширения: defaults манифеста + сохранённые."""
        if manifest is None:
            manifest = self._load_manifest(ext_id)
        defaults = (manifest.get("settings") or {}).get("defaults") or {}
        stored = self._state()["settings"].get(ext_id) or {}
        return {**defaults, **stored}

    def context(self, ext_id):
        """ExtensionContext расширения (TASK-053): значения values(ext_id), строки
        вида env:ИМЯ раскрыты через auth.secret (context.py). Новый объект на каждый
        вызов — правка настроек видна коду расширения без рестарта."""
        item = self.loaded.get(ext_id) or {}
        return ExtensionContext(ext_id, self.values(ext_id, item.get("manifest")))

    def action_context(self, name):
        """ExtensionContext расширения-владельца действия name (TASK-053): имя под
        префиксом (contributes.actions.prefix или id) или объявлено в
        permissions.actions — правило регистрации TASK-033; префикс сильнее объявления
        (владеет тот, кто регистрировал). Встроенное/неизвестное — None."""
        for ext_id in sorted(self.loaded):  # префиксные имена
            if name.startswith(str(self.action_prefix(ext_id)).rstrip(".") + "."):
                return self.context(ext_id)
        for ext_id in sorted(self.loaded):  # имена вне префикса, объявленные явно
            if name in (self.loaded[ext_id]["manifest"]["permissions"].get("actions") or []):
                return self.context(ext_id)
        return None

    # --- источники -------------------------------------------------------------------
    async def add_source(self, url):
        parse_source(url)  # валидация вида источника, без сети
        sources = self._state()["sources"]
        if url not in sources:
            sources.append(url)
            await self._save()
        return {"sources": list(sources)}

    async def remove_source(self, url):
        state = self._state()
        if url not in state["sources"]:
            raise ValueError("Источника нет в списке")
        state["sources"] = [s for s in state["sources"] if s != url]
        await self._save()
        return {"sources": list(state["sources"])}

    async def catalog(self, url):
        """Свежий список каталога (сеть) + кеш для GET/сверки обновлений."""
        session = self.runtime.lifecycle.client_session()
        entries = await catalog_entries(session, url)
        self.source_cache[url] = {"ts": time.time(), "entries": entries}
        return entries

    async def _resolve(self, session, url, entry_id):
        """URL → (payload|None, локальный путь|None, sha256, версия из каталога, запись
        прежнего реестра|None). Каталог разворачивается в запись, GitHub — в zipball
        последнего релиза или archive/<ref>.zip. Запись прежнего реестра сценариев
        (TASK-036) — пакет сценариев: ссылка того же origin, что и каталог, лимит 4 МБ;
        install() разворачивает его в data-расширение."""
        source = parse_source(url)
        sha = declared = legacy = None
        if isinstance(source, CatalogSource):
            if not entry_id:
                raise ValueError("Для каталога укажите id расширения")
            entries = await catalog_entries(session, source.url)
            entry = next((e for e in entries if e["id"] == entry_id), None)
            if entry is None:
                raise ValueError(f"В каталоге нет расширения {entry_id}")
            inner = entry["url"]
            if not urlsplit(inner).netloc:  # относительная ссылка — от базы каталога
                inner = source.url.rsplit("/", 1)[0] + "/" + inner
            sha, declared = entry.get("sha256"), entry.get("version")
            if entry.get("legacy"):
                if not legacy_registry.same_origin(source.url, inner):
                    raise ValueError("Пакет с другого origin — запрещено")
                legacy = entry
            source = parse_source(inner)
        if isinstance(source, LocalSource):
            return None, source.path, sha, declared, None
        zip_url = await github_zip_url(session, source) if isinstance(source, GithubSource) else source.url
        if not network_url_allowed(zip_url):  # сетевые источники — только https (http — loopback)
            raise ValueError("Сетевые источники — только https (http — только для localhost)")
        payload = await download_bytes(session, zip_url, label="Пакет сценариев" if legacy else "Архив расширения",
                                       limit=MAX_PACKAGE_BYTES if legacy else MAX_ZIP_BYTES, timeout=120)
        return payload, None, sha, declared, legacy

    # --- установка / обновление --------------------------------------------------------
    async def install(self, spec, confirm=False, require_newer=False):
        """Установка из источника. spec: {url} — zip/GitHub/каталог/локальная папка,
        для каталога + {id}. Класс code/install без confirm=True возвращает карточку
        подтверждения ({requires_confirmation: true, ...}) и ничего не меняет.
        Ошибка хука on_install или самопроверки-исключения — откат установки."""
        url = str(spec.get("url") or spec.get("source") or "")
        if not url:
            raise ValueError("Нужен url расширения или источника")
        session = self.runtime.lifecycle.client_session()
        payload, local_path, sha, declared, legacy = await self._resolve(session, url, spec.get("id"))
        staging = None
        try:
            if local_path is not None:  # режим разработки: без копирования
                ext_dir = local_path
            else:
                if sha and hashlib.sha256(payload).hexdigest() != sha:
                    raise ValueError("sha256 архива не совпадает")
                self.root.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
                if legacy:  # пакет реестра сценариев → data-расширение (TASK-036)
                    legacy_registry.convert_package(payload, legacy, staging)
                else:
                    extract_zip(payload, staging)
                ext_dir = self._manifest_dir(staging)
            try:
                manifest = validate_manifest(json.loads((ext_dir / MANIFEST).read_text(encoding="utf-8")))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"{MANIFEST} не читается или неверен: {str(exc)[:200]}") from exc
            ext_id, version = manifest["id"], manifest["version"]
            self._check_compatibility(manifest)
            if declared and str(declared) != version:
                raise ValueError(f"Версия каталога ({declared}) не совпадает с манифестом ({version})")
            current = (self._state()["installed"].get(ext_id) or {}).get("version")
            if require_newer and current:
                if version == current:
                    raise ValueError(f"В источнике уже установленная версия {current}")
                if version_key(version) <= version_key(current):
                    raise ValueError(f"В источнике нет более новой версии ({version} ≤ {current})")
            info = self._content_info(ext_dir, manifest)
            if info["class"] != "data" and not confirm:
                # код без песочницы и установщики — только с явным подтверждением UI
                return {"requires_confirmation": True, "id": ext_id, "version": version,
                        "name": manifest["name"], "description": manifest["description"],
                        **info, "permissions": manifest["permissions"]}
            if local_path is None:
                target = self.version_dir(ext_id, version)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.rmtree(target)  # переустановка той же версии
                shutil.move(str(ext_dir), str(target))
                ext_dir = target
            previous_entry = dict(self._state()["installed"].get(ext_id) or {})
            previous_current = self.current_version(ext_id) if local_path is None else None
            try:
                await self._hook(ext_id, ext_dir, "on_install", self.values(ext_id, manifest))
                try:
                    check = await self._selfcheck(ext_dir)
                except Exception as exc:  # провал самопроверки — предупреждение, не отказ
                    check = {"status": "warning", "detail": f"Самопроверка не удалась: {str(exc)[:200]}"}
                await self._register(ext_id, manifest, url, spec.get("id"), local_path, check)
            except Exception:
                if local_path is None:  # откат установки: файлы и указатель назад
                    shutil.rmtree(self.version_dir(ext_id, version), ignore_errors=True)
                    if previous_current:
                        self._switch_current(ext_id, previous_current)
                    else:
                        self.current_file(ext_id).unlink(missing_ok=True)
                    if previous_entry:
                        self._state()["installed"][ext_id] = previous_entry
                    else:
                        self._state()["installed"].pop(ext_id, None)
                raise
            return {"installed": ext_id, "version": version, "check": check,
                    "class": info["class"], "folders": info["folders"],
                    "python_packages": info["python_packages"]}
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    async def _register(self, ext_id, manifest, url, entry_id, local_path, check):
        state = self._state()
        old = state["installed"].get(ext_id)
        previous = old.get("version") if old and not old.get("dev") and local_path is None \
            and old.get("version") != manifest["version"] else None
        state["installed"][ext_id] = {
            "version": manifest["version"], "previous": previous,
            "source": "" if local_path is not None else url,
            "source_id": None if local_path is not None else entry_id,
            "dev": str(local_path) if local_path is not None else None,
            "installed_ts": time.time(), "check": check}
        if local_path is None:
            self._switch_current(ext_id, manifest["version"])
        await self._save()

    async def update(self, ext_id, confirm=False):
        """Обновление из источника расширения: версия источника должна быть новее;
        предыдущая версия остаётся на диске для отката."""
        entry = self._installed(ext_id)
        if entry.get("dev"):  # локальная папка: правки подхватываются, версия перечитывается
            manifest = self._load_manifest(ext_id)
            if manifest["version"] != entry["version"]:
                entry["version"] = manifest["version"]
                await self._save()
            return {"updated": ext_id, "version": manifest["version"], "dev": True}
        if not entry.get("source"):
            raise ValueError("У расширения нет источника для обновления")
        return await self.install({"url": entry["source"], "id": entry.get("source_id")},
                                  confirm=confirm, require_newer=True)

    async def rollback(self, ext_id):
        """Откат на предыдущую версию (она хранится на диске); повторный откат
        возвращает на свежую."""
        entry = self._installed(ext_id)
        if entry.get("dev"):
            raise ValueError("Локальная папка не хранит версий")
        previous = entry.get("previous")
        if not previous or not self.version_dir(ext_id, previous).is_dir():
            raise ValueError("Нет предыдущей версии для отката")
        current = entry.get("version")
        self._switch_current(ext_id, previous)
        entry["version"], entry["previous"] = previous, current
        await self._save()
        self.load_enabled()
        return {"rolled_back": ext_id, "version": previous, "previous": current}

    # --- включение / удаление -----------------------------------------------------------
    async def enable(self, ext_id):
        self._installed(ext_id)
        manifest = self._load_manifest(ext_id)
        await self._hook(ext_id, self._active_dir(ext_id), "on_enable", self.values(ext_id, manifest))
        self._state()["enabled"][ext_id] = True
        await self._save()
        self.load_enabled()
        return {"id": ext_id, "enabled": True}

    async def disable(self, ext_id):
        """Выключение: флаг в настройках, файлы не удаляются; всё, что расширение
        добавляет, перестаёт отдаваться (loaded пересобирается)."""
        self._installed(ext_id)
        if self._state()["enabled"].get(ext_id):
            manifest = self._load_manifest(ext_id)
            await self._hook(ext_id, self._active_dir(ext_id), "on_disable", self.values(ext_id, manifest))
            self._state()["enabled"][ext_id] = False
            await self._save()
        self.load_enabled()
        return {"id": ext_id, "enabled": False}

    async def uninstall(self, ext_id):
        """Удаление с файлами (локальная папка не удаляется — снимается подключение)."""
        entry = self._installed(ext_id)
        state = self._state()
        state["installed"].pop(ext_id, None)
        state["enabled"].pop(ext_id, None)
        state["settings"].pop(ext_id, None)
        if not entry.get("dev"):
            shutil.rmtree(self.ext_root(ext_id), ignore_errors=True)
        await self._save()
        self.load_enabled()
        return {"uninstalled": ext_id}

    async def set_values(self, ext_id, values):
        """Значения настроек расширения (схема — settings.fields манифеста)."""
        self._installed(ext_id)
        manifest = self._load_manifest(ext_id)
        if not isinstance(values, dict):
            raise ValueError("values: нужен объект")
        known = {f["key"] for f in (manifest.get("settings") or {}).get("fields") or []}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"Неизвестные поля настроек: {unknown}")
        stored = self._state()["settings"]
        stored[ext_id] = {**stored.get(ext_id, {}), **values}
        await self._save()
        return {"id": ext_id, "values": self.values(ext_id, manifest)}

    async def install_python_packages(self, ext_id, confirm=False):
        """requires.python_packages не ставятся молча: без confirm — список,
        с confirm — pip в текущее окружение (отдельная кнопка UI)."""
        manifest = self._load_manifest(ext_id)
        packages = list(manifest["requires"].get("python_packages") or [])
        if not packages:
            raise ValueError("Расширение не требует python-пакетов")
        if not confirm:
            return {"requires_confirmation": True, "packages": packages}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", *packages,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=600)
        except asyncio.TimeoutError:
            process.kill()
            raise ValueError("pip не уложился в таймаут 600 с")
        if process.returncode != 0:
            raise ValueError("pip завершился с ошибкой: " + output.decode(errors="replace")[-400:])
        return {"installed": packages}

    # --- хуки и самопроверка --------------------------------------------------------------
    async def _hook(self, ext_id, ext_dir, name, *args):
        """Хук hooks.py через ScriptRunner(prefix="extension_hook_<id>") с таймаутом;
        ошибка хука — исключение (установка откатывается)."""
        path = Path(ext_dir) / "hooks.py"
        if not path.is_file():
            return None
        runner = ScriptRunner(prefix=f"extension_hook_{ext_id}", timeout=HOOK_TIMEOUT)
        try:
            return await runner.run(path, *args, function=name)
        except Exception as exc:  # noqa: BLE001 — любой сбой хука откатывает операцию
            raise ValueError(f"Хук {name} расширения {ext_id}: {failure(exc)}") from exc

    async def _selfcheck(self, ext_dir):
        """Самопроверка tests/ (TASK-023): прогон сценариев расширения dry-run'ом
        (record из tools/record_scenario_golden.py — свой runtime, фиксированный мир,
        без сети). Кейсы — tests/cases.json ({сценарий: [фразы]}), иначе вывод из
        триггеров. Пока py-сценарии не подключены (TASK-019), расширения с *.py в
        scenarios/ дают предупреждение — честная деградация, не блокирует установку."""
        scenarios_dir, tests_dir = Path(ext_dir) / "scenarios", Path(ext_dir) / "tests"
        if not scenarios_dir.is_dir():
            return None  # проверять нечего
        if any(scenarios_dir.rglob("*.py")):
            return {"status": "warning",
                    "detail": "Самопроверка недоступна до TASK-019 (в расширении есть py-сценарии)"}
        if not tests_dir.is_dir():
            return None  # самопроверка — только для расширений с tests/ (карточка 029)
        try:
            from tools.record_scenario_golden import record  # механизм TASK-023
        except Exception as exc:  # noqa: BLE001 — tools может отсутствовать в установке
            return {"status": "skipped", "detail": f"Модуль самопроверки недоступен: {exc}"}
        cases = None
        cases_file = Path(ext_dir) / "tests" / "cases.json"
        if cases_file.is_file():
            cases = self._cases_plan(json.loads(cases_file.read_text(encoding="utf-8")))
        result = await record(catalog_dir=scenarios_dir, cases=cases)
        total = sum(len(entry["cases"]) for entry in result["scenarios"])
        missed = [case["name"] for entry in result["scenarios"] for case in entry["cases"]
                  if not all(run.get("handled") for run in case["runs"])]
        if missed:
            return {"status": "warning",
                    "detail": f"Самопроверка: не обработано {len(missed)} из {total} кейсов "
                              f"({', '.join(missed[:5])})"}
        return {"status": "ok", "detail": f"Самопроверка пройдена: {total} кейсов"}

    @staticmethod
    def _cases_plan(data):
        """tests/cases.json → план [(scenario_id, [case, …])]: объект
        {сценарий: ["фраза" | {phrase, answer?}]} или список {scenario, phrase}."""
        def case(item, index):
            if isinstance(item, str):
                return {"name": f"case{index}", "kind": "text", "phrase": item}
            if isinstance(item, dict) and item.get("phrase"):
                entry = {"name": str(item.get("name") or f"case{index}"), "kind": "text",
                         "phrase": str(item["phrase"])}
                if item.get("answer"):
                    entry["answer"] = str(item["answer"])
                return entry
            raise ValueError("Кейс без phrase")

        if isinstance(data, dict):
            return [(str(sid), [case(item, i) for i, item in enumerate(items or [])])
                    for sid, items in data.items()]
        if isinstance(data, list):
            grouped = {}
            for item in data:
                if not isinstance(item, dict) or not item.get("phrase"):
                    raise ValueError("tests/cases.json: нужен объект {сценарий: [фразы]} "
                                     "или список {scenario, phrase}")
                grouped.setdefault(str(item.get("scenario") or ""), []).append(case(item, len(grouped)))
            return list(grouped.items())
        raise ValueError("tests/cases.json: нужен объект {сценарий: [фразы]} или список {scenario, phrase}")

    # --- загрузка включённых и доступ другим точкам подключения (TASK-030+) ----------------
    def load_enabled(self):
        """Скан включённых расширений (старт и после включения/выключения): manifest
        и активный каталог, без сети и хуков. Битое расширение — в self.warnings.
        После скана — пересборка реестров действий/датасетов (TASK-033); до создания
        Executor в runtime (начало __init__) пересборка сама ничего не делает."""
        self.loaded, self.warnings = {}, []
        state = self._state()
        for ext_id in sorted(state["installed"]):
            if not state["enabled"].get(ext_id):
                continue
            try:
                manifest = self._load_manifest(ext_id)
                self.loaded[ext_id] = {"dir": str(self._active_dir(ext_id)), "manifest": manifest}
            except Exception as exc:  # noqa: BLE001 — расширение не должно ронять сервер
                self.warnings.append({"id": ext_id, "error": str(exc)[:200]})
                log.warning("Расширение %s не загружено: %s", ext_id, exc)
        rebuild = getattr(self.runtime, "rebuild_extension_registries", None)
        if callable(rebuild):
            rebuild()

    def contribution_dirs(self, name):
        """Каталоги включённых расширений для точки подключения TASK-030+:
        [(ext_id, Path)] — папка name или переопределение contributes[name].
        «actions» — не путь, а объявление префикса ({prefix}, TASK-033): всегда
        папка actions/ (схема манифеста другого вида contributes.actions не знает)."""
        result = []
        for ext_id, item in self.loaded.items():
            root = Path(item["dir"])
            override = None if name == "actions" else (item["manifest"].get("contributes") or {}).get(name)
            paths = [root / p for p in (override if isinstance(override, list) else [override])] \
                if override else [root / name]
            result.extend((ext_id, p) for p in paths if p.is_dir())
        return result

    def action_prefix(self, ext_id):
        """Префикс имён элементов расширения: contributes.actions.prefix или <id>."""
        manifest = (self.loaded.get(ext_id) or {}).get("manifest")
        if manifest is None:
            try:
                manifest = self._load_manifest(ext_id)
            except Exception:  # noqa: BLE001
                return ext_id
        return str((manifest.get("contributes") or {}).get("actions", {}).get("prefix") or ext_id)

    # --- карточка для UI -------------------------------------------------------------------
    def listing(self):
        """Список установленных расширений + источники и кеш каталогов. Доступное
        обновление — из последнего освежённого каталога (GET сети не делает)."""
        state = self._state()
        available = {}
        for cached in self.source_cache.values():
            for entry in cached["entries"]:
                available[entry["id"]] = entry
        items = []
        for ext_id, entry in sorted(state["installed"].items()):
            card = {"id": ext_id, "version": entry.get("version"), "enabled": bool(state["enabled"].get(ext_id)),
                    "source": entry.get("source"), "source_id": entry.get("source_id"),
                    "dev": entry.get("dev"), "previous": entry.get("previous"),
                    "installed_ts": entry.get("installed_ts"), "check": entry.get("check")}
            try:
                manifest = self._load_manifest(ext_id)
                info = self._content_info(self._active_dir(ext_id), manifest)
                card.update({"name": manifest["name"], "description": manifest["description"],
                             "author": manifest["author"], "license": manifest["license"],
                             "class": info["class"], "permissions": manifest["permissions"],
                             "folders": info["folders"], "python_packages": info["python_packages"],
                             "settings": {"fields": (manifest.get("settings") or {}).get("fields") or [],
                                          "values": self.values(ext_id, manifest)}})
                newer = available.get(ext_id)
                if newer and newer.get("version"):
                    try:
                        if version_key(str(newer["version"])) > version_key(str(card["version"])):
                            card["update"] = {"version": newer["version"], "url": newer["url"]}
                    except ValueError:
                        pass
            except Exception as exc:  # noqa: BLE001 — битое расширение видно в списке с ошибкой
                card["error"] = str(exc)[:200]
            items.append(card)
        return {"extensions": items, "sources": list(state["sources"]),
                "catalog": [entry for cached in self.source_cache.values() for entry in cached["entries"]],
                "sdk": SDK_VERSION, "emulator": EMULATOR_VERSION}
