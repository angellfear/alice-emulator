"""Журналы очереди «Эмулятор → Без ответа» — то, на что у эмулятора не нашлось обработчика.

Runtime держит три журнала (HTTP-промахи — отдельно, EndpointCatalog.note_miss):
* phrase_misses   — фразы без сценария (в любом режиме «Интеллект»: даже если ответила модель);
* aapi_misses     — WS AAPI-сообщения, которые не обработал ни разговор, ни протокольный каркас;
* callback_misses — служебные колбэки server_action (@@mm_*), не взятые protocol_event-сценарием.
Группировка по ключу (фраза — нормализованная), примеры, SN; при переполнении вытесняются самые старые.
"""
import json
import os
import re
import time
from pathlib import Path

LIMIT = 300
EXAMPLES = 3
EXAMPLE_CHARS = 600


def normalize(text):
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е")).split())


def exact(text):
    return " ".join(str(text or "").split())


class MissJournal:
    def __init__(self, path, key=normalize):
        self.path = Path(path)
        self.key = key  # normalize — фразы; exact — имена сообщений и колбэков
        self._items = None

    @property
    def items(self):
        if self._items is None:
            try:
                self._items = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._items = {}
        return self._items

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass  # журнал — подсказка администратору, не повод ронять ответ колонке

    def note(self, text, sn="", example=None):
        """text — то, что группирует запись; example — образец (по умолчанию сам text)."""
        key = self.key(text)
        if not key:
            return
        now = time.time()
        item = self.items.get(key) or {"phrase": key, "count": 0, "examples": [], "sns": [], "first_ts": now}
        item["count"] += 1
        item["last_ts"] = now
        example = text if example is None else str(example)[:EXAMPLE_CHARS]
        if example not in item["examples"] and len(item["examples"]) < EXAMPLES:
            item["examples"].append(example)
        if sn and sn not in item["sns"]:
            item["sns"] = (item["sns"] + [sn])[-5:]
        self.items[key] = item
        if len(self.items) > LIMIT:
            for old in sorted(self.items, key=lambda k: self.items[k]["last_ts"])[:len(self.items) - LIMIT]:
                del self.items[old]
        self._save()

    def forget(self, key=None):
        """Убрать фразу (key=None — очистить журнал)."""
        if key is None:
            self.items.clear()
        else:
            self.items.pop(key, None)
        self._save()

    def public(self):
        return sorted((dict(item, key=key) for key, item in self.items.items()), key=lambda m: -m["last_ts"])
