"""Кеш озвучки сценарных ответов по сегментам (файлы WAV).

Сценарий режет реплику на части (reply.speech_parts: «Сейчас» · «+12» ·
«градусов»); каждая часть синтезируется один раз и ложится в файл по хешу
(движок + голос + текст). Повторная фраза — чтение с диска вместо синтеза;
части склеиваются короткой паузой. Без speech_parts вся реплика — одна часть
(«Включаю.», «Блютуз выключен.» кешируются целиком).

index.json — какие фразы уже синтезировались ({digest: {text, identity}}): из него и из
стандартных ответов раздел «Звуковой кеш» строит список; regenerate() пересинтезирует
все фразы текущим движком в фоне, прогресс — job.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import wave
from pathlib import Path

import numpy as np

_TAG_RE = re.compile(r"<[^>]+>")

GAP_SECONDS = 0.06  # пауза между частями: стык без щелчка и без «слипания» слов
MAX_FILES = 4000  # потолок кеша: при превышении удаляются самые старые файлы


def engine_identity(tts):
    """Отпечаток движка: класс + скалярные настройки (голос/модель/скорость).
    Смена голоса или модели — другой ключ, старые файлы не подмешиваются."""
    attrs = sorted((k, v) for k, v in vars(tts).items()
                   if not k.startswith("_") and isinstance(v, (str, int, float, bool)))
    return type(tts).__name__ + repr(attrs)


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _spoken(value):
    """Строка для кеша как её озвучит _synthesize_cached: без SSML-тегов, пробелы схлопнуты;
    шаблоны с {{переменными}} — не фразы кеша (меняются от запроса к запросу)."""
    if not isinstance(value, str) or "{{" in value or "{%" in value:
        return ""
    return " ".join(_TAG_RE.sub("", value).split())


def scenario_phrases(scenarios):
    """Озвучиваемые фразы сценариев: reply.text/speech или speech_parts одной фразой, вопросы
    диалога (question) и сообщения об ошибке (message). Фразы с переменными не кешируются."""
    result = []

    def walk(node, title, ident):
        if isinstance(node, list):
            for item in node:
                walk(item, title, ident)
            return
        if not isinstance(node, dict):
            return
        reply = node.get("reply")
        if isinstance(reply, dict):
            parts = reply.get("speech_parts")
            if isinstance(parts, list) and parts:  # озвучивается одной фразой; с переменной — мимо кеша
                spoken = [_spoken(p) for p in parts]
                text = " ".join(spoken) if all(spoken) else ""
            else:
                text = _spoken(reply.get("speech") or reply.get("text"))
            if text:
                result.append((text, title, ident))
        for key in ("question", "message"):
            text = _spoken(node.get(key))
            if text:
                result.append((text, title, ident))
        for key, value in node.items():
            if key != "reply" and isinstance(value, (dict, list)):
                walk(value, title, ident)

    for scenario in scenarios:
        title = scenario.get("title")
        title = (title.get("ru") or title.get("en") or "") if isinstance(title, dict) else str(title or scenario.get("id", ""))
        walk(scenario.get("flow") or [], title, scenario.get("id", ""))
    return result


def _scenarios(runtime):
    """Включённые сценарии — как их видит движок (пользовательские + библиотека без выключенных)."""
    from .scenario_engine.library import SCENARIOS as LIBRARY
    result = [s for s in runtime.settings.get("scenarios", []) if isinstance(s, dict) and s.get("enabled", True)]
    if runtime.settings.get("builtin_scenarios", True):
        known, off = {s.get("id") for s in result}, set(runtime.settings.get("builtin_disabled") or [])
        result += [s for key, s in LIBRARY.items() if key not in known and key not in off]
    return result


def static_phrases(runtime):
    """Фразы, которые озвучиваются как есть: включённые стандартные ответы и строки сценариев
    без переменных. {text: {text, source, name, id, builtin?}}; всё прочее в кеше — значения переменных."""
    result = {}
    for reply in runtime.settings.get("standard_replies") or []:
        text = " ".join(str(reply.get("text") or "").split())
        if text and reply.get("enabled", True):
            result.setdefault(text, {"text": text, "source": "reply", "name": reply.get("name", ""), "id": reply.get("id", "")})
    from .behavior import extension_replies  # стандартные ответы расширений (TASK-032), только чтение
    known_ids = {reply.get("id") for reply in runtime.settings.get("standard_replies") or []}
    for _origin, reply in extension_replies():
        text = " ".join(str(reply.get("text") or "").split())
        if text and reply.get("enabled", True) and reply["id"] not in known_ids:
            result.setdefault(text, {"text": text, "source": "reply", "name": reply.get("name", ""), "id": reply["id"]})
    users = {s.get("id") for s in runtime.settings.get("scenarios", []) if isinstance(s, dict)}
    for text, title, ident in scenario_phrases(_scenarios(runtime)):
        result.setdefault(text, {"text": text, "source": "scenario", "name": title, "id": ident, "builtin": ident not in users})
    for text in agent_phrases(runtime):
        result.setdefault(text, {"text": text, "source": "agent"})
    return result


def agent_phrases(runtime):
    """Раздел «AI агент»: короткий ответ отложенного ответа (async_wait_text) и озвучиваемые фразы сбоев."""
    from .async_reply import MISSING_TEXT, wait_text
    from .intelligence import not_understood
    from .voice import failure_phrases
    return list(dict.fromkeys(_norm(t) for t in [wait_text(runtime), MISSING_TEXT, not_understood(runtime)["text"]]
                              + failure_phrases() if _norm(t)))


class TtsCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.hits = self.misses = 0
        self._index = None
        self.is_static = lambda text: True  # Runtime подставляет проверку по сценариям/ответам
        self.job = None  # {"total", "done", "current", "errors", "state": running|done|error, "items": {text: state}}
        self._task = None

    # --- индекс фраз ----------------------------------------------------------------
    @property
    def index(self):
        if self._index is None:
            try:
                self._index = json.loads((self.directory / "index.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._index = {}
        return self._index

    def _remember(self, path, identity, text):
        # dynamic — значение переменной (не строка сценария/ответа): в списке не показывается,
        # при очистке не удаляется как «лишнее» (ограничено лишь общим лимитом MAX_FILES)
        self.index[path.stem] = {"text": _norm(text), "identity": identity, "dynamic": not self.is_static(_norm(text))}
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / "index.json.tmp"
        temporary.write_text(json.dumps(self.index, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.directory / "index.json")

    def texts(self):
        """Все фразы, когда-либо попадавшие в кеш (любым движком), по порядку первого появления."""
        return list(dict.fromkeys(item["text"] for item in self.index.values()
                                  if item.get("text") and not item.get("dynamic")))

    def cached(self, identity, text):
        return self.path_for(identity, text).exists()

    def cleanup(self, keep):
        """Удалить статические фразы, которых больше нет в сценариях/ответах (файлы всех движков)."""
        keep = {_norm(t) for t in keep}
        gone = {item["text"] for item in self.index.values()
                if item.get("text") and not item.get("dynamic") and item["text"] not in keep}
        if self.job and self.job.get("state") == "running":
            gone -= set(self.job.get("items", {}))
        for text in gone:
            self.forget(text)
        return sorted(gone)

    def forget(self, text):
        """Убрать фразу из кеша: файлы всех движков и запись индекса."""
        text = _norm(text)
        self._drop([d for d, item in self.index.items() if item.get("text") == text])

    def drop_dynamic(self):
        """Смена синтеза: значения переменных (dynamic) удаляются — файлы и записи индекса."""
        digests = [d for d, item in self.index.items() if item.get("dynamic")]
        self._drop(digests)
        return len(digests)

    def _drop(self, digests):
        if not digests:
            return
        for digest in digests:
            for path in self.directory.glob(digest[:2] + "/" + digest + ".wav"):
                path.unlink(missing_ok=True)
            self.index.pop(digest, None)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "index.json").write_text(json.dumps(self.index, ensure_ascii=False), encoding="utf-8")

    # --- перегенерация ----------------------------------------------------------------
    def regenerate(self, tts, texts):
        """Фоновый пересинтез фраз текущим движком (старые файлы этого движка заменяются)."""
        if self._task and not self._task.done():
            raise ValueError("Перегенерация уже идёт")
        texts = [t for t in dict.fromkeys(_norm(t) for t in texts) if t]
        self.job = {"state": "running", "total": len(texts), "done": 0, "errors": 0, "current": "",
                    "started": time.time(), "items": {t: "pending" for t in texts}}
        self._task = asyncio.get_running_loop().create_task(self._regenerate(tts, texts))
        return self.job

    async def _regenerate(self, tts, texts):
        identity, job = engine_identity(tts), self.job
        for text in texts:
            job["current"], job["items"][text] = text, "running"
            try:
                self.path_for(identity, text).unlink(missing_ok=True)
                await self.part(tts, text, identity)
                job["items"][text] = "done"
            except Exception as exc:  # noqa: BLE001 — одна фраза не валит весь пересинтез
                job["items"][text], job["errors"] = "error", job["errors"] + 1
                job["error"] = f"{text[:40]}: {type(exc).__name__}: {str(exc)[:160]}"
            job["done"] += 1
        job["current"], job["state"] = "", "done"

    async def warm(self, tts, texts):
        """Досинтезировать недостающие фразы (раздел «AI агент»: «Секунду, думаю», сбои) в фоне,
        чтобы первая же реплика звучала из кеша. Ошибки синтеза — молча: фраза досинтезируется
        при использовании. Пока идёт пересинтез — не мешаем."""
        if (self.job or {}).get("state") == "running" or getattr(self, "_warming", False):
            return 0
        self._warming, done = True, 0
        try:
            identity = engine_identity(tts)
            for text in dict.fromkeys(_norm(t) for t in texts):
                if text and not self.cached(identity, text):
                    try:
                        await self.part(tts, text, identity)
                        done += 1
                    except Exception:  # noqa: BLE001 — TTS недоступен: следующие тоже не выйдут
                        break
        finally:
            self._warming = False
        return done

    def path_for(self, identity, text):
        digest = hashlib.sha1((identity + "\n" + _norm(text)).encode("utf-8")).hexdigest()
        return self.directory / digest[:2] / (digest + ".wav")

    def _read(self, path):
        """PCM файла кеша (float32 mono, rate) или None; битый файл удаляется."""
        try:
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
        except (OSError, wave.Error, EOFError):
            path.unlink(missing_ok=True)
            return None
        self.hits += 1
        return pcm.astype(np.float32) / 32768.0, rate

    def _pack_paths(self, text):
        """Файлы фразы всеми движками, самый свежий первым."""
        text = _norm(text)
        paths = [self.directory / d[:2] / (d + ".wav") for d, item in self.index.items() if item.get("text") == text]
        return sorted((p for p in paths if p.exists()), key=lambda p: -p.stat().st_mtime)

    def pack_path(self, text):
        """Файл фразы в пакете озвучки (любой движок, самый свежий) или None — без чтения звука."""
        return next(iter(self._pack_paths(text)), None)

    def find(self, text):
        """Пакет озвучки: фраза любым движком, когда-либо её синтезировавшим (самый свежий файл),
        — (pcm, rate) или None. Нужен режиму без TTS-движка: кеш играет роль готового пакета."""
        for path in self._pack_paths(text):
            found = self._read(path)
            if found is not None:
                return found
        return None

    async def part(self, tts, text, identity=None):
        """PCM части (float32 mono, rate): из файла или синтез + запись."""
        path = self.path_for(identity or engine_identity(tts), text)
        if path.exists() and (found := self._read(path)) is not None:
            return found
        pcm, rate = await tts.synth_pcm(_norm(text))
        self.misses += 1
        if pcm.size:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with wave.open(str(tmp), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes((np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
            tmp.replace(path)
            self._remember(path, identity or engine_identity(tts), text)
            if self.misses % 50 == 0:
                self.prune()
        return pcm.astype(np.float32), rate

    async def synth(self, tts, parts):
        """Склейка частей: (pcm, rate). Части другой частоты не склеиваются —
        такая часть синтезируется заново целиком вместе с остальными."""
        identity = engine_identity(tts)
        chunks, rate = [], None
        for text in parts:
            if not _norm(text):
                continue
            pcm, part_rate = await self.part(tts, text, identity)
            if rate is None:
                rate = part_rate
            if part_rate != rate:
                return await tts.synth_pcm(" ".join(_norm(p) for p in parts if _norm(p)))
            if pcm.size:
                if chunks:
                    chunks.append(np.zeros(int(rate * GAP_SECONDS), dtype=np.float32))
                chunks.append(pcm)
        if not chunks:
            return np.zeros(0, dtype=np.float32), rate or 16000
        return np.concatenate(chunks), rate

    def prune(self):
        files = sorted(self.directory.glob("*/*.wav"), key=lambda p: p.stat().st_mtime)
        for path in files[:max(0, len(files) - MAX_FILES)]:
            path.unlink(missing_ok=True)

    def stats(self):
        files = list(self.directory.glob("*/*.wav"))
        return {"files": len(files), "bytes": sum(p.stat().st_size for p in files),
                "hits": self.hits, "misses": self.misses}
