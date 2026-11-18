"""Реестр сервисных аккаунтов — «Настройки → Авторизация в сервисах» панели.

Вход в сторонние сервисы для поставщиков данных: Яндекс (вход по QR — несколько
аккаунтов, один основной) и Spotify (вход через точки login поставщика, redirect
на панель). Метаданные — state/service_accounts/index.json, секреты (токены) —
state/service_accounts/<id>.json: наружу API отдаёт только статусы, в журналы и
тексты ошибок значения не попадают; пароли не хранятся вообще (токены).

Отдельно от аккаунтов — токены колонок: колонка сама приносит свой OAuth-токен
(X-UPRX-OAUTH-TOKEN в WS-handshake, Authorization: OAuth … в HTTP), их по SN
собирает music.note_token в БД состояния, поставщики берут токен своей колонки;
реестр — запасной источник для сервиса, если у колонки токена нет.

Аккаунты «yandex» хранят x_token и кеш токенов сервисов в формате
account.AccountSession — тот же код читает и обмен x_token → токен сервиса
(oauth.mobile.yandex.net), и iot-импорт конфигуратора.

Перенос при первом запуске: старые сессии входа state/account/sessions/<файл>.json
(кнопка «Яндекс» у пользователя) становятся аккаунтами «yandex», их папка
удаляется; meta provider_login:<ext_id> плагинов Spotify переезжает в реестр при
первом чтении статуса поставщика (расширения к старту не загружены).
"""
import base64
import io
import json
import re
import shutil
import time
from uuid import uuid4

import qrcode

from .account import AccountError, AccountSession


