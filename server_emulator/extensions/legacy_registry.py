"""Переход реестра пакетов сценариев в каталог расширений (TASK-036).

Прежний реестр — манифест ``scenario-registry`` (``{format, packages:[{id, version, name,
path?, url, sha256?}]}``) и пакеты ``scenario-package`` (JSON или zip с ``package.json``) —
больше не отдельный механизм. Здесь три вещи:

* ``registry_entries`` — манифест реестра читается каталогом расширений (``catalog_entries``)
  как обычный источник-каталог: пакеты становятся записями каталога;
* ``convert_package`` — установка такой записи: пакет сценариев разворачивается в
  data-расширение (``extension.json`` + ``scenarios/<раздел>/<id>.json``), дальше всё как у
  любого расширения (включение, обновление, откат, удаление);
* ``migrate_registry`` — миграция настроек: ``settings.registry.url`` → источник в
  ``settings.extensions.sources``, установленные пакеты → расширения (по одному на пакет).

Скачивание, лимиты, sha256 и защита zip — общий код ``extensions/sources.py`` и менеджера;
модуль только преобразует формат. Подраздел пакета (``path``/``_group``) в слое расширений не
поддержан: сценарий попадает в раздел по ``category``.
"""
from __future__ import annotations

import json
import logging
import re
import time
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


def migrate_registry(runtime):
    """Миграция настроек прежнего реестра (TASK-036). Вызывается Runtime после создания
    менеджера расширений, до load_enabled.

    * registry.url → extensions.sources (если источник допустим: https или loopback);
    * каждый пакет из registry.installed → расширение класса data: версии из журнала
      раскладываются по state/extensions/<id>/<версия>/, активная — последняя, предыдущая
      (если была) доступна для отката; журнал откатов сохраняется, пока возможно;
    * нетронутые копии сценариев пакета (совпадают с эталоном и включены) убираются из
      пользовательских — теперь их отдаёт расширение; изменённые и выключенные остаются
      и перекрывают сценарии расширения (пользовательское > расширение);
    * ключ registry удаляется. Повторный запуск ничего не делает.

    Возвращает отчёт {source, extensions, removed, skipped} или None, если мигрировать нечего."""
    settings = runtime.settings
    if "registry" not in settings:
        return None
    from .sources import parse_source
    registry = settings.get("registry") if isinstance(settings.get("registry"), dict) else {}
    manager = runtime.extensions
    state = manager._state()
    report = {"source": None, "extensions": [], "removed": [], "skipped": []}

    url = str(registry.get("url") or "").strip()
    if url:
        try:
            parse_source(url)
        except ValueError as exc:
            report["skipped"].append(f"источник {url}: {exc}")
            log.warning("URL реестра сценариев не перенесён в источники расширений: %s", exc)
        else:
            if url not in state["sources"]:
                state["sources"].append(url)
            report["source"] = url

    grouped = {}
    for item in registry.get("installed") or []:
        if isinstance(item, dict) and item.get("id") and isinstance(item.get("snapshot"), list):
            grouped.setdefault(str(item["id"]), []).append(item)
    scenarios = settings.get("scenarios") or []
    for package_id, entries in grouped.items():
        ext_id = extension_id(package_id)
        if ext_id in state["installed"]:
            report["skipped"].append(f"{package_id}: расширение {ext_id} уже есть")
            continue
        entries.sort(key=lambda h: h.get("installed_ts") or 0)
        versions = {}  # semver → запись журнала (повтор версии — поздняя)
        for entry in entries:
            versions[normalize_version(entry.get("version"))] = entry
        written = {}
        for version, entry in versions.items():
            try:
                written[version] = write_tree(manager.version_dir(ext_id, version), ext_id, package_id,
                                              version, entry["snapshot"])
            except ValueError as exc:
                report["skipped"].append(f"{package_id} {version}: {exc}")
        if not written:
            continue
        order = [v for v in versions if v in written]
        current, previous = order[-1], (order[-2] if len(order) > 1 else None)
        state["installed"][ext_id] = {
            "version": current, "previous": previous, "source": url if report["source"] else "",
            "source_id": ext_id, "dev": None,
            "installed_ts": versions[current].get("installed_ts") or time.time(), "check": None}
        state["enabled"][ext_id] = True
        manager._switch_current(ext_id, current)
        snapshot = {s["id"]: _definition(s) for s in versions[current]["snapshot"]
                    if isinstance(s, dict) and s.get("id")}
        kept = []
        for scenario in scenarios:
            reference = snapshot.get(scenario.get("id")) if isinstance(scenario, dict) else None
            if reference is not None and scenario.get("enabled") is not False                     and _definition(scenario) == reference:
                report["removed"].append(scenario["id"])
                continue
            kept.append(scenario)
        scenarios = kept
        report["extensions"].append(ext_id)
    settings["scenarios"] = scenarios
    settings.pop("registry", None)
    runtime.write("settings.json", settings)
    return report
