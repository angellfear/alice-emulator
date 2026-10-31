"""Генератор HTTP-каталога сценариев (S-публикация).

Читает пользовательские сценарии из state-БД эмулятора и складывает готовый
к реестру каталог: manifest.json (индекс: названия, ссылки, sha256, разделы path)
+ package.json на сценарий (или zip-бандл с --zip). Полученный каталог можно
раздавать любым статик-сервером (python -m http.server) и указать его URL
в «Сценариях → Реестр».

Usage:
  python tools/make_registry.py <state-dir> <out-dir> [--zip]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import zipfile
from pathlib import Path


def load_scenarios(state_dir: Path):
    """Тела сценариев: файлы state/scenarios/*.json — источник правды (RF-08,
    ревизии настроек в БД хранят только заглушки {id, enabled}). Для legacy
    state без каталога файлов — последняя ревизия БД (там тела ещё полные)."""
    scenario_dir = state_dir / "scenarios"
    if scenario_dir.is_dir():
        files = sorted(scenario_dir.glob("*.json"))
        if files:
            loaded = []
            for path in files:
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue  # битый файл пропускаем, остальные работают
                if isinstance(item, dict) and item.get("id"):
                    loaded.append(item)
            return loaded
    db = state_dir / "emulator.sqlite3"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = conn.execute("SELECT payload FROM settings ORDER BY revision DESC LIMIT 1").fetchone()
    conn.close()
    settings = json.loads(row[0]) if row else {}
    return [s for s in settings.get("scenarios", []) if isinstance(s, dict)
            and ("triggers" in s or "flow" in s)]  # заглушки ревизий — не сценарии


def clean_scenario(scenario):
    return {k: v for k, v in scenario.items() if not k.startswith("_") and k != "enabled"}


def build(state_dir: Path, out_dir: Path, as_zip: bool):
    out_dir.mkdir(parents=True, exist_ok=True)
    scenarios = load_scenarios(state_dir)
    packages = []
    for scenario in scenarios:
        clean = clean_scenario(scenario)
        package = {"manifest": {"format": "scenario-package", "version": 1}, "scenarios": [clean]}
        package_id = clean["id"]
        if as_zip:
            name = package_id + ".zip"
            inner = json.dumps(package, ensure_ascii=False, indent=1).encode()
            import io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as bundle:
                bundle.writestr("package.json", inner)
            blob = buf.getvalue()
        else:
            name = package_id + ".package.json"
            blob = json.dumps(package, ensure_ascii=False, indent=1).encode()
        (out_dir / name).write_bytes(blob)
        packages.append({"id": package_id, "version": clean.get("version", "1.0.0"),
                         "name": clean.get("name") or package_id,
                         "path": scenario.get("_group") or "",
                         "url": name, "sha256": hashlib.sha256(blob).hexdigest()})
    manifest = {"format": "scenario-registry", "version": 1, "packages": packages}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path, help="каталог state эмулятора (с emulator.sqlite3)")
    parser.add_argument("out_dir", type=Path, help="выходной каталог каталога")
    parser.add_argument("--zip", action="store_true", help="пакеты zip-бандлами")
    args = parser.parse_args(argv)
    manifest = build(args.state_dir, args.out_dir, args.zip)
    print(f"manifest: {len(manifest['packages'])} пакет(ов) → {args.out_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
