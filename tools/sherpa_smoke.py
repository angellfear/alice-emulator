import time,wave,json
from pathlib import Path
import numpy as np
import sherpa_onnx
root=Path("/root/alice-sherpa-models")
a=root/"sherpa-onnx-streaming-t-one-russian-2025-09-08"
r=sherpa_onnx.OnlineRecognizer.from_t_one_ctc(tokens=str(a/"tokens.txt"),model=str(a/"model.onnx"),num_threads=2,sample_rate=8000)
with wave.open(str(a/"0.wav")) as w: rate=w.getframerate();audio=np.frombuffer(w.readframes(w.getnframes()),dtype="<i2").astype(np.float32)/32768
s=r.create_stream();partials=[];last="";start=time.monotonic()
for pos in range(0,len(audio),int(rate*.1)):
 s.accept_waveform(rate,audio[pos:pos+int(rate*.1)])
 while r.is_ready(s):r.decode_stream(s)
 text=r.get_result(s)
 if text!=last:partials.append({"input_seconds":pos/rate,"text":text});last=text
s.accept_waveform(rate,np.zeros(rate,dtype=np.float32));s.input_finished()
while r.is_ready(s):r.decode_stream(s)
print(json.dumps({"asr_partial_count":len(partials),"first":partials[:1],"final":r.get_result(s),"audio_seconds":len(audio)/rate,"compute_seconds":time.monotonic()-start},ensure_ascii=False))
t=root/"vits-piper-ru_RU-irina-medium"
c=sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(t/"ru_RU-irina-medium.onnx"),tokens=str(t/"tokens.txt"),data_dir=str(t/"espeak-ng-data")),num_threads=2),max_num_sentences=1)
tts=sherpa_onnx.OfflineTts(c);chunks=[];start=time.monotonic()
def callback(samples,progress):chunks.append({"seconds":time.monotonic()-start,"samples":len(samples),"progress":progress});return 1
out=tts.generate("Привет! Я новый голос. Сейчас проверяем потоковую озвучку.",callback=callback)
print(json.dumps({"tts_sample_rate":out.sample_rate,"chunks":chunks,"total_seconds":time.monotonic()-start}))
