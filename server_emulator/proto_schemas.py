"""Схемы protobuf для разбора тел эндпоинтов: импорт zip с .proto, именованный decode/encode.

Импорт: zip с текстами .proto (пути каталогами или сплющенные «a__b__c.proto» → «a/b/c.proto»)
собирается protoc из grpcio-tools в FileDescriptorSet (state/proto/quasar.pb, формат schemas/*.pb).
Отсутствующие импорты заменяются пустыми заглушками (обычно это файлы опций); файлы, которые
всё равно не собираются, отбрасываются с причиной. Режимы: «update» — новые файлы заменяют
одноимённые, остальные остаются; «replace» — набор целиком из архива.
"""
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

DIRECTORY: Path = Path(__file__).resolve().parent / "state" / "proto"
SET_NAME = "quasar.pb"
META_NAME = "quasar.json"
MODES = ("update", "replace")

# Методы gproxy → тип ответа, которые не выводятся из имени метода (исследование a311_research/gproxy_research)
KNOWN = {
    "endpoint_subscribe": "NAlice.TSubscribeIoTEndpointsResponse",
    "endpoint_unsubscribe": "NAlice.TUnsubscribeIoTEndpointsResponse",
    "push_events_batch": "NAlice.TIoTEventsBatchResponse",
    "push_historical_events": "NAlice.THistoricalEventsResponse",
    "custom_noises/samples/by_ids": "NAlice.TIoTGetCustomNoiseSamplesByIdsResponse",
    "memory_delete": "NAlice.NMemory.TMemoryDeleteResponse",
    "get_user_settings": "NAlice.NRpc.TGetUserSettingsResponse",
    "set_user_settings": "NAlice.NRpc.TSetUserSettingsResponse",
    "centaur_get_gallery": "NAlice.NTv.TCentaurCarouselResultData",
    "centaur_get_galleries": "NAlice.NTv.TCentaurFeedResultData",
    "get_teasers": "NAlice.NTeaser.TGetTeasersResponse",
    # не из таблицы исследования — по устройству захваченных тел (совпадают с centaur_-аналогами)
    "get_gallery": "NAlice.NTv.TCentaurCarouselResultData",
    "get_galleries": "NAlice.NTv.TCentaurFeedResultData",
    "get_screen_hints": "NAlice.NTv.TGetScreenHintsV2Response",
}

# Методы gproxy, известные по исследованию (.so и Retrofit-аннотации Java-слоя) — для таблицы привязок.
METHODS = tuple(sorted(set(KNOWN) | {
    "add_tv_channel_to_favorites", "delete_delayed_item", "get_music_album_info", "get_music_artist_info",
    "get_music_artist_playlists", "get_music_artist_popular_albums", "get_music_artist_popular_tracks",
    "get_music_artist_similar_artists", "get_music_category", "get_music_playlist_info", "get_search_suggests",
    "get_teaser_settings", "get_tv_channels", "get_tv_programs", "music_search", "music_search_history",
    "remove_tv_channel_from_favorites", "save_search_suggest_history", "search", "whats_new_complete_watch",
}))

ROUNDS = 200  # проходов protoc: каждый отбрасывает несобирающиеся файлы


def response_type(method: str, names: list[str]) -> str | None:
    """Тип ответа метода gproxy: таблица KNOWN или T<CamelCase(метод)>Response."""
    method = method.strip("/")
    if method.startswith("gproxy/"):
        method = method[len("gproxy/"):]
    if KNOWN.get(method) in names:
        return KNOWN[method]
    # get_music_album_info → TGetMusicAlbumInfoResponse («/» как «_»)
    candidate = "T" + "".join(p.capitalize() for p in re.split(r"[/_]", method) if p) + "Response"
    return next((name for name in names if name.rsplit(".", 1)[-1] == candidate), None)


# -----------------------------------------------------------------------------
# Сборка zip → дескрипторы
# -----------------------------------------------------------------------------


