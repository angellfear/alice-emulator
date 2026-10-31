"""Голосовой стек: пресеты (адаптер + настройки) и пакеты (установка речевого сервера на этот ПК).

Источник сервиса (asr/tts/llm) — одно из двух, выбранное в settings["voice_stack"][service]:

* пресет  "p:<id>" — state/voice_stack/presets/<service>/<id>.json:
      {"id", "service", "title", "adapter": "<id адаптера>", "values": {поля адаптера}}
  Адаптер — код (voice_stack/adapters/*.py или свой state/voice_stack/adapters/*.py), см. adapters/__init__.py.
* пакет   "k:<id>" — voice_stack/packages/<id>.json: локальный сервер, который ставится в свой venv
  (data/speech/<id>/venv) и запускается дочерним процессом на 127.0.0.1:<port>:

      {"id": "piper", "service": "tts", "title": "...", "description": "...", "order": 10,
       "requirements": {"gpu": false, "vram_gb": 0, "disk_gb": 0.2},
       "adapter": "openai",                                  # чем конвейер говорит с сервером
       "config": {"url": "http://127.0.0.1:{port}/v1/audio/speech", "voice": "{voice}", ...},
       "fields": [...], "defaults": {"device": "cpu", "port": 5014, ...},   # параметры запуска
       "server": {"script": "tools/builder_tts.py", "health": "/health", "env": {"TTS_THREADS": "{threads}"}},
       "platforms": {"windows": {"cpu": [{"pip": [...]}], "cuda": [{"pip": [...], "args": ["--index-url", "..."]}]},
                     "linux":   {...}},
       "notes": {"linux": "..."},                            # системные требования платформы
       "models": [{"url": ".../{voice}.tar.bz2"} | {"url": "...", "name": "x.onnx"}],
       "reinstall_on": ["device", "voice"]}

  {плейсхолдеры} — параметры запуска, {dir} — папка установки, {python} — python venv.
  Параметры запуска хранятся в state/voice_stack/packages/<id>.json: доступны до установки
  и переживают её удаление. Живой /health на порту считается «запущен» (переживает рестарт панели).

Применение пишет конфигурацию с ключом "adapter" в settings[stt|tts|model]; конвейер вызывает
адаптер оттуда (Runtime.provider / recognize_stream / speech_stream), профили устройств работают как раньше.

Расширения (TASK-031) добавляют свои слои (extensions/layers.source_layers) поверх встроенных:
voice/packages/*.json — пакеты с id «<ext-id>.<pkg>», voice/adapters/*.py — адаптеры с id
«<ext-id>.<файл>», voice/presets/*.json — пресеты с id «<ext-id>.<id>» (только чтение;
правка через save_preset с этим id создаёт пользовательскую копию). Пользовательский пресет
с тем же id перекрывает пресет расширения. Выключение расширения убирает его слои из списков.
"""
import asyncio
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout

from . import adapters as adapter_registry

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
PACKAGES = ROOT / "packages"
SERVICES = ("asr", "tts", "llm")
SETTINGS_KEY = {"asr": "stt", "tts": "tts", "llm": "model"}
FIELD_TYPES = ("text", "password", "number", "select", "textarea", "file")
WAV_LIMIT = 15 * 1024 ** 2
WAV_SECONDS = (2.0, 30.0)
KEEP = "__KEEP__"
PLATFORM = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, sys.platform)
# Ключи конфигураций конвейера (runtime.DEFAULT) — источник без поля получает пустое значение.
DEFAULT_KEYS = {
    "stt": {"url": "", "model": "", "api_key": "", "language": "ru"},
    "tts": {"url": "", "model": "", "api_key": "", "voice": "", "response_format": "opus"},
    "model": {"url": "", "model": "", "api_key": "", "temperature": 0.3, "stub_text": ""},
}


def fill(value, options):
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(options.get(m.group(1), m.group(0))), value)
    return value