def _qr_png(link):
    """QR-код ссылки подтверждения в PNG (data-URI) — панель показывает картинку,
    пользователь сканирует её приложением Яндекса на телефоне."""
    image = qrcode.make(link)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class ServiceAccounts:
    """Реестр аккаунтов сервисов: index.json (метаданные) + файл секретов на аккаунт.
    Чтение состояние не меняет; атомарная запись не нужна — панель административная,
    одновременных правок из двух окон не ждём."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.root = runtime.directory / "service_accounts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._qr = None  # scratch-сессия начатого QR-входа (csrf/track в памяти)
        self._migrate_sessions()

    # --- хранение ---------------------------------------------------------------
    def _index(self):
        """Список метаданных аккаунтов из index.json (без секретов)."""
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items
                if isinstance(item, dict) and item.get("id") and item.get("service")]

    def _save_index(self, accounts):
        self.index_path.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False),
                                   encoding="utf-8")

    def secret_path(self, account_id):
        return self.root / (re.sub(r"[^A-Za-z0-9_.-]", "_", str(account_id)) + ".json")

    def secret(self, account_id):
        """Файл секретов аккаунта (токены) или {} — наружу API не отдаётся."""
        try:
            data = json.loads(self.secret_path(account_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_secret(self, account_id, payload):
        self.secret_path(account_id).write_text(json.dumps(payload, ensure_ascii=False),
                                                encoding="utf-8")

    # --- реестр -------------------------------------------------------------------
    def list(self, service=None):
        """Аккаунты сервиса (или все) со статусом входа: [{id, service, title, default,
        created, login}] — секретов нет."""
        accounts = [dict(item) for item in self._index()
                    if service is None or item["service"] == service]
        for record in accounts:
            payload = self.secret(record["id"])
            record["login"] = bool(payload.get("x_token") or payload.get("access_token"))
        return accounts

    def record(self, account_id):
        found = next((item for item in self._index() if item["id"] == account_id), None)
        if found is None:
            raise ValueError("Аккаунт не найден")
        return found

    def default(self, service):
        """Основной аккаунт сервиса (или None — ещё не входили)."""
        return next((item for item in self.list(service) if item.get("default")), None)

    def add(self, service, payload, title=None):
        """Новый аккаунт сервиса: payload (токены) — в файл секретов, запись — в index.
        Повторный вход с тем же логином (title) обновляет существующий аккаунт, а не
        плодит дубликаты; первый аккаунт сервиса становится основным."""
        accounts = self._index()
        existing = next((item for item in accounts
                         if item["service"] == service and title and item.get("title") == title), None)
        if existing is not None:
            record, others = existing, [item for item in accounts if item["id"] != existing["id"]]
        else:
            record = {"id": f"{service}-{uuid4().hex[:8]}", "service": service,
                      "title": title or service, "created": int(time.time())}
            others = accounts
        record["default"] = not any(item.get("default")
                                    for item in others if item["service"] == service)
        self._save_secret(record["id"], payload)
        self._save_index(others + [record])
        return dict(record)

    def remove(self, account_id):
        """Удаление аккаунта: запись из index и файл секретов. Основной уходит вместе
        с аккаунтом — основным становится первый оставшийся того же сервиса."""
        accounts = self._index()
        dropped = self.record(account_id)
        rest = [item for item in accounts if item["id"] != account_id]
        if dropped.get("default"):
            successor = next((item for item in rest if item["service"] == dropped["service"]), None)
            if successor is not None:
                successor["default"] = True
        self._save_index(rest)
        self.secret_path(account_id).unlink(missing_ok=True)

    def set_default(self, account_id):
        """Основной аккаунт сервиса: у прочих того же сервиса флаг снимается."""
        record = self.record(account_id)
        accounts = self._index()
        for item in accounts:
            if item["service"] == record["service"]:
                item["default"] = item["id"] == account_id
        self._save_index(accounts)

    # --- вход в Яндекс по QR --------------------------------------------------------
    async def yandex_qr_start(self):
        """Начало QR-входа: QR-код ссылки подтверждения — прямо в панели, сканирует
        приложение Яндекса на телефоне (как в Home Assistant-интеграции YandexStation;
        браузер для входа не открывается). Механизм — account.AccountSession (страница
        pwl-yandex, magic-код), scratch-файл — до подтверждения, затем аккаунт
        переезжает в реестр."""
        session = AccountSession(self.runtime, path=self.root / "_qr.json")
        result = await session.qr_start()
        result["qr"] = _qr_png(result["link"])
        self._qr = session
        return result

    async def yandex_qr_poll(self):
        """Опрос подтверждения QR: waiting — ждём (сессия хранится до исхода); done —
        сессия стала аккаунтом реестра (x_token + имя входа из short_info). Разовый
        сбой паспорта — waiting (вход ещё идёт); таймаут окна подтверждения сбрасывает
        вход — следующий qr_start начинает заново."""
        session = self._qr
        if session is None:
            raise AccountError("QR-вход не начат")
        try:
            result = await session.qr_poll()
        except AccountError as exc:
            if not session.qr_pending():  # таймаут/полу-завершённый обмен — вход прекращён
                self._qr = None
                raise
            return {"state": "waiting"}  # разовый сбой сети — продолжаем ждать
        if result.get("state") != "done":
            return result
        self._qr = None
        payload = self.secret_path("_qr").read_text(encoding="utf-8")
        session.forget()  # scratch-файл удалён, токен уже в payload
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise AccountError("Вход не удался: сессия не сохранилась") from exc
        record = self.add("yandex", data, title=data.get("display_login") or None)
        return {"state": "done", "id": record["id"], "title": record["title"],
                "default": bool(record.get("default"))}

    # --- Spotify ---------------------------------------------------------------------
    def save_login(self, service, payload, title=None):
        """Токены входа поставщика (точки login плагина) → аккаунт реестра: с логином —
        свой аккаунт (повторный вход тем же логином обновляет запись), без логина
        (например, обновление токена по статусу) — перезапись основного аккаунта."""
        if title is None:
            record = self.default(service)
            if record is not None:
                self._save_secret(record["id"], payload)
                return dict(record)
        return self.add(service, payload, title=title)

    def tokens(self, service):
        """Секреты основного аккаунта сервиса (для точек login и вызовов поставщика)
        или None — аккаунта нет."""
        record = self.default(service)
        if record is None:
            return None
        return self.secret(record["id"]) or None

    # --- перенос ----------------------------------------------------------------------
    def _migrate_sessions(self):
        """Старые сессии входа (state/account/sessions/<файл>.json) → аккаунты «yandex»;
        папка account/ удаляется, когда опустела."""
        sessions = self.runtime.directory / "account" / "sessions"
        if not sessions.is_dir():
            return
        for path in sorted(sessions.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or not payload.get("x_token"):
                continue
            self.add("yandex", payload, title=payload.get("display_login") or None)
            path.unlink(missing_ok=True)
        if not any(sessions.iterdir()):
            shutil.rmtree(self.runtime.directory / "account", ignore_errors=True)

    def migrate_plugin_login(self, meta_key, service):
        """Разовый перенос meta provider_login:<ext_id> (токены входа плагина) →
        аккаунт сервиса; вызывается из ui/api/providers при первом чтении статуса —
        расширения к старту сервера ещё не загружены. Возвращает payload или None."""
        store = getattr(self.runtime, "store", None)
        if store is None:
            return None
        raw = store.meta(meta_key)
        if not raw:
            return self.tokens(service)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        store.set_meta(meta_key, "")  # перенос сделан (или ключ был мусором) — не повторять
        if isinstance(payload, dict) and payload.get("access_token"):
            self.save_login(service, payload)
        return self.tokens(service)
