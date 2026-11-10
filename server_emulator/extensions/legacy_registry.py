"""Реестр пакетов сценариев как источник-каталог расширений.

Прежний реестр — манифест ``scenario-registry`` (``{format, packages:[{id, version, name,
path?, url, sha256?}]}``) и пакеты ``scenario-package`` (JSON или zip с ``package.json``) —
больше не отдельный механизм, а обычный источник-каталог. Здесь:

* ``registry_entries`` — манифест реестра читается каталогом расширений (``catalog_entries``)
  как обычный источник-каталог: пакеты становятся записями каталога;
* ``convert_package`` — установка такой записи: пакет сценариев разворачивается в
  data-расширение (``extension.json`` + ``scenarios/<раздел>/<id>.json``), дальше всё как у
  любого расширения (включение, обновление, откат, удаление).

Скачивание, лимиты, sha256 и защита zip — общий код ``extensions/sources.py`` и менеджера;
модуль только преобразует формат. Подраздел пакета (``path``/``_group``) в слое расширений не
поддержан: сценарий попадает в раздел по ``category``.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
import io
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

REGISTRY_FORMAT = "scenario-registry"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
NUMERIC = re.compile(r"^\d+(?:\.\d+){0,3}$")
ID_SAFE = re.compile(r"[^a-z0-9.-]+")


def is_registry_manifest(data):
    return isinstance(data, dict) and data.get("format") == REGISTRY_FORMAT


def normalize_version(version):
    """Версия пакета реестра → semver для манифеста расширения: «1» → 1.0.0, «1.2» → 1.2.0,
    «1.2.3.4» → 1.2.3; нечисловая — 0.0.1."""
    text = str(version or "").strip()
    if SEMVER.match(text):
        return text
    if NUMERIC.match(text):
        parts = (text.split(".") + ["0", "0"])[:3]
        return ".".join(parts)
    return "0.0.1"


def extension_id(package_id):
    """id пакета → id расширения (строчная латиница, цифры, точки, дефисы)."""
    text = ID_SAFE.sub("-", str(package_id or "").lower()).strip(".-")
    return text or "scenarios"


def registry_entries(manifest, base_url):
    """Манифест реестра → записи каталога расширений. Относительные ссылки пакетов —
    от базы реестра; origin абсолютной ссылки проверяет менеджер при установке."""
    packages = manifest.get("packages")
    if not isinstance(packages, list):
        raise ValueError("Манифест реестра: нужен список packages")
    entries = []
    for package in packages:
        if not isinstance(package, dict) or not package.get("id") or not package.get("url"):
            raise ValueError("Запись реестра без id или url")
        link = str(package["url"])
        if not urlsplit(link).netloc:
            link = base_url.rsplit("/", 1)[0] + "/" + link
        entries.append({"id": extension_id(package["id"]), "url": link,
                        "version": normalize_version(package.get("version")),
                        "sha256": package.get("sha256"),
                        "description": str(package.get("name") or package["id"]),
                        "legacy": REGISTRY_FORMAT, "package_id": str(package["id"])})
    return entries


def same_origin(base_url, link):
    return urlsplit(base_url).netloc == urlsplit(link).netloc


def manifest_for(ext_id, name, version):
    """extension.json data-расширения из пакета сценариев."""
    return {"id": ext_id, "name": name or ext_id, "version": version,
            "description": f"Пакет сценариев из реестра: {name or ext_id}",
            "author": "Реестр сценариев", "license": "NOASSERTION",
            "requires": {"emulator": ">=0.0.0", "sdk": ">=1.0.0"}, "permissions": {}}


def write_tree(dest, ext_id, name, version, scenarios):
    """Дерево расширения: extension.json + scenarios/<category>/<id>.json (определение без
    служебных ключей и без enabled). Возвращает список записанных id."""
    from ..scenario_packages.validate import PackageError, validate_scenario
    dest = Path(dest)
    written = []
    for scenario in scenarios:
        try:
            validate_scenario(scenario)
        except PackageError as exc:
            log.warning("Сценарий %s не перенесён в расширение %s: %s", scenario.get("id"), ext_id, exc)
            continue
        clean = _definition(scenario)
        folder = dest / "scenarios" / clean["category"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{clean['id']}.json").write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(clean["id"])
    if not written:
        raise ValueError("В пакете нет ни одного пригодного сценария")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "extension.json").write_text(
        json.dumps(manifest_for(ext_id, name, version), ensure_ascii=False, indent=2), encoding="utf-8")
    return written


def convert_package(payload, entry, dest):
    """Скачанный пакет реестра (JSON или zip-бандл с package.json) → data-расширение в dest.
    Формат пакета проверяет validate_package (как раньше при установке из реестра)."""
    from ..scenario_packages.validate import PackageError, validate_package
    from .sources import MAX_PACKAGE_BYTES, zip_entry_bytes
    if len(payload) > MAX_PACKAGE_BYTES:
        raise ValueError(f"Пакет больше {MAX_PACKAGE_BYTES // (1024 * 1024)} МБ")
    if payload[:2] == b"PK":  # zip-бандл: package.json внутри, имя проверяется на traversal
        try:
            payload = zip_entry_bytes(zipfile.ZipFile(io.BytesIO(payload)), "package.json")
        except (ValueError, KeyError, RuntimeError, OSError, zipfile.BadZipFile) as exc:
            raise ValueError("Повреждённый zip-бандл: " + str(exc)[:80]) from exc
    try:
        package = validate_package(json.loads(payload))
    except (PackageError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Пакет сценариев неверен: " + str(exc)[:200]) from exc
    return write_tree(dest, entry["id"], entry.get("description"), entry["version"], package["scenarios"])


def _definition(scenario):
    """Определение без служебных ключей и enabled — то, что лежит в файле расширения."""
    clean = {k: v for k, v in scenario.items() if not str(k).startswith("_") and k != "enabled"}
    clean.setdefault("category", "custom")
    return clean