def coerce(fields, values):
    """Значения формы по описанию полей: number → int/float, чужие ключи отбрасываются;
    пути полей-файлов меняют только загрузка/удаление файла (форма их не перезаписывает)."""
    result = {}
    for field in fields:
        if field["key"] not in values or field.get("type") == "file":
            continue
        value = values[field["key"]]
        if field.get("type") == "number" and value not in ("", None):
            number = float(value)
            value = int(number) if field.get("step") == 1 or (number.is_integer() and field.get("step") is None) else number
        result[field["key"]] = value
    return result


_gpus = (0.0, [])


def gpus():
    """Видеокарты NVIDIA: [{"value": "0", "label": "0 · RTX 4070 SUPER · 12 ГБ"}] (кеш 60 с)."""
    global _gpus
    if time.monotonic() - _gpus[0] < 60:
        return _gpus[1]
    found = []
    try:
        done = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in done.stdout.splitlines():
            index, name, memory = [part.strip() for part in line.split(",")]
            found.append({"value": index, "label": f"{index} · {name.replace('NVIDIA ', '')} · {round(int(memory) / 1024)} ГБ"})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _gpus = (time.monotonic(), found)
    return found


def wav_info(data):
    """Проверка WAV (RIFF/WAVE, PCM или float, 1–2 канала) → {seconds, rate, channels, bits}; иначе ValueError."""
    import struct
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Нужен файл WAV (RIFF/WAVE)")
    position, fmt, size = 12, None, None
    while position + 8 <= len(data):
        chunk, length = data[position:position + 4], struct.unpack("<I", data[position + 4:position + 8])[0]
        body = data[position + 8:position + 8 + length]
        if chunk == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk == b"data":
            size = min(length, len(data) - position - 8)
            break
        position += 8 + length + (length & 1)
    if fmt is None or size is None:
        raise ValueError("WAV без fmt/data")
    tag, channels, rate, _, align, bits = fmt
    if tag not in (1, 3, 0xFFFE):
        raise ValueError("WAV: поддерживается только PCM или float, без сжатия")
    if channels not in (1, 2) or not 8000 <= rate <= 96000 or not align:
        raise ValueError("WAV: 1–2 канала, 8–96 кГц")
    seconds = size / align / rate
    if not WAV_SECONDS[0] <= seconds <= WAV_SECONDS[1]:
        raise ValueError(f"Длительность эталона {seconds:.1f} с — нужно {WAV_SECONDS[0]:.0f}–{WAV_SECONDS[1]:.0f} с")
    return {"seconds": round(seconds, 1), "rate": rate, "channels": channels, "bits": bits}


def validate_fields(fields):
    for field in fields:
        if field.get("type", "text") not in FIELD_TYPES or not field.get("key"):
            raise ValueError("fields: key + type " + "|".join(FIELD_TYPES))


# --- пакеты --------------------------------------------------------------------------
def load_packages(directory=PACKAGES):
    """{service: [пакет, ...]} по order; битые файлы пропускаются с предупреждением."""
    result = {s: [] for s in SERVICES}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            validate_package(item)
        except (OSError, ValueError) as exc:
            log.warning("Пакет голосового стека %s пропущен: %s", path, exc)
            continue
        result[item["service"]].append(item)
    for items in result.values():
        items.sort(key=lambda p: (p.get("order", 100), p["id"]))
    return result


