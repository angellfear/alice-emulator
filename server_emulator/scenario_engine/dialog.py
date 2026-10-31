"""R07: dialog/transport слой между событиями соединения и executor.

Порядок обработки (v9 §4): ожидающий диалог (continuation) продолжается
РАНЬШЕ выбора нового сценария — новый триггер не перебивает уточнение.
Выбор сценария: template-совпадения (TASK-022, вся фраза + слоты) приоритетнее
phrase-триггеров (router.with_templates); сущности шаблона попадают в ctx.slots.
Доставка — AAPI TServerMessage: MessageId генерируется, RefMessageId/RequestId
берутся из input-контекста запроса (correlation IDs). Вопрос ask доставляется
как AliceResponse simple_text; скомпонованные рецепты — как есть.
Голосовая заглушка (chat fallback) не трогается: DialogManager
подключается к Conversation точечно и только когда сценарий выбран.
"""
from __future__ import annotations

import json
import uuid

from ..response_compose import finalize
from .conditions import check
from .executor import Executor
from .normalize import lexicon_for, normalize_text, normalize_variants
from .probe import dependencies_ok, is_builtin
from .router import EVENT_FILTERS, candidates, with_templates
from .templates import TemplateIndex, builtin_source


def _finalize(message, ctx):
    """CardId/SequenceNumber/tts_play_placeholder — каркас реальных ответов."""
    sequence = (ctx.get("sequence") or 1) if isinstance(ctx.get("sequence"), int) else None
    finalize(message, sequence)
    _ref_header(message, ctx)


def _ref_header(message, ctx):
    """Correlation IDs доставки: RefMessageId ← входящее сообщение, MessageId новый."""
    if message.WhichOneof("Message") == "Event":
        if not message.Event.Header.MessageId:
            message.Event.Header.MessageId = uuid.uuid4().hex
        ref = (ctx.get("input") or {}).get("message_id")
        if ref and not message.Event.Header.RefMessageId:
            message.Event.Header.RefMessageId = ref


def question_message(server_type, text, ctx, listen=False):
    """Карточка-вопрос/заметка: с CardId и каркасом реального ответа. listen=True —
    вопрос уточнения (TASK-020): ShouldListen — колонка слушает ответ без «Алиса»;
    путь доставки один для JSON-ask и py-ask, флага хватает обоим."""
    message = server_type()
    response = message.Event.AliceResponse
    response.Header.RequestId = (ctx.get("input") or {}).get("request_id", "")
    response.Header.ResponseId = uuid.uuid4().hex
    response.Response.Cards.add(Type="simple_text", Text=text)
    response.VoiceResponse.OutputSpeech.Text = text
    response.VoiceResponse.HasVoiceResponse = False
    response.VoiceResponse.ShouldListen = bool(listen)
    _finalize(message, ctx)
    return message


def _speech_text(outcome):
    """Озвучиваемый текст прогона: последняя скомпонованная реплика; вопрос
    уточнения и текст ошибки озвучиваются тем же трактом (голосовой путь)."""
    text = ""
    for _ref, message in outcome.composed:
        if message.WhichOneof("Message") == "Event" and message.Event.WhichOneof("Event") == "AliceResponse":
            candidate = message.Event.AliceResponse.VoiceResponse.OutputSpeech.Text
            if candidate:
                text = candidate
    if not text and outcome.status == "awaiting" and outcome.question:
        text = outcome.question
    if not text and outcome.status == "errored" and not outcome.composed:
        text = "Команда не выполнена: " + (outcome.error or "неизвестная ошибка")
    return text


