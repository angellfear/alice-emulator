"""R06 / v9 §4: единый executor сценариев с ограниченными операциями.

Использует validate/bindings/proto_build. Операции:
resolve / action / compose / ask / finish / error (TASK-027: fetch/extract/load/refresh/
store/branch/set/wait_state удалены — сложная логика в py-сценариях).
Ограничения: число шагов, общий deadline, TTL ожидания. Action ID даёт
идемпотентность повторного запроса. Активный запуск закрепляет версию
определения (снимок сценария живёт в Execution). Ожидающие продолжения
диалоги персистентны (StateStore.executions) и переживают рестарт.

Executor не знает транспорт; отправка
скомпонованных сообщений — задача dialog/transport слоя (R07).

TASK-020: py-сценарий тоже уточняет (return ask(...) из sdk): ожидание хранится
в ТОМ ЖЕ механизме, что у JSON-шага ask (awaiting/awaiting_question + снимок в
StateStore.executions — переживает рестарт), продолжение — функция `then` того
же модуля; фразы отмены и TTL снимают ожидание, как у JSON.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace

from ..extensions.context import action_context as extension_for_action
from ..scripting import ScriptRunner, failure
from ..sdk import Ask, ScenarioContext
from .bindings import BindingError, expand

MAX_STEPS = 64
DEFAULT_DEADLINE_S = 300.0

TERMINAL_OPS = ("finish", "error")

# Отмена py-уточнения фразой (карточка TASK-020). Словарь lexicons «cancel»
# (список фраз), если появится, перекрывает встроенный набор.
CANCEL_PHRASES = ("отмена", "стоп")


class ExecutorTerminal(Exception):
    """Управляющий сигнал терминала: status + payload. Не ошибка исполнения."""


class Outcome:
    """Результат прогона до терминала/ожидания.
    status ∈ finished|errored|awaiting|cancelled|declined."""

    def __init__(self, status, execution):
        self.status = status
        self.trace = list(execution.trace)
        self.slots = dict(execution.slots)
        self.state = dict(execution.state)
        self.composed = list(execution.composed)
        self.speech_parts = list(execution.speech_parts)
        self.awaiting = execution.awaiting
        self.question = execution.awaiting_question
        self.error = execution.error
        self.effects = list(getattr(execution, "effects", []) or [])

    def as_dict(self):
        return {"status": self.status, "trace": self.trace, "slots": self.slots,
                "composed": len(self.composed), "awaiting": self.awaiting,
                "question": self.question, "error": self.error, "effects": self.effects}


class Execution:
    """Один запуск сценария; снимок сериализуем в StateStore.executions."""

    def __init__(self, dialog_id, scenario, deadline_s=DEFAULT_DEADLINE_S):
        self.dialog_id = dialog_id
        self.scenario = scenario  # закреплённая версия определения
        self.position = 0
        self.steps_done = 0
        self.state = {}
        self.slots = {}
        self.composed = []  # [(ref, protobuf Message)]
        self.speech_parts = []  # части озвучки последней реплики (кеш TTS по сегментам)
        self.trace = []     # [{step, op, status, reason?, t_ms}]
        self.action_results = {}  # action_id → результат (идемпотентность)
        self.awaiting = None
        self.awaiting_question = None
        self.script_ask = None  # py-уточнение (TASK-020): {path, mtime_ns, expect} пока ждём
        self.error = None
        self.effects = []  # [{kind, name, arguments}] — dry-run эффекты py-сценария (TASK-019)
        self.extension = None  # ExtensionContext владельца действия (TASK-053), на время вызова
        self.dry_run = False  # прогон-проверка: действия не выполняются, журнал ошибок молчит
        self.started = time.monotonic()
        self.deadline = self.started + deadline_s
        self.ctx = {}  # merged bindings-контекст последнего шага (transient, не сериализуется)

    def note(self, step, op, status, reason=None):
        record = {"step": step, "op": op, "status": status,
                  "t_ms": round((time.monotonic() - self.started) * 1000, 1)}
        if reason:
            record["reason"] = str(reason)[:200]
        self.trace.append(record)

    # --- сериализация (переживание рестарта) -----------------------------------
    def snapshot(self):
        return {"dialog_id": self.dialog_id, "scenario": self.scenario,
                "position": self.position, "steps_done": self.steps_done,
                "state": self.state, "slots": self.slots,
                "trace": self.trace, "action_ids": sorted(self.action_results),
                "awaiting": self.awaiting, "awaiting_question": self.awaiting_question,
                "script_ask": self.script_ask,
                "deadline_remaining_s": max(0.0, self.deadline - time.monotonic())}

    @classmethod
    def restore(cls, payload):
        execution = cls(payload["dialog_id"], payload["scenario"],
                        deadline_s=payload.get("deadline_remaining_s", DEFAULT_DEADLINE_S))
        execution.position = payload.get("position", 0)
        execution.steps_done = payload.get("steps_done", 0)
        execution.state = payload.get("state", {})
        execution.slots = payload.get("slots", {})
        execution.trace = payload.get("trace", [])
        execution.awaiting = payload.get("awaiting")
        execution.awaiting_question = payload.get("awaiting_question")
        execution.script_ask = payload.get("script_ask")
        # action_results по рестарту пусты: идемпотентность гарантирует Action ID
        # на стороне исполнителя действия (одноразовый side effect — приемка v9 §4)
        return execution


def builtin_script_path(scenario):
    """Резолвер .py по умолчанию: только встроенная библиотека (_file — «<раздел>/<id>.json»
    от library.ROOT); прочие источники резолвит Runtime (runtime.py, scenario_script_path)."""
    if scenario.get("_origin") != "builtin" or not scenario.get("_file"):
        return None
    from . import library  # library.ROOT читается при вызове — подмена библиотеки в тестах
    return (library.ROOT / scenario["_file"]).with_suffix(".py")


class Executor:
    """Примитивы закрыты кодом, логика — в JSON сценария.

    actions: ref → callable(arguments: dict, execution) → Any (эффекты: таймер,
      push, свет — без текстов и выбора веток). На время вызова execution.extension —
      ExtensionContext расширения-владельца действия (TASK-053), встроенному — None.
    resolvers: ref → callable(execution) → значение слота
    fetcher: async (url, query, ttl) → JSON (ctx.fetch_json SDK; кеш — забота fetcher)
    data_store: хранилище данных сценариев (scenario_data: user/device/system)
    lexicons: общие словари (scenarios/lexicons.json)
    on_error: callable(entry) — журнал ошибок Runtime (recent); вызывается на каждый
      исход errored (JSON и script), кроме прогона с dry-run; исключение колбэка глотается
    script_path: callable(scenario) → Path — резолвер файла .py для kind: script
      (Runtime: встроенный — рядом с JSON библиотеки, пользовательский — scenario_dir)
    """

    def __init__(self, actions=None, resolvers=None, schemas_root=None, store=None,
                 fetcher=None, data_store=None, lexicons=None, plugins=None,
                 on_error=None, script_path=None):
        self.actions = dict(actions or {})
        self.resolvers = dict(resolvers or {})
        self.fetcher = fetcher
        self.data_store = data_store
        self.plugins = plugins or {}  # name → async (key, args) → значение записи датасета
        self._refreshing = {}  # (dataset, key) → задача плагина: параллельные запросы ждут одну
        self.lexicons = lexicons if lexicons is not None else {}
        self.schemas_root = schemas_root
        self.store = store
        self.live = {}  # dialog_id → (Execution, Task|None)
        self.on_error = on_error
        # без резолвера (Executor вне Runtime: тесты, инструменты) — встроенная библиотека
        self.script_path = script_path or builtin_script_path
        self.scripts = ScriptRunner(prefix="scenario_script")  # один раннер на Executor

    # --- публичный API ----------------------------------------------------------
    async def execute(self, scenario, ctx, dialog_id):
        if dialog_id in self.live:
            raise ValueError(f"диалог {dialog_id} уже исполняется")
        execution = Execution(dialog_id, scenario)
        # TASK-022: сущности template-триггера приходят через ctx["slots"] (dialog.py) —
        # доступны шагам JSON ({{slots.*}}) так же, как слоты extract-шагов
        execution.slots.update(ctx.get("slots") or {})
        # TASK-079: то же для ctx["state"] — стартовые значения state сценария от вызывающего
        # (в диалоге voice.py state не приходит — поведение не меняется; прогон эталона
        # задаёт им фиксированный id директив вместо случайного uuid)
        execution.state.update(ctx.get("state") or {})
        execution.state["_store"] = self._load_store(scenario, ctx)
        execution.dry_run = bool((ctx or {}).get("dry_run"))  # флаг для on_error (TASK-019)
        if (scenario.get("executor") or {}).get("kind") == "script":
            return await self._run_script(execution, ctx)
        return await self._drive(execution, ctx)

    async def answer(self, dialog_id, value, ctx):
        """Продолжение после ask: значение → слот (JSON) или вызов продолжения
        py-сценария (TASK-020), исполнение дальше."""
        execution = self._execution_for(dialog_id)
        if execution is None:
            return None
        if not execution.awaiting:
            return None
        if (execution.scenario.get("executor") or {}).get("kind") == "script":
            return await self._answer_script(execution, value, ctx)
        slot = execution.awaiting
        execution.slots[slot] = value
        execution.awaiting = None
        execution.awaiting_question = None
        execution.note(slot, "ask", "answered")
        self._forget(dialog_id)
        ask_step = execution.scenario["flow"][execution.position]
        target = ask_step.get("next")
        execution.position = self._index_of(execution, target) if target else execution.position + 1
        return await self._drive(execution, ctx)

    def cancel(self, dialog_id):
        """Отмена ожидающего диалога: состояние снято, персистентность удалена."""
        execution = self._execution_for(dialog_id)
        if execution is None:
            return None
        execution.note(execution.awaiting or dialog_id, "cancel", "cancelled")
        self._forget(dialog_id)
        return Outcome("cancelled", execution)

    def pending(self):
        """Снимки ожидающих диалогов (в памяти или из store — после рестарта)."""
        if self.store is not None:
            return self.store.list_executions()
        return [execution.snapshot() for execution, _ in self.live.values() if execution.awaiting]

    # --- движение по графу ------------------------------------------------------
    async def _drive(self, execution, ctx):
        self.live[execution.dialog_id] = (execution, None)
        try:
            while True:
                if execution.steps_done >= MAX_STEPS:
                    execution.error = "step_limit"
                    execution.note("*", "error", "terminal", "step_limit")
                    return await self._finish_with(execution, "errored")
                if time.monotonic() > execution.deadline:
                    execution.error = "deadline"
                    execution.note("*", "error", "terminal", "deadline")
                    return await self._finish_with(execution, "errored")
                step = execution.scenario["flow"][execution.position]
                op = step["op"]
                execution.steps_done += 1
                try:
                    await self._step(step, op, execution, ctx)
                except ExecutorTerminal as terminal:
                    return await self._finish_with(execution, terminal.args[0])
                if execution.awaiting:
                    self._persist(execution)
                    return Outcome("awaiting", execution)
        finally:
            self.live.pop(execution.dialog_id, None)

    async def _finish_with(self, execution, status):
        # TASK-019: одна точка журнала ошибок (Runtime.recent) для JSON и script;
        # dry-run — проверка сценария, её сбои в журнал не попадают
        if status == "errored" and not execution.dry_run and self.on_error is not None:
            try:
                self.on_error({"kind": "scenario", "id": execution.scenario.get("id", ""),
                               "error": str(execution.error or "")[-300:]})
            except Exception:  # сбой колбэка не должен ломать исполнение
                pass
        self._forget(execution.dialog_id)
        return Outcome(status, execution)

    # --- py-исполнитель (TASK-019): kind: script → handle(ctx) из <id>.py -----------

    async def _run_script(self, execution, ctx):
        self.live[execution.dialog_id] = (execution, None)
        try:
            merged = self._ctx(execution, ctx)
            execution.ctx = merged
            path = self.script_path(execution.scenario) if self.script_path else None
            # SDK-контекст по решению координатора: совместимый объект с scenario_executor —
            # сервисы SDK работают через реестры этого Executor, Runtime внутрь не передаётся;
            # ресурсы сценария (ctx.asset) — каталог <id>.assets рядом со скриптом (TASK-050)
            sdk_ctx = ScenarioContext(SimpleNamespace(scenario_executor=self), merged,
                                      dry_run=bool((ctx or {}).get("dry_run")),
                                      assets_dir=self._assets_dir(path))
            execution.effects = sdk_ctx.effects
            if path is None or not path.is_file():
                execution.error = f"script_missing:{execution.scenario.get('id', '')}"
                execution.note("script", "script", "error", execution.error)
                return await self._finish_with(execution, "errored")
            try:
                result = await self.scripts.run(path, sdk_ctx)
            except Exception as exc:  # ScriptError / asyncio.TimeoutError / исключение скрипта
                execution.error = "script_failed:" + failure(exc)[-300:]
                execution.note("script", "script", "error", execution.error)
                return await self._finish_with(execution, "errored")
            return await self._script_result(execution, path, merged, result,
                                             none_is_decline=True)
        finally:
            self.live.pop(execution.dialog_id, None)

    @staticmethod
    def _assets_dir(path):
        """Каталог ресурсов py-сценария (ctx.asset): <имя скрипта>.assets рядом с ним —
        на уровень глубже раздела, в глоб библиотеки */*.json не попадает."""
        return path.with_suffix(".assets") if path is not None else None

    async def _script_result(self, execution, path, merged, result, none_is_decline):
        """Разбор return-значения handle/продолжения (TASK-019/020): None — отказ или
        тихое завершение; Ask — ожидание в общем с JSON механизме (_wait_script_ask);
        dict/list из dict — сборка существующим compose (ошибки compose — как у шага).
        Пустой список `[]` — сценарий отработал без сообщений: finished, composed пуст
        (как JSON finish без compose); не то же, что None (отказ) и silent() (service-ответ)."""
        if result is None:
            if none_is_decline:  # первый вход: сценарий отказался — как guard=false
                execution.note("script", "script", "ok", "declined")
                return await self._finish_with(execution, "declined")
            execution.note("script", "script", "ok", "silent")
            return await self._finish_with(execution, "finished")
        if isinstance(result, Ask):
            return await self._wait_script_ask(execution, path, result)
        if isinstance(result, dict):
            specs = [result]
        elif isinstance(result, (list, tuple)) and all(isinstance(item, dict) for item in result):
            specs = list(result)
        else:
            execution.error = f"script_result:{type(result).__name__}"
            execution.note("script", "script", "error", execution.error)
            return await self._finish_with(execution, "errored")
        try:  # сборка ответа — существующий compose: composed/speech_parts/reply:service
            for spec in specs:  # и ошибки compose работают как у JSON-шага
                await self._compose({"id": "script", "reply": spec}, execution, merged)
        except ExecutorTerminal:
            return await self._finish_with(execution, "errored")
        execution.note("script", "script", "ok")
        return await self._finish_with(execution, "finished")

    async def _wait_script_ask(self, execution, path, ask):
        """py-уточнение хранится ТОТ ЖЕ способ, что JSON-ask: awaiting/awaiting_question
        и снимок в StateStore.executions (сценарий, версия файла, ctx.state, имя функции
        продолжения, TTL) — переживает рестарт, видно в pending(), снимается cancel().
        Продолжение — функция `then` того же модуля; expect — подсказка парсеру (TASK-022)."""
        try:  # продолжение должно существовать уже при первом ask; валидатор файл
            self.scripts.function(path, ask.then)  # не читает — понятная ошибка здесь
        except Exception:
            execution.error = f"script_continuation_missing:{ask.then}"
            execution.note("script", "ask", "error", execution.error)
            return await self._finish_with(execution, "errored")
        execution.awaiting = ask.then  # имя функции продолжения — что ждём (у JSON — слот)
        execution.awaiting_question = ask.question
        execution.script_ask = {"path": str(path), "mtime_ns": path.stat().st_mtime_ns,
                                "expect": ask.expect}
        execution.note("script", "ask", "await", f"then={ask.then}")
        if not execution.dry_run:  # прогон-проверка (H02) состояний не создаёт
            self._persist(execution)
        return Outcome("awaiting", execution)

    async def _answer_script(self, execution, value, ctx):
        """Ответ на py-уточнение: фраза пришла сюда МИНУЯ роутер (dialog.py продолжает
        ожидающий диалог раньше выбора сценария) — в on_duration(ctx, answer) того же
        модуля. Отмена и TTL снимают ожидание так же, как у JSON-ask."""
        if self._is_cancel(value):  # «отмена»/«стоп» — ожидание снято, реплики нет
            execution.note(execution.awaiting, "cancel", "cancelled")
            self._forget(execution.dialog_id)
            return Outcome("cancelled", execution)
        if time.monotonic() > execution.deadline:  # TTL — deadline того же снимка
            execution.error = "deadline"
            execution.note(execution.awaiting, "ask", "timeout", execution.error)
            return await self._finish_with(execution, "errored")
        function = execution.awaiting
        question = execution.awaiting_question
        ask_data = dict(execution.script_ask or {})
        execution.awaiting = None
        execution.awaiting_question = None
        execution.script_ask = None
        execution.note(function, "ask", "answered")
        self._forget(execution.dialog_id)  # как JSON: ожидание снято; повторный ask — в _wait_script_ask
        path = self.script_path(execution.scenario) if self.script_path else None
        if path is None or not path.is_file():
            execution.error = f"script_missing:{execution.scenario.get('id', '')}"
            execution.note(function, "script", "error", execution.error)
            return await self._finish_with(execution, "errored")
        if ask_data.get("mtime_ns") and ask_data["mtime_ns"] != path.stat().st_mtime_ns:
            execution.note(function, "script", "ok",
                           "script_changed_during_ask")  # код правили, пока ждали ответ
        merged = self._ctx(execution, ctx)  # state — тот же dict исполнения (пережил рестарт)
        merged["ask"] = {"question": question or "", "expect": ask_data.get("expect")}
        execution.ctx = merged
        sdk_ctx = ScenarioContext(SimpleNamespace(scenario_executor=self), merged,
                                  assets_dir=self._assets_dir(path))
        execution.effects = sdk_ctx.effects
        try:
            result = await self.scripts.run(path, sdk_ctx, value, function=function)
        except Exception as exc:  # ScriptError (нет функции после правки) / таймаут / исключение
            execution.error = "script_failed:" + failure(exc)[-300:]
            execution.note(function, "script", "error", execution.error)
            return await self._finish_with(execution, "errored")
        # None после ответа — тихое завершение (продолжение уже владеет репликой,
        # «отказаться» в роутер нельзя); первый вход различает отказ в _script_result
        return await self._script_result(execution, path, merged, result, none_is_decline=False)

    def _cancel_phrases(self):
        """Фразы отмены ожидания: словарь lexicons «cancel» (список), если задан,
        иначе встроенные («отмена», «стоп») из карточки TASK-020."""
        cancel = self.lexicons.get("cancel") if isinstance(self.lexicons, dict) else None
        if isinstance(cancel, dict) and cancel.get("phrases"):
            return tuple(str(phrase).lower() for phrase in cancel["phrases"])
        if isinstance(cancel, (list, tuple)) and cancel:
            return tuple(str(phrase).lower() for phrase in cancel)
        return CANCEL_PHRASES

    def _is_cancel(self, text):
        """Отмена — только фраза целиком («стоп таймер» — другой сценарий, не отмена)."""
        normalized = str(text or "").strip().lower().rstrip(".!? ")
        return normalized in self._cancel_phrases()

    async def _step(self, step, op, execution, ctx):
        merged = self._ctx(execution, ctx)
        execution.ctx = merged  # actions/resolvers видят input-контекст запроса
        if op == "finish":
            execution.note(step["id"], op, "terminal")
            raise ExecutorTerminal("finished")
        if op == "error":
            execution.error = self._literal(step.get("message"), merged)
            execution.note(step["id"], op, "terminal", execution.error)
            raise ExecutorTerminal("errored")
        if op == "resolve":
            ref = step["ref"]
            resolver = self.resolvers.get(ref)
            if resolver is None:
                execution.error = f"missing_resolver:{ref}"
                execution.note(step["id"], op, "error", execution.error)
                raise ExecutorTerminal("errored")
            value = resolver(execution)
            if inspect.isawaitable(value):
                value = await value
            slot = step.get("slot") or ref.rsplit(".", 1)[-1]
            execution.slots[slot] = value
            execution.note(step["id"], op, "ok", f"slot={slot}")
        elif op == "action":
            if await self._action(step, execution, merged):
                return  # уже перешли по on_failure; обычное продвижение не применяется
        elif op == "compose":
            await self._compose(step, execution, merged)
        elif op == "ask":
            execution.awaiting = step["await"]
            execution.awaiting_question = self._literal(step.get("question"), merged)
            execution.note(step["id"], op, "await", f"slot={execution.awaiting}")
            return  # _drive увидит awaiting и вернёт управление
        else:
            execution.error = f"unknown_op:{op}"
            execution.note(step["id"], op, "error", execution.error)
            raise ExecutorTerminal("errored")
        self._advance(step, execution)

    async def _action(self, step, execution, merged) -> bool:
        """Возвращает True, если выполнен переход on_failure (без обычного advance)."""
        ref = step["ref"]
        handler = self.actions.get(ref)
        if handler is None:
            execution.error = f"missing_action:{ref}"
            execution.note(step["id"], "action", "error", execution.error)
            raise ExecutorTerminal("errored")
        arguments = {}
        for key, value in (step.get("arguments") or {}).items():
            try:
                arguments[key] = expand(value, merged)
            except BindingError as exc:
                # необязательный аргумент — одна подстановка без значения («sn»: {{slots.target.sn}},
                # адресат не назван): не передаётся; прочие ошибки шаблона — честный отказ
                if isinstance(value, str) and value.strip().startswith("{{") and value.strip().endswith("}}") \
                        and value.count("{{") == 1:
                    continue
                execution.error = f"action_arguments:{ref}:{exc}"
                execution.note(step["id"], "action", "error", execution.error)
                raise ExecutorTerminal("errored")
        action_id = self._action_id(ref, arguments)
        if action_id in execution.action_results:
            execution.note(step["id"], "action", "ok", f"idempotent:{action_id}")
            return
        execution.extension = extension_for_action(ref)  # TASK-053: настройки владельца, встроенному — None
        try:
            result = handler(arguments, execution)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # ошибка действия → ветка on_failure или error
            execution.note(step["id"], "action", "failed", type(exc).__name__)
            target = step.get("on_failure")
            if target:
                execution.position = self._index_of(execution, target)
                return True
            execution.error = f"action_failed:{ref}:{type(exc).__name__}"
            execution.note(step["id"], "action", "error", execution.error)
            raise ExecutorTerminal("errored")
        finally:
            execution.extension = None  # контекст действия не живёт дольше вызова
        execution.action_results[action_id] = True
        if step.get("slot"):
            execution.slots[step["slot"]] = result  # результат эффекта — данные для веток JSON
        execution.note(step["id"], "action", "ok", f"action_id={action_id}")

    async def _compose(self, step, execution, merged):
        """Ответ — только декларация JSON: reply (карточка/реплика/директивы)
        или отладочный raw (сообщение целиком из полей). Кодовых композеров нет."""
        from .reply_compose import compose_raw, compose_reply
        try:
            if "raw" in step:
                message = compose_raw(step["raw"], merged, self.schemas_root)
                tag = "raw"
            else:
                reply = step.get("reply") or {}
                message = compose_reply(reply, merged, self.schemas_root)
                tag = "reply:service" if reply.get("envelope") == "service" else "reply"
                if tag == "reply":  # части озвучки: озвучиваются одной фразой (voice._synthesize_cached)
                    parts = expand(reply.get("speech_parts"), merged) if reply.get("speech_parts") else None
                    execution.speech_parts = [str(p) for p in parts or [] if str(p or "").strip()]
        except Exception as exc:  # bindings/дескриптор/base64 — ошибка значения
            execution.error = f"compose_failed:{type(exc).__name__}:{exc}"
            execution.note(step["id"], "compose", "error", execution.error)
            raise ExecutorTerminal("errored")
        execution.composed.append((tag, message))
        execution.note(step["id"], "compose", "ok", tag)

    # --- датасеты: данные плагинов в БД (scenario_data, scenario_id dataset:<имя>) ---
    @staticmethod
    def _dataset_id(name):
        return "dataset:" + str(name)

    async def refresh_dataset(self, name, key, args=None, timeout=10.0, dataset=None):
        """Обновить запись датасета плагином `name` и дождаться с таймаутом (TASK-050).

        Логика SDK (ctx.dataset_record): плагин запускается задачей,
        результат пишет в БД сам executor; параллельные вызовы на ту же запись (dataset,
        key) ждут одну задачу (_refreshing). Вышел таймаут — asyncio.TimeoutError, задача
        дорабатывает в фоне (запись появится к следующему запросу). Сбой плагина — его
        исключение. Нет плагина — LookupError. Возвращает значение записи."""
        plugin = self.plugins.get(name)
        if plugin is None:
            raise LookupError(f"нет плагина {name}")
        dataset = dataset or name
        key = str(key)
        job = (dataset, key)
        task = self._refreshing.get(job)
        if task is None or task.done():
            task = asyncio.ensure_future(self._run_plugin(plugin, dataset, key, dict(args or {})))
            self._refreshing[job] = task
            task.add_done_callback(lambda _t, job=job: self._refreshing.pop(job, None))
        return await asyncio.wait_for(asyncio.shield(task), timeout)

    async def _run_plugin(self, plugin, dataset, key, args):
        value = await plugin(key=key, args=args)
        if self.data_store is not None:
            self.data_store.scenario_data_set(self._dataset_id(dataset), "system", "", key, value)
        return value

    @staticmethod
    def _owner(scope, ctx):
        if scope == "user":
            return str((ctx.get("user") or {}).get("id") or "")
        if scope == "device":
            return str((ctx.get("device") or {}).get("sn") or (ctx.get("input") or {}).get("sn") or "")
        return ""

    def _load_store(self, scenario, ctx):
        """Данные сценария для контекста: store.user / store.device / store.system."""
        result = {"user": {}, "device": {}, "system": {}}
        if self.data_store is None:
            return result
        for scope in result:
            owner = self._owner(scope, ctx or {})
            if scope != "system" and not owner:
                continue
            result[scope] = self.data_store.scenario_data_get(scenario.get("id", ""), scope, owner)
        return result

    # --- утилиты ----------------------------------------------------------------
    def _ctx(self, execution, ctx):
        merged = dict(ctx or {})
        merged["slots"] = execution.slots
        merged["state"] = execution.state
        merged["store"] = execution.state.get("_store") or {}
        merged["tables"] = execution.scenario.get("tables") or {}
        merged["lexicon"] = {**self.lexicons, **(execution.scenario.get("lexicon") or {})}
        clock = dict(merged.get("clock") or {})
        now = clock.get("now")
        if now is not None and hasattr(now, "hour"):
            clock.update(hour=now.hour, minute=now.minute, month=now.month,
                         weekday=now.isoweekday(), epoch=int(now.timestamp()))
        merged["clock"] = clock
        return merged

    @staticmethod
    def _literal(value, merged):
        return expand(value, merged)

    @staticmethod
    def _action_id(ref, arguments):
        import json
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        return f"{ref}:{hash(canonical) & 0xffffffffffff:012x}"

    @staticmethod
    def _index_of(execution, step_id):
        for index, step in enumerate(execution.scenario["flow"]):
            if step["id"] == step_id:
                return index
        return len(execution.scenario["flow"])  # невалидная ссылка отсечена валидатором

    def _advance(self, step, execution):
        target = step.get("next")
        execution.position = self._index_of(execution, target) if target else execution.position + 1
        if execution.position >= len(execution.scenario["flow"]):
            # fallthrough за последним шагом без терминала — валидатор не пропустит,
            # но исполнение обязано завершиться детерминированно
            execution.error = "flow_end_without_terminal"
            execution.note("*", "error", "terminal", execution.error)
            raise ExecutorTerminal("errored")

    def _persist(self, execution):
        if self.store is not None:
            self.store.save_execution(execution.dialog_id, execution.snapshot())

    def _forget(self, dialog_id):
        self.live.pop(dialog_id, None)
        if self.store is not None:
            self.store.delete_execution(dialog_id)

    def _execution_for(self, dialog_id):
        entry = self.live.get(dialog_id)
        if entry:
            return entry[0]
        if self.store is not None:
            payload = self.store.load_execution(dialog_id)
            if payload:
                return Execution.restore(payload)  # рестарт во время ожидания
        return None