def validate_package(item):
    if not isinstance(item, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", str(item.get("id", ""))):
        raise ValueError("id: латиница/цифры/_-")
    if item.get("service") not in SERVICES:
        raise ValueError("service: asr|tts|llm")
    if not (item.get("adapter") and isinstance(item.get("config"), dict)):
        raise ValueError("adapter + config")
    if not (item.get("server", {}).get("script") and "port" in item.get("defaults", {})):
        raise ValueError("server.script + defaults.port")
    if not isinstance(item.get("platforms"), dict):
        raise ValueError("platforms: {windows|linux: {cpu|cuda: [шаги]}}")
    validate_fields(item.get("fields", []))


def load_extension_packages(origin, directory):
    """Пакеты одного расширения (voice/packages/*.json): id с префиксом «<ext-id>.»
    (валидация — по id в файле, расширение не занимает чужие id), метка _origin —
    слой. Битые файлы пропускаются с предупреждением."""
    ext_id = str(origin).split(":", 1)[-1]
    result = {service: [] for service in SERVICES}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            validate_package(item)
        except (OSError, ValueError) as exc:
            log.warning("Пакет голосового стека %s пропущен: %s", path, exc)
            continue
        result[item["service"]].append({**item, "id": f"{ext_id}.{item['id']}", "_origin": origin})
    return result


class PcmTTS:
    """Движок PCM-синтеза из текущих settings["tts"] для звукового кеша (tts_cache).
    Скалярные атрибуты — отпечаток кеша: другая модель/голос/URL — другие файлы."""

    def __init__(self, runtime, adapter_id, adapter, config):
        self._runtime, self._adapter, self._config = runtime, adapter, config
        self.adapter, self.url, self.model = adapter_id, config.get("url", ""), config.get("model", "")
        self.voice, self.response_format = config.get("voice", ""), config.get("response_format", "")

    async def synth_pcm(self, text):
        return await self._adapter.synth_pcm(self._runtime, self._config, text)

    def label(self):
        return " · ".join(x for x in (self.model or self.adapter, self.voice) if x)


def pcm_tts(runtime):
    """PcmTTS для текущих настроек синтеза или None (адаптер не отдаёт PCM — кеш не используется)."""
    config = copy.deepcopy(runtime.settings.get("tts") or {})
    try:
        adapter = adapter_registry.resolve("tts", config)
    except ValueError:
        return None
    if not hasattr(adapter, "synth_pcm") or not getattr(adapter, "streams", lambda c: False)(config):
        return None
    return PcmTTS(runtime, config.get("adapter") or "openai", adapter, config)


class LocalSpeech:
    """Установка пакетов (venv + pip + модели) и процессы их серверов."""

    def __init__(self, directory, options_dir):
        self.directory = Path(directory)
        self.options_dir = Path(options_dir)
        self.jobs = {}       # id → {"stage", "progress", "error"}
        self.processes = {}  # id → Popen
        self.dirty = set()   # id пакетов с новым файлом-параметром: перезапуск при сохранении настроек
        self._tasks = set()

    def folder(self, package):
        return self.directory / package["id"]

    def python(self, package):
        venv = self.folder(package) / "venv"
        return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"

    @staticmethod
    def devices(package):
        return list(package["platforms"].get(PLATFORM, {}))

    # --- параметры запуска -------------------------------------------------------
    def options(self, package):
        saved = {}
        try:
            saved = json.loads((self.options_dir / (package["id"] + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        options = {**package["defaults"], **(saved if isinstance(saved, dict) else {})}
        devices = self.devices(package)
        if "device" in options and devices and options["device"] not in devices:
            options["device"] = devices[0]
        return options

    def save_options(self, package, values):
        options = {**self.options(package), **coerce(package.get("fields", []), values)}
        if not 1024 <= int(options.get("port") or 0) <= 65535:
            raise ValueError("Порт: 1024–65535")
        if "device" in options and options["device"] not in self.devices(package):
            raise ValueError(f"Устройство {options['device']} не поддерживается пакетом на {PLATFORM}")
        self.options_dir.mkdir(parents=True, exist_ok=True)
        (self.options_dir / (package["id"] + ".json")).write_text(
            json.dumps(options, ensure_ascii=False, indent=2), encoding="utf-8")
        return options

    # --- файлы параметров (эталон голоса): один файл на поле пакета ----------------------
    @staticmethod
    def _file_field(package, key):
        field = next((f for f in package.get("fields", []) if f["key"] == key and f.get("type") == "file"), None)
        if field is None:
            raise ValueError("У пакета нет поля-файла " + key)
        return field

    def file_path(self, package, key):
        self._file_field(package, key)
        return self.options_dir / package["id"] / (key + ".wav")

    def upload(self, package, key, data):
        self._file_field(package, key)
        if len(data) > WAV_LIMIT:
            raise ValueError("Файл больше 15 МБ")
        info = wav_info(data)
        target = self.file_path(package, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        self._set_file(package, key, str(target))
        return info

    def _set_file(self, package, key, value):
        options = {**self.options(package), key: value}
        self.options_dir.mkdir(parents=True, exist_ok=True)
        (self.options_dir / (package["id"] + ".json")).write_text(
            json.dumps(options, ensure_ascii=False, indent=2), encoding="utf-8")
        self.dirty.add(package["id"])

    def remove_file(self, package, key):
        path = self.file_path(package, key)
        path.unlink(missing_ok=True)
        for baked in path.parent.glob(key + ".*.pt"):  # запечённый сервером голос этого эталона
            baked.unlink(missing_ok=True)
        self._set_file(package, key, "")

    def files(self, package):
        """{key: {name, seconds, ...}} для полей-файлов с загруженным файлом."""
        result, options = {}, self.options(package)
        for field in package.get("fields", []):
            if field.get("type") != "file" or not options.get(field["key"]):
                continue
            path = Path(options[field["key"]])
            try:
                result[field["key"]] = {"name": path.name, **wav_info(path.read_bytes())}
            except (OSError, ValueError) as exc:
                result[field["key"]] = {"name": path.name, "error": str(exc)}
        return result

    def marker(self, package):
        try:
            return json.loads((self.folder(package) / "installed.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def installed(self, package):
        return self.marker(package) is not None

    def outdated(self, package):
        """Параметры, требующие переустановки (устройство, голос), изменились после установки."""
        marker, options = self.marker(package) or {}, self.options(package)
        covers = {"cuda": "cpu"}  # сборка под видеокарту работает и на процессоре
        return [k for k in package.get("reinstall_on", []) if k in marker and marker[k] != options.get(k)
                and not (k == "device" and covers.get(marker[k]) == options.get(k))]

    async def health(self, package):
        """JSON /health работающего сервера пакета или None (не запущен)."""
        url = f"http://127.0.0.1:{self.options(package)['port']}{package['server'].get('health', '/health')}"
        try:
            async with ClientSession(timeout=ClientTimeout(total=1.5)) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    data = await response.json(content_type=None)
                    return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — нет сервера = не запущен
            return None

    async def healthy(self, package):
        return await self.health(package) is not None

    async def state(self, package):
        job = self.jobs.get(package["id"])
        if job and job["stage"] not in ("done", "error"):
            return {"status": "installing", **job}
        if not self.devices(package):
            return {"status": "unsupported"}
        if not self.installed(package):
            return {"status": "error", **job} if job and job["stage"] == "error" else {"status": "absent"}
        extra = {"outdated": self.outdated(package)}
        health = await self.health(package)
        if health is not None:
            return {"status": "running", **extra, **({"voice": health["voice"]} if health.get("voice") else {})}
        process = self.processes.get(package["id"])
        if process is not None and process.poll() is None:
            return {"status": "starting", "log": self._tail(package), **extra}
        return {"status": "stopped", "log": self._tail(package), **extra}

    def _tail(self, package):
        try:
            return (self.folder(package) / "server.log").read_text(encoding="utf-8", errors="replace")[-600:]
        except OSError:
            return ""

    # --- установка ---------------------------------------------------------------
    def install(self, package):
        if not self.devices(package):
            raise ValueError(f"Пакет не поддерживает {PLATFORM}")
        job = self.jobs.get(package["id"])
        if job and job["stage"] not in ("done", "error"):
            raise ValueError("Установка уже идёт")
        self.jobs[package["id"]] = {"stage": "Подготовка", "progress": 0.0, "error": None}
        task = asyncio.get_running_loop().create_task(self._install(package))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _install(self, package):
        job = self.jobs[package["id"]]
        try:
            self.stop(package)
            await asyncio.to_thread(self._install_sync, package, job)
            job.update(stage="done", progress=1.0)
            await self.start(package)
        except Exception as exc:  # noqa: BLE001 — причина видна в карточке
            log.warning("Установка %s не удалась: %s", package["id"], exc)
            job.update(stage="error", error=str(exc)[-600:])

    def _run(self, command, job, stage):
        job.update(stage=stage)
        done = subprocess.run(command, capture_output=True, text=True, timeout=3600,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode:
            raise RuntimeError(stage + ": " + (done.stderr or done.stdout).strip()[-500:])

    def _install_sync(self, package, job):
        folder, options = self.folder(package), self.options(package)
        steps = package["platforms"][PLATFORM][options.get("device", "cpu")]
        (folder / "installed.json").unlink(missing_ok=True)
        folder.mkdir(parents=True, exist_ok=True)
        if not self.python(package).exists():
            self._run([sys.executable, "-m", "venv", str(folder / "venv")], job, "Создание окружения Python")
        python = str(self.python(package))
        for index, step in enumerate(steps):
            job.update(progress=round(0.05 + 0.45 * index / max(1, len(steps)), 3))
            if step.get("pip"):
                packages = [fill(p, options) for p in step["pip"]]
                self._run([python, "-m", "pip", "install", "--disable-pip-version-check",
                           *[fill(a, options) for a in step.get("args", [])], *packages],
                          job, "Установка пакетов: " + ", ".join(packages))
            elif step.get("run"):
                self._run([fill(a, {**options, "python": python, "dir": str(folder)}) for a in step["run"]],
                          job, step.get("title") or "Подготовка")
        models = [{k: fill(v, options) for k, v in m.items()} for m in package.get("models", [])]
        for index, model in enumerate(models):
            name = model.get("name") or model["url"].rsplit("/", 1)[-1]
            archive = name.endswith((".tar.bz2", ".tar.gz"))
            if (folder / name.split(".tar.")[0]).is_dir() if archive else (folder / name).exists():
                continue  # уже скачано (переустановка с теми же моделями)
            self._download(model["url"], folder / name, job, name, (index, len(models)))
            if archive:
                job.update(stage="Распаковка " + name)
                with tarfile.open(folder / name) as source:
                    source.extractall(folder, filter="data")
                (folder / name).unlink()
        marker = {k: options.get(k) for k in package.get("reinstall_on", [])}
        (folder / "installed.json").write_text(json.dumps(
            {**marker, "platform": PLATFORM, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")

    @staticmethod
    def _download(url, target, job, name, share):
        index, total = share
        temporary = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=60) as response, open(temporary, "wb") as out:
            size = int(response.headers.get("Content-Length") or 0)
            got = 0
            while chunk := response.read(1 << 20):
                out.write(chunk)
                got += len(chunk)
                job.update(stage=f"Скачивание {name} ({got >> 20} МБ)",
                           progress=round(0.5 + 0.45 * (index + (got / size if size else 0)) / total, 3))
        os.replace(temporary, target)

    def uninstall(self, package):
        """Удаляются venv, модели и лог; параметры запуска остаются."""
        self.stop(package)
        self.jobs.pop(package["id"], None)
        shutil.rmtree(self.folder(package), ignore_errors=True)

    # --- процесс -----------------------------------------------------------------
    async def start(self, package):
        if not self.installed(package) or await self.healthy(package):
            return
        process = self.processes.get(package["id"])
        if process is not None and process.poll() is None:
            return
        folder = self.folder(package)
        options = {**self.options(package), "dir": str(folder), "python": str(self.python(package))}
        env = dict(os.environ, HOST="127.0.0.1", PORT=str(options["port"]), PYTHONIOENCODING="utf-8")
        env.update({k: fill(v, options) for k, v in package["server"].get("env", {}).items()})
        flags, extra = getattr(subprocess, "CREATE_NO_WINDOW", 0), {}
        if options.get("priority") == "low":  # не мешать остальной системе: фоновый приоритет процесса
            if os.name == "nt":
                flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
            else:
                extra["preexec_fn"] = lambda: os.nice(10)
        with open(folder / "server.log", "w", encoding="utf-8") as output:
            self.processes[package["id"]] = subprocess.Popen(
                [str(self.python(package)), str(PROJECT / package["server"]["script"])], cwd=str(folder), env=env,
                stdout=output, stderr=subprocess.STDOUT, creationflags=flags, **extra)
        (folder / "server.pid").write_text(str(self.processes[package["id"]].pid), encoding="ascii")
        log.info("Речевой сервер %s запущен на порту %s", package["id"], options["port"])

    def stop(self, package):
        """Остановить сервер, в том числе запущенный прошлым экземпляром панели (по server.pid)."""
        self.stop_key(package["id"])
        pid_file = self.folder(package) / "server.pid"
        try:
            pid = int(pid_file.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return
        pid_file.unlink(missing_ok=True)
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, 15)
        except (OSError, subprocess.SubprocessError):
            pass

    def stop_key(self, identifier):
        process = self.processes.pop(identifier, None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()


class VoiceStack:
    def __init__(self, runtime, packages_dir=PACKAGES, state_dir=None, speech_dir=None):
        self.runtime = runtime
        self.packages_dir = packages_dir
        self.state_dir = Path(state_dir or Path(runtime.directory) / "voice_stack")
        self.local = LocalSpeech(speech_dir or PROJECT / "data" / "speech", self.state_dir / "packages")
        adapter_registry.configure(self.state_dir / "adapters",
                                   extension_dirs=lambda: self._extension_layers("voice/adapters"))

    def _extension_layers(self, name):
        """Слои точки подключения name («voice/packages|adapters|presets») включённых
        расширений: [(origin, Path)] — общий помощник extensions/layers.source_layers;
        runtime без менеджера расширений (тесты) — слой пуст."""
        from ..extensions.layers import source_layers
        return source_layers(getattr(self.runtime, "extensions", None), name)

    # --- справочники -------------------------------------------------------------
    def packages(self):
        """Встроенные пакеты + пакеты включённых расширений («<ext-id>.<pkg>», _origin);
        файлы маленькие: правка JSON видна без рестарта."""
        result = load_packages(self.packages_dir)
        for origin, path in self._extension_layers("voice/packages"):
            for service, items in load_extension_packages(origin, path).items():
                result[service].extend(items)
        for items in result.values():
            items.sort(key=lambda p: (p.get("order", 100), p["id"]))
        return result

    def package(self, identifier):
        for items in self.packages().values():
            for package in items:
                if package["id"] == identifier:
                    return package
        raise ValueError(f"Пакет «{identifier}» не найден")

    @staticmethod
    def adapter(service, identifier):
        if service not in SERVICES:
            raise ValueError("service: asr|tts|llm")
        module = adapter_registry.get(identifier)
        if service not in module.SERVICES:
            raise ValueError(f"Адаптер «{identifier}» не умеет {service}")
        return module

    # --- пресеты -----------------------------------------------------------------
    def presets(self, service):
        """Свои пресеты (state/voice_stack/presets) + пресеты включённых расширений
        (voice/presets/*.json, id «<ext-id>.<id>», только чтение — метки origin/readonly).
        Пользовательский пресет с тем же id перекрывает пресет расширения («Сделать
        копию» — save_preset с этим id, правка уходит в state-копию)."""
        result = {}
        for path in sorted((self.state_dir / "presets" / service).glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(item, dict) and item.get("id") == path.stem and item.get("adapter"):
                result[item["id"]] = item
        for origin, path in self._extension_layers("voice/presets"):
            ext_id = str(origin).split(":", 1)[-1]
            for file in sorted(Path(path).glob("*.json")):
                try:
                    item = json.loads(file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not (isinstance(item, dict) and item.get("id") == file.stem and item.get("adapter")):
                    continue
                identifier = f"{ext_id}.{item['id']}"
                result.setdefault(identifier, {**item, "id": identifier, "origin": origin, "readonly": True})
        return sorted(result.values(), key=lambda p: p.get("title", "").lower())

    def preset(self, service, identifier):
        found = next((p for p in self.presets(service) if p["id"] == identifier), None)
        if found is None:
            raise ValueError("Пресет не найден")
        return found

    def save_preset(self, service, adapter_id, title, values, identifier=None):
        """Создать (identifier=None) или обновить пресет; API-ключ KEEP — прежнее значение."""
        module = self.adapter(service, adapter_id)
        title = str(title or "").strip()[:80]
        if not title:
            raise ValueError("Название пресета обязательно")
        previous = self.preset(service, identifier) if identifier else None
        values = coerce(module.SERVICES[service].get("fields", []), values)
        for key, value in list(values.items()):
            if value == KEEP:
                values[key] = (previous or {}).get("values", {}).get(key, "")
        if identifier is None:
            base = re.sub(r"[^a-z0-9]+", "-", adapter_id.lower()).strip("-") or "preset"
            existing = {p["id"] for p in self.presets(service)}
            identifier, number = base, 2
            while identifier in existing:
                identifier, number = f"{base}-{number}", number + 1
        item = {"id": identifier, "service": service, "title": title, "adapter": adapter_id, "values": values}
        folder = self.state_dir / "presets" / service
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (identifier + ".json")).write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        return item

    def delete_preset(self, service, identifier):
        self.preset(service, identifier)
        if self.active(service) == "p:" + identifier:
            raise ValueError("Пресет применён — сначала примените другой")
        file = self.state_dir / "presets" / service / (identifier + ".json")
        if not file.is_file():
            raise ValueError("Пресет расширения — только чтение; сделайте копию")
        file.unlink()

    # --- активный источник -------------------------------------------------------
    def active(self, service):
        """Применённый источник; ссылка на удалённый пресет/пакет считается пустой
        (в том числе пресет выключившегося расширения — см. unavailable_source)."""
        ref = str((self.runtime.settings.get("voice_stack") or {}).get(service, ""))
        kind, _, identifier = ref.partition(":")
        if kind == "p" and any(p["id"] == identifier for p in self.presets(service)):
            return ref
        if kind == "k" and any(p["id"] == identifier for p in self.packages()[service]):
            return ref
        return ""

    def unavailable_source(self, service):
        """Применённый источник, которого больше нет в списках (например, пресет или
        пакет выключившегося/удалённого расширения): понятный текст для карточки."""
        ref = str((self.runtime.settings.get("voice_stack") or {}).get(service, ""))
        if not ref or self.active(service) or ref.partition(":")[0] not in ("p", "k"):
            return None
        return (f"Применённый источник «{ref}» недоступен — выберите другой "
                "(расширение могло быть выключено или удалено)")

    def config(self, service, ref):
        """ref → (конфигурация settings[stt|tts|model], пакет | None)."""
        kind, _, identifier = ref.partition(":")
        name = SETTINGS_KEY[service]
        if kind == "p":
            preset = self.preset(service, identifier)
            module = self.adapter(service, preset["adapter"])
            spec = module.SERVICES[service]
            config = {**copy.deepcopy(spec.get("defaults", {})), **preset.get("values", {}), "adapter": preset["adapter"]}
            package = None
        elif kind == "k":
            package = self.package(identifier)
            if package["service"] != service:
                raise ValueError("Пакет другого сервиса")
            if not self.local.installed(package):
                raise ValueError("Сначала установите пакет")
            options = self.local.options(package)
            config = {k: fill(v, options) for k, v in package["config"].items()}
            config["adapter"] = package["adapter"]
        else:
            raise ValueError("Источник: p:<пресет> или k:<пакет>")
        return {**DEFAULT_KEYS[name], **config}, package

    async def apply(self, service, ref):
        if service not in SERVICES:
            raise ValueError("service: asr|tts|llm")
        config, package = self.config(service, ref)
        settings = copy.deepcopy(self.runtime.settings)
        settings[SETTINGS_KEY[service]] = config
        settings["voice_stack"] = {**(settings.get("voice_stack") or {}), service: ref}
        await self.runtime.save_settings(settings, preserve_mode=True)
        if package:
            await self.local.start(package)
        return {"service": service, "active": ref}

    async def migrate(self):
        """Настройки без выбранного источника → пресет «Текущие настройки» (ничего не
        теряется). Источник с записанной ссылкой не трогается, даже если она висящая
        (пресет выключившегося расширения — ошибка видна в карточке); сбой одного
        сервиса (например, битый адаптер) пропускается с предупреждением — карточка
        голосового стека не падает."""
        for service in SERVICES:
            if (self.runtime.settings.get("voice_stack") or {}).get(service) or self.active(service):
                continue
            try:
                await self._migrate_service(service)
            except Exception as exc:  # noqa: BLE001 — миграция не должна валить карточку
                log.warning("Миграция настроек %s пропущена: %s", service, exc)

    async def _migrate_service(self, service):
        name = SETTINGS_KEY[service]
        current = self.runtime.settings.get(name) or {}
        if not (current.get("url") or current.get("stub_text")):
            return
        adapter_id = "stub" if current.get("stub_text") else adapter_registry.resolve(name, current).__name__.rsplit(".", 1)[-1]
        fields = self.adapter(service, adapter_id).SERVICES[service].get("fields", [])
        preset = self.save_preset(service, adapter_id, "Текущие настройки",
                                  {f["key"]: current[f["key"]] for f in fields if f["key"] in current})
        settings = copy.deepcopy(self.runtime.settings)
        settings[name] = {**current, "adapter": adapter_id}
        settings["voice_stack"] = {**(settings.get("voice_stack") or {}), service: "p:" + preset["id"]}
        await self.runtime.save_settings(settings, preserve_mode=True)

    async def public(self):
        await self.migrate()
        packages, adapters, result = self.packages(), adapter_registry.available(), {}
        for service in SERVICES:
            presets = [{**p, "values": {k: (KEEP if k == "api_key" and v else v) for k, v in p.get("values", {}).items()}}
                       for p in self.presets(service)]
            items = []
            for package in packages[service]:
                devices = self.local.devices(package)
                fields = [dict(f, options=[o for o in f.get("options", []) if (o["value"] if isinstance(o, dict) else o) in devices])
                          if f["key"] == "device" else dict(f, options=gpus()) if f.get("options_from") == "gpus" else f
                          for f in package.get("fields", [])]
                items.append({k: package[k] for k in ("id", "title", "description", "requirements") if k in package}
                             | {"fields": fields, "values": self.local.options(package), "files": self.local.files(package), "platform": PLATFORM,
                                "note": package.get("notes", {}).get(PLATFORM, ""), "state": await self.local.state(package)}
                             | ({"origin": package["_origin"]} if package.get("_origin") else {}))
            kinds = [{"id": key, "title": getattr(module, "TITLE", key), "description": getattr(module, "DESCRIPTION", ""),
                      **{k: module.SERVICES[service].get(k, d) for k, d in (("fields", []), ("defaults", {}), ("examples", {}))}}
                     for key, module in sorted(adapters.items(), key=lambda kv: getattr(kv[1], "TITLE", kv[0]))
                     if service in module.SERVICES]
            entry = {"presets": presets, "packages": items, "adapters": kinds, "active": self.active(service)}
            unavailable = self.unavailable_source(service)
            if unavailable:
                entry["warning"] = unavailable  # понятная ошибка в карточке, не падение
            result[service] = entry
        return result

    async def autostart(self):
        """Поднять серверы применённых пакетов (старт панели)."""
        for service in SERVICES:
            kind, _, identifier = self.active(service).partition(":")
            if kind != "k":
                continue
            try:
                await self.local.start(self.package(identifier))
            except (OSError, ValueError) as exc:
                log.warning("Не удалось запустить %s: %s", identifier, exc)