def _member_path(member: str) -> str | None:
    """Путь .proto внутри набора: каталоги как есть, сплющенное имя «a__b.proto» → «a/b.proto»."""
    member = member.replace("\\", "/").lstrip("/")
    if not member.endswith(".proto") or member.endswith("/"):
        return None
    parts = [p for p in member.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts) if len(parts) > 1 else parts[0].replace("__", "/")


_FIELD_NO_LABEL = re.compile(
    r"^(\s+)(?!(?:optional|repeated|required|message|enum|oneof|option|reserved|extensions|extend|map)\b)"
    r"([.\w]+\s+\w+\s*=\s*\d+\s*(?:\[.*\])?\s*;)", re.M)


def _fix_proto2(text: str) -> str:
    """proto2 из восстановленных дескрипторов: у полей required метка потеряна — вернуть."""
    if re.search(r'^\s*syntax\s*=\s*"proto3"', text, re.M):
        return text
    return _FIELD_NO_LABEL.sub(r"\1required \2", text)


def _fix_oneofs(text: str) -> str:
    """Поля внутри oneof без меток: восстановленные файлы пишут им optional/required."""
    return re.sub(r"(\boneof\s+\w+\s*\{)([^{}]*)(\})",
                  lambda m: m[1] + re.sub(r"^(\s*)(?:optional|required)\s+", r"\1", m[2], flags=re.M) + m[3], text)


def _protoc(root: Path, files: list[str], out: Path) -> tuple[bool, str]:
    import grpc_tools

    include = Path(grpc_tools.__file__).resolve().parent / "_proto"
    # список файлов — через @файл: 600+ путей не помещаются в командную строку Windows
    listing = out.with_suffix(".args")
    listing.write_text("\n".join([f"-I{root}", f"-I{include}", f"--descriptor_set_out={out}", "--include_imports", *files]),
                       encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "grpc_tools.protoc", f"@{listing}"], cwd=root, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=300)
    return result.returncode == 0, result.stderr


