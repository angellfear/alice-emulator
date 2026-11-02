"""TASK-088: переключение режима «Интеллект» не отменяет выбор голосового стека.

Дефект: переключатель в шапке слал PUT /api/settings полную копию настроек, загруженных
при открытии страницы (`setLevel` в ui/app.js). Если во вкладке «Голос» выбран источник
ASR/TTS/LLM (VoiceStack.apply пишет settings.stt|tts|model|voice_stack), копия устаревает,
и сохранение режима откатывала их — распознавание замолкало.

Исправление (корень): смена режима — PUT /api/intelligence, сохраняется только ключ
`intelligence`; полная копия формы с устаревшей ревизией `_rev` отклоняется (409, UI
перечитывает и сохраняет заново). Запросы без `_rev` (внешние клиенты) сохраняются как раньше.
"""
import pytest
from aiohttp import ClientSession

from server_emulator.runtime import Runtime
from server_emulator.server import create_app
from tests.conftest import listen

HDRS = {"X-Emulator": "1"}
ASR_VALUES = {"url": "http://127.0.0.1:9/v1/audio/transcriptions", "model": "whisper", "api_key": "k1"}


async def _apply_asr_source(client, url):
    """Выбрать источник ASR API голосового стека — как карточка вкладки «Голос»."""
    r = await client.post(url + "/emulator/api/voice-stack-preset",
                          json={"service": "asr", "adapter": "openai", "title": "Наш ASR", "values": ASR_VALUES})
    preset_id = (await r.json())["id"]
    r = await client.post(url + "/emulator/api/voice-stack-apply", json={"service": "asr", "ref": "p:" + preset_id})
    assert r.status == 200
    return "p:" + preset_id


async def test_mode_switch_keeps_selected_asr_source(tmp_path):
    """Воспроизведение дефекта и фикс: «Простой → Модель» после выбора ASR не откатывает
    stt/voice_stack, устаревшая полная копия отсекается ревизией."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers=HDRS) as client:
            stale = await (await client.get(url + "/emulator/api/settings")).json()  # копия уже открытой вкладки
            ref = await _apply_asr_source(client, url)
            assert runtime.settings["stt"]["url"] == ASR_VALUES["url"]
            assert runtime.settings["voice_stack"]["asr"] == ref

            r = await client.put(url + "/emulator/api/intelligence", json={"intelligence": "model"})
            assert r.status == 200
            assert (await r.json())["intelligence"] == "model"
            assert runtime.settings["stt"]["url"] == ASR_VALUES["url"]  # голосовой тракт жив (до фикса — откат)
            assert runtime.settings["voice_stack"]["asr"] == ref

            stale["intelligence"] = "agent"  # полное сохранение устаревшей копии — сервер отвергает
            r = await client.put(url + "/emulator/api/settings", json=stale)
            assert r.status == 409 and (await r.json())["stale"]
            assert runtime.settings["intelligence"] == "model"
            assert runtime.settings["voice_stack"]["asr"] == ref

            fresh = await (await client.get(url + "/emulator/api/settings")).json()  # свежая копия — сохраняет
            fresh["intelligence"] = "scenarios"
            r = await client.put(url + "/emulator/api/settings", json=fresh)
            assert r.status == 200
            assert runtime.settings["intelligence"] == "scenarios"
            assert runtime.settings["voice_stack"]["asr"] == ref
            assert runtime.settings["stt"]["url"] == ASR_VALUES["url"]
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_full_save_without_rev_keeps_legacy_behaviour(tmp_path):
    """Запрос без `_rev` (внешний клиент/старый UI) сохраняется по-прежнему; секрет __KEEP__
    формы при этом восстанавливается из новых настроек, а не из присланной копии."""
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers=HDRS) as client:
            ref = await _apply_asr_source(client, url)
            copy_without_rev = await (await client.get(url + "/emulator/api/settings")).json()
            copy_without_rev.pop("_rev")
            copy_without_rev["system_prompt"] = "from external client"
            r = await client.put(url + "/emulator/api/settings", json=copy_without_rev)
            assert r.status == 200 and runtime.settings["system_prompt"] == "from external client"
            assert runtime.settings["voice_stack"]["asr"] == ref  # копия была свежая — источник на месте
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_mode_switch_touches_voice_stack_and_cache_nothing(tmp_path, monkeypatch):
    """Смена режима не трогает stt/tts/model/voice_stack, не трогает локальные пакеты
    (не останавливает и не перезапускает) и не прогревает звуковой кеш (локальный TTS
    не занимает речевой сервер во время распознавания). Полный save — прогревает."""
    runtime = Runtime(tmp_path)
    events = []

    async def start_noop(package):
        events.append("start")

    monkeypatch.setattr(runtime.voice_stack.local, "start", start_noop)
    monkeypatch.setattr(runtime.voice_stack.local, "stop", lambda package: events.append("stop"))
    warmed = []
    monkeypatch.setattr(runtime, "warm_sound_cache", lambda delay=0: warmed.append(delay))
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers=HDRS) as client:
            ref = await _apply_asr_source(client, url)
            assert events == []  # облачный пресет: сервера пакета нет и при применении не было
            before = await (await client.get(url + "/emulator/api/settings")).json()
            warm_before = list(warmed)  # прогрев старта панели и применения — считаем базой
            r = await client.put(url + "/emulator/api/intelligence", json={"intelligence": "model"})
            assert r.status == 200
            after = await r.json()
            assert after["intelligence"] == "model"
            for key in ("stt", "tts", "model", "voice_stack"):
                assert after[key] == before.get(key)
            assert after["voice_stack"]["asr"] == ref
            assert warmed == warm_before and events == []  # пакет не тронут, смена режима кеш не грела

            fresh = await (await client.get(url + "/emulator/api/settings")).json()
            assert (await client.put(url + "/emulator/api/settings", json=fresh)).status == 200
            assert warmed == warm_before + [0]  # полный save — прежний прогрев
    finally:
        await runner.cleanup()
        await runtime.close()


async def test_intelligence_put_validates_mode(tmp_path):
    runtime = Runtime(tmp_path)
    runner, url = await listen(create_app(runtime=runtime))
    try:
        async with ClientSession(headers=HDRS) as client:
            for bad in ({"intelligence": "bogus"}, {"intelligence": ""}, {}, {"intelligence": 5}):
                r = await client.put(url + "/emulator/api/intelligence", json=bad)
                assert r.status == 400, bad
            assert runtime.settings["intelligence"] == "agent"  # дефолт не изменился
            # формат ext:<id> законен (недоступный режим откатывается на сценарии при ответе, TASK-034)
            r = await client.put(url + "/emulator/api/intelligence", json={"intelligence": "ext:no-such"})
            assert r.status == 200 and runtime.settings["intelligence"] == "ext:no-such"
    finally:
        await runner.cleanup()
        await runtime.close()
