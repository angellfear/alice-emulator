"""Bounded asynchronous audio conversion, no whole-file intermediate buffers."""
import asyncio
import re


class NativeCodecUnavailable(ValueError):
    pass


async def transcode(chunks, source, target):
    """Stateful in-process conversion for declared PCM and Ogg/Opus."""
    import numpy as np
    from .native_audio import decode_opus, encode_opus, resample
    match=re.fullmatch(r'audio/x-pcm;bit=(16|32);rate=(\d+);channels=(\d+)(;float=1)?',source)
    if match:
        bits,rate,channels=map(int,match.groups()[:3]);floating=bool(match.group(4))
        if not 8000<=rate<=192000 or not 1<=channels<=8 or (bits==32 and not floating):
            raise ValueError('Unsupported PCM declaration')
        async def samples():
            pending=b'';width=(bits//8)*channels
            async for chunk in chunks:
                pending+=chunk;size=len(pending)//width*width
                if size:
                    x=np.frombuffer(pending[:size],dtype='<f4' if floating else '<i2').astype(np.float32)
                    if not floating:x/=32768.0
                    pending=pending[size:]
                    yield x.reshape(-1,channels).mean(axis=1)
            if pending:raise ValueError('Incomplete PCM sample')
        audio=samples()
    elif source == 'audio/wav':
        # Streaming RIFF parser: skip optional chunks, never assume a 44-byte header.
        async def samples():
            buf=bytearray();header=False;remaining=0;kind=None;fmt=None;pad=False
            async for chunk in chunks:
                buf.extend(chunk)
                if not header:
                    if len(buf)<12:continue
                    if buf[:4]!=b'RIFF' or buf[8:12]!=b'WAVE':raise ValueError('Invalid WAV')
                    del buf[:12];header=True
                while True:
                    if kind is None:
                        if pad:
                            if not buf:break
                            del buf[:1];pad=False
                        if len(buf)<8:break
                        import struct
                        kind=bytes(buf[:4]);remaining=struct.unpack_from('<I',buf,4)[0];del buf[:8]
                        pad=bool(remaining%2)
                    if kind==b'fmt ':
                        if remaining>65536:raise ValueError('WAV fmt too large')
                        if len(buf)<remaining:break
                        if remaining<16:raise ValueError('Invalid WAV fmt')
                        import struct
                        code,nchan,hz,_,_,bits=struct.unpack_from('<HHIIHH',buf)
                        if code!=1 or bits!=16 or not 1<=nchan<=8 or not 8000<=hz<=192000:
                            raise ValueError('Only PCM16 WAV is supported')
                        fmt=(nchan,hz);del buf[:remaining];remaining=0
                    elif kind==b'data':
                        if not fmt:raise ValueError('WAV data before fmt')
                        nchan,hz=fmt;width=nchan*2
                        size=min(len(buf),remaining)//width*width
                        if not size:break
                        x=np.frombuffer(bytes(buf[:size]),dtype='<i2').astype(np.float32)/32768
                        del buf[:size];remaining-=size
                        yield x.reshape(-1,nchan).mean(axis=1),hz
                    else:
                        size=min(len(buf),remaining);del buf[:size];remaining-=size
                    if remaining:break
                    kind=None
            if not header or remaining or buf:raise ValueError('Truncated WAV')
        # Rate is discovered lazily from fmt; resample inside this source.
        async def normalized():
            converter=None;hz=None
            from .native_audio import resample
            async for x,hz in samples():
                if converter is None:
                    import soxr
                    converter=soxr.ResampleStream(hz,48000,1,dtype='float32')
                out=converter.resample_chunk(x)
                if len(out):yield out
            if converter:
                out=converter.resample_chunk(np.empty(0,np.float32),last=True)
                if len(out):yield out
        audio=normalized();rate=48000
    elif source in ('audio/opus','audio/ogg'):
        audio=decode_opus(chunks);rate=48000
    else:raise NativeCodecUnavailable('Unsupported source format: '+source)
    if target=='opus':
        async for page in encode_opus(resample(audio,rate,48000)):yield page
    else:
        if target not in ('pcm16','f32_8000'):raise ValueError('Unsupported audio target')
        async for x in resample(audio,rate,8000 if target=='f32_8000' else 16000):
            if target=='pcm16':yield (np.clip(x,-1,32767/32768)*32768).astype('<i2').tobytes()
            else:yield x.astype('<f4').tobytes()


async def ogg_pages(chunks):
    buffer = bytearray()
    eos = False
    async for chunk in chunks:
        buffer.extend(chunk)
        while len(buffer) >= 27:
            if buffer[:4] != b'OggS':
                raise ValueError('Invalid Ogg stream')
            count = buffer[26]
            if len(buffer) < 27+count:
                break
            size = 27+count+sum(buffer[27:27+count])
            if len(buffer) < size:
                break
            page = bytes(buffer[:size]); del buffer[:size]
            eos = bool(page[5] & 4)
            yield page
    if buffer or not eos:
        raise ValueError('Truncated Ogg stream')
