"""AgentService: агент на LangChain create_agent (рантайм LangGraph).

Треды — checkpointer LangGraph в state/agent.sqlite3: история диалога переживает
переподключение колонки и рестарт. Длинный тред сжимается SummarizationMiddleware.
Инструменты класса external/config/destructive (agent/policy.py) останавливают прогон
interrupt'ом HumanInTheLoopMiddleware; подтверждение — голосом («да»/«нет») следующей
репликой в том же треде или кнопкой панели (resume). Ответ «другое» снимает
подтверждение: модель получает реплику вместо результата инструмента.

Долговременная память (agent/memory.py): перед прогоном в промпт подставляется то, что
агент помнит о владельце колонки; после прогона тред ставится в очередь фоновой рефлексии.

Эксплуатация (agent/ops.py): каждый прогон пишется в журнал, по нему считается дневной бюджет
пользователя; PII-фильтр вырезает секреты и номера карт до отправки провайдеру модели.

Соединение SQLite открывается на прогон: нет привязки к event loop и фоновых потоков
между вызовами; прогоны одного треда сериализуются замком.
"""
import asyncio
import json
import re
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import (HumanInTheLoopMiddleware, ModelCallLimitMiddleware,
                                         SummarizationMiddleware)
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .. import mcp_hub
from . import memory, ops, policy, scenarios
from .model import RuntimeChatModel
from .tools import build

DEFAULTS = {
    "engine": "graph", "max_model_calls": 6, "summarize_after": 40, "keep_messages": 20,
    "confirm_yes": ["да", "ага", "угу", "подтверждаю", "выполняй", "давай", "конечно", "хорошо", "ок", "окей"],
    "confirm_no": ["нет", "не", "отмена", "отмени", "стоп", "не надо", "не нужно"],
    "confirm_prompt": "Подтвердите: сказать «да» или «нет».",
}
LIMIT_TEXT = "Не получилось завершить действие: слишком много шагов."
VOICE_NOTE = ("Ответ произносит колонка голосом: говори коротко, обычными фразами, без Markdown, "
              "списков, эмодзи и ссылок; если вариантов несколько — перечисли их одной фразой.")
DECISIONS = ["approve", "reject", "respond"]
SUMMARY_PROMPT = """Сожми историю диалога голосового помощника с пользователем. Сохрани: что пользователь
просил и решил, его предпочтения и факты о доме, что уже выполнено (чтобы не повторять), что осталось.
Пиши кратко по-русски, без вступлений.

<messages>
{messages}
</messages>"""


def options(runtime):
    return {**DEFAULTS, **(runtime.settings.get("agent") or {})}


def _words(text):
    return re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е")).split()


def classify(runtime, text):
    """Ответ на запрос подтверждения → approve | reject | other."""
    cfg, words = options(runtime), _words(text)
    if not words:
        return "other"
    phrase = " ".join(words)
    if words[0] in cfg["confirm_yes"] and not any(w in ("не", "нет") for w in words[1:2]):
        return "approve"
    if words[0] in cfg["confirm_no"] or phrase in cfg["confirm_no"]:
        return "reject"
    return "other"


def _openai_call(call, thread):
    return {"id": call["id"], "type": "function", "thread_id": thread, "origin": "agent",
            "function": {"name": call["name"], "arguments": json.dumps(call["args"], ensure_ascii=False)}}


