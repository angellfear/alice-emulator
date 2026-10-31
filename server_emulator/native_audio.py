"""In-process libopus + libsoxr codecs. No subprocess, FFmpeg or media files."""
import ctypes as ct
import struct
import uuid
import numpy as np
import soxr
from pyogg import opus

def crc(data):
    value=0
    for byte in data:
        value ^= byte<<24
        for _ in range(8):
            value=((value<<1)^0x04c11db7 if value&0x80000000 else value<<1)&0xffffffff
    return value

def page(packet, serial, sequence, granule=0, flags=0):
    sizes=bytes([255]*(len(packet)//255)+[len(packet)%255])
    raw=bytearray(struct.pack('<4sBBQIIIB',b'OggS',0,flags,granule,serial,sequence,0,len(sizes))+sizes+packet)
    struct.pack_into('<I',raw,22,crc(raw))
    return bytes(raw)

async def packets(chunks, require_eos=False):
    buf=bytearray();pending=bytearray();serial=None;seq=None;eos=False
    async for chunk in chunks:
        if eos and chunk:raise ValueError('Bytes after Ogg EOS')
        buf.extend(chunk)
        while len(buf)>=27:
            if buf[:4]!=b'OggS' or buf[4]!=0:raise ValueError('Invalid Ogg header')
            n=buf[26]
            if len(buf)<27+n:break
            size=27+n+sum(buf[27:27+n])
            if len(buf)<size:break
            raw=bytearray(buf[:size]);del buf[:size]
            checksum=struct.unpack_from('<I',raw,22)[0];raw[22:26]=b'\0'*4
            if crc(raw)!=checksum:raise ValueError('Ogg CRC mismatch')
            granule,stream,number=struct.unpack_from('<QII',raw,6)
            if serial is None:serial=stream;seq=number
            if stream!=serial or number!=seq:raise ValueError('Ogg stream/sequence changed')
            seq+=1
            if bool(raw[5]&1)!=bool(pending):raise ValueError('Invalid Ogg continuation')
            pos=27+n
            completed=[]
            for length in raw[27:27+n]:
                pending.extend(raw[pos:pos+length]);pos+=length
                if len(pending)>65536:raise ValueError('Ogg packet exceeds audio limit')
                if length<255:completed.append(bytes(pending));pending.clear()
            eos=bool(raw[5]&4)
            for i,packet in enumerate(completed):
                yield packet, granule if eos and i==len(completed)-1 else None
            if eos and buf:raise ValueError('Bytes after Ogg EOS')
    # Device capture can end at the AAPI channel boundary without an Ogg EOS page.
    # This permits only complete CRC-checked pages/packets, never truncated bytes.
    if buf or pending or (require_eos and not eos):raise ValueError('Truncated Ogg/Opus')

async def decode_opus(chunks):
    decoder=None;skip=0;decoded=0;channels=0;header=False;tags=False;gain=1.0
    try:
        async for packet,granule in packets(chunks):
            if not header:
                if len(packet)<19 or packet[:8]!=b'OpusHead' or packet[18]!=0 or packet[9] not in (1,2):
                    raise ValueError('Unsupported OpusHead')
                channels=packet[9];skip=struct.unpack_from('<H',packet,10)[0]
                gain=10**(struct.unpack_from('<h',packet,16)[0]/(256*20))
                error=ct.c_int();decoder=opus.opus_decoder_create(48000,channels,ct.byref(error))
                if error.value or not decoder:raise ValueError('Opus decoder initialization failed')
                header=True;continue
            if not tags:
                if not packet.startswith(b'OpusTags'):raise ValueError('Missing OpusTags')
                tags=True;continue
            pcm=np.empty(5760*channels,dtype=np.float32)
            data=(ct.c_ubyte*len(packet)).from_buffer_copy(packet)
            count=opus.opus_decode_float(decoder,data,len(packet),pcm.ctypes.data_as(ct.POINTER(ct.c_float)),5760,0)
            if count<0:raise ValueError('Invalid Opus packet: '+str(count))
            start=decoded;decoded+=count
            begin=max(0,skip-start);end=count if granule is None else min(count,max(0,granule-start))
            samples=pcm[:count*channels].reshape(-1,channels)[begin:end].mean(axis=1)*gain
            if len(samples):yield samples
        if not header or not tags:raise ValueError('Missing Opus headers')
    finally:
        if decoder:opus.opus_decoder_destroy(decoder)

async def resample(chunks, rate, target):
    converter=soxr.ResampleStream(rate,target,1,dtype='float32',quality='HQ') if rate!=target else None
    async for samples in chunks:
        out=converter.resample_chunk(samples) if converter else samples
        if len(out):yield out
    if converter:
        out=converter.resample_chunk(np.empty(0,np.float32),last=True)
        if len(out):yield out

async def encode_opus(chunks):
    error=ct.c_int();encoder=opus.opus_encoder_create(48000,1,2048,ct.byref(error))
    if error.value or not encoder:raise ValueError('Opus encoder initialization failed')
    skip=ct.c_int();opus.opus_encoder_ctl(encoder,4027,ct.byref(skip))
    serial=uuid.uuid4().int&0xffffffff;seq=2;total=0;encoded=0;pending=None;buf=np.empty(0,np.float32)
    try:
        yield page(b'OpusHead'+struct.pack('<BBHIhB',1,1,skip.value,48000,0,0),serial,0,flags=2)
        yield page(b'OpusTags'+struct.pack('<I',5)+b'Alice'+struct.pack('<I',0),serial,1)
        def encode(frame):
            output=(ct.c_ubyte*4000)()
            size=opus.opus_encode_float(encoder,frame.ctypes.data_as(ct.POINTER(ct.c_float)),960,output,4000)
            if size<0:raise ValueError('Opus encoding failed')
            return bytes(output[:size])
        async for samples in chunks:
            total+=len(samples);buf=np.concatenate((buf,samples))
            while len(buf)>=960:
                packet=encode(buf[:960]);buf=buf[960:];encoded+=960
                if pending:yield page(*pending)
                pending=(packet,serial,seq,encoded);seq+=1
        # Encode encoder lookahead and pad the last frame; final granule trims padding.
        needed=max(0,total+skip.value-encoded)
        buf=np.pad(buf,(0,max(0,((needed+959)//960)*960-len(buf))))
        while len(buf)>=960:
            packet=encode(buf[:960]);buf=buf[960:];encoded+=960
            if pending:yield page(*pending)
            pending=(packet,serial,seq,encoded);seq+=1
        if pending:
            packet,serial,seq,_=pending
            yield page(packet,serial,seq,total+skip.value,4)
    finally:opus.opus_encoder_destroy(encoder)
