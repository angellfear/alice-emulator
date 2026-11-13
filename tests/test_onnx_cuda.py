"""CUDA у onnx-asr (GigaAM, Parakeet): установка ставит сборку под видеокарту, проверка идёт в окружении движка,
сообщение называет причину. onnxruntime, nvidia-smi и pip — заглушки: ничего не ставится и не запускается."""
import sys
import types
from pathlib import Path

import pytest

from server_emulator.voice_stack import PLATFORM, LocalSpeech, load_packages
from server_emulator.voice_stack import gpus as real_gpus

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import onnx_cuda  # noqa: E402

CPU_ONLY = {"onnxruntime": "1.31.1", "onnxruntime-gpu": None}
GPU_ONLY = {"onnxruntime": None, "onnxruntime-gpu": "1.24.4"}


def asr(identifier):
    return next(p for p in load_packages()["asr"] if p["id"] == identifier)


# --- причина недоступности CUDA ------------------------------------------------------------
def test_cpu_build_is_named_with_version_and_fix():
    reason = onnx_cuda.explain(["CPUExecutionProvider"], CPU_ONLY, "581.80")
    assert "onnxruntime 1.31.1" in reason and "только для процессора" in reason and "581.80" in reason
    assert "Переустановить" in reason


def test_both_builds_conflict():
    reason = onnx_cuda.explain(["CPUExecutionProvider"], {"onnxruntime": "1.31.1", "onnxruntime-gpu": "1.24.4"}, "581.80")
    assert "одновременно" in reason and "1.24.4" in reason


def test_driver_problems_come_first():
    assert "не найден" in onnx_cuda.explain([], GPU_ONLY, "")
    old = onnx_cuda.explain([], GPU_ONLY, "471.11")
    assert "471.11" in old and "525" in old


def test_gpu_build_without_provider_and_with_fallback_session():
    assert "CPUExecutionProvider" in onnx_cuda.explain(["CPUExecutionProvider"], GPU_ONLY, "581.80")
    assert onnx_cuda.explain([onnx_cuda.CUDA], GPU_ONLY, "581.80") is None
    fallback = onnx_cuda.explain([onnx_cuda.CUDA], GPU_ONLY, "581.80", loaded=["CPUExecutionProvider"])
    assert "cuDNN" in fallback and "581.80" in fallback
    assert onnx_cuda.explain([onnx_cuda.CUDA], GPU_ONLY, "581.80", loaded=[onnx_cuda.CUDA, "CPUExecutionProvider"]) is None


def fake_runtime(monkeypatch, tmp_path, providers, session_providers, versions, driver="581.80"):
    class Session:
        def __init__(self, path, providers):
            self.path = path

        def get_providers(self):
            return session_providers

    module = types.SimpleNamespace(get_available_providers=lambda: providers, preload_dlls=lambda: None,
                                   InferenceSession=Session)
    monkeypatch.setitem(sys.modules, "onnxruntime", module)
    monkeypatch.setattr(onnx_cuda, "distributions", lambda: versions)
    monkeypatch.setattr(onnx_cuda, "driver_version", lambda: driver)
    (tmp_path / "big.onnx").write_bytes(b"0" * 100)
    (tmp_path / "small.onnx").write_bytes(b"0" * 10)


def test_problem_probes_a_real_session(monkeypatch, tmp_path):
    fake_runtime(monkeypatch, tmp_path, [onnx_cuda.CUDA], ["CPUExecutionProvider"], GPU_ONLY)
    assert "не смог загрузить" in onnx_cuda.problem(str(tmp_path))
    fake_runtime(monkeypatch, tmp_path, [onnx_cuda.CUDA], [onnx_cuda.CUDA, "CPUExecutionProvider"], GPU_ONLY)
    assert onnx_cuda.problem(str(tmp_path)) is None
    fake_runtime(monkeypatch, tmp_path, ["CPUExecutionProvider"], ["CPUExecutionProvider"], CPU_ONLY)
    assert "onnxruntime 1.31.1" in onnx_cuda.problem(str(tmp_path))
    assert onnx_cuda.smallest_model(str(tmp_path)).endswith("small.onnx")


