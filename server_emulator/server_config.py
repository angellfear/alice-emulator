"""Настройки самостоятельного сервера (TASK-042): `state/server.json` — адрес, порты, SSL, сертификат.

Читаются до старта приложения: адрес и порты нужны до запуска `Runtime`. Аргументы командной строки
перекрывают файл (`effective`). Применение — перезапуском: `save` хранит прежний файл
(`server.json.prev`) и метку `server.json.pending`; если сервер с новыми настройками не поднялся,
`rollback` возвращает прежние. Колонкам нужны 443 (TLS) и 80 (HTTP, `quasar.yandex.net`).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import uuid
from pathlib import Path

FILE = "server.json"
DEFAULTS = {"host": "0.0.0.0", "port": 443, "ssl": True, "cert": "", "key": "", "http_port": 80,
            "front": True, "front_dns": ["8.8.8.8", "1.1.1.1"]}  # фронт TLS и DNS туннелей (TASK-067)
RESTART_CODE = 75   # код выхода «перезапустить»: лаунчер (TASK-045) запускает сервер сразу
UPDATE_CODE = 76    # код выхода «идёт самообновление» (TASK-040): updater перезапустит сервер сам


def _path(state):
    return Path(state) / FILE


def resolve(state, value):
    """Путь к файлу сертификата: абсолютный или относительно папки данных; пусто — None."""
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(state) / path


def load(state):
    """Настройки из файла поверх значений по умолчанию; битый файл — значения по умолчанию."""
    try:
        value = json.loads(_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {key: value.get(key, default) for key, default in DEFAULTS.items()}


def effective(config, overrides):
    """Аргументы командной строки (не None) поверх файла → (настройки, перекрытые поля)."""
    overridden = sorted(key for key, value in overrides.items() if value is not None and key in DEFAULTS)
    return {**config, **{key: overrides[key] for key in overridden}}, overridden


def _port(value, name, allow_zero=False):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: нужно число") from None
    if not (0 if allow_zero else 1) <= number <= 65535 or isinstance(value, bool):
        raise ValueError(f"{name}: от {0 if allow_zero else 1} до 65535")
    return number


def ssl_context(state, config):
    """SSLContext по настройкам или None (SSL выключен); ошибка сертификата — ValueError по-русски."""
    if not config.get("ssl"):
        return None
    cert, key = resolve(state, config.get("cert")), resolve(state, config.get("key"))
    if cert is None:
        raise ValueError("SSL включён, но не указан сертификат")
    for label, path in (("Сертификат", cert), ("Ключ", key)):
        if path is not None and not path.is_file():
            raise ValueError(f"{label}: файл не найден — {path}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as exc:
        raise ValueError(f"Сертификат не загружается: {exc}") from None
    return context


def validate(state, value):
    """Проверка формы перед сохранением → нормализованные настройки; ошибка — ValueError."""
    if not isinstance(value, dict):
        raise ValueError("Ожидается объект настроек")
    host = str(value.get("host", DEFAULTS["host"])).strip()
    if not host:
        raise ValueError("Адрес: пусто")
    dns = value.get("front_dns", DEFAULTS["front_dns"])
    if isinstance(dns, str):  # из формы: через запятую
        dns = [item.strip() for item in dns.split(",")]
    if not isinstance(dns, list) or not dns or len(dns) > 4 \
            or not all(isinstance(item, str) and item.strip() for item in dns):
        raise ValueError("DNS туннелей: адреса через запятую (до 4)")
    config = {"host": host, "port": _port(value.get("port", DEFAULTS["port"]), "Порт"),
              "ssl": bool(value.get("ssl", DEFAULTS["ssl"])),
              "cert": str(value.get("cert") or "").strip(), "key": str(value.get("key") or "").strip(),
              "http_port": _port(value.get("http_port", DEFAULTS["http_port"]), "HTTP-порт", allow_zero=True),
              "front": bool(value.get("front", DEFAULTS["front"])),
              "front_dns": [item.strip() for item in dns]}
    if config["port"] == config["http_port"]:
        raise ValueError("Порт и HTTP-порт должны различаться")
    ssl_context(state, config)
    return config


def port_busy(host, port):
    """Пробная привязка: True — порт уже занят (другим процессом или этим сервером)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def _write(path, data):
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(data, encoding="utf-8")
    os.replace(temp, path)


def save(state, config):
    """Запись настроек: прежний файл — в `.prev`, метка `.pending` до успешного старта."""
    path = _path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copyfile(path, path.with_name(FILE + ".prev"))
    _write(path, json.dumps(config, ensure_ascii=False, indent=2))
    path.with_name(FILE + ".pending").write_text("1", encoding="utf-8")


def confirm(state):
    """Сервер поднялся с новыми настройками — метка снимается."""
    _path(state).with_name(FILE + ".pending").unlink(missing_ok=True)


def rollback(state):
    """Старт не удался после сохранения: вернуть прежний файл. True — вернули (нужен перезапуск)."""
    path = _path(state)
    pending, previous = path.with_name(FILE + ".pending"), path.with_name(FILE + ".prev")
    if not pending.is_file():
        return False
    pending.unlink(missing_ok=True)
    if previous.is_file():
        os.replace(previous, path)
    else:
        path.unlink(missing_ok=True)
    return True


def local_url(config):
    """Адрес сервера для этого компьютера (трей, панель): HTTP-порт, если включён, иначе основной."""
    host = config["host"] if config["host"] not in ("0.0.0.0", "::", "") else "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    if config.get("http_port"):
        return f"http://{host}:{config['http_port']}"
    return f"{'https' if config.get('ssl') else 'http'}://{host}:{config['port']}"
