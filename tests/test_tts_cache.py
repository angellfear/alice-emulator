"""Кеш озвучки по частям: одна часть — один синтез, дальше файл."""
import asyncio

import numpy as np

from server_emulator.tts_cache import GAP_SECONDS, TtsCache, engine_identity


class FakeTTS:
    def __init__(self, voice="alena", rate=16000):
        self.voice, self.rate, self.calls = voice, rate, []

    async def synth_pcm(self, text):
        self.calls.append(text)
        return np.full(160 * len(text), 0.25, dtype=np.float32), self.rate


def test_parts_cached_and_glued(tmp_path):
    cache, tts = TtsCache(tmp_path), FakeTTS()
    pcm, rate = asyncio.run(cache.synth(tts, ["Сейчас", " ", "+12"]))
    assert tts.calls == ["Сейчас", "+12"]  # пустая часть пропущена
    assert pcm.size == 160 * 6 + int(rate * GAP_SECONDS) + 160 * 3
    again, _ = asyncio.run(cache.synth(tts, ["Сейчас", "+12"]))
    assert len(tts.calls) == 2 and np.allclose(again, pcm, atol=1e-3)  # из файлов, int16 round-trip
    assert cache.stats()["files"] == 2 and cache.stats()["hits"] == 2


def test_key_depends_on_engine_and_normalizes_spaces(tmp_path):
    cache = TtsCache(tmp_path)
    assert cache.path_for(engine_identity(FakeTTS()), "Сейчас  ") == cache.path_for(engine_identity(FakeTTS()), "Сейчас")
    assert engine_identity(FakeTTS()) != engine_identity(FakeTTS(voice="other"))
    asyncio.run(cache.synth(FakeTTS(), ["Включаю."]))
    other = FakeTTS(voice="other")
    asyncio.run(cache.synth(other, ["Включаю."]))
    assert other.calls == ["Включаю."]


def test_corrupted_file_resynthesized(tmp_path):
    cache, tts = TtsCache(tmp_path), FakeTTS()
    path = cache.path_for(engine_identity(tts), "Тише.")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"garbage")
    pcm, _ = asyncio.run(cache.synth(tts, ["Тише."]))
    assert tts.calls == ["Тише."] and pcm.size > 0


def test_empty_engine_output(tmp_path):
    class Silent(FakeTTS):
        async def synth_pcm(self, text):
            return np.zeros(0, dtype=np.float32), 16000
    cache = TtsCache(tmp_path)
    pcm, _ = asyncio.run(cache.synth(Silent(), ["…"]))
    assert pcm.size == 0 and cache.stats()["files"] == 0


def test_find_any_engine_freshest(tmp_path):
    """Пакет озвучки (TASK-015): фраза находится без движка, по тексту, любым голосом."""
    cache = TtsCache(tmp_path)
    asyncio.run(cache.synth(FakeTTS(), ["Включаю."]))
    pcm, rate = cache.find("Включаю.  ")
    assert rate == 16000 and pcm.size == 160 * len("Включаю.")
    assert cache.find("Выключаю.") is None


def test_synthesize_cached_plays_pack_without_engine(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from server_emulator import audio_buffer
    cache = TtsCache(tmp_path)
    asyncio.run(cache.synth(FakeTTS(), ["Я пока этого не умею."]))
    runtime = SimpleNamespace(get_tts=lambda: None, tts_cache=cache)

    async def pages(pcm, rate):
        return [b"ogg:%d" % pcm.size]

    async def network(runtime, text, wait=None):
        raise RuntimeError("TTS не настроен: " + text)
    monkeypatch.setattr(audio_buffer, "pcm_opus_pages", pages)
    monkeypatch.setattr(audio_buffer, "synthesize", network)
    result, speech = asyncio.run(audio_buffer.synthesize_cached(runtime, ["Я пока этого не умею."]))
    assert result == [b"ogg:%d" % (160 * len("Я пока этого не умею."))] and speech is None
    try:  # нет в пакете — обычный синтез; без движка он падает, ответ уйдёт карточкой
        asyncio.run(audio_buffer.synthesize_cached(runtime, ["Другая фраза."]))
        raise AssertionError("ожидался сбой синтеза")
    except RuntimeError as exc:
        assert "Другая фраза." in str(exc)


def test_pack_path_without_reading(tmp_path):
    cache = TtsCache(tmp_path)
    assert cache.pack_path("Включаю.") is None
    asyncio.run(cache.synth(FakeTTS(), ["Включаю."]))
    assert cache.pack_path(" Включаю. ") == cache.path_for(engine_identity(FakeTTS()), "Включаю.")
