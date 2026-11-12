"""Домен providers («Поставщики данных»): виды поставщиков, выбор активного, вход аккаунта.

Виды приходят из объявлений contributes.providers включённых плагинов (реестр
server_emulator/providers.py): сейчас музыка, погода и новости, новый вид появляется
объявлением без правки ядра. GET отдаёт только виды, у которых есть поставщики: по ним
строится группа вкладок. Выбор активного — точечное сохранение ключа
settings["providers"][<вид>] (пусто — первый включённый по алфавиту), как у
intelligence: полная копия настроек откатывала бы остальные ключи. Неизвестный вид или
выключенный/неизвестный id — 400.

Вход аккаунта (точки login_start/login_callback/login_status поставщика, кнопка «Войти»
только у объявивших): POST start возвращает URL страницы входа (redirect — loopback
из Host запроса; Spotify разрешает любой порт для http://127.0.0.1), GET callback —
цель redirect из браузера (без X-Emulator: браузер после Spotify): код меняется на
токены, они сохраняются в meta state/ (ключ provider_login:<ext_id>) и наружу не идут —
браузеру и GET отдают только статус.
"""
import copy
import json

from aiohttp import web

from ... import music
from ... import providers
from . import route

LOGIN_META_PREFIX = "provider_login:"  # + ext_id — JSON токенов входа в meta state/
LOGIN_CALLBACK_PAGE = ("<!doctype html><html lang=ru><meta charset=utf-8>"
                       "<title>Вход в аккаунт</title>"
                       "<body style='font-family:sans-serif;max-width:400px;margin:40px auto'>"
                       "<p>{}</p><p style=font-size:13px>Окно можно закрыть.</p></body>")


def provider_items(kind, runtime=None):
    """Записи вида (по алфавиту id) для API: origin встроенного — builtin.
    У поставщиков с точками login — флаг login и статус (токены наружу не идут)."""
    items = []
    for pid, item in sorted((providers.PROVIDERS.get(kind) or {}).items()):
        status = {}
        if music.is_yandex_music(item):
            status["oauth_captured"] = bool(music.captured_oauth())  # сам токен наружу не идёт
        login = bool(item.get("login"))
        if login and runtime is not None:
            status["login_authorized"] = bool(_login_tokens(runtime, item))
        items.append({"id": pid, "title": str(item.get("title") or pid),
                      "extension": str(item.get("ext_id") or ""),
                      "origin": str(item.get("origin") or "extension"),
                      "enabled": True, "login": login, "status": status})
    return items


def _login_context(runtime, record):
    """ExtensionContext плагина-поставщика для точек login (настройки свежие)."""
    return runtime.extensions.context(record["ext_id"])


def _login_meta(runtime, record):
    """Сохранённые токены входа из meta state/ (JSON) или None — наружу не отдаются."""
    raw = runtime.store.meta(LOGIN_META_PREFIX + str(record["ext_id"]))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _login_tokens(runtime, record):
    """Сохранённые токены входа (для точек login поставщика) или None."""
    return _login_meta(runtime, record)


def _save_login_tokens(runtime, record, tokens):
    """Токены входа → meta state/ (не в логи и не в ответы API)."""
    runtime.store.set_meta(LOGIN_META_PREFIX + str(record["ext_id"]),
                           json.dumps(tokens, ensure_ascii=False))


def _record(runtime, kind, pid):
    if kind not in providers.kinds():
        raise ValueError("Неизвестный вид поставщиков: " + kind)
    record = (providers.PROVIDERS.get(kind) or {}).get(pid)
    if record is None:
        raise ValueError("Неизвестный или выключенный поставщик: " + pid)
    if not record.get("login"):
        raise ValueError("Поставщик не объявляет вход аккаунта: " + pid)
    return record


