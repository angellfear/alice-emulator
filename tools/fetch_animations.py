"""Выгрузка LED-анимаций с static-alice.s3.yandex.net в каталог раздела.

Семена — капчи 24.10 + разведка типовых имён (emotions v1/v2/v3, погода,
плеер). Повторный запуск идемпотентен: скачанное не трогает, новые URL
дописывает. Сеть недоступна — только отчёт, записи остаются URL-заглушками.

Запуск: python tools/fetch_animations.py [--probe]  (каталог рантайма: server_emulator/state/animations)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server_emulator.animations import YANDEX_LED  # noqa: E402

EMOTIONS = ["joy", "wink", "sad", "love", "angry", "surprise", "animate_in",
            "animate_out", "neutral", "sleep", "thinking", "listen"]
SCENES = ["weather/clear/night", "weather/clear/day", "weather/rain/day",
          "weather/snow/day", "player_play", "player_pause"]


def urls():
    seen = set()
    for version in ("v1", "v2", "v3"):
        for name in EMOTIONS:
            seen.add(f"emotions/{version}/{name}.gif")
    for scene in SCENES:
        seen.add(f"animations/{scene}")
    return sorted(seen)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="server_emulator/state/animations")
    parser.add_argument("--probe", action="store_true", help="только проверить доступность")
    args = parser.parse_args()
    root = Path(args.root)
    catalog = root / "catalog.json"
    data = json.loads(catalog.read_text(encoding="utf-8")) if catalog.is_file() else {"animations": []}
    entries = data.setdefault("animations", [])
    by_url = {a.get("url"): a for a in entries}
    stats = {"downloaded": 0, "kept": 0, "new": 0, "failed": 0}
    for tail in urls():
        url = YANDEX_LED + tail
        existing = by_url.get(url)
        if existing and existing.get("file"):
            stats["kept"] += 1
            continue
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except Exception:
            stats["failed"] += 1
            continue
        if len(raw) > 4 * 1024 * 1024 or not raw.startswith(b"GIF8"):
            stats["failed"] += 1
            continue
        name = re.sub(r"[^a-z0-9_.-]", "_", tail) + ".gif"
        (root / "media").mkdir(parents=True, exist_ok=True)
        (root / "media" / name).write_bytes(raw)
        entry = {"id": "yandex." + re.sub(r"[^a-z0-9_.-]", "_", tail), "title": tail,
                 "models": [], "url": url, "file": name,
                 "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                 "source": "download", "added_ts": time.time()}
        if existing:
            entries[entries.index(existing)] = entry
        else:
            entries.append(entry)
        stats["downloaded" if not existing else "kept"] += 1
        if not existing:
            stats["new"] += 1
        print(("+" if not existing else "~"), tail, len(raw), "bytes")
    if not args.probe:
        temporary = catalog.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(catalog)
    print("итог:", stats)


if __name__ == "__main__":
    main()
