"""Сборка встроенных дефолтов /get_sync_info (источник generated) из quasar-server.

Ответ /get_sync_info собирает mapStationInfoToStationSyncInfoResponseConfig в
https://github.com/v3rt3p/quasar-server (MIT, атрибуция в NOTICE.md), commit
fefcd58803012c61f8a9f8c88351dabff1305c81 (src/routers/quasar.yandex.net.ts:345–3159).
Файлы quasar_defaults/ собираются из ответов ОРИГИНАЛЬНОГО кода: dump.ts вызывает этот
сборщик для всех 13 платформ на zod-дефолте quasarConfig.parse({}), а скрипт берёт
common.json из базовой формы и строит platforms/<p>.json — merge-patch от базовой
формы до ответа платформы (null = удалить ключ). Личных значений в дефолте нет;
device_config.name (в оригинале — info.name станции) подменяется токеном {{device.name}}.

Повторение выгрузки (клон и node_modules во временной папке вне репозитория;
в репозиторий попадают только собранные server_emulator/quasar_defaults/*.json):

  git clone --depth 1 https://github.com/v3rt3p/quasar-server <tmp>
  cd <tmp>
  npx pnpm install --ignore-scripts      # постустановочные скрипты чужих пакетов не запускаем
  python tools/build_quasar_defaults.py <tmp> --write-dump
  npx pnpm tsx dump.ts
  python tools/build_quasar_defaults.py <tmp>

--write-dump кладёт в клон dump.ts. Он импортирует приватный сборщик, поэтому функция
извлекается из исходника с добавлением export (reference.ts, нужен для dump.ts):

  { echo "import type { StationInfo } from './src/storage/types'";
    sed -n '345,3159p' src/routers/quasar.yandex.net.ts |
    sed '1s/^function /export function /'; } > reference.ts
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

QUASAR_SERVER_COMMIT = "fefcd58803012c61f8a9f8c88351dabff1305c81"
OUT_DIR = Path(__file__).resolve().parent.parent / "server_emulator" / "quasar_defaults"
BASE_PLATFORM = "yandexstation"  # базовая форма (supported_frames-массив, с hybrid_voice_inputs)
QUIRK_PLATFORMS = ("chiron", "magritte")
# 13 платформ; квирк supported_frames-объект/без hybrid_voice_inputs — chiron и magritte
PLATFORMS = ["chiron", "cucumber", "magritte", "malevich", "monet", "yandexmicro",
             "yandexmidi", "yandexmini", "yandexmini_2", "yandexmodule",
             "yandexmodule_2", "yandexstation", "yandexstation_2"]
NAME_TOKEN = "{{device.name}}"

DUMP_TS = """import { writeFileSync } from 'node:fs'
import { quasarConfig } from './src/storage/types'
import { mapStationInfoToStationSyncInfoResponseConfig } from './reference'

const config = quasarConfig.parse({})
for (const platform of %s) {
  const info = {
    duid: 'test-duid',
    glagolSecurity: { certificate: 'test-token', privateKey: 'test-token' },
    name: 'Станция',
    networkInfo: null,
    platform,
    quasarConfig: config,
  }
  writeFileSync(`reference-${platform}.json`,
    JSON.stringify(mapStationInfoToStationSyncInfoResponseConfig(info), null, 2))
}

writeFileSync('reference-top.json', JSON.stringify({
  alice_pro_subscription: {
    enabled: config.aliceProSubscription.enabled,
    ttl: config.aliceProSubscription.enabled ? config.aliceProSubscription.ttl : undefined,
  },
  subscription: { mode: 'transaction' },
  tags: config.tags,
}, null, 2))
""" % json.dumps(PLATFORMS)


def merge_patch(target, patch):
    """RFC 7396: объекты сливаются, null удаляет ключ. Совпадает с
    server_emulator.quasar_config.merge_patch — здесь своя копия, чтобы скрипт
    собирался без пакета эмулятора."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def patch_of(base, target):
    """Merge-patch base → target: None = нет отличий, null в патче = удалить ключ."""
    if base == target:
        return None
    if isinstance(base, dict) and isinstance(target, dict):
        patch = {}
        for key in set(base) | set(target):
            if key not in target:
                patch[key] = None
            else:
                item = patch_of(base.get(key), target[key])
                if item is not None:
                    patch[key] = item
        return patch or None
    return copy.deepcopy(target)


def load_reference(clone, platform):
    path = clone / f"reference-{platform}.json"
    if not path.exists():
        raise SystemExit(f"{path.name} не найден — запустите dump.ts в клоне (см. docstring)")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--write-dump" in sys.argv:
        if not args or not Path(args[0]).is_dir():
            raise SystemExit("укажите путь к клону quasar-server")
        (Path(args[0]) / "dump.ts").write_text(DUMP_TS, encoding="utf-8")
        print(f"dump.ts записан в {Path(args[0]) / 'dump.ts'}; далее в клоне: npx pnpm tsx dump.ts")
        return
    if not args:
        raise SystemExit("использование: build_quasar_defaults.py <клон-quasar-server> [--write-dump]")
    clone = Path(args[0])
    if not clone.is_dir():
        raise SystemExit(f"клон не найден: {clone}")

    base = load_reference(clone, BASE_PLATFORM)
    # квирк платформ — только chiron/magritte: остальные ответы обязаны совпадать с базой
    for platform in PLATFORMS:
        if platform in (*QUIRK_PLATFORMS, BASE_PLATFORM):
            continue
        if load_reference(clone, platform) != base:
            raise SystemExit(f"ответ платформы {platform} отличается от базовой формы — квирк шире заявленного")

    top = json.loads((clone / "reference-top.json").read_text(encoding="utf-8"))
    common = {"config": {**base, "device_config":
                         {**base["device_config"], "name": NAME_TOKEN}}, **top}
    (OUT_DIR / "platforms").mkdir(parents=True, exist_ok=True)
    write_json(OUT_DIR / "common.json", common)

    for platform in PLATFORMS:
        target = load_reference(clone, platform)
        patch = patch_of(base, target)
        if patch is not None:  # восстановление ответа платформы из патча — контроль
            merged = merge_patch(base, patch)
            if merged != target:
                raise SystemExit(f"патч {platform} не воспроизводит ответ оригинала через merge_patch")
        write_json(OUT_DIR / "platforms" / (platform + ".json"),
                   {"config": patch} if patch is not None else {})

    print(f"quasar-server commit: {QUASAR_SERVER_COMMIT}")
    print(f"common.json: база {BASE_PLATFORM}, name → {NAME_TOKEN}; "
          f"квирк: {', '.join(QUIRK_PLATFORMS)}; остальные платформы: {{}}")


if __name__ == "__main__":
    main()
