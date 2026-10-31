"""Сборка protobuf из JSON сценария: тип + поля → сообщение по дескриптору.

Сценарий описывает payload директивы (или целое сообщение в отладочном raw)
деревом полей по именам proto; значения уже раскрыты шаблонизатором
(bindings.expand), здесь только приведение типов по дескриптору: строки
шаблонов → int/float/bool, enum — по имени или числу, bytes — base64.
Предметной логики нет: что и каким значением заполнить, решает JSON.
"""
from __future__ import annotations

from google.protobuf import json_format
from google.protobuf.descriptor import FieldDescriptor

from ..schema import load_type, schema_file

# Наборы дескрипторов (server_emulator/schemas, из APK станции): alice-api — транспорт
# AAPI (TServerMessage), centaur — capability-директивы станции (alica-<хеш>.pb).

_INTS = {FieldDescriptor.CPPTYPE_INT32, FieldDescriptor.CPPTYPE_INT64,
         FieldDescriptor.CPPTYPE_UINT32, FieldDescriptor.CPPTYPE_UINT64}
_FLOATS = {FieldDescriptor.CPPTYPE_FLOAT, FieldDescriptor.CPPTYPE_DOUBLE}
_WELL_KNOWN_JSON = {"google.protobuf.Struct", "google.protobuf.Value", "google.protobuf.ListValue",
                    "google.protobuf.Any", "google.protobuf.Timestamp", "google.protobuf.Duration"}


class ProtoBuildError(ValueError):
    pass


def message_class(schemas_root, proto, schema="centaur"):
    filename = schema_file(schemas_root, schema)
    try:
        return load_type(schemas_root, filename, proto)
    except KeyError as exc:
        raise ProtoBuildError(f"тип {proto} отсутствует в схеме {filename}") from exc


def build(schemas_root, proto, fields, schema="centaur"):
    """Сообщение proto из дерева fields (имена полей proto или json_name)."""
    cls = message_class(schemas_root, proto, schema)
    message = cls()
    try:
        json_format.ParseDict(_coerce_message(cls.DESCRIPTOR, fields or {}, proto), message)
    except json_format.ParseError as exc:
        raise ProtoBuildError(f"{proto}: {exc}") from exc
    return message


def encode(schemas_root, proto, fields, schema="centaur"):
    return build(schemas_root, proto, fields, schema).SerializeToString()


def _field(descriptor, name):
    field = descriptor.fields_by_name.get(name)
    if field is None:
        field = next((f for f in descriptor.fields if f.json_name == name), None)
    return field


def _coerce_message(descriptor, value, path):
    if descriptor.full_name in _WELL_KNOWN_JSON:
        return value  # JSON-представление well-known типов json_format принимает как есть
    if not isinstance(value, dict):
        raise ProtoBuildError(f"{path}: ожидается объект {descriptor.full_name}")
    result = {}
    for name, item in value.items():
        field = _field(descriptor, name)
        if field is None:
            raise ProtoBuildError(f"{path}: поле {name!r} отсутствует в {descriptor.full_name}")
        where = f"{path}.{name}"
        if field.message_type is not None and field.message_type.GetOptions().map_entry:
            if not isinstance(item, dict):
                raise ProtoBuildError(f"{where}: map — объект")
            value_field = field.message_type.fields_by_name["value"]
            result[name] = {str(k): _coerce_single(value_field, v, f"{where}[{k}]") for k, v in item.items()}
        elif field.is_repeated:
            if not isinstance(item, list):
                raise ProtoBuildError(f"{where}: repeated — список")
            result[name] = [_coerce_single(field, v, f"{where}[{i}]") for i, v in enumerate(item)]
        else:
            result[name] = _coerce_single(field, item, where)
    return result


def _coerce_single(field, value, path):
    if field.cpp_type == FieldDescriptor.CPPTYPE_MESSAGE:
        return _coerce_message(field.message_type, value, path)
    try:
        if field.cpp_type in _INTS:
            return int(float(value)) if isinstance(value, str) else int(value)
        if field.cpp_type in _FLOATS:
            return float(value)
        if field.cpp_type == FieldDescriptor.CPPTYPE_BOOL:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "да")
            return bool(value)
        if field.cpp_type == FieldDescriptor.CPPTYPE_ENUM:
            return int(value) if isinstance(value, str) and value.lstrip("-").isdigit() else value
        if field.cpp_type == FieldDescriptor.CPPTYPE_STRING:
            return value if field.type == FieldDescriptor.TYPE_BYTES else str(value)
    except (TypeError, ValueError) as exc:
        raise ProtoBuildError(f"{path}: значение {value!r} не приводится к типу поля") from exc
    return value
