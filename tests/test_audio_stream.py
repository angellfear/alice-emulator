import asyncio
import pytest
from server_emulator.audio_stream import transcode, ogg_pages, NativeCodecUnavailable

async def test_pcm_stream_emits_before_eof():
    end=asyncio.Event()
    async def source():
        yield b"\0\0"*800
        await end.wait()
    stream=transcode(source(),"audio/x-pcm;bit=16;rate=16000;channels=1","pcm16")
    try:
        assert len(await asyncio.wait_for(anext(stream),1))==1600
        assert not end.is_set()
    finally:await stream.aclose()

async def test_external_conversion_is_not_a_fallback(monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError("External process forbidden")
    monkeypatch.setattr(asyncio,"create_subprocess_exec",forbidden)
    async def source():yield b"OggS"
    with pytest.raises(ValueError):
        [p async for p in transcode(source(),"audio/opus","pcm16")]

async def test_incomplete_pcm_and_ogg_rejected():
    async def source():yield b"x"
    with pytest.raises(ValueError):
        [p async for p in transcode(source(),"audio/x-pcm;bit=16;rate=16000;channels=1","pcm16")]
    with pytest.raises(ValueError):
        [p async for p in ogg_pages(source())]

async def test_native_opus_roundtrip_with_fragmentation_and_crc():
    import numpy as np
    raw=(np.sin(np.arange(16000)*2*np.pi*440/16000)*12000).astype('<i2').tobytes()
    async def source():
        for pos in range(0,len(raw),777):yield raw[pos:pos+777]
    pages=[p async for p in transcode(source(),'audio/x-pcm;bit=16;rate=16000;channels=1','opus')]
    assert b'OpusHead' in pages[0] and pages[-1][5]&4
    async def encoded(broken=False):
        content=bytearray(b''.join(pages))
        if broken:content[-1]^=1
        for pos in range(0,len(content),13):yield bytes(content[pos:pos+13])
    out=b''.join([x async for x in transcode(encoded(),'audio/opus','pcm16')])
    assert len(out)==len(raw)
    a=np.frombuffer(raw,dtype='<i2');b=np.frombuffer(out,dtype='<i2')
    assert np.corrcoef(a,b)[0,1]>.9
    with pytest.raises(ValueError,match='CRC'):
        [x async for x in transcode(encoded(True),'audio/opus','pcm16')]

async def test_float_pcm_resampling_keeps_duration():
    import numpy as np
    data=np.zeros(22050,dtype='<f4').tobytes()
    async def source():
        for pos in range(0,len(data),399):yield data[pos:pos+399]
    out=b''.join([x async for x in transcode(source(),'audio/x-pcm;bit=32;rate=22050;channels=1;float=1','f32_8000')])
    assert len(out)==8000*4

async def test_device_channel_boundary_without_ogg_eos():
    import numpy as np
    import struct
    from server_emulator.native_audio import crc
    async def source():yield np.zeros(16000,dtype='<i2').tobytes()
    pages=[p async for p in transcode(source(),'audio/x-pcm;bit=16;rate=16000;channels=1','opus')]
    tail=bytearray(pages[-1]);tail[5]&=~4;tail[22:26]=b'\0'*4
    struct.pack_into('<I',tail,22,crc(tail));pages[-1]=bytes(tail)
    async def encoded():
        for page in pages:yield page
    output=b''.join([p async for p in transcode(encoded(),'audio/opus','f32_8000')])
    assert 8000<=len(output)//4<8500

async def test_wav_header_and_samples_can_cross_arbitrary_chunks():
    import wave,io
    buf=io.BytesIO()
    with wave.open(buf,'wb') as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(b'\0\0'*16000)
    async def source():
        data=buf.getvalue()
        for pos in range(0,len(data),37):yield data[pos:pos+37]
    output=b''.join([p async for p in transcode(source(),'audio/wav','f32_8000')])
    assert len(output)==8000*4

async def test_new_logical_stream_in_one_channel_is_decoded_in_sequence():
    """Колонка начинает в канале новый Ogg-поток (звук слова активации, затем реплика): оба декодируются."""
    import numpy as np
    import struct
    from server_emulator.native_audio import crc
    async def source():yield np.zeros(16000,dtype='<i2').tobytes()
    chained=[]
    for _ in range(2):
        pages=[p async for p in transcode(source(),'audio/x-pcm;bit=16;rate=16000;channels=1','opus')]
        tail=bytearray(pages[-1]);tail[5]&=~4;tail[22:26]=b'\0'*4
        struct.pack_into('<I',tail,22,crc(tail));pages[-1]=bytes(tail)
        chained+=pages
    async def encoded():
        for page in chained:yield page
    output=b''.join([p async for p in transcode(encoded(),'audio/opus','f32_8000')])
    assert 16000<=len(output)//4<17000

async def test_new_logical_stream_drops_unfinished_packet_of_previous():
    """Новый Ogg-поток (BOS, страница 0) приходит, пока пакет прежнего потока не дособран:
    недособранный пакет отбрасывается, новый поток читается без ошибки."""
    import struct
    from server_emulator.native_audio import crc, page, packets
    def raw_page(segments, body, serial, sequence, flags=0):
        raw = bytearray(struct.pack('<4sBBQIIIB', b'OggS', 0, flags, 0, serial, sequence, 0, len(segments))
                        + bytes(segments) + body)
        struct.pack_into('<I', raw, 22, crc(raw))
        return bytes(raw)
    chain = [page(b'a' * 10, 1, 0, flags=2),
             raw_page([255], b'x' * 255, 1, 1),  # пакет не закончен: продолжения не будет
             page(b'b' * 5, 2, 0, flags=2),
             page(b'c' * 3, 2, 1, flags=4)]
    async def source():
        for item in chain: yield item
    assert [p async for p, _granule in packets(source())] == [b'a' * 10, b'b' * 5, b'c' * 3]
