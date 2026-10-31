"""Маршрутизация вызовов ASR/TTS/LLM и цикл инструментов legacy-агента (TASK-007, DEBT-01).

ProviderManager выбирает адаптер voice_stack по settings[stt|tts|model] и вызывает
его (stt → JSON {text}, tts → (аудио, тип), model → JSON ответа чата), умеет
потоковые режимы ASR/TTS и PCM-движок звукового кеша, а также прежний (legacy)
цикл «модель → инструменты».

Методы получают runtime явным аргументом: WS-сессии работают со scoped-копиями
runtime со своими настройками профиля устройства (identities.settings_for) —
резолвинг адаптера обязан брать настройки того runtime, из которого пришёл вызов.
Публичные имена runtime.provider / chat / get_tts / ... сохранены делегатами:
тесты подменяют их на заглушки присваиванием атрибута (voice e2e, agent тесты).
"""
import copy
import json
import logging
import time

log = logging.getLogger(__name__)


class ProviderManager:
    def __init__(self, runtime):
        self.runtime = runtime  # root-runtime; scoped-копии приходят аргументом вызова

    # --- адаптеры -------------------------------------------------------------

    def adapter(self, runtime, kind):
        """Адаптер ASR/TTS/LLM для settings[stt|tts|model] (voice_stack/adapters)."""
        from .voice_stack.adapters import resolve
        return resolve(kind, runtime.settings[kind])

    async def call(self, runtime, kind, payload=None, audio=None, filename="input.wav"):
        """Вызов сервиса через адаптер: stt → JSON {text}, tts → (аудио, тип), model → JSON ответа чата."""
        cfg = copy.deepcopy(runtime.settings[kind])
        adapter = self.adapter(runtime, kind)
        if kind == "stt":
            text = await adapter.transcribe(runtime, cfg, audio, filename)
            return json.dumps({"text": text}, ensure_ascii=False).encode(), "application/json"
        if kind == "tts":
            return await adapter.speech(runtime, cfg, payload)
        return json.dumps(await adapter.complete(runtime, cfg, payload), ensure_ascii=False).encode(), "application/json"

    def asr_streaming(self, runtime):
        """Распознавание потоком (частичные результаты) — если адаптер ASR это умеет."""
        try:
            return hasattr(self.adapter(runtime, "stt"), "recognize_stream")
        except ValueError:
            return False

    async def recognize_stream(self, runtime, chunks, fmt, on_partial=None):
        return await self.adapter(runtime, "stt").recognize_stream(runtime, runtime.settings["stt"], chunks, fmt, on_partial)

    def tts_streaming(self, runtime):
        """Синтез потоком Ogg-страниц — если адаптер TTS умеет это для текущей конфигурации."""
        try:
            adapter = self.adapter(runtime, "tts")
        except ValueError:
            return False
        return hasattr(adapter, "speech_stream") and adapter.streams(runtime.settings["tts"])

    def speech_stream(self, runtime, text):
        return self.adapter(runtime, "tts").speech_stream(runtime, runtime.settings["tts"], text)

    def pcm_tts(self, runtime):
        """PCM-движок синтеза из settings["tts"] для звукового кеша (synth_pcm) или None —
        тогда озвучка идёт целиком без кеша (адаптер не отдаёт PCM, например Ogg Opus)."""
        from .voice_stack import pcm_tts
        return pcm_tts(runtime)

    # --- движок ответа модели (legacy) -----------------------------------------

    async def chat(self, runtime, text, history=None, thread=None, **context):
        """Ответ модели. graph: thread — тред агента (история в checkpoint, history не нужна),
        context — sn/device_id/dialog_id/request_id запроса; legacy: history вызывающего.
        Режим расширения (intelligence = "ext:<id>", TASK-034) — ответ его answer; расширение
        выключено/удалено/сбой — откат на scenarios с записью в recent (intelligence.py)."""
        from . import agent, intelligence
        mode_id = intelligence.ext_id(runtime.settings.get("intelligence"))
        if mode_id is not None:
            mode = intelligence.extension_mode(runtime, mode_id)
            if mode is None:
                return intelligence.ext_fallback(runtime, mode_id, "расширение выключено или режим не найден")
            return await intelligence.answer(runtime, mode, text, history, context)
        current = intelligence.level(runtime)
        if current == "scenarios":  # модель выключена режимом — фраза без сценария
            return intelligence.not_understood(runtime)
        cfg = runtime.settings["model"]
        stub = cfg.get('stub_text', '')
        if stub:
            if not isinstance(stub, str) or len(stub) > 1000:
                raise ValueError('Текст заглушки: строка до 1000 символов')
            return {'text': stub, 'tool_calls': [], 'usage': {}, 'source': 'stub'}
        if agent.engine(runtime) == "graph":
            return await runtime.agent.chat(runtime, text, thread=thread, **context)
        return await self._chat_legacy(runtime, text, history, mcp=current == "agent")

    async def _chat_legacy(self, runtime, text, history=None, mcp=True):
        """mcp=False — режим «модель»: один ответ без MCP-инструментов (model_actions допустимы)."""
        cfg = runtime.settings["model"]
        from . import mcp_hub
        tools = mcp_hub.model_tools(runtime) if mcp else []
        if runtime.settings.get("model_actions"):
            from .model_actions import TOOL_SCHEMA
            if not any(t.get("function", {}).get("name") == TOOL_SCHEMA["function"]["name"] for t in tools):
                tools.append(TOOL_SCHEMA)
        device_context = runtime.settings["device_context"]
        context = {"sn": device_context.get("sn"), "user_id": device_context.get("user_id")}
        # Базовые сведения (время, пользователь, колонки) — всегда; свой device_context профиля —
        # без служебных sn/user_id/state (state несёт конфиг колонки целиком, модели он не нужен)
        extra = {k: v for k, v in device_context.items() if k not in ("sn", "user_id", "state")}
        system = runtime.settings["system_prompt"] + "\n\n" + mcp_hub.base_context(runtime, context) \
            + ("\nКонтекст устройства: " + json.dumps(extra, ensure_ascii=False) if extra else "")
        messages = [{"role": "system", "content": system}] + (history or [])[-12:] + [{"role": "user", "content": text}]
        runs, usage = [], {}
        # Цикл инструментов: вызовы auto-инструментов (mcp_hub) исполняются, результат
        # возвращается модели, она отвечает дальше. Остальные вызовы (confirm,
        # alice_action) — предложения: уходят наружу как раньше, цикл на них прерывается.
        for _round in range(5):
            payload = {"model": cfg["model"], "temperature": cfg.get("temperature", 0.3), "messages": messages}
            if tools:
                payload["tools"] = tools
            raw, _ = await runtime.provider("model", payload)  # заглушки тестов подменяют runtime.provider
            result = json.loads(raw)
            usage = result.get("usage", usage)
            message = result["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            auto = [c for c in calls if mcp_hub.is_auto(runtime, (c.get("function") or {}).get("name"))]
            if not auto or len(auto) != len(calls):
                pending = [c for c in calls if c not in auto]
                for call in auto:  # смешанный ответ: auto исполняем, предложения возвращаем
                    runs.append(await self._run_tool(runtime, call, context))
                return {"text": message.get("content") or "", "tool_calls": pending, "usage": usage, "tool_runs": runs}
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
            for call in calls:
                run = await self._run_tool(runtime, call, context)
                runs.append(run)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": run["result"]})
        return {"text": "Не получилось завершить действие: слишком много шагов.", "tool_calls": [], "usage": usage, "tool_runs": runs}

    async def _run_tool(self, runtime, call, context):
        """Исполнить вызов модели через mcp_hub; ошибка — текстом для модели, не исключением."""
        from . import mcp_hub
        function = call.get("function") or {}
        name = function.get("name", "")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
            text = mcp_hub.result_text(await mcp_hub.call(runtime, name, arguments, context))
        except Exception as exc:  # инструмент не должен ронять ответ модели
            arguments, text = function.get("arguments"), "Ошибка инструмента: " + (str(exc) or type(exc).__name__)
        run = {"name": name, "arguments": arguments, "result": text, "ts": time.time()}
        runtime.tool_log.append(run)  # на месте: сессионная копия runtime делит журнал с панелью
        del runtime.tool_log[:-100]
        return run
