"""Добыча файлов анимаций с колонки и импорт в хранилище проектов эмулятора.

Файлы Яндекса в репозиторий не кладутся: скрипт берёт их у пользователя и импортирует
в state/animations/<платформа>/ (проекты раздела «Анимации»).

Источники файлов (на выбор):
  adb   — `adb pull <каталог на колонке> <локальная папка>`; каталог указывает пользователь
          (найти: `adb shell find / -name "*.scled" -o -name "*.led" 2>/dev/null`);
  OTA   — распакованный образ обновления (папка с файлами прошивки).

Примеры:
  python tools/import_animations.py pull --remote /system/data/ledpatterns --dest artifacts/led
  python tools/import_animations.py import --platform yandexmini_2 --kind ring artifacts/led
  python tools/import_animations.py import --platform yandexmini_2 --kind clock artifacts/clock
  python tools/import_animations.py import --platform yandexstation_2 --kind frontal artifacts/frontal

Расширение файла должно подходить дисплею: ring/panel/oknix — .led, clock — .scled,
frontal — .gif (25×16). Неподходящие и неразобранные файлы пропускаются с причиной.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server_emulator.animation_storage import AnimationProjectStore  # noqa: E402

EXTENSIONS = {"ring": ".led", "panel": ".led", "oknix": ".led", "clock": ".scled", "frontal": ".gif"}


def project_id(source: Path, root: Path, taken: set[str]) -> str:
    relative = source.relative_to(root).with_suffix("")
    base = re.sub(r"[^a-z0-9_.-]+", "-", "-".join(relative.parts).lower()).strip("-.")[:56] or "import"
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        candidate = f"{base}-{number}"
    taken.add(candidate)
    return candidate


def run_import(args) -> int:
    root = Path(args.source).resolve()
    if not root.is_dir():
        print(f"Нет папки: {root}", file=sys.stderr)
        return 2
    store = AnimationProjectStore(Path(args.state) / "animations")
    extension = EXTENSIONS[args.kind]
    taken = {p["id"] for p in store.list(args.platform)}
    done = skipped = 0
    for source in sorted(root.rglob("*")):
        if not source.is_file() or source.suffix.lower() != extension:
            continue
        identifier = project_id(source, root, taken)
        try:
            store.import_file(args.platform, identifier, source.stem, args.kind, source.read_bytes(), source.name)
            done += 1
        except ValueError as exc:
            taken.discard(identifier)
            skipped += 1
            print(f"пропущен {source.relative_to(root)}: {exc}", file=sys.stderr)
    print(f"Импортировано: {done}, пропущено: {skipped} → {Path(args.state) / 'animations' / args.platform}")
    return 0 if done else 1


def run_pull(args) -> int:
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    command = ["adb"] + (["-s", args.serial] if args.serial else []) + ["pull", args.remote, str(dest)]
    return subprocess.call(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    pull = sub.add_parser("pull", help="скачать каталог с колонки по adb")
    pull.add_argument("--remote", required=True, help="каталог на колонке")
    pull.add_argument("--dest", required=True, help="локальная папка")
    pull.add_argument("--serial", help="адрес колонки для adb -s (например 192.0.2.10:5555)")
    pull.set_defaults(run=run_pull)
    imp = sub.add_parser("import", help="импортировать файлы папки в хранилище проектов")
    imp.add_argument("source", help="папка с файлами (после adb pull или распакованная OTA)")
    imp.add_argument("--platform", required=True, help="yandexstation_2, yandexmidi, yandexmini_2, yandexstation, yandexmini")
    imp.add_argument("--kind", required=True, choices=sorted(EXTENSIONS))
    imp.add_argument("--state", default=str(Path(__file__).resolve().parents[1] / "state"), help="папка state эмулятора")
    imp.set_defaults(run=run_import)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