@route("providers_login", methods=("POST",))
async def providers_login(request, runtime, app):
    """Вход аккаунта поставщика: POST start → URL страницы входа (redirect — loopback
    из Host); POST status → {"authorized", "expires_in"?} — при истёкшем access
    поставщик сам обновляет токен (новый набор перезаписывается в meta). Цель redirect —
    отдельный GET providers_login_callback (браузер после Spotify, без X-Emulator)."""
    body = await request.json()
    kind = str(body.get("kind") or "")
    pid = str(body.get("id") or "").strip()
    try:
        record = _record(runtime, kind, pid)
        action = str(body.get("action") or "").strip()
        if action == "start":
            redirect_uri = f"http://{request.host}/emulator/api/providers_login_callback"
            result = await record["login"]["login_start"](_login_context(runtime, record), redirect_uri)
            LOGIN_PENDING[str(result.get("state") or "")] = (kind, pid)
            return web.json_response({"url": result["url"]})
        if action == "status":
            tokens = _login_tokens(runtime, record)
            result = await record["login"]["login_status"](_login_context(runtime, record), tokens)
            if result.get("tokens"):
                _save_login_tokens(runtime, record, result["tokens"])
            return web.json_response({"authorized": bool(result.get("authorized")),
                                      "expires_in": result.get("expires_in"),
                                      "error": result.get("error")})
        raise ValueError("Неизвестное действие входа: " + action)
    except ValueError:
        raise  # домен сам отдаёт 400
    except Exception as exc:  # noqa: BLE001 — готовый текст сбоя входа панели
        return web.json_response({"error": str(exc)[:300]}, status=502)


@route("providers_login_callback", methods=("GET",))
async def providers_login_callback(request, runtime, app):
    """Цель redirect входа аккаунта (браузер после Spotify — без X-Emulator):
    обмен кода на токены с сохранением в meta state/, браузеру — страница-статус."""
    params = dict(request.query)
    pending = LOGIN_PENDING.pop(str(params.get("state") or ""), None)
    if pending is None:
        return web.Response(status=400, content_type="text/html",
                            text=LOGIN_CALLBACK_PAGE.format("Вход не удался: неизвестная заявка входа."))
    kind, pid = pending
    try:
        record = _record(runtime, kind, pid)
        result = await record["login"]["login_callback"](
            _login_context(runtime, record), params)
    except Exception as exc:  # noqa: BLE001 — текст ошибки в странице отказа
        return web.Response(status=400, content_type="text/html",
                            text=LOGIN_CALLBACK_PAGE.format("Вход не удался: " + str(exc)[:200]))
    if result.get("tokens"):
        _save_login_tokens(runtime, record, result["tokens"])
    ok = bool(result.get("authorized"))
    text = ("Вход в «" + str(record.get("title") or pid) + "» выполнен." if ok
            else "Вход не удался: " + str(result.get("error") or "неизвестная ошибка"))
    return web.Response(status=200 if ok else 400, content_type="text/html",
                        text=LOGIN_CALLBACK_PAGE.format(text))


LOGIN_PENDING = {}  # state → (kind, pid) — между start и callback (authlib state)


@route("providers", methods=("GET", "PUT"))
async def providers_view(request, runtime, app):
    if request.method == "GET":
        kinds = {}
        for kind in providers.kinds():
            kinds[kind] = {"title": providers.kind_title(kind),
                           "selected": str(((runtime.settings or {}).get("providers") or {})
                                           .get(kind) or ""),
                           "providers": provider_items(kind, runtime)}
        return web.json_response({"kinds": kinds,
                                  "phrase_order": copy.deepcopy(
                                      runtime.settings.get("phrase_order")
                                      or {"kinds": [], "plugins": {}})})

    body = await request.json()
    if "order" in body:  # ручной порядок справочника фраз: drag-and-drop вкладок видов
        settings = copy.deepcopy(runtime.settings)
        settings["phrase_order"] = body.get("order")  # форму проверит settings_service
        return web.json_response(await runtime.save_settings(settings, warm=False))
    kind = str(body.get("kind") or "")
    pid = str(body.get("id") or "").strip()
    if kind not in providers.kinds():
        raise ValueError("Неизвестный вид поставщиков: " + kind)
    if pid and pid not in (providers.PROVIDERS.get(kind) or {}):
        raise ValueError("Неизвестный или выключенный поставщик: " + pid)
    settings = copy.deepcopy(runtime.settings)
    settings.setdefault("providers", {})[kind] = pid  # "" — авто: первый включённый
    return web.json_response(await runtime.save_settings(settings, warm=False))
