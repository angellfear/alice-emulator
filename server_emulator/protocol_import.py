"""Протокол — из библиотеки станции пользователя или из архива с описаниями.

Дескрипторы протокола (`schemas/*.pb`) в репозиторий не входят. Пользователь загружает один файл, эмулятор
разбирает его локально и ничего никуда не отправляет:
- `libquasar_daemons.so` станции (из APK `centaur-app-launcher.apk`, `lib/arm64-v8a/`): в ней лежат
  сериализованные `FileDescriptorProto`; поиск — как в protodump (маркер `.proto`, разбор верхнего уровня
  protobuf);
- zip-архив с `.proto` (собираются `grpc_tools.protoc`) и/или скомпилированными наборами `.pb` (FileDescriptorSet).

CLI без UI: `python -m server_emulator.protocol_import <файл>`.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

from google.protobuf import descriptor_pb2
from google.protobuf.message import DecodeError

from . import schema as schema_module

ROOT = Path(__file__).resolve().parent
STATION_LIBRARY = "libquasar_daemons.so"
REQUIRED_TYPES = ("NAlice.NAliceApi.TClientMessage", "NAlice.NAliceApi.TServerMessage")
LIBRARY_LIMIT = 512 * 1024 ** 2   # .so не больше (у станции ~77 МБ)
ELF_MAGIC = b"\x7fELF"
SET_SUFFIXES = (".pb", ".desc", ".protoset", ".binpb")
ZIP_MEMBERS_LIMIT = 20000
ZIP_TOTAL_LIMIT = 256 * 1024 ** 2   # распакованные .proto / .pb суммарно
PROTOC_TIMEOUT = 300
IMPORT_RE = re.compile(r'^\s*import\s+(?:public\s+|weak\s+)?"([^"]+)"', re.M)
ROOT_MESSAGE_RE = re.compile(r"\bmessage\s+T(?:Client|Server)Message\b")


class ProtocolImportError(ValueError):
    """Понятная пользователю ошибка разбора протокола (по-русски)."""


# ── Поиск дескрипторов (порт protodump: scan.go) ────────────────────────────

class _Stop(Exception):
    """Разбор верхнего уровня закончился: «неверный номер поля» — конец данных, не ошибка."""


class _Bad(Exception):
    """Настоящая ошибка разбора protobuf — кандидат пропускается."""


def _varint(data, pos):
    """Varint с позиции pos → (значение, новая позиция); как protowire.ConsumeVarint (до 10 байт)."""
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise _Bad("обрыв varint")
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise _Bad("переполнение varint")
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
    raise _Bad("переполнение varint")


def _tag(data, pos):
    key, pos = _varint(data, pos)
    number = key >> 3
    if number > 0x7FFFFFFF or number < 1:
        raise _Stop
    return number, key & 7, pos


def _consume_value(data, number, wire, pos):
    """Пропуск значения поля (protowire.ConsumeFieldValue); группы — итеративно."""
    stack = []
    while True:
        if wire == 0:
            _, pos = _varint(data, pos)
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        elif wire == 2:
            size, pos = _varint(data, pos)
            pos += size
        elif wire == 3:
            stack.append(number)
            number, wire, pos = _tag(data, pos)
            continue
        elif wire == 4:
            if not stack or stack.pop() != number:
                raise _Bad("лишний конец группы")
            if not stack:
                return pos
            number, wire, pos = _tag(data, pos)
            continue
        else:
            raise _Bad("зарезервированный тип")
        if pos > len(data):
            raise _Bad("обрыв поля")
        if not stack:
            return pos
        number, wire, pos = _tag(data, pos)


def _consume_message(data, start):
    """Длина protobuf-сообщения, начатого в start (как consumeBytes в protodump).

    Поле 1 допускается один раз: так не склеиваются соседние дескрипторы в памяти."""
    position, seen_name, size = start, False, len(data)
    while True:
        try:
            number, wire, after_tag = _tag(data, position)
            end = _consume_value(data, number, wire, after_tag)
        except _Stop:
            return position - start
        if number == 1:
            if seen_name:
                return position - start
            seen_name = True
        position = end
        if position >= size:
            return position - start


def scan_descriptors(data):
    """Все кандидаты-`FileDescriptorProto` (сырые байты) в бинарнике, в порядке следования."""
    results, base = [], 0
    find, rfind = data.find, data.rfind
    while True:
        index = find(b".proto", base)
        if index == -1:
            break
        # байт 0x0a: тег поля 1 (имя файла), длина-разделитель
        start = rfind(b"\n", base, index)
        if start == -1:
            base = index + 1
            continue
        # имя из 10 символов: 0x0a оказался длиной строки, тег — на байт раньше
        if index - start == 5 and start > base and data[start - 1] == 0x0A:
            start -= 1
        try:
            length = _consume_message(data, start)
        except _Bad:
            base = index + 1
            continue
        results.append(data[start:start + length])
        base = start + length
    return results


def descriptor_files(chunks):
    """Разбор кандидатов в `FileDescriptorProto`: валидные, с именем `*.proto`, первое имя побеждает."""
    files, seen = [], set()
    for chunk in chunks:
        file = descriptor_pb2.FileDescriptorProto()
        try:
            file.ParseFromString(chunk)
        except DecodeError:
            continue
        name = file.name
        if not name.endswith(".proto") or name in seen:
            continue
        seen.add(name)
        files.append(file)
    return files


def _closure(files, roots):
    """Файлы-корни и их зависимости (в порядке `files`); пропавшая зависимость — ошибка набора."""
    by_name = {file.name: file for file in files}
    wanted, pending = set(), list(roots)
    while pending:
        name = pending.pop()
        if name in wanted:
            continue
        if name not in by_name:
            raise ProtocolImportError(f"В наборе нет файла протокола {name}: набор неполный")
        wanted.add(name)
        pending.extend(by_name[name].dependency)
    return [file for file in files if file.name in wanted]


def _alice_roots(files):
    wanted = {tuple(name.rsplit(".", 1)): name for name in REQUIRED_TYPES}
    roots, found = [], set()
    for file in files:
        hit = {wanted[(file.package, message.name)] for message in file.message_type
               if (file.package, message.name) in wanted}
        if hit:
            roots.append(file.name)
            found |= hit
    return roots if len(found) == len(REQUIRED_TYPES) else []


def build_sets(files):
    """(полный набор станции, выборка alice-api) как FileDescriptorSet; проверка загрузкой в DescriptorPool."""
    if not files:
        raise ProtocolImportError(f"В файле не найдено ни одного описания протокола: нужен {STATION_LIBRARY} станции "
                                  "или zip с .proto / .pb")
    roots = _alice_roots(files)
    if not roots:
        raise ProtocolImportError("В наборе нет протокола NAlice.NAliceApi (TClientMessage и TServerMessage): "
                                  f"нужен {STATION_LIBRARY} станции или архив с его описаниями")
    full = descriptor_pb2.FileDescriptorSet(file=files)
    alice = descriptor_pb2.FileDescriptorSet(file=_closure(files, roots))
    for label, item in (("полный набор", full), ("alice-api", alice)):
        try:
            pool = schema_module.build_pool(item)
            for type_name in REQUIRED_TYPES:
                pool.FindMessageTypeByName(type_name)
        except (ValueError, KeyError, TypeError) as exc:
            raise ProtocolImportError(f"Протокол не загружается ({label}): {exc}") from exc
    return full, alice


# ── Источники: libquasar_daemons.so или zip с .proto / .pb ───────────────────

def source_kind(path):
    """"library" (ELF) или "zip" — по сигнатуре файла, не по имени."""
    with open(path, "rb") as handle:
        head = handle.read(4)
    if head == ELF_MAGIC:
        return "library"
    if head[:2] == b"PK":
        return "zip"
    raise ProtocolImportError(f"Нужен файл {STATION_LIBRARY} или zip-архив с .proto / .pb")


def files_from_library(path):
    """Дескрипторы из `libquasar_daemons.so` (поиск как в protodump)."""
    if os.path.getsize(path) > LIBRARY_LIMIT:
        raise ProtocolImportError("Библиотека слишком большая: файл не похож на libquasar_daemons.so станции")
    return descriptor_files(scan_descriptors(Path(path).read_bytes()))


def _member_path(name):
    """Относительный путь члена архива или None (абсолютный, «..», диск или поток NTFS)."""
    name = name.replace("\\", "/")
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or any(part == ".." or ":" in part for part in parts):
        return None
    return PurePosixPath(*parts)


def _proto_root(sources):
    """Каталог, от которого считаются import: префикс, при котором разрешается больше всего import."""
    counts = Counter()
    for text in sources.values():
        for imported in IMPORT_RE.findall(text):
            for path in sources:
                if path == imported or path.endswith("/" + imported):
                    counts[path[:len(path) - len(imported)]] += 1
    return counts.most_common(1)[0][0] if counts else ""


def _protoc(root, names):
    """`.proto` → FileDescriptorSet (с зависимостями) через grpc_tools.protoc; ошибка — текст protoc."""
    if importlib.util.find_spec("grpc_tools") is None:
        raise ProtocolImportError("Для сборки .proto нужен пакет grpcio-tools: pip install grpcio-tools")
    with tempfile.TemporaryDirectory(prefix="alica-protoc-") as work:
        output, arguments = Path(work) / "set.pb", Path(work) / "args.txt"
        # аргументы — файлом (@args.txt): сотни путей не влезают в командную строку Windows
        arguments.write_text("\n".join([f"--proto_path={root}", "--include_imports",
                                        f"--descriptor_set_out={output}", *names]), encoding="utf-8")
        try:
            run = subprocess.run([sys.executable, "-m", "grpc_tools.protoc", f"@{arguments}"], cwd=work,
                                 capture_output=True, timeout=PROTOC_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise ProtocolImportError("Сборка .proto не уложилась во время") from exc
        if run.returncode != 0 or not output.is_file():
            text = run.stderr.decode("utf-8", "replace")
            for prefix in {str(root) + os.sep, str(root).replace("\\", "/") + "/"}:   # пути — от корня архива
                text = text.replace(prefix, "")
            text = text.strip()
            raise ProtocolImportError("Не собрались .proto: " + ("\n".join(text.splitlines()[-12:]) or "protoc без вывода"))
        return list(descriptor_pb2.FileDescriptorSet.FromString(output.read_bytes()).file)


def _compile_sources(root, sources):
    prefix = _proto_root(sources)
    names = sorted(path[len(prefix):] for path in sources if path.startswith(prefix))
    base = Path(root) / prefix if prefix else Path(root)
    try:
        return _protoc(base, names)
    except ProtocolImportError as first:
        # в наборе бывают сломанные файлы: собираем только корни протокола — protoc сам подтянет import
        roots = [name for name in names if ROOT_MESSAGE_RE.search(sources[prefix + name])]
        if not roots or len(roots) == len(names):
            raise
        try:
            return _protoc(base, roots)
        except ProtocolImportError:
            raise first from None


def files_from_zip(path):
    """Дескрипторы из zip: `.pb`/`.desc`/`.protoset` (FileDescriptorSet) и `.proto` (собираются protoc)."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ProtocolImportError("Архив не открывается: нужен zip с .proto / .pb") from exc
    with archive, tempfile.TemporaryDirectory(prefix="alica-proto-") as work:
        members = archive.infolist()
        if len(members) > ZIP_MEMBERS_LIMIT:
            raise ProtocolImportError("В архиве слишком много файлов")
        sets, sources, total = [], {}, 0
        for info in members:
            lower = info.filename.lower()
            if info.is_dir() or not lower.endswith((".proto",) + SET_SUFFIXES):
                continue
            relative = _member_path(info.filename)
            if relative is None:
                continue
            total += info.file_size
            if total > ZIP_TOTAL_LIMIT:
                raise ProtocolImportError("Файлы протокола в архиве слишком большие")
            data = archive.read(info)   # распаковка ограничена объявленным размером
            if lower.endswith(".proto"):
                target = Path(work, *relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                sources[relative.as_posix()] = data.decode("utf-8", "replace")
                continue
            try:
                sets.extend(descriptor_pb2.FileDescriptorSet.FromString(data).file)
            except DecodeError:
                continue
        if not sets and not sources:
            raise ProtocolImportError("В архиве нет .proto и .pb (FileDescriptorSet)")
        compiled = _compile_sources(work, sources) if sources else []
    files, seen = [], set()
    for file in sets + compiled:
        if file.name and file.name not in seen:
            seen.add(file.name)
            files.append(file)
    return files


def schemas_from_source(path):
    """libquasar_daemons.so или zip → {"centaur": bytes, "alice-api": bytes, "source": вид, "files": {...}}."""
    kind = source_kind(path)
    full, alice = build_sets(files_from_library(path) if kind == "library" else files_from_zip(path))
    return {"centaur": full.SerializeToString(), "alice-api": alice.SerializeToString(), "source": kind,
            "files": {"centaur": len(full.file), "alice-api": len(alice.file)}}

# ── Запись ───────────────────────────────────────────────────────────────────

def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def install_schemas(result, directory=None):
    """Запись дескрипторов в каталог схем (`alica-<хеш>.pb`); прежние `alica-*.pb` удаляются."""
    directory = Path(directory or schema_module.schemas_dir())
    name = f"alica-{hashlib.sha256(result['centaur']).hexdigest()[:12]}.pb"
    info = {"source": result.get("source", ""), "files": result.get("files", {}), "imported": int(time.time())}
    write_atomic(directory / schema_module.ALICE_API, result["alice-api"])
    write_atomic(directory / name, result["centaur"])
    write_atomic(directory / (name + ".json"), json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"))
    for stale in directory.glob(schema_module.STATION_GLOB):
        if stale.name != name:
            stale.unlink(missing_ok=True)
            (directory / (stale.name + ".json")).unlink(missing_ok=True)
    return {**info, "file": name}


def import_source(path, directory=None):
    """libquasar_daemons.so или zip → схемы в каталоге; возвращает сводку (вид источника, число файлов)."""
    return install_schemas(schemas_from_source(path), directory)


# ── Состояние ────────────────────────────────────────────────────────────────

def protocol_info(directory=None):
    """Сводка установленного протокола из `alica-*.pb.json` ({} — нет или без сводки)."""
    directory = Path(directory or schema_module.schemas_dir())
    name = schema_module.station_file(directory)
    if not name:
        return {}
    try:
        value = json.loads((directory / (name + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def setup_status():
    files = protocol_info().get("files") or {}
    return {"schemas": schema_module.schemas_ready(),
            "protocol_files": files.get("centaur", 0) if isinstance(files, dict) else 0}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m server_emulator.protocol_import",
                                     description="Протокол Алисы из libquasar_daemons.so или zip с .proto / .pb (локально)")
    parser.add_argument("source", type=Path, help=f"{STATION_LIBRARY} станции или zip-архив с .proto / .pb")
    args = parser.parse_args(argv)
    try:
        info = import_source(args.source)
    except (ProtocolImportError, OSError) as exc:
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1
    print(f"Протокол: файлов {info['files'].get('centaur')} (alice-api — {info['files'].get('alice-api')}) "
          f"→ {schema_module.schemas_dir()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
