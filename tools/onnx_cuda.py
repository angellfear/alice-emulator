"""Проверка CUDA для onnxruntime в окружении речевого сервера (onnx-asr: GigaAM, Parakeet).

Одна проверка на двоих: при установке пакета (`python onnx_cuda.py <папка с *.onnx>` — выход ≠ 0
с понятным текстом) и при запуске сервера (`problem()` в tools/onnx_asr_server.py). Проверка идёт в том
самом интерпретаторе venv, который потом запускает движок: драйвер и сборка видны именно ему.

ORT при сбое загрузки CUDA молча уходит на процессор, поэтому «есть CUDAExecutionProvider в списке»
мало: настоящая проверка — сессия на небольшой модели, у которой первым провайдером должна остаться CUDA.

Многокомпонентные модели (GigaAM: encoder/decoder/joint) держат архитектурно разный ONNX-граф в
каждом файле — RNN-операции (cudnnSetDropoutDescriptor и т.п.) есть только в decoder.onnx, а не в
маленьком joint.onnx. Проверка самого маленького файла может молча пропустить именно ту часть, что
падает на CUDA (например, RNN-декодер на GPU старой архитектуры, где cuDNN 9 рвётся отдельно от
остальных операций) — поэтому проверяются все файлы до разумного предела размера, а не один.
"""
import glob
import os
import subprocess
import sys

MIN_DRIVER = 525  # onnxruntime-gpu под CUDA 12.x: драйвер NVIDIA 525+ (Linux) / 528+ (Windows)
CUDA = "CUDAExecutionProvider"
SAMPLE_SIZE_LIMIT = 50 * 1024 * 1024  # байт: крупный encoder пропускаем — долго грузить, суть не в нём


def distributions():
    """{'onnxruntime': версия | None, 'onnxruntime-gpu': версия | None} — какие сборки стоят в окружении."""
    from importlib import metadata
    result = {}
    for name in ("onnxruntime", "onnxruntime-gpu"):
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = None
    return result


def driver_version():
    """Версия драйвера NVIDIA из nvidia-smi или '' (нет драйвера/видеокарты)."""
    try:
        done = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = done.stdout.strip().splitlines() if done.returncode == 0 else []
    return lines[0].strip() if lines else ""


def explain(available, versions, driver, loaded=None):
    """Причина, по которой CUDA не работает, или None.
    available — get_available_providers(); versions — distributions(); driver — driver_version();
    loaded — провайдеры сессии на реальной модели (None — сессию не создавали)."""
    cpu, gpu = versions.get("onnxruntime"), versions.get("onnxruntime-gpu")
    fix = "Откройте «Голосовой стек» и нажмите «Переустановить» у пакета."
    if not driver:
        return "драйвер NVIDIA не найден (nvidia-smi не отвечает): установите драйвер или выберите «Процессор (CPU)»."
    major = int(driver.split(".")[0]) if driver.split(".")[0].isdigit() else 0
    if major and major < MIN_DRIVER:
        return (f"драйвер NVIDIA {driver} слишком старый для CUDA 12 (нужен {MIN_DRIVER}+): обновите драйвер "
                "или выберите «Процессор (CPU)».")
    if not gpu:
        return (f"в окружении стоит onnxruntime {cpu or '—'} — сборка только для процессора "
                f"(драйвер NVIDIA {driver} исправен). {fix}")
    if cpu:
        return (f"в окружении одновременно onnxruntime {cpu} и onnxruntime-gpu {gpu}: они перекрывают друг друга, "
                f"и видна сборка без CUDA. {fix}")
    if CUDA not in available:
        return (f"onnxruntime-gpu {gpu} не отдаёт {CUDA} (доступно: {', '.join(available) or 'ничего'}). {fix}")
    if loaded is not None and (not loaded or loaded[0] != CUDA):
        return (f"onnxruntime-gpu {gpu} не смог загрузить библиотеки CUDA/cuDNN (драйвер NVIDIA {driver}), "
                f"сессия ушла на процессор. {fix} Если не поможет — проверьте журнал сервера пакета.")
    return None


def sample_models(folder):
    """Все *.onnx до SAMPLE_SIZE_LIMIT, от меньшего к большему — не только самый маленький
    (у многокомпонентных моделей разные файлы держат разные операции, см. docstring модуля)."""
    files = glob.glob(os.path.join(folder, "**", "*.onnx"), recursive=True) if folder else []
    small = [f for f in files if os.path.getsize(f) <= SAMPLE_SIZE_LIMIT]
    return sorted(small, key=os.path.getsize)


def problem(model_dir=None):
    """Причина (русский текст) недоступности CUDA в этом окружении или None, если CUDA рабочая."""
    import onnxruntime
    try:
        from speech_common import preload_cuda_libs
        preload_cuda_libs()
    except ImportError:  # запуск отдельным скриптом вне tools/ — путь поиска DLL не готовим
        pass
    try:
        onnxruntime.preload_dlls()
    except Exception:  # noqa: BLE001 — старые сборки без preload_dlls
        pass
    versions, driver, available = distributions(), driver_version(), onnxruntime.get_available_providers()
    reason = explain(available, versions, driver)
    if reason:
        return reason
    paths = sample_models(model_dir)
    if not paths:
        return None
    for path in paths:
        try:
            session = onnxruntime.InferenceSession(path, providers=[CUDA, "CPUExecutionProvider"])
        except Exception as exc:  # noqa: BLE001 — сообщение ORT говорит, какой именно DLL нет
            return explain(available, versions, driver, loaded=[]) + f" Ошибка onnxruntime ({os.path.basename(path)}): {str(exc)[:300]}"
        reason = explain(available, versions, driver, loaded=session.get_providers())
        if reason:
            return reason
    return None


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    message = problem(sys.argv[1] if len(sys.argv) > 1 else None)
    if message:
        sys.exit("CUDA не работает: " + message)
    print("CUDA для onnxruntime работает", flush=True)
