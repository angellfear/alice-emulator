"""Локальная заглушка Home Assistant: aiohttp-сервер на 127.0.0.1 (порт выбирается
системой), ручки — только те, что использует плагин home-assistant:

  GET  /api/states                        — список состояний (light.* — лампы)
  POST /api/template                      — area_name(entity_id): строка на сущность
  POST /api/services/<domain>/<service>   — вызов сервиса (тело и токен — в calls)
  POST /api/conversation/process          — conversation-агент (режим «Интеллекта»)
  POST /api/stt/<provider>                — STT (проверка X-Speech-Content)
  POST /api/tts_get_url                   — TTS: {"url", "path"} на /api/tts_proxy/<token>
  GET  /api/tts_proxy/<token>             — аудио (без авторизации, как в HA)

Состояния ламп настраиваются: stub.states (список состояний HA) и stub.areas
{entity_id: название площадки}. Каждый вызов пишется в stub.calls (dict без
аудио-байтов — только размер) для проверок тестов. Реальный Home Assistant по
сети тесты не трогают; токен — test-token (фикстуры обезличены).
"""
import re

from aiohttp import web

SPEECH = "Готово, включаю свет."
STT_TEXT = "включи свет на кухне"
AUDIO = b"ID3\x04fake-ha-audio-bytes"

DEFAULT_LIGHTS = [  # состояния как в GET /api/states HA: state + attributes.friendly_name
    {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Кухня"}},
    {"entity_id": "light.bedroom", "state": "on", "attributes": {"friendly_name": "Спальня"}},
]


class StubHA:
    """Заглушка: ручки HA + журнал вызовов; states/areas настраиваемые."""

    def __init__(self, token="test-token"):
        self.token = token
        self.base = ""  # заполняется в start()
        self.calls = []
        self.states = [dict(s) for s in DEFAULT_LIGHTS]
        self.areas = {}  # entity_id → название площадки; нет — friendly_name
        self.tts_relative = False  # True — tts_get_url отвечает только {"path"} (без url)
        self.app = web.Application()
        self.app.router.add_get("/api/states", self.states_route)
        self.app.router.add_post("/api/template", self.template)
        self.app.router.add_post("/api/conversation/process", self.conversation)
        self.app.router.add_post("/api/services/{domain}/{service}", self.service)
        self.app.router.add_post("/api/stt/{provider}", self.stt)
        self.app.router.add_post("/api/tts_get_url", self.tts_get_url)
        self.app.router.add_get("/api/tts_proxy/{token}", self.tts_proxy)

    def set_lights(self, names):
        """Лампы по названиям комнат: light.<латиница> + friendly_name — названием;
        [] — HA без ламп (закрытый список нечем закрыть)."""
        slug = {"кухня": "kitchen", "спальня": "bedroom", "ванная": "bathroom"}
        self.states = [{"entity_id": "light." + slug.get(str(n).lower(), str(n)),
                        "state": "off", "attributes": {"friendly_name": n}} for n in names]

    def _check_auth(self, request):
        """Bearer-проверка как в HA: Authorization: Bearer <токен долгоживущий>.
        Отказ тоже пишется в calls (kind: denied) — тесты видят попытку."""
        expected = f"Bearer {self.token}" if self.token else None
        given = request.headers.get("Authorization", "")
        if expected is not None and given != expected:
            self.calls.append({"kind": "denied", "path": request.path, "auth": given})
            return web.json_response({"error": "unauthorized"}, status=401)
        return None

    async def states_route(self, request):
        denied = self._check_auth(request)
        if denied is not None:
            return denied
        self.calls.append({"kind": "states", "auth": request.headers.get("Authorization", "")})
        return web.json_response(self.states)

    async def template(self, request):
        """area_name(entity_id) из датасета: {% for id in [...] %}{{ area_name(id) }}
        — строка на сущность, пустая — площадки нет (заглушка разбирает только этот
        шаблон плагина)."""
        denied = self._check_auth(request)
        if denied is not None:
            return denied
        body = await request.json()
        self.calls.append({"kind": "template", "template": body.get("template", "")})
        ids = re.findall(r'"(light\.[a-z0-9_]+)"', str(body.get("template") or ""))
        result = "\n".join(self.areas.get(entity_id, "") for entity_id in ids)
        return web.json_response({"result": result})

    async def conversation(self, request):
        denied = self._check_auth(request)
        if denied is not None:
            return denied
        body = await request.json()
        self.calls.append({"kind": "conversation", "body": body,
                           "auth": request.headers.get("Authorization", "")})
        return web.json_response({
            "response": {"speech": {"plain": {"speech": SPEECH, "extra_data": None}},
                         "card": {}, "language": body.get("language") or "ru",
                         "response_type": "action_done"},
            "conversation_id": body.get("conversation_id") or "cid-1"})

    async def service(self, request):
        denied = self._check_auth(request)
        if denied is not None:
            return denied
        body = await request.json()
        self.calls.append({"kind": "service",
                           "path": request.path,
                           "domain": request.match_info["domain"],
                           "service": request.match_info["service"],
                           "body": body,
                           "auth": request.headers.get("Authorization", "")})
        entities = body.get("entity_id") or "light.kitchen"
        on = request.match_info["service"] == "turn_on"
        attrs = {k: v for k, v in body.items() if k != "entity_id"}  # данные вызова — как есть
        states = [{"entity_id": entity, "state": "on" if on else "off", "attributes": attrs}
                  for entity in (entities if isinstance(entities, list) else [entities])]
        return web.json_response(states)  # HA отвечает новыми состояниями сущностей

    async def stt(self, request):
        denied = self._check_auth(request)
        if denied is not None:
            return denied
        audio = await request.read()
        self.calls.append({"kind": "stt", "provider": request.match_info["provider"],
                           "speech_content": request.headers.get("X-Speech-Content", ""),
                           "audio_bytes": len(audio),
                           "auth": request.headers.get("Authorization", "")})
        return web.json_response({"text": STT_TEXT, "result": "success"})

    async def tts_get_url(self, request):
        denied = self._check_auth(request)
        if denied is not None:
            return denied
        body = await request.json()
        self.calls.append({"kind": "tts_get_url", "body": body,
                           "auth": request.headers.get("Authorization", "")})
        if self.tts_relative:  # вариант ответа: только относительный path
            return web.json_response({"path": "/api/tts_proxy/token-1"})
        return web.json_response({"url": self.base + "/api/tts_proxy/token-1",
                                  "path": "/api/tts_proxy/token-1"})

    async def tts_proxy(self, request):
        self.calls.append({"kind": "tts_proxy", "token": request.match_info["token"]})
        return web.Response(body=AUDIO, content_type="audio/mpeg")


async def start(token="test-token"):
    """Запуск заглушки на свободном порту 127.0.0.1 → (runner, stub, base_url)."""
    stub = StubHA(token=token)
    runner = web.AppRunner(stub.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    stub.base = f"http://127.0.0.1:{port}"
    return runner, stub, stub.base