class DialogManager:
    def __init__(self, executor: Executor, scenarios_provider, server_type, lexicon_sources=None):
        self.executor = executor
        self.scenarios = scenarios_provider  # callable → список валидных определений
        self.server_type = server_type
        # Источники словарей шаблонов (TASK-022): встроенный lexicons.json; слои
        # расширений/пользовательские добавляются ПОЗЖЕ в списке (TASK-030) —
        # поздний источник приоритетнее (merge_lexicons).
        self.lexicon_sources = [builtin_source()] if lexicon_sources is None else list(lexicon_sources)
        self._templates = None  # TemplateIndex каталога — кэш (компиляция один раз)
        self._templates_key = None
        self._templates_sources = None

    def _template_index(self, catalog):
        """Кэш шаблонов: компиляция один раз на версию каталога. Ключ — содержимое
        template-триггеров (id + предложения/списки): загрузка каталога и сохранение
        сценария меняют его → перекомпиляция; смена источников словарей — тоже."""
        key = tuple(
            (str(scenario.get("id")),
             json.dumps([t for t in (scenario.get("triggers") or [])
                         if isinstance(t, dict) and t.get("kind") == "template"],
                        ensure_ascii=False, sort_keys=True, default=str))
            for scenario in catalog
            if any(isinstance(t, dict) and t.get("kind") == "template"
                   for t in (scenario.get("triggers") or [])))
        if (self._templates is None or key != self._templates_key
                or self._templates_sources is not self.lexicon_sources):
            self._templates = TemplateIndex(catalog, self.lexicon_sources)
            self._templates_key = key
            self._templates_sources = self.lexicon_sources
        return self._templates

    def _candidates(self, catalog, templates, replica):
        """Кандидаты реплики: template-совпадения (вся фраза) приоритетнее phrase;
        возвращает ([(scenario, reason)], {scenario_id: slots}) — слоты совпавших
        шаблонов для победителя (guard/зависимости решают, кто победит)."""
        found = candidates(catalog, text=replica)
        if templates is None:
            return found, {}
        hits = templates.match_all(replica)
        if not hits:
            return found, {}
        by_id = {}
        for scenario in catalog:
            by_id.setdefault(str(scenario.get("id")), scenario)
        matched = [by_id[sid] for sid, _slots in hits if sid in by_id]
        return with_templates(found, matched), dict(hits)

    async def handle_text(self, text, ctx, dialog_id, send, deliver=True):
        """Текстовая реплика: continuation → probe (H02, без эффектов) → router →
        execute → доставка. Возвращает отчёт {handled, verdict, reason, status};
        handled=False — сценарий не выбран, вызывающий использует свой fallback.
        deliver=False — сообщения НЕ отправляются, а возвращаются финализированными
        в report['messages']: голосовой тракт сначала синтезирует TTS и ставит
        HasVoiceResponse как облако (True только когда аудио действительно пойдёт),
        затем отправляет сам."""
        execution = self.executor._execution_for(dialog_id) if hasattr(self.executor, "_execution_for") else None
        if execution is not None and execution.awaiting:
            outcome = await self.executor.answer(dialog_id, text, ctx)  # слот-ответ — сырая реплика
            return await self._finish(outcome, ctx, send, deliver, "continuation",
                                      execution.scenario.get("id"))
        # Нормализация только для выбора сценария (S01): ASR-финалы бывают
        # склеенными и с wake-словом; ctx.input.text остаётся исходным.
        catalog = self.scenarios()
        lexicon = lexicon_for(catalog)
        templates = self._template_index(catalog)
        found, slots_by_id = self._candidates(catalog, templates, normalize_text(text, lexicon))
        if not found:
            # однобуквенные огрехи ASR («вдключи сказки»): пробуем нечёткие варианты;
            # сматченный вариант становится input.text — парсеры действий читают его
            for alt in normalize_variants(text, lexicon):
                found, slots_by_id = self._candidates(catalog, templates, alt)
                if found:
                    ctx.setdefault("input", {})["text_raw"] = text
                    ctx["input"]["text"] = alt
                    break
        if not found:
            return {"handled": False, "verdict": "unsupported", "reason": "no_match", "status": None}
        # Кандидаты по порядку: зависимости → guard (право отказа: условие не сошлось —
        # сценарий сам отказался, реплика уходит следующему кандидату или в LLM).
        passing, declined, dep_reason = [], [], None
        for scenario, reason in found:
            verdict, candidate_reason = dependencies_ok(scenario, self.executor)
            if verdict != "supported":
                dep_reason = dep_reason or candidate_reason  # первый кандидат — в отчёт
                continue
            guard = scenario.get("guard")
            if guard is not None and not check(guard, ctx):
                declined.append(scenario.get("id"))
                continue
            passing.append((scenario, reason))
        if not passing:
            reason = ("guard_declined:" + ",".join(str(x) for x in declined)) if declined \
                else (dep_reason or "unsupported")
            return {"handled": False, "verdict": "unsupported", "reason": reason, "status": None}
        scenario, reason = passing[0]
        template_slots = slots_by_id.get(str(scenario.get("id")))
        if template_slots:
            # Сущности шаблона → slots контекста запроса: guard и py-сценарии читают
            # ctx.slots; JSON-сценарий получает их как slots.* после seed'а в executor
            # (контракт — docs/contracts/scenario-json-v2.md «Триггер template»).
            ctx.setdefault("slots", {}).update(template_slots)
        for other, other_reason in passing[1:]:
            if is_builtin(scenario) and is_builtin(other):
                continue
            if other_reason == reason:
                return {"handled": False, "verdict": "ambiguous",
                        "reason": f"tie:{other.get('id')}:{reason}", "status": None}
        # TASK-019: declined (py-сценарий отказался, как guard=false) — реплика следующему
        # кандидату по порядку; все отказались → реплика уходит в режим «Интеллект».
        declined = []
        for candidate, candidate_reason in passing:
            outcome = await self.executor.execute(candidate, ctx, dialog_id)
            if outcome.status == "declined":
                declined.append(candidate.get("id"))
                continue
            return await self._finish(outcome, ctx, send, deliver, candidate_reason,
                                      candidate.get("id"))
        return {"handled": False, "verdict": "unsupported",
                "reason": "script_declined:" + ",".join(str(x) for x in declined),
                "status": None}

    async def handle_event(self, event_name, ctx, dialog_id, send):
        """Служебное событие (event-триггеры с фильтрами callback/purpose/frame).
        Порядок — priority каталога; fallback-сценарии (catch-all) последними."""
        fields = {key: (ctx.get("input") or {}).get(key) for key in EVENT_FILTERS}
        for scenario, reason in candidates(self.scenarios(), event_name=event_name, event=fields):
            verdict, dep_reason = dependencies_ok(scenario, self.executor)
            if verdict != "supported":
                continue
            guard = scenario.get("guard")
            if guard is not None and not check(guard, ctx):
                continue
            outcome = await self.executor.execute(scenario, ctx, dialog_id)
            if outcome.status == "declined":  # py-сценарий отказался — следующий кандидат
                continue
            return await self._finish(outcome, ctx, send, True, reason, scenario.get("id"))
        return {"handled": False, "verdict": "unsupported", "reason": "no_match", "status": None}

    async def quiet_ack(self, ctx):
        """Умолчание колбэка без поставщика (TASK-058): тот же тихий пустой ответ,
        что собирает compose сценария system.service_ack."""
        from .reply_compose import compose_reply
        message = compose_reply({"text": "", "card": False}, ctx, self.executor.schemas_root)
        _finalize(message, ctx)
        return message

    def provider(self, event):
        """Поставщик данных внутреннего события (вкладка «WS», TASK-058): первый сценарий
        с provide-триггером этого события в порядке каталога (его переопределяет пользовательский
        с той же ролью) или None — отвечает умолчание колбэка."""
        for scenario in self.scenarios():
            for trigger in scenario.get("triggers", []):
                if trigger.get("kind") == "provide" and \
                        (trigger.get("event") or "").lower() == (event or "").lower():
                    return scenario
        return None

    async def run_scenario(self, scenario, ctx, dialog_id):
        """Запуск выбранного сценария без роутера (инструмент агента scenario_run):
        сообщения не отправляются, а возвращаются в report['messages']."""
        outcome = await self.executor.execute(scenario, ctx, dialog_id)
        if outcome.status == "declined":  # py-сценарий отказался — агенту честное not-run
            return {"handled": False, "verdict": "unsupported", "reason": "script_declined",
                    "status": None}
        return await self._finish(outcome, ctx, None, False, "agent", scenario.get("id"))

    async def _finish(self, outcome, ctx, send, deliver, reason, scenario_id):
        messages = self._messages_for(outcome, ctx)
        if deliver:
            for message in messages:
                await send(b"AAPI" + message.SerializeToString())
        report = {"handled": True, "reason": reason, "status": outcome.status,
                  "scenario": scenario_id, "speech": _speech_text(outcome),
                  "speech_parts": list(getattr(outcome, "speech_parts", []) or []),
                  "effects": list(getattr(outcome, "effects", []) or []),  # TASK-019: dry-run py-сценария
                  "trace": [{"step": t["step"], "op": t["op"], "status": t["status"]} for t in outcome.trace]}
        if not deliver:
            report["messages"] = messages
        return report

    def _messages_for(self, outcome, ctx):
        """Финализированные сообщения прогона в порядке доставки (ответ → уточнение
        → текст ошибки). HasVoiceResponse здесь не поднимается: это решение
        голосового тракта (аудио синтезировано — флаг поднимется перед отправкой).
        reply:service — тихий служебный ответ облака (envelope service): каркас
        ResponseId/EffectiveSettings собран compose_reply, tts_play_placeholder и
        SequenceNumber не добавляются. raw — отладочное сообщение целиком из JSON:
        только correlation-заголовок."""
        messages = []
        for ref, message in outcome.composed:
            if ref in ("reply:service", "raw"):
                _ref_header(message, ctx)
            else:
                _finalize(message, ctx)
            messages.append(message)
        if outcome.status == "awaiting" and outcome.question:
            messages.append(question_message(self.server_type, outcome.question, ctx, listen=True))
        elif outcome.status == "errored" and not outcome.composed:
            text = "Команда не выполнена: " + (outcome.error or "неизвестная ошибка")
            messages.append(question_message(self.server_type, text, ctx))
        return messages
