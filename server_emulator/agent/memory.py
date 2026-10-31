"""Долговременная память агента: факты, эпизоды, инструкции пользователя.

Хранилище — LangGraph AsyncSqliteStore в state/agent.sqlite3 (таблицы store/store_vectors),
векторный поиск — sqlite-vec по эмбеддингам OpenAI-совместимого /v1/embeddings
(settings.memory.embed). Без эмбеддингов память работает без семантического поиска:
в промпт идут последние записи.

Пространства имён — по владельцу колонки (пользователь реестра):
    ("memories", <user_id>, "facts")         — факты и предпочтения (LangMem: обновление/удаление)
    ("memories", <user_id>, "episodes")      — сводки прошлых разговоров
    ("memories", <user_id>, "instructions")  — ключ "main": как отвечать этому пользователю
Колонка общая для семьи, голос не различается: память — владельца колонки (дома).

Запись:
* по просьбе пользователя («запомни», «забудь») — инструмент manage_memory (LangMem);
* фоново — Reflector: после паузы в диалоге LangMem-менеджер разбирает новые реплики
  треда. Ему идут только реплики пользователя и ответы агента, без результатов
  инструментов: содержимое MCP/HA не попадает в память (защита от prompt injection).
"""
import asyncio
import copy
import json
import time
import uuid
from contextlib import asynccontextmanager

from aiohttp import ClientTimeout
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.store.sqlite.aio import AsyncSqliteStore
from langmem import create_manage_memory_tool, create_memory_store_manager, create_search_memory_tool
from pydantic import BaseModel, Field

from ..auth import secret

DEFAULTS = {"enabled": True, "embed": {"url": "", "model": "", "api_key": ""},
            "recall_limit": 6, "reflect_after_s": 120}
KINDS = ("facts", "episodes", "instructions")
META = ("memories_meta",)
INSTRUCTIONS_KEY = "main"
NAMES = {"manage_memory", "search_memory", "memory_instructions"}
RISKS = {"manage_memory": "device", "search_memory": "read", "memory_instructions": "config"}

FACTS_PROMPT = """Ты ведёшь долговременную память голосового помощника умного дома о его пользователе.
Из новых реплик извлеки то, что пригодится в будущих разговорах: факты о пользователе и его доме
(имена, комнаты, устройства, распорядок), устойчивые предпочтения, договорённости. Не сохраняй
разовые команды («включи свет»), погоду, время, пароли и коды, догадки. Каждая запись — одна
короткая фраза по-русски от третьего лица («Пользователь любит джаз»). Если новое противоречит
сохранённому — исправь запись; устаревшее и ошибочное удали."""
EPISODES_PROMPT = """Сожми этот разговор голосового помощника с пользователем в один эпизод: о чём
говорили, что сделано, что осталось нерешённым. Одна-две фразы по-русски. Если разговор пустой
(приветствие, разовая команда) — ничего не записывай."""
MANAGE_PROMPT = """Вызывай, когда пользователь просит запомнить или забыть что-то о себе и доме,
или сообщает устойчивое предпочтение. Запись — короткая фраза по-русски. Удаление — по id записи
из памяти в промпте или из search_memory."""


class Fact(BaseModel):
    """Факт о пользователе, его доме или устойчивое предпочтение."""
    text: str = Field(description="одна короткая фраза по-русски")


class Episode(BaseModel):
    """Сводка разговора: о чём говорили и чем закончилось."""
    text: str = Field(description="одна-две фразы по-русски")


def options(runtime):
    value = copy.deepcopy(DEFAULTS)
    stored = runtime.settings.get("memory") or {}
    value.update({k: v for k, v in stored.items() if k != "embed"})
    value["embed"].update(stored.get("embed") or {})
    return value


def namespace(user_id, kind):
    return ("memories", user_id, kind)


def text_of(value):
    """Текст записи: LangMem пишет {"kind", "content": {text}} или {"content": "…"}."""
    content = (value or {}).get("content")
    if isinstance(content, dict):
        return content.get("text") or json.dumps(content, ensure_ascii=False)
    return content if isinstance(content, str) else json.dumps(value, ensure_ascii=False)


