"""Пользовательский адаптер state/voice_stack/adapters/ старого формата: загружается
реестром и работает рядом с встроенными адаптерами на LiteLLM — контракт модуля
(SERVICES, complete(runtime, config, payload) в формате OpenAI, aiohttp через
runtime.lifecycle.client_session()) не менялся."""
import json
import sys

from aiohttp import ClientSession, web

from server_emulator.voice_stack import adapters


ADAPTER = '''
"""Свой LLM-адаптер старого формата: OpenAI-совместимый сервер напрямую через aiohttp."""
from aiohttp import ClientTimeout

TITLE = "Мой старый адаптер"
SERVICES = {"llm": {"fields": [{"key": "url", "label": "URL", "type": "text"},
                               {"key": "model", "label": "Модель", "type": "text"},
                               {"key": "api_key", "label": "Ключ", "type": "password"}],
                    "defaults": {"url": "https://api.example.com/v1/chat/completions", "model": "my-model"}}}


async def complete(runtime, config, payload):
    async with runtime.lifecycle.client_session().post(
            config["url"], json=payload, timeout=ClientTimeout(total=30)) as response:
        return json.loads(await response.text())
'''


async def test_old_format_user_adapter_loads_and_serves(tmp_path):
    user_dir = tmp_path / "state" / "voice_stack" / "adapters"
    user_dir.mkdir(parents=True)
    (user_dir / "myllm.py").write_text("import json\n" + ADAPTER, encoding="utf-8")
    previous, cache = adapters._user_dir, dict(adapters._cache)
    adapters.configure(user_dir)
    try:
        module = adapters.get("myllm")
        assert module.TITLE == "Мой старый адаптер"
        assert "myllm" in adapters.available()

        async def chat(request):
            body = await request.json()
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "ok " + body["messages"][0]["content"]}}],
                                      "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            class Lifecycle:
                def __init__(self, session):
                    self.session = session

                def client_session(self):
                    return self.session

            async with ClientSession() as session:
                result = await module.complete(
                    type("R", (), {"lifecycle": Lifecycle(session)})(),
                    {"url": f"http://127.0.0.1:{runner.addresses[0][1]}/v1/chat/completions", "model": "my-model"},
                    {"model": "my-model", "messages": [{"role": "user", "content": "привет"}]})
        finally:
            await runner.cleanup()
        assert result["choices"][0]["message"]["content"] == "ok привет"
        assert result["usage"]["total_tokens"] == 2
    finally:
        adapters._cache.clear()
        adapters._cache.update(cache)
        if previous:
            adapters.configure(previous)
        else:
            adapters._user_dir = None
        for name in [m for m in sys.modules if m.startswith("voice_adapter_")]:
            del sys.modules[name]