def test_server_refuses_cuda_with_the_reason(monkeypatch, tmp_path):
    import onnx_asr_server
    fake_runtime(monkeypatch, tmp_path, ["CPUExecutionProvider"], ["CPUExecutionProvider"], CPU_ONLY)
    monkeypatch.setitem(sys.modules, "onnx_asr", types.SimpleNamespace())
    monkeypatch.setenv("ASR_MODEL_DIR", str(tmp_path))
    load_model, _ = onnx_asr_server.real_loaders()
    with pytest.raises(SystemExit, match="onnxruntime 1.31.1"):
        load_model("gigaam-v3-e2e-rnnt", "cuda", None)


# --- установка и запуск пакета -------------------------------------------------------------
@pytest.mark.parametrize("identifier", ["gigaam", "parakeet"])
def test_cuda_install_replaces_cpu_build_and_verifies(tmp_path, monkeypatch, identifier):
    package = asr(identifier)
    local = LocalSpeech(tmp_path / "speech", tmp_path / "options")
    local.save_options(package, {"device": "cuda"})
    monkeypatch.setattr("server_emulator.voice_stack.gpus", lambda: [{"value": "0", "label": "0"}])
    commands = []
    monkeypatch.setattr(LocalSpeech, "_run", lambda self, command, job, stage: commands.append(command))
    local._install_sync(package, {})
    words = [" ".join(c[1:4]) if "pip" in c else "run" for c in commands]
    pips = [c for c in commands if "pip" in c]
    assert pips[0][2:5] == ["pip", "uninstall", "--yes"] and {"onnxruntime", "onnxruntime-gpu"} <= set(pips[0])
    assert any(a.startswith("onnxruntime-gpu[cuda,cudnn]") for a in pips[1])
    check = commands[-1]
    assert check[1].replace("\\", "/").endswith("tools/onnx_cuda.py") and Path(check[1]).is_file()
    assert len(words) == len(commands) and local.marker(package)["device"] == "cuda"


def test_cpu_install_does_not_leave_gpu_build(tmp_path, monkeypatch):
    package = asr("gigaam")
    local = LocalSpeech(tmp_path / "speech", tmp_path / "options")
    commands = []
    monkeypatch.setattr(LocalSpeech, "_run", lambda self, command, job, stage: commands.append(command))
    local._install_sync(package, {})
    assert "uninstall" in next(c for c in commands if "pip" in c)
    assert not any("onnx_cuda.py" in " ".join(c) for c in commands)


def test_cuda_install_without_video_card_fails_before_pip(tmp_path, monkeypatch):
    package = asr("gigaam")
    local = LocalSpeech(tmp_path / "speech", tmp_path / "options")
    local.save_options(package, {"device": "cuda"})
    monkeypatch.setattr("server_emulator.voice_stack.gpus", lambda: [])
    monkeypatch.setattr(LocalSpeech, "_run", lambda *a: pytest.fail("pip не должен запускаться"))
    with pytest.raises(RuntimeError, match="nvidia-smi"):
        local._install_sync(package, {})


async def test_start_refuses_cuda_on_cpu_install(tmp_path):
    package = asr("gigaam")
    local = LocalSpeech(tmp_path / "speech", tmp_path / "options")
    folder = local.folder(package)
    folder.mkdir(parents=True)
    (folder / "installed.json").write_text('{"device": "cpu", "variant": "e2e-rnnt"}', encoding="utf-8")
    local.save_options(package, {"device": "cuda"})
    with pytest.raises(ValueError, match="Переустановить"):
        await local.start(package)
    assert "gigaam" not in local.processes
    local.save_options(package, {"device": "cpu"})
    assert local.wrong_device(package) == ""