class RuntimeEmbeddings(Embeddings):
    """OpenAI-совместимый POST {url} {model, input: [...]} → data[].embedding (Ollama, LM Studio, OpenAI)."""

    def __init__(self, runtime, config):
        self.runtime, self.config = runtime, config

    def embed_documents(self, texts):
        raise NotImplementedError("RuntimeEmbeddings работает только асинхронно")

    def embed_query(self, text):
        raise NotImplementedError("RuntimeEmbeddings работает только асинхронно")

    async def aembed_documents(self, texts):
        if not texts:
            return []
        headers = {}
        if secret(self.config.get("api_key")):
            headers["Authorization"] = "Bearer " + secret(self.config["api_key"])
        session = self.runtime.lifecycle.client_session()
        async with session.post(self.config["url"], json={"model": self.config.get("model", ""), "input": list(texts)},
                                headers=headers, timeout=ClientTimeout(total=30, sock_connect=3)) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise ValueError(f"embeddings: HTTP {response.status}")
        return [item["embedding"] for item in sorted(body["data"], key=lambda item: item.get("index", 0))]

    async def aembed_query(self, text):
        return (await self.aembed_documents([text]))[0]


_DIMS = {}  # (url, model) → размерность вектора
_DOWN = {}  # (url, model) → время сбоя: сервис эмбеддингов не дёргается DOWN_S секунд
DOWN_S = 60


class _Embedder(RuntimeEmbeddings):
    """Сбой запроса отмечает сервис недоступным: следующие прогоны не ждут таймаута."""

    async def aembed_documents(self, texts):
        signature = (self.config["url"], self.config.get("model", ""))
        try:
            return await super().aembed_documents(texts)
        except Exception:
            _DOWN[signature] = time.monotonic()
            raise


async def _index(runtime, cfg):
    embed = cfg["embed"]
    if not embed.get("url"):
        return None
    signature = (embed["url"], embed.get("model", ""))
    if time.monotonic() - _DOWN.get(signature, -DOWN_S) < DOWN_S:
        raise ConnectionError("сервис эмбеддингов недоступен, повтор через минуту")
    embeddings = _Embedder(runtime, embed)
    if signature not in _DIMS:
        _DIMS[signature] = len(await embeddings.aembed_query("проверка"))
    return {"dims": _DIMS[signature], "embed": embeddings, "fields": ["content"]}


async def _reindex_if_changed(store, index):
    """Смена модели эмбеддингов: прежние векторы несравнимы — записи переиндексируются."""
    signature = f"{index['embed'].config['url']}|{index['embed'].config.get('model', '')}|{index['dims']}" if index else ""
    meta = await store.aget(META, "embed")
    if (meta.value if meta else {}).get("signature", "") == signature:
        return
    if index:
        offset = 0
        while True:
            items = await store.asearch(("memories",), limit=100, offset=offset)
            for item in items:
                await store.aput(item.namespace, item.key, item.value)
            if len(items) < 100:
                break
            offset += 100
    await store.aput(META, "embed", {"signature": signature}, index=False)


@asynccontextmanager
async def open_store(runtime, path):
    """Store на прогон. Сервис эмбеддингов недоступен — память без семантического поиска
    (векторы не трогаются: переиндексация только при смене модели, не при сбое)."""
    try:
        index, failed = await _index(runtime, options(runtime)), False
    except Exception as exc:
        index, failed = None, True
        runtime.recent = (runtime.recent + [{"kind": "memory", "error": "эмбеддинги: " + (str(exc) or type(exc).__name__)}])[-30:]
    async with AsyncSqliteStore.from_conn_string(str(path), index=index) as store:
        await store.setup()
        if not failed:
            await _reindex_if_changed(store, index)
        yield store


async def items(store, user_id, kind, query=None, limit=50):
    found = await store.asearch(namespace(user_id, kind), query=query or None, limit=limit)
    return [{"key": i.key, "text": text_of(i.value), "updated": i.updated_at.isoformat(),
             "score": i.score} for i in found]


async def recall(runtime, store, user_id, text):
    """Раздел системного промпта: что агент помнит о пользователе."""
    cfg = options(runtime)
    limit = int(cfg["recall_limit"])
    semantic = cfg["embed"].get("url")
    facts = await items(store, user_id, "facts", text if semantic else None, limit if semantic else limit * 3)
    episodes = await items(store, user_id, "episodes", text if semantic else None, 2)
    instructions = await store.aget(namespace(user_id, "instructions"), INSTRUCTIONS_KEY)
    parts = []
    if facts:
        parts.append("Что ты помнишь о пользователе (из прошлых разговоров; используй к месту, не пересказывай):\n"
                     + "\n".join(f"- [{f['key']}] {f['text']}" for f in facts))
    if episodes:
        parts.append("Прошлые разговоры:\n" + "\n".join(f"- {e['updated'][:10]}: {e['text']}" for e in episodes))
    if instructions and text_of(instructions.value):
        parts.append("Пожелания пользователя к твоим ответам:\n" + text_of(instructions.value))
    return "\n\n".join(parts)