def compile_zip(path: Path) -> tuple[list[descriptor_pb2.FileDescriptorProto], dict]:
    """zip с .proto → (дескрипторы собранных файлов, отчёт {found, stubs, duplicates, dropped})."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError("Нужен zip с файлами .proto") from None
    with tempfile.TemporaryDirectory() as temp, archive:
        root = Path(temp) / "src"
        sources = {}
        members = {member: _member_path(member) for member in archive.namelist()}
        top = _common_folder([name for name in members.values() if name], archive)
        for member, name in members.items():
            if name and top:
                name = _member_path(name[len(top) + 1:])
            # google/protobuf/* — настоящие у protoc; восстановленные копии в архиве неполные
            if name and name not in sources and not name.startswith("google/protobuf/"):
                text = archive.read(member).decode("utf-8", "replace")
                if not re.search(r"^\s*package\s+google\.protobuf\s*;", text, re.M):  # копии из Go-модулей
                    sources[name] = _fix_oneofs(_fix_proto2(text))
        if not sources:
            raise ValueError("В архиве нет файлов .proto")
        sources = _match_case(sources)
        for name, text in sources.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        # отсутствующие импорты (кроме google/protobuf — они есть у protoc) → пустые заглушки
        stubs = set()
        for text in sources.values():
            for imported in re.findall(r'^\s*import\s+(?:public\s+|weak\s+)?"([^"]+)"\s*;', text, re.M):
                if imported not in sources and not imported.startswith("google/protobuf/"):
                    stubs.add(imported)
        for name in stubs:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('syntax = "proto3";\n', encoding="utf-8")
        duplicates, dropped = _duplicates(sources), {}
        for name, keepers in duplicates.items():  # импортирующие дубликат получат типы из оставленного файла
            (root / name).write_text('syntax = "proto3";\n' + "".join(f'import public "{k}";\n' for k in keepers),
                                     encoding="utf-8")
        files = sorted(name for name in sources if name not in duplicates)
        out = Path(temp) / "set.pb"
        # protoc останавливается на первом файле с ошибкой: всё до него собралось — проход продолжается с него
        checked = 0
        for _ in range(ROUNDS):
            if not files:
                break
            ok, errors = _protoc(root, files[checked:], out) if checked < len(files) else (True, "")
            if ok:
                if checked:
                    ok, errors = _protoc(root, files, out)
                if ok:
                    break
                checked = 0
            bad = {}
            for line in errors.splitlines():  # «a/b.proto:12:3: "X" is not defined.»
                match = re.match(r"(.+?\.proto)(?::\d+:\d+)?:\s*(.+)", line.strip())
                if match and not match.group(2).startswith("warning:"):
                    name = match.group(1).replace("\\", "/")
                    name = name[len(str(root)) + 1:].replace("\\", "/") if name.startswith(str(root).replace("\\", "/")) else name
                    if name in files:
                        bad.setdefault(name, match.group(2))
            if not bad:
                raise ValueError("protoc: " + errors.strip()[:500])
            checked = max(checked, min(files.index(name) for name in bad))
            _cascade(bad, sources, set(files))
            dropped.update(bad)
            checked -= sum(1 for name in files[:checked] if name in bad)
            files = [f for f in files if f not in bad]
            for name in bad:  # импортирующие его файлы соберутся, если им не нужны его типы
                (root / name).write_text('syntax = "proto3";\n', encoding="utf-8")
        else:
            raise ValueError("protoc: слишком много проходов, набор не собирается")
        if not files:
            raise ValueError("Ни один .proto не собрался: " + "; ".join(list(dropped.values())[:3]))
        fds = descriptor_pb2.FileDescriptorSet.FromString(out.read_bytes())
    report = {"found": len(sources), "stubs": sorted(stubs), "duplicates": len(duplicates),
              "dropped": [{"name": k, "error": v[:300]} for k, v in sorted(dropped.items())]}
    return list(fds.file), report


def _match_case(sources: dict) -> dict:
    """Имена, различающиеся только регистром («Common.proto» и импорт «common.proto»).

    На Windows это один файл: заглушка для импорта затёрла бы настоящий файл. Из одноимённых без учёта
    регистра остаётся первый, импорты переписываются на его имя.
    """
    canonical = {}
    for name in sources:
        canonical.setdefault(name.lower(), name)
    def fix(match):
        imported = match[2]
        return match[1] + canonical.get(imported.lower(), imported) + match[3]
    return {name: re.sub(r'^(\s*import\s+(?:public\s+|weak\s+)?")([^"]+)("\s*;)', fix, text, flags=re.M)
            for name, text in sources.items() if canonical[name.lower()] == name}


def _duplicates(sources: dict) -> dict[str, list[str]]:
    """Файлы, объявляющие те же типы, что другой (архив из нескольких источников) → {дубликат: [оставленные]}.

    Остаётся файл, который чаще импортируют, при равенстве — длиннее.
    """
    declared = {}
    for name, text in sources.items():
        package = (re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M) or [None, ""])[1]
        declared[name] = {f"{package}.{t}" for t in re.findall(r"^(?:message|enum|service)\s+(\w+)", text, re.M)}
    imports = {}
    for text in sources.values():
        for imported in re.findall(r'^\s*import\s+(?:public\s+|weak\s+)?"([^"]+)"', text, re.M):
            imports[imported] = imports.get(imported, 0) + 1
    owner, result = {}, {}
    for name in sorted(sources, key=lambda n: (-imports.get(n, 0), -len(sources[n]), n)):
        keepers = sorted({owner[t] for t in declared[name] if t in owner})
        if keepers:
            result[name] = keepers
        else:
            owner.update(dict.fromkeys(declared[name], name))
    return result


def _common_folder(names: list[str], archive: zipfile.ZipFile) -> str | None:
    """Общая папка-обёртка архива («proto_final/…»): без неё находится больше импортов, чем с ней."""
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) != 1 or any("/" not in name for name in names):
        return None
    top = tops.pop()
    inner = {_member_path(name[len(top) + 1:]) for name in names}
    imports = [imported for info in archive.infolist() if info.filename.endswith(".proto")
               for imported in re.findall(r'^\s*import\s+(?:public\s+|weak\s+)?"([^"]+)"',
                                          archive.read(info).decode("utf-8", "replace"), re.M)]
    return top if sum(i in inner for i in imports) > sum(i in names for i in imports) else None


def _cascade(bad: dict, sources: dict, files: set) -> None:
    """Сразу отбросить файлы, которые импортируют отброшенный и ссылаются на его типы.

    Иначе каждый уровень цепочки зависимостей стоит отдельного прохода protoc (десятки проходов).
    """
    queue = list(bad)
    while queue:
        name = queue.pop()
        text = sources.get(name, "")
        package = (re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M) or [None, ""])[1]
        types = re.findall(r"^\s*(?:message|enum)\s+(\w+)", text, re.M)
        refs = [f"{package}.{t}" if package else t for t in types]
        for other in files:
            if other in bad:
                continue
            body = sources[other]
            if f'"{name}"' in body and any(re.search(r"(?<![\w.])\.?" + re.escape(ref) + r"\b", body) for ref in refs):
                bad[other] = f"Использует типы из {name}, который не собрался"
                queue.append(other)


def _validate_pool(fdps: list[descriptor_pb2.FileDescriptorProto]) -> tuple[list, list[dict]]:
    """Добавить в DescriptorPool в порядке зависимостей; не загрузившиеся — в отчёт."""
    pool = descriptor_pool.DescriptorPool()
    pending, loaded, dropped = list(fdps), [], []
    while pending:
        remaining, errors = [], {}
        for fdp in pending:
            try:
                pool.Add(fdp)
                loaded.append(fdp)
            except Exception as exc:  # noqa: BLE001 — зависимость ещё не загружена или конфликт имён
                remaining.append(fdp)
                errors[fdp.name] = str(exc)
        if len(remaining) == len(pending):
            dropped = [{"name": fdp.name, "error": errors[fdp.name][:300]} for fdp in remaining]
            break
        pending = remaining
    return loaded, dropped


def _count(messages) -> int:
    return sum(1 + _count(m.nested_type) for m in messages)


def import_zip(path: Path, source_name: str, sha256: str, mode: str = "update") -> dict:
    """Импорт zip: «update» — поверх имеющегося набора, «replace» — вместо него; → мета."""
    if mode not in MODES:
        raise ValueError("mode: " + ", ".join(MODES))
    fdps, report = compile_zip(path)
    merged = {}
    set_path = DIRECTORY / SET_NAME
    if mode == "update" and set_path.is_file():
        merged = {f.name: f for f in descriptor_pb2.FileDescriptorSet.FromString(set_path.read_bytes()).file}
    replaced = sum(1 for f in fdps if f.name in merged)
    merged.update((f.name, f) for f in fdps)
    loaded, conflicts = _validate_pool(list(merged.values()))
    fds = descriptor_pb2.FileDescriptorSet()
    fds.file.extend(loaded)
    previous = (meta() or {}) if mode == "update" else {}
    source = {"name": source_name, "sha256": sha256, "mode": mode, "found": report["found"],
              "compiled": len(fdps), "replaced": replaced, "stubs": len(report["stubs"]),
              "duplicates": report["duplicates"],
              "imported": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    names = set_names(fds)
    info = {"files": len(loaded), "messages": sum(_count(f.message_type) for f in loaded),
            "dropped": report["dropped"] + conflicts, "stubs": report["stubs"],
            "sources": [s for s in previous.get("sources") or [] if s.get("sha256") != sha256] + [source],
            "last": source, "imported": source["imported"],
            "bindings": {m: response_type(m, names) for m in METHODS}}
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    set_path.write_bytes(fds.SerializeToString())
    (DIRECTORY / META_NAME).write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


# -----------------------------------------------------------------------------
# Загруженный набор: имена, классы, decode/encode
# -----------------------------------------------------------------------------


def meta() -> dict | None:
    path = DIRECTORY / META_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _set_path() -> Path:
    path = DIRECTORY / SET_NAME
    if not path.is_file():
        raise ValueError("Схемы не импортированы")
    return path


@lru_cache(maxsize=8)
def _cached_pool(path: str, mtime_ns: int, size: int) -> descriptor_pool.DescriptorPool:
    fds = descriptor_pb2.FileDescriptorSet.FromString(Path(path).read_bytes())
    pool = descriptor_pool.DescriptorPool()
    pending = list(fds.file)
    while pending:
        remaining = []
        for fdp in pending:
            try:
                pool.Add(fdp)
            except Exception:  # noqa: BLE001 — зависимость добавится следующим проходом
                remaining.append(fdp)
        if len(remaining) == len(pending):
            raise ValueError("Не загружены зависимости descriptor")
        pending = remaining
    return pool


def pool() -> descriptor_pool.DescriptorPool:
    path = _set_path()
    stat = path.stat()
    return _cached_pool(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=8)
def _cached_names(path: str, mtime_ns: int, size: int) -> tuple[str, ...]:
    return tuple(set_names(descriptor_pb2.FileDescriptorSet.FromString(Path(path).read_bytes())))


def names() -> list[str]:
    """Полные имена всех типов сообщений (с вложенными) импортированного набора."""
    path = _set_path()
    stat = path.stat()
    return list(_cached_names(str(path), stat.st_mtime_ns, stat.st_size))


def set_names(fds: descriptor_pb2.FileDescriptorSet) -> list[str]:
    result = []

    def walk(prefix, messages):
        for message in messages:
            result.append(prefix + message.name)
            walk(prefix + message.name + ".", message.nested_type)

    for fdp in fds.file:
        walk(fdp.package + "." if fdp.package else "", fdp.message_type)
    return sorted(set(result))


def message_class(name: str):
    try:
        return message_factory.GetMessageClass(pool().FindMessageTypeByName(name))
    except KeyError:
        raise ValueError(f"Тип {name} не найден в схемах") from None


def decode_typed(name: str, data: bytes) -> dict:
    """Тело → dict по схеме; ValueError, если тело не этого типа (неизвестные поля, не разбирается)."""
    cls = message_class(name)
    try:
        message = cls.FromString(data)
    except Exception as exc:  # noqa: BLE001 — DecodeError не ValueError: тело другого типа
        raise ValueError(f"Тело не разбирается как {name}: {exc}") from None
    value = json_format.MessageToDict(message, preserving_proto_field_name=True)
    # Обратимость по смыслу: map (в т.ч. Struct) сериализуется в своём порядке ключей, байты могут
    # переставиться; потерянные неизвестные поля (тело не этого типа) укорачивают сериализацию.
    again = encode_message(name, value)
    if len(again) != len(data) or cls.FromString(again) != message:
        raise ValueError(f"Тело не совпадает со схемой {name} (лишние или неизвестные поля)")
    return value


def encode_message(name: str, value: dict) -> bytes:
    """dict (protobuf JSON, имена полей как в .proto) → двоичное тело по схеме."""
    return json_format.ParseDict(value, message_class(name)()).SerializeToString()


if __name__ == "__main__":
    fdps, report = compile_zip(Path(sys.argv[1]))
    print(f"found {report['found']}, compiled {len(fdps)}, stubs {len(report['stubs'])}, dropped {len(report['dropped'])}")
    for item in report["dropped"][:20]:
        print(f"  - {item['name']}: {item['error'][:150]}")
