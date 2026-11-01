"""NVIDIA Parakeet-tdt-0.6b-v3 (многоязычная, в том числе русский): OpenAI Audio API /v1/audio/transcriptions
на onnx-asr (ONNX-версия без NeMo). Окружение — tools/onnx_asr_server.py.
"""
import onnx_asr_server

if __name__ == "__main__":
    onnx_asr_server.main("nemo-parakeet-tdt-0.6b-v3", "Parakeet v3", 5016)