class AgentService:
    def __init__(self, directory):
        self.path = Path(directory) / "agent.sqlite3"
        self._locks = {}
        self.pending = {}  # thread → вызовы, ждущие подтверждения (кеш; источник — checkpoint)
        self.reflector = memory.Reflector(self)
        self.ops = ops.Ops(directory)

    def lock(self, thread):
        return self._locks.setdefault(thread, asyncio.Lock())

    @asynccontextmanager
    async def _graph(self, runtime, context, text=None):
        """Агент на прогон. text — реплика пользователя: по ней память подбирает воспоминания."""
        cfg = options(runtime)
        async with AsyncExitStack() as stack:
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(self.path)))
            store, extra, user_id = None, [], context.get("user_id")
            if user_id and memory.options(runtime)["enabled"]:
                try:  # память не должна ломать ответ: сбой — прогон без неё
                    store = await stack.enter_async_context(memory.open_store(runtime, self.path))
                    if text is not None:
                        context["memory"] = await memory.recall(runtime, store, user_id, text)
                    extra = memory.tool_specs(store, user_id)
                except Exception as exc:
                    runtime.recent = (runtime.recent + [{"kind": "memory", "error": type(exc).__name__ + ": " + str(exc)[:160]}])[-30:]
            routines = getattr(runtime, "routines", None)
            if user_id and routines is not None:
                from .routines import inbox_prompt, tool_specs
                extra = extra + tool_specs(routines, user_id, context.get("sn") or "",
                                           mcp_hub._timezone(runtime, context) or "Europe/Moscow")
                unread = [] if context.get("background") or text is None else routines.inbox(user_id, unread=True)
                if unread:
                    context["inbox"], context["inbox_ids"] = inbox_prompt(unread), [i["id"] for i in unread]
            if getattr(runtime, "dialog", None) is not None:
                from . import scenario_edit
                extra = extra + scenario_edit.tool_specs(runtime, user_id)
            tools = build(runtime, context, extra)
            model = RuntimeChatModel(runtime=runtime, counter=context.setdefault("usage", {"calls": 0, "tokens": 0}))
            interrupt_on = {}
            for tool in tools:
                if tool.name == "scenario_run":
                    interrupt_on[tool.name] = {"allowed_decisions": DECISIONS, "when": lambda request: policy.needs_confirm(
                        runtime, "scenario_run", request.tool_call["args"])}
                elif policy.needs_confirm(runtime, tool.name):
                    interrupt_on[tool.name] = {"allowed_decisions": DECISIONS}
            middleware = ops.middleware(runtime) + [SummarizationMiddleware(model, trigger=("messages", int(cfg["summarize_after"])),
                                                  keep=("messages", int(cfg["keep_messages"])),
                                                  summary_prompt=SUMMARY_PROMPT),
                          ModelCallLimitMiddleware(run_limit=int(cfg["max_model_calls"]), exit_behavior="end")]
            if interrupt_on:
                middleware.append(HumanInTheLoopMiddleware(interrupt_on))
            yield create_agent(model, tools, system_prompt=self.system_prompt(runtime, context, interrupt_on),
                               middleware=middleware, checkpointer=saver, store=store)

    def system_prompt(self, runtime, context, confirm=()):
        device_context = runtime.settings["device_context"]
        # Базовые сведения (время, пользователь, колонки) — всегда; свой device_context профиля —
        # без служебных sn/user_id/state (state несёт конфиг колонки целиком, модели он не нужен)
        extra = {k: v for k, v in device_context.items() if k not in ("sn", "user_id", "state")}
        parts = [runtime.settings["system_prompt"], mcp_hub.base_context(runtime, context)]
        if extra:
            parts.append("Контекст устройства: " + json.dumps(extra, ensure_ascii=False))
        catalog = scenarios.index(runtime)
        if catalog:
            parts.append("Сценарии — быстрые команды, которые колонка выполняет без тебя. Если просьба — одна из них "
                         "или несколько сразу, выполни через scenario_run, не изобретай свои действия:\n" + catalog)
        if context.get("request_id"):  # реплика с колонки (voice.py передаёт request_id)
            parts.append(VOICE_NOTE)
        if context.get("memory"):
            parts.append(context["memory"])
        if context.get("inbox"):
            parts.append(context["inbox"])
        if context.get("background"):
            parts.append(f"Это фоновое задание по расписанию «{context['background']}»: пользователь не ведёт "
                         "диалог и не ответит на вопросы. Выполни задание и сформулируй итог одной-двумя фразами — "
                         "он будет произнесён на колонке или передан пользователю позже.")
        if confirm:
            parts.append("Инструменты " + ", ".join(sorted(confirm)) + " выполняются после подтверждения "
                         "пользователя: перед вызовом одной фразой скажи, что именно сделаешь.")
        return "\n\n".join(parts)

    @staticmethod
    def context(runtime, **values):
        device_context = runtime.settings["device_context"]
        base = {"sn": device_context.get("sn"), "user_id": device_context.get("user_id")}
        base.update({k: v for k, v in values.items() if v})
        return {**base, "runs": [], "directives": [], "scenario_messages": [], "started": time.time()}

    async def has_pending(self, runtime, thread):
        if thread not in self.pending:
            async with self.lock(thread), self._graph(runtime, self.context(runtime)) as graph:
                self.pending[thread] = await self._pending_calls(graph, thread)
        return bool(self.pending[thread])

    @staticmethod
    async def _pending_calls(graph, thread):
        state = await graph.aget_state({"configurable": {"thread_id": thread}})
        if not state.interrupts:
            return []
        requests = [r for i in state.interrupts for r in (i.value or {}).get("action_requests", [])]
        last = next((m for m in reversed(state.values.get("messages", [])) if isinstance(m, AIMessage)), None)
        calls, used = [], set()
        for request in requests:  # action_request не несёт id: сопоставление с tool_calls по порядку
            call = next((c for c in (last.tool_calls if last else []) if c["id"] not in used
                         and c["name"] == request["name"] and c["args"] == request["args"]), None)
            if call:
                used.add(call["id"])
                calls.append(call)
        return calls

    async def chat(self, runtime, text, thread=None, **context_values):
        """Реплика пользователя → ответ. Если тред ждёт подтверждения — реплика и есть ответ на него."""
        thread = thread or "studio:" + uuid.uuid4().hex
        context = self.context(runtime, **context_values)
        context["text"] = text
        if self.ops.over_budget(runtime, context.get("user_id")):
            return self._budget_answer(thread, context)
        async with self.lock(thread), self._graph(runtime, context, text) as graph:
            config = {"configurable": {"thread_id": thread}}
            pending = await self._pending_calls(graph, thread)
            context.setdefault("kind", "confirm" if pending else "background" if context.get("background") else "request")
            if pending:
                verdict = classify(runtime, text)
                if verdict == "approve":
                    decisions = [{"type": "approve"} for _ in pending]
                elif verdict == "reject":
                    decisions = [{"type": "respond", "message": "Пользователь отказался, действие не выполнено. "
                                                                "Не повторяй вызов без новой просьбы."} for _ in pending]
                else:
                    decisions = [{"type": "respond", "message": f"Пользователь не подтвердил действие и сказал: «{text}». "
                                                                "Действие не выполнено; ответь на сказанное."}
                                 for _ in pending]
                command = Command(resume={"decisions": decisions})
            else:
                command = {"messages": [HumanMessage(text)]}
            try:
                result = await graph.ainvoke(command, config)
            except Exception as exc:  # сбой провайдера модели — тоже в журнал прогонов
                self._trace(thread, context, type(exc).__name__ + ": " + str(exc)[:200], "error")
                raise
            except asyncio.CancelledError:  # колонка отменила запрос, не дождавшись модели
                self._trace(thread, context, "Отменено колонкой до ответа модели", "cancelled")
                raise
            return await self._answer(runtime, graph, thread, result, context)

    async def resume(self, runtime, thread, approve=(), reason="Отклонено в панели."):
        """Решение панели: approve — id подтверждённых вызовов, прочие ожидающие отклоняются."""
        context = self.context(runtime, kind="resume")
        async with self.lock(thread), self._graph(runtime, context) as graph:
            pending = await self._pending_calls(graph, thread)
            if not pending:
                raise ValueError("Нет вызовов, ждущих подтверждения: " + thread)
            decisions = [{"type": "approve"} if call["id"] in approve else
                         {"type": "respond", "message": reason + " Действие не выполнено."} for call in pending]
            result = await graph.ainvoke(Command(resume={"decisions": decisions}),
                                         {"configurable": {"thread_id": thread}})
            return await self._answer(runtime, graph, thread, result, context)

    async def note(self, runtime, thread, text, origin):
        """Реплика, сказанная на колонке не из этого треда (задание, событие), — в тред как ответ
        агента: ответ пользователя («да» на вопрос задания) придёт сюда и будет понятен модели.
        Тред, ждущий подтверждения, не трогается — сообщение сломало бы interrupt."""
        context = self.context(runtime)
        context["user_id"] = None  # без памяти и инструментов заданий: граф нужен только для состояния
        async with self.lock(thread), self._graph(runtime, context) as graph:
            config = {"configurable": {"thread_id": thread}}
            state = await graph.aget_state(config)
            if state.interrupts:
                return False
            messages = [AIMessage(text)]
            if not state.values.get("messages"):  # тред начинается с реплики пользователя
                messages.insert(0, HumanMessage(f"[{origin}]"))
            await graph.aupdate_state(config, {"messages": messages}, as_node="model")
        return True

    async def thread_messages(self, thread):
        """Сообщения треда из последнего checkpoint (для рефлексии памяти)."""
        async with AsyncSqliteSaver.from_conn_string(str(self.path)) as saver:
            found = await saver.aget_tuple({"configurable": {"thread_id": thread}})
        return list((found.checkpoint.get("channel_values") or {}).get("messages", [])) if found else []

    async def reset(self, runtime, thread):
        """ResetSession: тред начинается заново (checkpoint удаляется)."""
        async with self.lock(thread), AsyncSqliteSaver.from_conn_string(str(self.path)) as saver:
            await saver.adelete_thread(thread)
        self.pending[thread] = []

    async def _answer(self, runtime, graph, thread, result, context):
        pending = await self._pending_calls(graph, thread)
        self.pending[thread] = pending
        if context.get("inbox_ids"):
            runtime.routines.mark_read(context["inbox_ids"])
        if not context.get("background"):
            self.reflector.schedule(runtime, thread, context.get("user_id"))
        last = next((m for m in reversed(result.get("messages", [])) if isinstance(m, AIMessage)), None)
        text = last.text if last is not None else ""
        # ModelCallLimitMiddleware завершает прогон своим AIMessage без usage провайдера
        if last is not None and not pending and "usage" not in last.response_metadata:
            text = LIMIT_TEXT
        if pending and not text:
            text = options(runtime)["confirm_prompt"]
        elif pending:
            text = text.rstrip() + " " + options(runtime)["confirm_prompt"]
        self._trace(thread, context, text, "pending" if pending else "limit" if text == LIMIT_TEXT else "ok")
        return {"text": text, "tool_calls": [_openai_call(c, thread) for c in pending],
                "usage": last.response_metadata.get("usage", {}) if last is not None else {},
                "tool_runs": context["runs"], "directives": context["directives"],
                "scenario_messages": context["scenario_messages"], "thread": thread, "engine": "graph"}

    def _trace(self, thread, context, answer, status):
        usage = context.get("usage") or {}
        try:  # журнал не должен ломать ответ
            self.ops.record({"ts": context["started"], "thread": thread, "kind": context.get("kind"),
                             "user_id": context.get("user_id"), "sn": context.get("sn"), "text": context.get("text"),
                             "answer": answer, "tools": context["runs"], "model_calls": usage.get("calls", 0),
                             "tokens": usage.get("tokens", 0), "ms": int((time.time() - context["started"]) * 1000),
                             "status": status})
        except Exception:
            pass

    def _budget_answer(self, thread, context):
        context.setdefault("kind", "background" if context.get("background") else "request")
        self._trace(thread, context, ops.BUDGET_TEXT, "budget")
        return {"text": ops.BUDGET_TEXT, "tool_calls": [], "usage": {}, "tool_runs": [], "directives": [],
                "scenario_messages": [], "thread": thread, "engine": "graph"}
