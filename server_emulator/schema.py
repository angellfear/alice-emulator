"""Cached, dependency-complete protobuf descriptor pools for decoder plugins.

Дескрипторы протокола (`schemas/*.pb`) не входят в репозиторий: их извлекает `protocol_import.py` из
`libquasar_daemons.so` станции пользователя или из zip с `.proto` / `.pb`. Пока их нет, `load_type` поднимает `SetupRequired` — сервер при этом
работает (UI и admin-API), а протокольные обработчики отвечают `setup_required`.
"""
import os
from functools import lru_cache
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

DEFAULT_SCHEMAS = Path(__file__).resolve().parent / "schemas"
ALICE_API = "alice-api.pb"
STATION_GLOB = "alica-*.pb"


class SetupRequired(RuntimeError):
    """Нет дескрипторов протокола: нужен libquasar_daemons.so или zip с .proto (первый запуск)."""

    code = "setup_required"

    def __init__(self, message="Нужна настройка: загрузите протокол (libquasar_daemons.so или zip с .proto / .pb)"):
        super().__init__(message)


def schemas_dir():
    """Каталог дескрипторов: `ALICA_SCHEMAS_DIR` или `server_emulator/schemas/`."""
    override = os.environ.get("ALICA_SCHEMAS_DIR")
    return Path(override) if override else DEFAULT_SCHEMAS


def station_file(root=None):
    """Имя файла полного набора станции (`alica-<хеш>.pb`) или None; при нескольких — новейший."""
    found = sorted(Path(root or schemas_dir()).glob(STATION_GLOB), key=lambda p: (p.stat().st_mtime_ns, p.name))
    return found[-1].name if found else None


def schema_file(root, schema):
    """Имя набора в каталоге: "alice-api", "centaur" (файл станции любой версии) или готовое имя файла."""
    if schema == "alice-api":
        return ALICE_API
    if schema == "centaur":
        root = Path(root).resolve()
        return station_file(schemas_dir() if root == DEFAULT_SCHEMAS.resolve() else root) or "alica-missing.pb"
    return schema


def schemas_ready():
    root = schemas_dir()
    return (root / ALICE_API).is_file() and station_file(root) is not None


def build_pool(descriptor):
    """DescriptorPool из FileDescriptorSet; файлы добавляются по мере готовности зависимостей."""
    pool=descriptor_pool.DescriptorPool()
    pending=list(descriptor.file)
    while pending:
        remaining=[]
        errors=[]
        for file in pending:
            try:pool.Add(file)
            except Exception as exc:
                remaining.append(file)
                errors.append(str(exc))
        if len(remaining)==len(pending):
            raise ValueError("Не загружены зависимости descriptor: "+"; ".join(errors[:3]))
        pending=remaining
    return pool


@lru_cache(maxsize=8)
def _pool(path,mtime,size):
    return build_pool(descriptor_pb2.FileDescriptorSet.FromString(Path(path).read_bytes()))


def load_type(root,filename,message_type):
    root=Path(root).resolve()
    if root==DEFAULT_SCHEMAS.resolve():  # подмена каталога схем: тесты «без схем», нестандартная установка
        root=schemas_dir().resolve()
    path=(root/filename).resolve()
    if not path.is_relative_to(root):raise ValueError("Descriptor должен находиться в schemas/")
    try:stat=path.stat()
    except FileNotFoundError as exc:raise SetupRequired() from exc
    pool=_pool(str(path),stat.st_mtime_ns,stat.st_size)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(message_type))


def message_dict(message):
    return json_format.MessageToDict(message,preserving_proto_field_name=True,descriptor_pool=message.DESCRIPTOR.file.pool)