def tool_specs(store, user_id):
    """[(name, description, schema, executor)] инструментов памяти для прогона."""
    facts = namespace(user_id, "facts")
    manage = create_manage_memory_tool(facts, store=store, instructions=MANAGE_PROMPT)
    search = create_search_memory_tool(facts, store=store)

    async def instructions(text=""):
        await store.aput(namespace(user_id, "instructions"), INSTRUCTIONS_KEY,
                         {"kind": "Instructions", "content": {"text": text}}, index=False)
        return "Пожелания сохранены." if text else "Пожелания очищены."

    def spec(tool):
        function = convert_to_openai_tool(tool)["function"]
        return function["name"], function.get("description", ""), function.get("parameters")

    return [(*spec(manage), lambda **a: manage.ainvoke(a)),
            (*spec(search), lambda **a: search.ainvoke(a)),
            ("memory_instructions", "Задать пожелания пользователя к ответам (стиль, длина, обращение) — "
             "заменяет прежние целиком; пустой text очищает. Только по прямой просьбе пользователя.",
             {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, instructions)]


def dialogue(messages):
    """Реплики пользователя и ответы агента без инструментов — вход рефлексии."""
    result = []
    for message in messages:
        if isinstance(message, HumanMessage) and message.text.strip():
            result.append((message.id, {"role": "user", "content": message.text}))
        elif isinstance(message, AIMessage) and message.text.strip():
            result.append((message.id, {"role": "assistant", "content": message.text}))
    return result


class Reflector:
    """Фоновая рефлексия: через reflect_after_s тишины в треде LangMem-менеджеры
    обновляют факты и добавляют эпизод. Новая реплика переносит запуск."""

    def __init__(self, service):
        self.service = service
        self.tasks = {}  # thread → asyncio.Task отложенного запуска
        self.log = []  # последние запуски для панели

    def schedule(self, runtime, thread, user_id):
        cfg = options(runtime)
        if not (cfg["enabled"] and user_id):
            return
        previous = self.tasks.pop(thread, None)
        if previous and not previous.done():
            previous.cancel()

        async def later():
            await asyncio.sleep(float(cfg["reflect_after_s"]))
            await self.reflect(runtime, thread, user_id)
        self.tasks[thread] = runtime.lifecycle.spawn(later())

    async def reflect(self, runtime, thread, user_id):
        record = {"thread": thread, "user_id": user_id, "ts": time.time()}
        try:
            record.update(await self._reflect(runtime, thread, user_id))
        except Exception as exc:  # фон не должен ронять сервер; ошибка видна в панели
            record["error"] = type(exc).__name__ + ": " + str(exc)[:200]
        self.log = (self.log + [record])[-30:]
        return record

    async def _reflect(self, runtime, thread, user_id):
        from .model import RuntimeChatModel
        messages = await self.service.thread_messages(thread)
        async with self.service.lock(thread), open_store(runtime, self.service.path) as store:
            marker = await store.aget(META + ("reflected",), thread)
            pairs = dialogue(messages)
            ids = [i for i, _ in pairs]
            last = (marker.value if marker else {}).get("id")
            fresh = [m for _, m in (pairs[ids.index(last) + 1:] if last in ids else pairs)]
            if not any(m["role"] == "user" for m in fresh):
                return {"skipped": "нет новых реплик"}
            model = RuntimeChatModel(runtime=runtime)
            limit = int(options(runtime)["recall_limit"])
            facts = create_memory_store_manager(model, schemas=[Fact], instructions=FACTS_PROMPT, enable_deletes=True,
                                                namespace=namespace(user_id, "facts"), store=store, query_limit=limit)
            episodes = create_memory_store_manager(model, schemas=[Episode], instructions=EPISODES_PROMPT,
                                                   namespace=namespace(user_id, "episodes"), store=store, query_limit=0)
            changed = await facts.ainvoke({"messages": fresh, "max_steps": 1})
            await episodes.ainvoke({"messages": fresh, "max_steps": 1})
            await store.aput(META + ("reflected",), thread, {"id": ids[-1]}, index=False)
            return {"messages": len(fresh), "facts": len(changed)}


async def put(store, user_id, kind, text, key=None):
    if kind not in KINDS:
        raise ValueError("kind ∈ " + ", ".join(KINDS))
    key = INSTRUCTIONS_KEY if kind == "instructions" else (key or str(uuid.uuid4()))
    value = {"kind": {"facts": "Fact", "episodes": "Episode", "instructions": "Instructions"}[kind],
             "content": {"text": text}}
    await store.aput(namespace(user_id, kind), key, value, index=False if kind == "instructions" else None)
    return key
