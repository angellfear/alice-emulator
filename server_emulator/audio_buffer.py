"""Низкоуровневая работа со звуком голосового конвейера (TASK-005, DEBT-02).

Чистый модуль: только аудио-данные (PCM, WAV, Ogg/Opus), без runtime и protobuf.
voice.py импортирует отсюда приём входного аудио (input_audio), разбор/сборку
Ogg-страниц (ogg_pages, pcm_opus_pages), буферизацию входных аудиоканалов
(AudioBuffer с лимитами длительности/размера) и фрейминг исходящего аудио
(write_frames — 4-байтный номер канала + страница).
"""
import asyncio
import io
import re
import struct
import time
import wave

# Границы формата входного PCM (input_audio): s16le, 8 кГц…192 кГц, 1…8 каналов.
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000
MAX_PCM_CHANNELS = 8

# Лимиты входного аудиопотока на соединение (receive_audio): дольше 90 с или
# больше 16 MiB суммарно по всем каналам — канал закрывается с причиной «лимит»;
# не больше 4 одновременных входных каналов.
MAX_INPUT_SECONDS = 90
MAX_INPUT_BYTES = 16 * 1024 ** 2
MAX_INPUT_STREAMS = 4


class InputLimit(Exception):
    """Превышен лимит длительности или размера входного аудио."""


def input_audio(data, fmt):
    """Фрейм входного аудио → (данные для ASR, имя файла): PCM s16le → WAV,
    Opus в Ogg — как есть. Иначе ValueError (формат не поддерживается)."""
    match = re.fullmatch(r'audio/x-pcm;bit=16;rate=(\d+);channels=(\d+)', fmt)
    if match:
        rate, channels = map(int, match.groups())
        if not MIN_SAMPLE_RATE <= rate <= MAX_SAMPLE_RATE or not 1 <= channels <= MAX_PCM_CHANNELS or len(data) % (channels * 2):
            raise ValueError('Некорректные параметры PCM или неполный сэмпл')
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(data)
        return buffer.getvalue(), 'input.wav'
    if fmt == 'audio/opus' and data.startswith(b'OggS'):
        return data, 'input.ogg'
    raise ValueError('Поддержаны PCM s16le и Opus в Ogg-контейнере')


def ogg_pages(data):
    """Ответ TTS → список полных Ogg-страниц. Только Ogg/Opus с заголовком
    OpusHead; обрыв страницы/таблицы или отсутствие EOS — ValueError."""
    if not data.startswith(b'OggS') or b'OpusHead' not in data[:128]:
        raise ValueError('Для устройства TTS должен возвращать Ogg/Opus')
    result, pos = [], 0
    while pos < len(data):
        if data[pos:pos+4] != b'OggS' or len(data)-pos < 27:
            raise ValueError('Оборванный Ogg от TTS')
        count = data[pos+26]
        if len(data)-pos < 27+count:
            raise ValueError('Оборванная таблица Ogg')
        size = 27+count+sum(data[pos+27:pos+27+count])
        if size > len(data)-pos:
            raise ValueError('Оборванная страница Ogg')
        result.append(data[pos:pos+size])
        pos += size
    if not result[-1][5] & 4:
        raise ValueError('TTS Ogg не содержит завершающей EOS-страницы')
    return result


async def pcm_opus_pages(pcm, rate):
    """PCM движка TTS (float32 mono) → Ogg/Opus-страницы для аудиоканала станции
    (тот же кодек, что у потокового синтеза: audio_stream.transcode)."""
    import numpy as np
    from .audio_stream import ogg_pages as stream_pages, transcode

    async def source():
        yield np.asarray(pcm, dtype='<f4').tobytes()

    return [page async for page in stream_pages(
        transcode(source(), f'audio/x-pcm;bit=32;rate={int(rate)};channels=1;float=1', 'opus'))]


async def write_frames(send, channel, pages, speech=None):
    """Фрейминг исходящего аудио: каждый кадр — 4-байтный большой-endian номер
    канала + Ogg-страница. Страницы идут уступом цикла (sleep(0)), чтобы WS-очередь
    не забивалась одним кадром. speech — остаток потока (страницы из асинхронного
    синтеза). Возвращает число отправленных байт страниц."""
    sent_bytes = 0
    for page in pages:
        await send(struct.pack('>I', channel) + page)
        sent_bytes += len(page)
        await asyncio.sleep(0)
    if speech:
        async for page in speech:
            await send(struct.pack('>I', channel) + page)
            sent_bytes += len(page)
    return sent_bytes


