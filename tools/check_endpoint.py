import sherpa_onnx,numpy as np,wave,soxr
from pathlib import Path
r=Path('/root/alice-sherpa-models')
c=sherpa_onnx.VadModelConfig();c.sample_rate=16000;c.silero_vad.model=str(r/'silero_vad.onnx');c.silero_vad.window_size=512;c.silero_vad.min_silence_duration=.8
v=sherpa_onnx.VoiceActivityDetector(c,buffer_size_in_seconds=30)
with wave.open(str(r/'sherpa-onnx-streaming-t-one-russian-2025-09-08/0.wav')) as w:x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
x=soxr.resample(x,8000,16000).astype(np.float32)
x=np.concatenate([x,np.zeros(32000,dtype=np.float32)])
for i in range(0,len(x),512):v.accept_waveform(x[i:i+512])
print('vad_has_completed_speech',not v.empty())
