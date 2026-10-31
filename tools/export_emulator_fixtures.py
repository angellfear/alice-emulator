"""A02: export sanitized reference episodes from the proxy capture DB (readonly).

Usage: python tools/export_emulator_fixtures.py [--db data/traffic.sqlite3] [--out tests/fixtures/emulator]

The capture database is opened read-only. Exported fixtures contain:
- ordered device-side messages (c2s and lifecycle; server responses are dropped) with relative times and channels;
- decoded+sanitized protobuf control frames (rebuildable via server_emulator/schemas/alice-api.pb);
- synthetic markers instead of captured audio bytes;
- alias-mapped personal identifiers (message/request/dialog IDs, device IDs, SNs);
- provenance (event ids, window, schema hash) without secrets.

Each fixture documents what it proves; tests/test_emulator_fixtures.py validates
decode round-trips, correlation links and absence of secrets.
"""
import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zstandard

from server_emulator.schema import load_type, message_dict

ROOT = Path(__file__).resolve().parent.parent

# name, connection, anchor event id, seconds before/after, what the fixture proves
EPISODES = [
    ("sync-basic", "000000000000000000000000feed1001",
     ("label", "SynchronizeStateRequest"), 4.0, 4.0,
     "Открытие WS-соединения: SynchronizeStateRequest с состоянием приложения -> SynchronizeStateResponse."),
    ("text-basic", "000000000000000000000000feed1002",
     ("label", "TextInput"), 2.0, 4.0,
     "Текстовый запрос TextInput (с диалоговой историей) -> Push notify + Ack -> InputStartAck -> AliceResponse."),
    ("voice-clock", "000000000000000000000000feed1003",
     ("id", "000000000000000000000000feed1004"), 1.5, 6.0,
     "Полный голосовой цикл «сколько времени»: VoiceInput -> partial AsrResult -> Close -> AliceResponse («Сейчас 12 часов дня») -> TtsSpeak + Ogg/Opus -> Close. Основа сценария R10 clock."),
    ("cancel-request", "000000000000000000000000feed1005",
     ("label", "CancelRequest"), 8.0, 3.0,
     "CancelRequest с RequestMessageId сразу после TextInput; сервер продолжает отвечать SynchronizeStateResponse/Push/RequestStatAck и AliceResponse приходит после отмены."),
    ("volume-set", "000000000000000000000000feed1006",
     ("id", "000000000000000000000000feed1007"), 6.0, 4.0,
     "Команда громкости текстом: AliceResponse содержит директиву sound_set_level + TtsSpeak/TtsTimings на канале 102 + Close."),
    ("screen-divui", "000000000000000000000000feed1006",
     ("id", "000000000000000000000000feed1008"), 1.0, 1.5,
     "Экранное действие по голосу: AliceResponse с директивой divui_show_view, DeferredAliceResponse и TTS-ответ."),
    ("reconnect-goaway", "000000000000000000000000feed1009",
     ("label", "GoAway"), 3.0, 5.0,
     "Сервер присылает GoAway, соединение закрывается; устройство переподключается новым соединением с SynchronizeStateRequest."),
    ("logspotter-ack", "000000000000000000000000feed100a",
     ("id", "000000000000000000000000feed100b"), 0.3, 0.7,
     "LogSpotter от устройства и подтверждающий LogAck сервера (Header.RefMessageId = MessageId запроса)."),
]
# second connection attached to the reconnect episode
RECONNECT_NEXT = ("000000000000000000000000feed100c", 6.0)

ID_KEYS = {"MessageId", "RefMessageId", "RequestId", "PrevReqId", "ResponseId", "RtLogId",
           "DialogId", "SessionId", "DeduplicationPushId", "RequestMessageId", "CardId", "PushId"}
CREDENTIAL_KEYS = {"OAuthToken", "ApiKey", "ICookie", "YexpCookie", "SaeCookie", "MegamindCookies",
                   "Authorization", "AccessToken", "RefreshToken"}
