"""Восстановление HTTP-эндпоинтов из capture-базы: tools/endpoints_recover.py.

Для каждого маршрута, получавшего от эмулятора 40* (или по явному --host/--method/--path),
ищет последний РЕАЛЬНЫЙ ответ облака (status 200, не эмулированный), извлекает тело
из payloads/blocks (zstd-блоки, gzip/deflate-тела распаковываются) и пишет файл
state/endpoints/<id>.json — каталог «Эндпоинты» подхватывает его без перезапуска
(файлы читаются на старте; после записи каталог обновите в UI или перезапустите сервер).

--sync-configs: для каждого устройства реестра берёт последний реальный ответ
/get_sync_info этого устройства и заливает config/tags/glagol/subscription в
state.quasar через admin-API — лечит 409 device_config_not_set.

Эмулятор остаётся отделённым от capture-базы: утилита — внешний источник файлов.
"""
import argparse
import base64
import json
import sqlite3
import sys
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_KEYS = ("config", "tags", "glagol", "subscription", "alice_pro_subscription")

# Маршруты builtin Quasar с stateful-ответом: реплей их не достигает и не нужен
# (токен выдаётся эмулятором; sync/capability/state — зависят от устройства).
# device_list/check_token без маркеров вызывающего builtin сознательно пропускает
# каталогу (quasar.py: device is None → None; не-JSON capability → None).
BUILTIN_ROUTES = {("quasar.yandex.net", "GET", "/glagol/token"),
                                    ("quasar.yandex.net", "GET", "/get_sync_info"),
                  ("quasar.yandex.net", "GET", "/check_updates"),
                  ("quasar.yandex.net", "GET", "/get_sync_info"),
                                                      ("quasar.yandex.net", "POST", "/update_device_state"),
                  }