class AudioBuffer:
    """Входные аудиоканалы одного соединения: буфер чанков с лимитами.

    Канал открывается open(channel) — state это dict, вызывающий дополняет его
    своими ключами (входящее сообщение, trace, задача ASR). push добавляет чанк
    (после 4-байтного заголовка канала) и следит за суммарными размером и
    длительностью: при превышении — InputLimit (вызывающий закрывает канал).
    Без зависимости от runtime/protobuf — только счётчики и байты.
    """

    def __init__(self, max_seconds=MAX_INPUT_SECONDS, max_bytes=MAX_INPUT_BYTES):
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self.channels = {}

    def open(self, channel):
        """Новый входной канал: буфер, счётчик байт, момент старта (monotonic)."""
        state = {'audio': bytearray(), 'size': 0, 'started': time.monotonic()}
        self.channels[channel] = state
        return state

    def get(self, channel):
        return self.channels.get(channel)

    def total_bytes(self):
        return sum(state['size'] for state in self.channels.values())

    def too_long(self, state):
        """Канал живёт дольше лимита (проверка при закрытии клиентом)."""
        return time.monotonic() - state['started'] > self.max_seconds

    def push(self, channel, chunk, buffered=True):
        """Чанк в счёт канал. InputLimit — суммарно больше max_bytes или канал
        живёт дольше max_seconds; байты при этом не учитываются.
        buffered=False — чанк уходит потоковому читателю (queue), в буфер не пишется."""
        state = self.channels.get(channel)
        if state is None:
            return
        if self.too_long(state) or self.total_bytes() + len(chunk) > self.max_bytes:
            raise InputLimit('Превышен лимит длительности или размера входного аудио')
        if buffered:
            state['audio'].extend(chunk)
        state['size'] += len(chunk)


# --- исходящее аудио (TTS): выбор движка и сборка страниц ----------------------


def streaming_source(maxsize=32):
    """Очередь + генератор чанков для потокового ASR: chunks() читает очередь
    до маркера None. receive() кладёт чанки, recognize_stream() потребляет."""
    queue = asyncio.Queue(maxsize=maxsize)

    async def chunks():
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

    return queue, chunks


async def collect_pages(pages, speech=None):
    """Первые страницы + остаток потока → один список (пререндер отложенного
    ответа: весь звук нужен целиком, до Push)."""
    result = list(pages)
    if speech:
        result += [page async for page in speech]
    return result


async def synthesize(runtime, text, wait=None):
    """Синтез реплики → (первые ogg-страницы, поток хвоста | None) по настройкам
    runtime.settings['tts'] (потоковый движок или batch + ogg_pages).
    Первый звук — не дольше wait (по умолчанию tts_wait, TimeoutError): колонка ждёт ответ ~17 с."""
    from .spoken import tts_wait
    cfg = runtime.settings['tts']
    wait = wait or tts_wait(runtime)
    if runtime.tts_streaming():
        speech = runtime.speech_stream(text)
        try:
            return [await asyncio.wait_for(anext(speech), wait)], speech
        except BaseException:
            await speech.aclose()
            raise
    raw, _ = await asyncio.wait_for(runtime.provider('tts', {
        'model': cfg['model'], 'voice': cfg['voice'], 'response_format': cfg['response_format'], 'input': text}),
        wait)
    return ogg_pages(raw), None


async def synthesize_cached(runtime, parts, wait=None):
    """Озвучка реплики всегда одной фразой (склейка кусков звучит плохо). Фраза целиком
    из сценария/стандартного ответа (без переменных) — из файлового кеша (tts_cache);
    без PCM-движка — из пакета озвучки (кеш любого движка), иначе обычный синтез;
    с подставленными значениями — обычный синтез целиком, мимо кеша."""
    from .spoken import tts_wait
    tts = runtime.get_tts()
    cache = getattr(runtime, 'tts_cache', None)
    text = ' '.join(' '.join(re.sub(r'<[^>]+>', '', p).split()) for p in parts if p).strip()
    if not text:
        return [], None
    if tts is None and cache is not None and (found := cache.find(text)) is not None:
        pcm, rate = found  # нет PCM-движка — фраза из пакета озвучки (кеш любого движка)
        return (await pcm_opus_pages(pcm, rate) if pcm.size else []), None
    if tts is None or cache is None:
        return await synthesize(runtime, text, wait)
    pcm, rate = await asyncio.wait_for(cache.synth(tts, [text]) if cache.is_static(text) else tts.synth_pcm(text),
                                       wait or tts_wait(runtime))
    if pcm.size == 0:
        return [], None
    return await pcm_opus_pages(pcm, rate), None