HEX_DASH = re.compile(r"^[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]+){2,}$")
HEX32 = re.compile(r"^[0-9A-Fa-f]{32}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
SECRETS = ("Cookie", "Authorization", "Bearer ", "PRIVATE KEY", "BEGIN CERTIFICATE", "oauth_token")
LONG_B64 = re.compile(r"[A-Za-z0-9+/]{200,}")
OAUTH_LIKE = re.compile(r"\b\d+\.\d+\.\d+\.\d+\.[0-9a-zA-Z_.-]{60,}")
B64_BLOB = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
LONG_B64_IN = re.compile(r"[A-Za-z0-9+/]{200,}")
BLOB_LIMIT = 48  # longer base64 values are bytes fields and may embed identifiers
LIST_LIMIT, STRING_LIMIT = 40, 6000


def redact_credentials(node, stats, path="msg"):
    """Учетные данные перехватываются как поля протокола — значения недопустимы в fixtures."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in CREDENTIAL_KEYS and isinstance(value, (str, bytes)) and value not in ("", b""):
                stats.append({"path": f"{path}.{key}", "original_len": len(value)})
                out[key] = f"<redacted credential {len(value)} chars>"
            else:
                out[key] = redact_credentials(value, stats, f"{path}.{key}")
        return out
    if isinstance(node, list):
        return [redact_credentials(v, stats, path + "[]") for v in node]
    return node


def redact_blobs(node, stats, path="msg"):
    """Replace embedded byte blobs: pure bytes fields and base64 runs inside JSON strings."""
    if isinstance(node, dict):
        return {k: redact_blobs(v, stats, f"{path}.{k}") for k, v in node.items()}
    if isinstance(node, list):
        return [redact_blobs(v, stats, path + "[]") for v in node]
    if isinstance(node, str):
        if len(node) > BLOB_LIMIT and len(node) % 4 == 0 and B64_BLOB.fullmatch(node):
            marker = f"<redacted {len(node)}B {hashlib.sha256(node.encode()).hexdigest()[:8]}>"
            stats.append({"path": path, "original_len": len(node)})
            return base64.b64encode(marker.encode()).decode()

        def inner(match):
            stats.append({"path": path, "original_len": len(match.group())})
            return f"<redacted {len(match.group())}B {hashlib.sha256(match.group().encode()).hexdigest()[:8]}>"
        return LONG_B64_IN.sub(inner, node)
    return node


def open_ro(db_path):
    uri = "file:" + str(Path(db_path).resolve()).replace("\\", "/") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def payload_fn(db):
    blocks = {}  # block_id -> decompressed bytes; episodes hit the same blocks repeatedly

    def read(digest):
        row = db.execute(
            "SELECT p.offset,p.size,b.id AS block,b.data,b.raw_size FROM payloads p JOIN blocks b ON b.id=p.block_id WHERE p.hash=?",
            (digest,)).fetchone()
        if row is None:
            raise KeyError(digest)
        raw = blocks.get(row["block"])
        if raw is None:
            raw = zstandard.ZstdDecompressor().decompress(row["data"], max_output_size=row["raw_size"])
            blocks[row["block"]] = raw
        return raw[row["offset"]:row["offset"] + row["size"]]
    return read


class Sanitizer:
    """Two-pass: collect identifier-shaped values from ID fields/SNs, then alias them."""

    def __init__(self):
        self.order = []          # first-seen originals
        self.aliases = {}
        self.counters = {"id": 0, "dev": 0}

    def add(self, value, kind="id"):
        if value in self.aliases or not value:
            return
        self.counters[kind] = self.counters.get(kind, 0) + 1
        prefix = {"id": "id", "dev": "dev"}[kind]
        self.aliases[value] = f"{prefix}-{self.counters[kind]:03d}"
        self.order.append(value)

    def collect(self, node, device_ids):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_KEYS and isinstance(value, str):
                    if UUID_RE.fullmatch(value) or HEX32.fullmatch(value) or HEX_DASH.fullmatch(value):
                        self.add(value)
                if key == "PushIds" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            self.add(item)
                self.collect(value, device_ids)
        elif isinstance(node, list):
            for item in node:
                self.collect(item, device_ids)
        elif isinstance(node, str):
            for dev in device_ids:
                if dev and dev in node:
                    self.add(dev, "dev")

    def rewrite(self, node):
        if isinstance(node, dict):
            return {k: (self.aliases.get(v, v) if k in ID_KEYS and isinstance(v, str) else self.rewrite(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [self.aliases.get(v, v) if isinstance(v, str) and v in self.aliases else self.rewrite(v)
                    for v in node]
        if isinstance(node, str):
            for original, alias in self.aliases.items():
                if original in node:
                    node = node.replace(original, alias)
            return node
        return node


def truncate(node, stats, path="msg"):
    """Keep protobuf-parseable shape: shrink oversized lists/strings, record stats."""
    if isinstance(node, dict):
        return {k: truncate(v, stats, f"{path}.{k}") for k, v in node.items()}
    if isinstance(node, list):
        if len(node) > LIST_LIMIT:
            stats.append({"path": path, "kept": node[:LIST_LIMIT][:1] and LIST_LIMIT, "total": len(node)})
            return [truncate(v, stats, path + "[]") for v in node[:LIST_LIMIT]]
        return [truncate(v, stats, path + "[]") for v in node]
    if isinstance(node, str) and len(node) > STRING_LIMIT:
        stats.append({"path": path, "kept": STRING_LIMIT, "total": len(node)})
        return node[:STRING_LIMIT]
    return node


def anchor_ts(db, conn, anchor):
    mode, value = anchor
    if mode == "id":
        return db.execute("SELECT ts FROM events WHERE id=?", (value,)).fetchone()["ts"]
    return db.execute(
        "SELECT e.ts FROM events e JOIN event_analysis a ON a.event_id=e.id WHERE e.connection_id=? AND a.label=? LIMIT 1",
        (conn, value)).fetchone()["ts"]


def export_episode(db, read, name, conn, anchor, before, after, proves, http_context=False, extra_connections=None):
    t0 = anchor_ts(db, conn, anchor)

    def fetch(connection, low, high, connection_tag=None):
        rows = db.execute(
            """SELECT e.id,e.ts,e.direction,e.size,a.label,a.channel,a.detail,
                      ep.hash AS original_hash, es.hash AS sent_hash
               FROM events e
               LEFT JOIN event_analysis a ON a.event_id=e.id
               LEFT JOIN event_payloads ep ON ep.event_id=e.id AND ep.role='original'
               LEFT JOIN event_payloads es ON es.event_id=e.id AND es.role='sent'
               WHERE e.connection_id=? AND e.ts BETWEEN ? AND ? ORDER BY e.ts,e.rowid""",
            (connection, low, high)).fetchall()
        out = []
        for r in rows:
            entry = {"t_ms": round((r["ts"] - t0) * 1000),
                     "dir": {"client_to_server": "c2s", "server_to_client": "s2c"}.get(r["direction"], r["direction"] or "lifecycle")}
            if connection_tag:
                entry["connection"] = connection_tag
            out.append((entry, r))
        return out

    client_t = load_type(ROOT, "server_emulator/schemas/alice-api.pb", "NAlice.NAliceApi.TClientMessage")
    server_t = load_type(ROOT, "server_emulator/schemas/alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    fetched = fetch(conn, t0 - before, t0 + after)
    for extra_conn, extra_start, extra_window, extra_tag in (extra_connections or []):
        fetched += fetch(extra_conn, extra_start, extra_start + extra_window, extra_tag)
    rows = [r for _, r in fetched]

    analyzer = json.loads(rows[0]["detail"]) if rows else {}
    device_ids = {analyzer.get("device_id")} if analyzer.get("device_id") else set()
    for r in rows:
        if r["detail"]:
            try:
                detail = json.loads(r["detail"])
                if detail.get("device_id"):
                    device_ids.add(detail["device_id"])
            except ValueError:
                pass

    messages, pending = [], []
    stream_totals = {}  # необработанные chunk'и аудиоканалов сворачиваются в одну сводку
    for entry, r in fetched:
        label = r["label"]
        if r["direction"] is None or r["direction"] == "lifecycle":  # ws open/close
            entry["kind"] = "lifecycle"
            entry["event"] = json.loads(r["detail"]).get("event", "open") if r["detail"] else "open"
            messages.append(entry)
            continue
        if label == "Данные потока":  # сырые chunk'и канала без декодера
            total = stream_totals.setdefault("count", 0)
            stream_totals["count"] = total + 1
            stream_totals["bytes"] = stream_totals.get("bytes", 0) + (r["size"] or 0)
            stream_totals.setdefault("first_ms", entry["t_ms"])
            stream_totals["last_ms"] = entry["t_ms"]
            if total == 0:
                entry.update(kind="stream_data", label=label, synthetic="raw-channel-chunks")
                messages.append(entry)
            continue
        data = None
        if r["original_hash"]:
            try:
                data = read(r["original_hash"])
            except KeyError:
                data = None
        if data and data[:4] == b"AAPI":
            cls = server_t if r["direction"] == "server_to_client" else client_t
            try:
                decoded = message_dict(cls.FromString(data[4:]))
            except Exception as exc:
                entry.update(kind="control", decode="failed", error=type(exc).__name__,
                             bytes=len(data), sha256=hashlib.sha256(data).hexdigest()[:16])
                messages.append(entry)
                continue
            entry["kind"] = "control"
            entry["label"] = label
            entry["bytes"] = r["size"]
            pending.append((entry, decoded, r["original_hash"]))
        elif label in ("Ogg / Opus", "Ogg · фрагмент") or (data and data[:4] == b"OggS"):
            entry.update(kind="audio", channel=r["channel"], label=label,
                         bytes=r["size"], synthetic="ogg-opus-silence")
        else:
            entry.update(kind="opaque", label=label, bytes=r["size"])
            if data:
                entry["sha256"] = hashlib.sha256(data).hexdigest()[:16]
        messages.append(entry)
    if stream_totals:
        summary = next((m for m in messages if m.get("kind") == "stream_data"), None)
        if summary:
            summary.update(chunks=stream_totals["count"], bytes=stream_totals.get("bytes", 0),
                           t_ms_last=stream_totals.get("last_ms"))

    sanitizer = Sanitizer()
    for _, decoded, _ in pending:
        sanitizer.collect(decoded, device_ids)
    for entry, decoded, digest in pending:
        stats, blobs, creds = [], [], []
        entry["message"] = truncate(redact_blobs(redact_credentials(sanitizer.rewrite(decoded), creds), blobs), stats)
        if stats:
            entry["truncated"] = stats
        if blobs:
            entry["redacted_blobs"] = blobs
        if creds:
            entry["redacted_credentials"] = creds

    http = []
    if http_context and device_ids:
        dev = sorted(device_ids)[0]
        for r in db.execute(
                """SELECT e.ts,e.method,e.host,e.url,e.status,e.content_type,ep.hash
                   FROM events e LEFT JOIN event_payloads ep ON ep.event_id=e.id AND ep.role='response_original'
                   WHERE e.kind='http' AND e.ts BETWEEN ? AND ? ORDER BY e.ts""",
                (t0 - before, t0 + after)):
            url = re.sub(r"([?&]device_id=)[^&]+", r"\1" + sanitizer.aliases.get(dev, "dev-A"), r["url"] or "")
            item = {"t_ms": round((r["ts"] - t0) * 1000), "method": r["method"], "host": r["host"],
                    "url": url, "status": r["status"], "content_type": r["content_type"]}
            if r["hash"]:
                try:
                    body = read(r["hash"])
                    if len(body) < 65536:
                        item["response_json"] = json.loads(body)
                except (KeyError, ValueError, UnicodeError):
                    pass
            http.append(item)

    captured = datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(timespec="seconds")
    fixture = {
        "format_version": 1,
        "name": name,
        "proves": proves,
        "source": {
            "kind": "capture",
            "database": "data/traffic.sqlite3",
            "captured_at": captured,
            "connection": conn,
            "anchor_event": anchor[1],
            "window": [round(-before, 2), round(after, 2)],
            "event_ids": [r["id"] for r in rows],
            "sanitization": "идентификаторы сообщений/запросов/устройств заменены алиасами; аудио заменено синтетическими маркерами; длинные списки/строки усечены (см. truncated)",
        },
        "schema": {"file": "server_emulator/schemas/alice-api.pb",
                   "sha256": hashlib.sha256((ROOT / "server_emulator" / "schemas" / "alice-api.pb").read_bytes()).hexdigest(),
                   "types": ["NAlice.NAliceApi.TClientMessage", "NAlice.NAliceApi.TServerMessage"]},
        "messages": messages,
    }
    if http:
        fixture["http_context"] = http
    return fixture


def verify(fixture):
    """Decode round-trip, correlation links, no secrets. Returns list of problems."""
    problems = []
    client_t = load_type(ROOT, "server_emulator/schemas/alice-api.pb", "NAlice.NAliceApi.TClientMessage")
    server_t = load_type(ROOT, "server_emulator/schemas/alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    from google.protobuf import json_format
    message_ids, known_ids = set(), set()

    def walk_ids(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_KEYS and isinstance(value, str):
                    known_ids.add(value)
                walk_ids(value)
        elif isinstance(node, list):
            for item in node:
                walk_ids(item)

    for m in fixture["messages"]:
        if m["kind"] != "control" or "message" not in m:
            continue
        cls = client_t if m["dir"] == "c2s" else server_t
        try:
            json_format.ParseDict(m["message"], cls(), descriptor_pool=cls.DESCRIPTOR.file.pool)
        except Exception as exc:
            problems.append(f"{m.get('label')}: roundtrip failed: {exc}")
        walk_ids(m["message"])
        header = m["message"].get("Event", {}).get("Header", {})
        if header.get("MessageId"):
            message_ids.add(header["MessageId"])
    external = set()
    for m in fixture["messages"]:
        if m["kind"] != "control" or "message" not in m:
            continue
        header = m["message"].get("Event", {}).get("Header", {})
        ref = header.get("RefMessageId")
        if ref and ref not in message_ids:
            if ref in known_ids or ref in fixture.get("external_references", []):
                external.add(ref)  # ссылка на сообщение до окна эпизода (живой диалог)
            else:
                problems.append(f"{m.get('label')}: RefMessageId {ref} неизвестен в эпизоде")
    if external:
        fixture["external_references"] = sorted(external | set(fixture.get("external_references", [])))
    text = json.dumps(fixture, ensure_ascii=False)
    for marker in SECRETS:
        if marker in text:
            problems.append(f"обнаружен секрет-маркер: {marker}")
    for hit in LONG_B64.findall(text):
        problems.append(f"длинный base64-блоб: {hit[:24]}…")
    for hit in OAUTH_LIKE.findall(text):
        problems.append(f"похоже на живой OAuth-токен: {hit[:32]}…")
    for hit in re.findall(r'"(?:%s)":\s*"(?!<redacted)[^"]+"' % "|".join(CREDENTIAL_KEYS), text):
        problems.append(f"поле с учетными данными не закрыто: {hit[:48]}…")
    return problems


def client_side_only(fixture):
    """Оставить только кадры колонки (c2s и lifecycle): ответы сервера Яндекса в репозиторий не попадают.

    Ссылки c2s на убранные ответы сервера (RefMessageId) записываются в external_references."""
    dropped = set()
    for m in fixture["messages"]:
        if m["dir"] == "s2c" and m["kind"] == "control" and "message" in m:
            header = m["message"].get("Event", {}).get("Header", {})
            if header.get("MessageId"):
                dropped.add(header["MessageId"])
    fixture["messages"] = [m for m in fixture["messages"] if m["dir"] != "s2c"]
    kept = {m["message"].get("Event", {}).get("Header", {}).get("MessageId")
            for m in fixture["messages"] if m["kind"] == "control" and "message" in m}
    external = set(fixture.get("external_references", []))
    for m in fixture["messages"]:
        if m["kind"] == "control" and "message" in m:
            ref = m["message"].get("Event", {}).get("Header", {}).get("RefMessageId")
            if ref and ref in dropped and ref not in kept:
                external.add(ref)
    if external:
        fixture["external_references"] = sorted(external)
    return fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/traffic.sqlite3")
    parser.add_argument("--out", default="tests/fixtures/emulator")
    args = parser.parse_args()
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    db = open_ro(args.db)
    read = payload_fn(db)

    readme = ["# Эталонные обмены эмулятора (A02)", "",
              "Экспорт из перехвата (readonly) с санитизацией: ID заменены алиасами, аудио синтетическое,",
              "длинные поля усечены и помечены. Хранятся только кадры колонки (c2s и lifecycle): ответы сервера Яндекса",
              "в репозиторий не входят, ссылки на них перечислены в `external_references`; описания эпизодов ниже — что",
              "происходило в исходном обмене. Управляющие фреймы декодируются обратно через",
              "`server_emulator/schemas/alice-api.pb`; корректность проверяет `tests/test_emulator_fixtures.py`.", ""]
    failures = 0
    for name, conn, anchor, before, after, proves in EPISODES:
        extra = None
        if name == "reconnect-goaway":
            nxt_conn, nxt_window = RECONNECT_NEXT
            first = db.execute("SELECT MIN(ts) st FROM events WHERE connection_id=?", (nxt_conn,)).fetchone()["st"]
            extra = [(nxt_conn, first, nxt_window, "next")]
        fixture = export_episode(db, read, name, conn, anchor, before, after, proves,
                                 http_context=(name == "sync-basic"), extra_connections=extra)
        problems = verify(client_side_only(fixture))
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
        status = "OK" if not problems else "PROBLEMS"
        failures += len(problems)
        print(f"{name}: {status} ({len(fixture['messages'])} сообщений, {path.stat().st_size // 1024} KiB)")
        for p in problems:
            print("   !", p)
        readme.append(f"## {name}\n\n{proves}\n\nИсточник: соединение `{conn}`, окно ± вокруг якорного события; "
                      f"подробности в `source` внутри файла.\n")
    (out_dir / "README.md").write_text("\n".join(readme), encoding="utf-8")
    db.close()
    print("Готово." if not failures else f"Завершено с {failures} проблемами.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