def open_db(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def block_bytes(db, event_id, role="response_original"):
    row = db.execute(
        "SELECT p.offset, p.size, b.data, b.raw_size FROM event_payloads ep "
        "JOIN payloads p ON p.hash=ep.hash JOIN blocks b ON b.id=p.block_id "
        "WHERE ep.event_id=? AND ep.role=?", (event_id, role)).fetchone()
    if row is None:
        return None
    off, size, blob, raw_size = row
    if blob is None:
        return None
    if len(blob) != raw_size:  # блок сжат zstd целиком
        import zstandard
        blob = zstandard.ZstdDecompressor().decompress(blob, max_output_size=64 * 1024 * 1024)
    return blob[off:off + size]


def response_body(db, event_id, headers):
    data = block_bytes(db, event_id)
    if data is None:
        return None
    encoding = ""
    for key, value in headers or []:
        if key.lower() == "content-encoding":
            encoding = (value or "").lower()
    try:
        if "gzip" in encoding:
            data = zlib.decompressobj(31).decompress(data, 64 * 1024 * 1024)
        elif "deflate" in encoding:
            data = zlib.decompress(data)
    except zlib.error:
        return None  # битое сжатие — пропускаем, возьмём другой захват
    return data


def header_value(headers, name):
    for key, value in headers or []:
        if key.lower() == name.lower():
            return value
    return None


def latest_real(db, host, method, path, want_json=False):
    """Последний реальный (не эмулированный) 200-ответ на host+method+path.
    Возвращает (event_id, status, content_type, body_bytes) или None."""
    rows = db.execute(
        "SELECT id, ts, status, detail, url FROM events WHERE kind='http' AND host=? AND method=? "
        "AND json_extract(detail,'$.emulated') IS NULL AND status>=200 AND status<300 AND url LIKE ? "
        "ORDER BY ts DESC LIMIT 40", (host, method, path + "%")).fetchall()
    for event_id, _ts, status, detail, url in rows:
        if url.split("?")[0] != path:
            continue
        try:
            parsed = json.loads(detail)
        except ValueError:
            continue
        headers = (parsed.get("headers") or {}).get("response_original")
        body = response_body(db, event_id, headers)
        if body is None:
            continue
        if want_json:
            try:
                json.loads(body)
            except ValueError:
                continue
        return event_id, status, header_value(headers, "content-type") or "application/octet-stream", body
    return None


def endpoint_id(host, path):
    tail = path.strip("/").replace("/", ".").replace(" ", "_")
    return f"{host.lower()}.{tail}" if tail else f"{host.lower()}._root"


def alias_map(base):
    """Алиасы настроек эмулятора: алиас → канонический хост (для canonical_host dispatch)."""
    try:
        return {k.lower(): v.lower() for k, v in (api(base, "/api/settings").get("aliases") or {}).items()}
    except Exception:
        return {}


def collect_broken(db, aliases=None):
    """Все маршруты, получавшие эмулированные 4xx/5xx: {(host, method, path): hits}.
    Хост канонизируется алиасами — так же, как это делает dispatch."""
    aliases = aliases or {}
    rows = db.execute(
        "SELECT host, method, url, COUNT(*) FROM events WHERE kind='http' "
        "AND json_extract(detail,'$.emulated')=1 AND status>=400 GROUP BY host, method, url").fetchall()
    broken = {}
    for host, method, url, hits in rows:
        canonical = aliases.get((host or "").lower(), (host or "").lower())
        key = (canonical, method, url.split("?")[0])
        broken[key] = broken.get(key, 0) + hits
    return broken


def recover(db, state_dir, targets, dry=False):
    created, skipped = [], []
    for (host, method, path), hits in sorted(targets.items(), key=lambda kv: -kv[1]):
        if (host, method, path) in BUILTIN_ROUTES:
            continue  # stateful builtin: обслуживается всегда, реплей не нужен
        found = latest_real(db, host, method, path)
        if found is None:
            skipped.append(f"{method} {host}{path} ({hits} запросов 40*) — реального ответа в базе нет")
            continue
        event_id, status, content_type, body = found
        item = {
            "id": endpoint_id(host, path), "title": f"{method} {path}",
            "_group": host.lower(), "enabled": True, "host": host.lower(),
            "method": method, "path": path, "status": status,
            "content_type": content_type, "body_base64": base64.b64encode(body).decode(),
            "captured_ts": db.execute("SELECT ts FROM events WHERE id=?", (event_id,)).fetchone()[0],
            "source": "traffic.sqlite3", "notes": f"восстановлено; {hits} запросов 40* до этого",
        }
        line = f"{method} {host}{path} → {status} {content_type} {len(body)} Б"
        if not dry:
            target = state_dir / "endpoints" / (item["id"] + ".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(line)
    return created, skipped


def api(base, path, method="GET", value=None):
    request = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(value).encode() if value is not None else None,
                                     headers={"X-Emulator": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def sync_configs(db, base):
    """Реальные конфиги /get_sync_info → state.quasar устройств (лечит 409)."""
    registry = api(base, "/api/identities")
    for device in registry.get("devices", []):
        sn = device["sn"]
        rows = db.execute(
            "SELECT id, detail FROM events WHERE kind='http' AND host='quasar.yandex.net' "
            "AND json_extract(detail,'$.emulated') IS NULL AND status=200 "
            "AND url LIKE ? ORDER BY ts DESC LIMIT 10", (f"/get_sync_info?device_id={sn}%",)).fetchall()
        body = None
        for event_id, detail in rows:
            if json.loads(detail).get("url", "?").split("?")[0] != "/get_sync_info":
                continue
            parsed = json.loads(detail)
            data = response_body(db, event_id, (parsed.get("headers") or {}).get("response_original"))
            if not data:
                continue
            try:
                body = json.loads(data)
                break
            except ValueError:
                continue
        if not body:
            print(f"  {sn}: реального get_sync_info нет — пропущен")
            continue
        quasar = {k: body[k] for k in CONTENT_KEYS if k in body}
        if "config" not in quasar:
            print(f"  {sn}: в захвате нет config — пропущен")
            continue
        state = dict(device.get("state") or {})
        state["quasar"] = quasar
        api(base, "/api/identities", "POST",
            {"operation": "state", "sn": sn, "state": state})
        print(f"  {sn}: config залит ({len(json.dumps(quasar))} Б)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "traffic.sqlite3"))
    parser.add_argument("--state", default=str(ROOT / "server_emulator" / "state"))
    parser.add_argument("--api", default="http://127.0.0.1:8765/emulator")
    parser.add_argument("--all", action="store_true", help="восстановить все 40* маршруты")
    parser.add_argument("--host"); parser.add_argument("--method", default="GET"); parser.add_argument("--path")
    parser.add_argument("--sync-configs", action="store_true", help="залить реальные конфиги sync устройствам")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()
    if not (args.all or args.host or args.sync_configs):
        parser.error("укажите --all, --host/--method/--path или --sync-configs")
    db = open_db(args.db)
    state_dir = Path(args.state)
    if args.all:
        targets = collect_broken(db, alias_map(args.api))
        created, skipped = recover(db, state_dir, targets, dry=args.dry)
        for line in created:
            print("  +", line)
        for line in skipped:
            print("  -", line)
        print(f"итого: восстановлено {len(created)}, без захвата {len(skipped)}")
    if args.host and args.path:
        targets = {(args.host, args.method.upper(), args.path): 0}
        created, skipped = recover(db, state_dir, targets, dry=args.dry)
        for line in created + skipped:
            print("  ", line)
    if args.sync_configs:
        print("sync-конфиги:")
        sync_configs(db, args.api)


if __name__ == "__main__":
    sys.exit(main())
