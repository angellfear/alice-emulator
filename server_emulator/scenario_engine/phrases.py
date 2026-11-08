"""Справочник фраз плагинов (contributes.providers[].phrases): записи «на что
реагировать и кто отвечает» — шаблоны hassil + свои списки + обработчик. Запись —
не сценарий: свой сценарий плагин подключает только когда у ядра нет штатного
исполнителя; справочник направляет совпавшую фразу обработчику.

Обработчик: `core:<id сценария>` — штатный исполнитель ядра (музыка — стек директив
плеера по образцу журнала; плагины вызывают его, не копируют); `<ext-id>.<модуль:функция>`
— callable плагина для случаев без штатного исполнителя (разрешается группой
переноса записей). Записи принадлежат плагину (id с его префиксом) и его виду —
на этом строится ручной порядок (settings["phrase_order"]) и «чей поставщик отвечает».

Файлы собираются из включённых плагинов (пути из объявлений, manager.phrase_files),
компилируются в hassil Intents с общими слоями словарей (как шаблоны сценариев);
совпадения участвуют в ранжировании кандидатов наравне с шаблонными триггерами —
метрики те же (закрытый список, литеральные куски текста). Выключил плагин —
его фразы исчезли. Сейчас файлов в поставке нет — записи музыки переезжают в
справочник группой переноса; до этого механизм работает вхолостую.
"""
from __future__ import annotations

import json
from pathlib import Path

from hassil import Intents
from hassil.errors import HassilError
from hassil.recognize import recognize_all

from .templates import LANGUAGE, clean, merge_lexicons


class PhraseEntryError(ValueError):
    """Ошибка записи справочника фраз (битый JSON или поля)."""


def load_entries(ext_id, path):
    """Записи одного файла: [{"id", "sentences", "handler", "lists"?, "search"?}];
    id получает префикс расширения. Битая запись — ошибка файла (файл целиком не
    участвует, причина видна в сборке реестра)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — битый файл не включается
        raise PhraseEntryError(f"{Path(path).name}: не читается ({type(exc).__name__})") from exc
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise PhraseEntryError(f"{Path(path).name}: нужен объект с непустым entries[]")
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PhraseEntryError(f"{Path(path).name}: запись — объект")
        handler = str(entry.get("handler") or "").strip()
        sentences = [s for s in entry.get("sentences") or []
                     if isinstance(s, str) and s.strip()]
        if not handler or not sentences or not str(entry.get("id") or "").strip():
            raise PhraseEntryError(f"{Path(path).name}: записям нужны id, sentences[] и handler")
        eid = str(entry["id"]).strip()
        if not eid.startswith(ext_id + "."):
            eid = f"{ext_id}.{eid}"
        item = {"id": eid, "handler": handler, "sentences": sentences,
                "ext_id": ext_id, "search": bool(entry.get("search"))}
        if isinstance(entry.get("lists"), dict) and entry["lists"]:
            item["lists"] = entry["lists"]
        result.append(item)
    return result


class PhraseDirectory:
    """Скомпилированный справочник: match(text) → [(entry, slots, closed, chunks)]
    в порядке объявлений; записи — dict с id/handler/ext_id/search. Битый файл —
    warning без падения раздела (self.warnings), остальные работают."""

    def __init__(self, files, lexicon_sources):
        self.entries = {}
        self.warnings = []
        lists, skip_words = merge_lexicons(lexicon_sources)
        self.skip_words = skip_words
        self.wildcards = {name for name, values in lists.items()
                          if isinstance(values, dict) and values.get("wildcard")}
        intents = {}
        for ext_id, path, kind in files or []:
            try:
                entries = load_entries(ext_id, path)
            except PhraseEntryError as exc:
                self.warnings.append(str(exc))
                continue
            for entry in entries:
                entry["kind"] = kind  # вид объявления — ручной порядок и чей поставщик
                if entry["id"] in self.entries:
                    self.warnings.append(f"{Path(path).name}: запись {entry['id']!r} "
                                         f"уже объявлена — дубль пропущен")
                    continue
                self.entries[entry["id"]] = entry
                data = {"sentences": entry["sentences"]}
                if entry.get("lists"):
                    data["lists"] = entry["lists"]
                intents[entry["id"]] = {"data": [data]}
        self.intents = Intents.from_dict({"language": LANGUAGE, "intents": intents, "lists": lists}) \
            if intents else None

    def match(self, text):
        """[(entry, slots, score)] в порядке объявлений; score — как в
        templates.ranked (меньше wildcard-сущностей, длиннее литеральный текст,
        меньше захвачено хвостами). Битая ссылка на список в рантайме — не
        совпадение (ловится валидатором при установке)."""
        if self.intents is None or not text:
            return []
        cleaned = clean(text)
        try:
            results = list(recognize_all(cleaned, self.intents, skip_words=self.skip_words))
        except HassilError:
            return []
        out = []
        for result in results:
            entry = self.entries.get(result.intent.name)
            if entry is None:
                continue
            from .templates import _mid_word_wildcard
            if _mid_word_wildcard(cleaned, result.entities_list):
                continue  # хвост посреди слова — не совпадение (см. templates.py)
            wild = [entity for entity in result.entities_list if entity.is_wildcard]
            wild_len = sum(len(entity.text or "") for entity in wild)
            slots = {entity.name: entity.value for entity in result.entities_list}
            out.append((entry, slots, (len(wild), -(len(cleaned) - wild_len), wild_len)))
        return out

    def handler_scenario(self, entry):
        """id сценария ядра из handler `core:<id>` или None (обработчик плагина)."""
        handler = entry.get("handler") or ""
        return handler[len("core:"):] if handler.startswith("core:") else None

    def order_key(self, entry, kind, phrase_order):
        """Ключ ручного порядка записи: (порядок вида, порядок плагина в виде);
        плагины без вида — группа «Прочее» (_other); вне списков — после
        перечисленных, между собой — по id (стабильная сортировка сохранит и
        порядок каталога при прочих равных)."""
        order = phrase_order if isinstance(phrase_order, dict) else {}
        kinds = [k for k in order.get("kinds") or [] if isinstance(k, str)]
        groups = order.get("plugins") if isinstance(order.get("plugins"), dict) else {}
        within = [p for p in groups.get(kind) or [] if isinstance(p, str)]
        ext_id = entry.get("ext_id") or ""
        return (kinds.index(kind) if kind in kinds else len(kinds),
                within.index(ext_id) if ext_id in within else len(within), ext_id)
