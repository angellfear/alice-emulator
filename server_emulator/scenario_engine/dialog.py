"""dialog/transport слой между событиями соединения и executor.

Порядок обработки (v9 §4): ожидающий диалог (continuation) продолжается
РАНЬШЕ выбора нового сценария — новый триггер не перебивает уточнение.
Выбор сценария: template-совпадения (вся фраза + слоты) приоритетнее
phrase-триггеров (router.with_templates); сущности шаблона попадают в ctx.slots.
Доставка — AAPI TServerMessage: MessageId генерируется, RefMessageId/RequestId
берутся из input-контекста запроса (correlation IDs). Вопрос ask доставляется
как AliceResponse simple_text; скомпонованные рецепты — как есть.
Голосовая заглушка (chat fallback) не трогается: DialogManager
подключается к Conversation точечно и только когда сценарий выбран.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from types import SimpleNamespace

from ..response_compose import finalize
from ..sdk import ScenarioContext
from .conditions import check
from .executor import Executor
from .normalize import lexicon_for, normalize_text, normalize_variants
from .probe import dependencies_ok, is_builtin
from .router import EVENT_FILTERS, candidates
from .templates import TemplateIndex, builtin_source

log = logging.getLogger(__name__)


def _merge_templates(found, matched):
    """Шаблонные совпадения (с причиной: template или dir:<запись справочника>) —
    первыми в своём слое, phrase-кандидаты остаются fallback'ом (guard/зависимости
    диалоговый слой разрешает по объединённому списку как раньше)."""
    if not matched:
        return list(found)
    return list(matched) + list(found)


def _finalize(message, ctx):
    """CardId/SequenceNumber/tts_play_placeholder — каркас реальных ответов."""
    sequence = (ctx.get("sequence") or 1) if isinstance(ctx.get("sequence"), int) else None
    finalize(message, sequence, ctx.get("settings"))
    _ref_header(message, ctx)

def finalize_reply(message, ctx):
    """Публичная обёртка каркаса для внешних сборщиков ответа (sdk._AapiContext.reply):
    CardId/SequenceNumber/RefMessageId — то же, что делает диалоговый слой."""
    _finalize(message, ctx)


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
    вопрос уточнения: ShouldListen — колонка слушает ответ без «Алиса»;
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


SCENARIO_FAILED = "Не получилось выполнить команду."


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
        # Источники словарей шаблонов: встроенный lexicons.json; слои
        # расширений/пользовательские добавляются ПОЗЖЕ в списке —
        # поздний источник приоритетнее (merge_lexicons).
        self.lexicon_sources = [builtin_source()] if lexicon_sources is None else list(lexicon_sources)
        self._templates = None  # TemplateIndex каталога — кэш (компиляция один раз)
        self._templates_key = None
        self._templates_sources = None
        # Справочник фраз плагинов (каркас «от плагинов»): callable → PhraseDirectory
        # (собирается при пересборке реестров) и ручной порядок — callable → settings.
        # None — справочника нет (в поставке файлов пока нет). extension_context и
        # search_timeout ставит runtime — опрос каталогов ступени свободных хвостов.
        self.phrase_directory = None
        self.phrase_order = lambda: {}
        self.extension_context = None  # callable ext_id → ExtensionContext | None
        self.search_timeout = lambda: 3.0

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
        """Кандидаты реплики послойно: свой слой (пользователь + расширения) целиком
        раньше встроенного; внутри слоя шаблонные совпадения (вся фраза) приоритетнее
        phrase (полная фраза — самый строгий сигнал), между собой — по точности
        (score hassil-духа: меньше wildcard-сущностей, длиннее литеральный текст,
        меньше захвачено хвостами); равная точность — порядок каталога (стабильная
        сортировка). Записи справочника: handler `core:<id>` — кандидат в сценарий-
        обработчик со своими слотами (причина dir:<запись>); записи search:true — не
        кандидаты, а ступень опроса каталогов (их собирает handle_text, когда кандидатов
        нет). Сценарий входит в кандидаты один раз — самым точным совпадением.
        Возвращает ([(scenario, reason)], {scenario_id: slots}, [записи поиска])."""
        found = candidates(catalog, text=replica)
        if templates is None:
            return found, {}, []
        by_id = {}
        for scenario in catalog:
            by_id.setdefault(str(scenario.get("id")), scenario)
        order = self.phrase_order() or {}
        best = {}  # sid → (slots, ключ порядка, reason): самое точное совпадение сценария
        for sid, slots, score in templates.ranked(replica):
            if sid not in by_id:
                continue
            key = (score, -1, -1)  # -1: сценарий ядра раньше записей при равной точности
            if sid not in best or key < best[sid][1]:
                best[sid] = (slots, key, "template")  # -1: сценарий ядра раньше записей при равной точности
        search_hits = []
        directory = self.phrase_directory() if self.phrase_directory else None
        if directory is not None:
            matches = directory.match(replica)
            search_hits = [(entry, slots) for entry, slots, _score in matches if entry.get("search")]
            # ручной порядок по ВСЕМ совпавшим записям вида: выбранный поставщик
            # вида первым (selected из runtime), дальше список владельца, алфавит
            positions = self._entry_positions(matches, order)
            for entry, slots, score in matches:
                if entry.get("search"):
                    continue
                sid = directory.handler_scenario(entry)
                if sid is None or sid not in by_id:
                    continue  # обработчик-плагин — группа переноса записей
                within, kind_rank = positions.get(entry.get("kind") or "_other", ({}, 0))
                key = (score, kind_rank, within.get(entry.get("ext_id") or "", 0))
                if sid not in best or key < best[sid][1]:
                    best[sid] = (slots, key, "dir:" + entry["id"])
        slots_by_id, ranked = {}, []
        for builtin_layer in (False, True):  # свой слой (пользователь + расширения) → встроенный
            hits = sorted(((_key, sid, reason) for sid, (slots, _key, reason) in best.items()
                           if (by_id[sid].get("_origin") == "builtin") == builtin_layer),
                          key=lambda item: item[0])  # стабильная: равная точность — каталог
            for _key, sid, reason in hits:
                slots_by_id[sid] = best[sid][0]
                ranked.append((by_id[sid], reason))
            ranked += [pair for pair in found
                       if (pair[0].get("_origin") == "builtin") == builtin_layer]
        return ranked, slots_by_id, search_hits


    @staticmethod
    def _entry_positions(matches, order):
        """Позиции ручного порядка для совпавших записей (settings["phrase_order"]):
        {kind: (позиция плагина в виде, позиция вида)} — выбранный поставщик вида
        первым, список владельца из настроек, остальное по порядку совпадений."""
        kinds_list = [k for k in (order.get("kinds") or []) if isinstance(k, str)]
        groups = order.get("plugins") if isinstance(order.get("plugins"), dict) else {}
        positions = {}
        for kind in {entry.get("kind") or "_other" for entry, _s, _sc in matches}:
            within = [p for p in groups.get(kind) or [] if isinstance(p, str)]
            for ext in (entry.get("ext_id") or "" for entry, _s, _sc in matches):
                if ext and ext not in within:
                    within.append(ext)
            selected = str((order.get("selected") or {}).get(kind) or "")
            if selected in within:
                within.remove(selected)
                within.insert(0, selected)
            positions[kind] = ({ext: i for i, ext in enumerate(within)},
                               kinds_list.index(kind) if kind in kinds_list else len(kinds_list))
        return positions

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
            if outcome is not None:
                return await self._finish(outcome, ctx, send, deliver, "continuation",
                                          execution.scenario.get("id"))
            # TTL ожидания истёк и снят — реплика не съедается, а идёт
            # обычным путём (сценарии → «Вместе» → модель)
        # Нормализация только для выбора сценария (S01): ASR-финалы бывают
        # склеенными и с wake-словом; ctx.input.text остаётся исходным.
        catalog = self.scenarios()
        lexicon = lexicon_for(catalog)
        templates = self._template_index(catalog)
        found, slots_by_id, search_hits = self._candidates(catalog, templates, normalize_text(text, lexicon))
        if not found and not search_hits:
            # однобуквенные огрехи ASR («вдключи сказки»): пробуем нечёткие варианты;
            # сматченный вариант становится input.text — парсеры действий читают его
            for alt in normalize_variants(text, lexicon):
                found, slots_by_id, search_hits = self._candidates(catalog, templates, alt)
                if found:
                    ctx.setdefault("input", {})["text_raw"] = text
                    ctx["input"]["text"] = alt
                    break
        if not found:
            # ступень опроса каталогов: свободные хвосты («включи {X}») — параллельный
            # поиск по записям search:true, отвечает нашедший (равный — ручной порядок);
            # не нашёл никто — «Не нашла «X».» в режиме сценариев, иначе «Интеллект»
            return await self._search_stage(search_hits, catalog, text, ctx, dialog_id, send, deliver)
        # сухой прогон («Проверка»): кто был кандидатом и в каком порядке — до guard
        # и зависимостей; ранжирование видно по порядку
        if ctx.get("dry_run"):
            ctx["_candidates"] = [(str(scenario.get("id")), reason) for scenario, reason in found]
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
            report = {"handled": False, "verdict": "unsupported", "reason": reason, "status": None}
            if ctx.get("dry_run"):
                report["candidates"] = list(ctx.get("_candidates") or [])
            return report
        scenario, reason = passing[0]
        if reason.startswith("dir:"):  # запись справочника — исполнителю её владелец
            entry = (self.phrase_directory().entries if self.phrase_directory else {}).get(reason[4:]) or {}
            ctx["phrase_entry"] = {"id": entry.get("id"), "ext_id": entry.get("ext_id"),
                                   "kind": entry.get("kind")}
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
                report = {"handled": False, "verdict": "ambiguous",
                          "reason": f"tie:{other.get('id')}:{reason}", "status": None}
                if ctx.get("dry_run"):
                    report["candidates"] = list(ctx.get("_candidates") or [])
                return report
        # declined (py-сценарий отказался, как guard=false) — реплика следующему
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

    async def _search_stage(self, search_hits, catalog, text, ctx, dialog_id, send, deliver):
        """Ступень свободных хвостов («включи {X}»): параллельный опрос каталогов
        записей search:true (по одному на плагин) с таймаутом search_timeout; отвечает
        нашедший — его запись, исполнитель получает готовый результат поиска. Равный
        результат — ручной порядок (выбранный поставщик вида первым). Не нашёл никто:
        отчёт с not_found_query — режим «сценарии» отвечает «Не нашла «X».», model/agent
        уводят фразу в «Интеллект». Записей поиска нет — прежний честный no_match."""
        if not search_hits:
            return {"handled": False, "verdict": "unsupported", "reason": "no_match", "status": None}
        poll, seen = [], set()
        for entry, slots in search_hits:
            if entry.get("ext_id") in seen:
                continue  # один опрос на плагин: одинаковые хвосты дают тот же запрос
            seen.add(entry.get("ext_id"))
            query = str(slots.get("query") or "").strip() or text
            poll.append((entry, query))
        from .. import providers as providers_registry
        async def _ask(entry, query):
            # SDK-контекст — свой у каждого опроса: потребитель вида кладёт в
            # ctx.extension контекст своего поставщика (музыка — токен аккаунта) и
            # восстанавливает по окончании; общий контекст параллельных опросов
            # подменял бы токен соседу посреди его вызова
            sdk_ctx = ScenarioContext(SimpleNamespace(scenario_executor=self.executor), ctx,
                                      dry_run=bool((ctx or {}).get("dry_run")))
            record = providers_registry.for_ext(entry.get("kind") or "music", entry.get("ext_id"))
            if record is None or not callable(record.get("search")) or not self.extension_context:
                log.warning("Ступень поиска «%s»: %s не вызван — нет записи реестра или "
                            "каталога расширений", query, entry.get("ext_id"))
                return None
            kind = str(entry.get("kind") or "music")
            started = time.monotonic()
            try:
                found = await providers_registry.invoke(sdk_ctx, record, record["search"],
                                                        query, kind)
            except Exception as exc:  # noqa: BLE001 — каталог не ответил: он просто не нашёл
                log.warning("Ступень поиска «%s»: %s упал за %.1f с — %s: %s",
                            query, entry.get("ext_id"), time.monotonic() - started,
                            type(exc).__name__, exc)
                return None
            if isinstance(found, dict) and found.get("tracks"):
                context = found.get("context") if isinstance(found.get("context"), dict) else {}
                log.info("Ступень поиска «%s»: %s нашёл за %.1f с — %d треков, best %s",
                         query, entry.get("ext_id"), time.monotonic() - started,
                         len(found["tracks"]), context.get("best") or "none")
                return found
            note = str((found or {}).get("note") or "пустой ответ без note")
            log.warning("Ступень поиска «%s»: %s не нашёл за %.1f с (%s)",
                        query, entry.get("ext_id"), time.monotonic() - started, note)
            return None

        timeout = self.search_timeout()
        results = await asyncio.gather(
            *[asyncio.wait_for(_ask(entry, query), timeout) for entry, query in poll],
            return_exceptions=True)
        for (entry, query), found in zip(poll, results):
            if isinstance(found, BaseException):  # тайм-аут опроса (wait_for) и прочие
                log.warning("Ступень поиска «%s»: %s не ответил за %.1f с — %s: %s",
                            query, entry.get("ext_id"), timeout,
                            type(found).__name__, found)
        found_pairs = [(entry, query, found) for (entry, query), found in zip(poll, results)
                       if isinstance(found, dict) and found.get("tracks")]
        query = poll[0][1]
        if not found_pairs:
            report = {"handled": False, "verdict": "unsupported", "reason": "no_match",
                      "not_found_query": query, "status": None}
            if ctx.get("dry_run"):
                report["candidates"] = [(entry["id"], "search") for entry, _q in poll]
            return report
        matches = [(entry, {"query": query}, (0, 0, 0)) for entry, query in poll]
        positions = self._entry_positions(matches, self.phrase_order() or {})
        def _order(item):
            within, kind_rank = positions.get(item[0].get("kind") or "_other", ({}, 0))
            return (kind_rank, within.get(item[0].get("ext_id") or "", 0))
        found_pairs.sort(key=_order)  # ручной порядок: выбранный поставщик первым
        entry, query, found = found_pairs[0]
        by_id = {str(scenario.get("id")): scenario for scenario in catalog}
        scenario = by_id.get(self.phrase_directory().handler_scenario(entry) or "")
        if scenario is None:
            return {"handled": False, "verdict": "unsupported", "reason": "no_match", "status": None}
        ctx["phrase_entry"] = {"id": entry["id"], "ext_id": entry.get("ext_id"),
                               "kind": entry.get("kind"), "search_result": found}
        ctx.setdefault("slots", {}).update({"query": query})
        outcome = await self.executor.execute(scenario, ctx, dialog_id)
        return await self._finish(outcome, ctx, send, deliver, "search:" + entry["id"],
                                  scenario.get("id"))

    async def handle_event(self, event_name, ctx, dialog_id, send, deliver=True):
        """Служебное событие (event-триггеры с фильтрами callback/purpose/frame).
        Порядок — priority каталога; fallback-сценарии (catch-all) последними.
        deliver=False — сообщения возвращаются в report['messages'] неотправленными
        (голосовой конвейер синтезирует озвучку реплики перед доставкой)."""
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
            return await self._finish(outcome, ctx, send, deliver, reason, scenario.get("id"))
        return {"handled": False, "verdict": "unsupported", "reason": "no_match", "status": None}

    async def quiet_ack(self, ctx):
        """Умолчание колбэка без поставщика: тот же тихий пустой ответ,
        что собирает compose сценария system.service_ack."""
        from .reply_compose import compose_reply
        message = compose_reply({"text": "", "card": False}, ctx, self.executor.schemas_root)
        _finalize(message, ctx)
        return message

    def provider(self, event):
        """Поставщик данных внутреннего события (вкладка «WS»): первый сценарий
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
                  "effects": list(getattr(outcome, "effects", []) or []),  # dry-run py-сценария
                  "trace": [{"step": t["step"], "op": t["op"], "status": t["status"]} for t in outcome.trace]}
        if ctx.get("dry_run"):  # вкладка «Проверка»: какие сущности шаблона попали в слоты
            report["slots"] = copy.deepcopy(ctx.get("slots") or {})
            report["candidates"] = list(ctx.get("_candidates") or [])  # и порядок кандидатов
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
            # колонке — короткая фраза; причина сбоя — в журнале сервера и отчёте проверки
            messages.append(question_message(self.server_type, SCENARIO_FAILED, ctx))
        return messages
