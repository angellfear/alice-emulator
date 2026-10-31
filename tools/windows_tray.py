"""Трей-лаунчер эмулятора для Windows (TASK-045); код сервера от этого модуля не зависит.

Запускает `python -m server_emulator.server` без окна (настройки — state/server.json), следит за ним:
упал — перезапуск через паузу, код RESTART_CODE — сразу; штатная остановка — `/__control/stop`
(ключ из state/control.token), зависла — taskkill. Один экземпляр на папку (mutex). Логи сервера —
state/logs/server.log с ротацией. Решения перенесены из трея внешнего прокси (supervisor не умирает,
эскалация зависшей остановки, GUI не грузится при импорте сервера).
"""
import json
import os
from pathlib import Path
import queue
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server_emulator import control, server_config  # noqa: E402  (лёгкие модули, без aiohttp-приложения)

STATE = ROOT / "server_emulator" / "state"
ICON = ROOT / "tools" / "assets" / "alica.ico"  # логотип трея и ярлыков
LOG_LIMIT = 5 * 1024 ** 2
LOG_KEEP = 3
STOP_TIMEOUT = 45
AUTOSTART = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Алиса — эмулятор.lnk"


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def python_executable():
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    return venv if venv.is_file() else Path(sys.executable).with_name("python.exe")


def rotate(path, limit=LOG_LIMIT, keep=LOG_KEEP):
    """server.log → server.log.1 … .keep, если файл больше limit."""
    try:
        if path.stat().st_size <= limit:
            return
    except OSError:
        return
    for index in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
    os.replace(path, path.with_name(path.name + ".1"))


class Server:
    """Связь с сервером по адресу из server.json и ключу управления."""

    def __init__(self, state):
        self.state = Path(state)

    def url(self):
        return server_config.local_url(server_config.load(self.state))

    def panel(self):
        return self.url() + "/emulator/"

    def _request(self, path, method="GET", timeout=3):
        request = urllib.request.Request(self.url() + path, method=method, data=b"{}" if method == "POST" else None,
                                         headers={control.HEADER: control.read_token(self.state)})
        context = ssl._create_unverified_context()  # свой сервер на 127.0.0.1, сертификат для колонок
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.load(response)

    def status(self):
        try:
            value = self._request("/__control/status")
            return value if isinstance(value, dict) and "pid" in value else None
        except Exception:
            return None

    def stop(self):
        self._request("/__control/stop", method="POST", timeout=4)


class Controller:
    def __init__(self, root=ROOT, state=STATE, server=None, on_change=lambda: None, on_exit=lambda: None,
                 open_panel=False, opener=webbrowser.open):
        self.root, self.state = Path(root).resolve(), Path(state).resolve()
        self.logs = self.state / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.server = server or Server(self.state)
        self.commands = queue.Queue()
        self.on_change, self.on_exit, self.opener = on_change, on_exit, opener
        self.desired, self.exiting, self.stopping, self.stop_error, self.ready = True, False, False, False, False
        self.updating, self.update_deadline = False, 0  # TASK-040: ждём план state/update/plan.json
        self.state_text = "Запуск сервера…"
        self.child, self.pid, self.retry_at, self.stop_deadline, self.value_missing_since = None, None, 0, 0, 0
        self.open_panel = open_panel

    def request(self, command):
        self.commands.put(command)

    def note(self, message):
        with (self.logs / "launcher.log").open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n")

    def own(self, value):
        return isinstance(value.get("state"), str) and Path(value["state"]).resolve() == self.state

    def stop_server(self, value):
        if self.stopping:
            return
        if not self.own(value):
            self.stop_error, self.state_text = True, "На порту другой сервер"
            self.note("Refused to stop a server with another data folder")
            return
        try:
            self.server.stop()
            self.stopping, self.stop_deadline = True, time.monotonic() + STOP_TIMEOUT
            self.state_text = "Остановка…"
        except Exception as exc:
            self.stop_error, self.state_text = True, "Не удалось остановить сервер"
            self.note("Stop request failed: " + type(exc).__name__)

    def kill_server(self, pid):
        """Принудительное завершение зависшего сервера."""
        if not pid or not process_alive(pid):
            return
        self.note("Force-killing server PID " + str(pid))
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, check=False)

    def supervise_stuck(self, now):
        if self.stopping and self.stop_deadline and now > self.stop_deadline:
            self.kill_server(self.pid)
            self.stopping, self.stop_deadline, self.pid = False, 0, None
            self.state_text = "Сервер остановлен принудительно"
        if self.value_missing_since and now - self.value_missing_since > 45 and self.child is None:
            self.note("Status timeout with pid " + str(self.pid) + "; treating server as dead")
            self.pid, self.value_missing_since = None, 0

    def update_plan(self):
        """План самообновления state/update/plan.json (TASK-040): None — ещё не готов или битый."""
        try:
            return json.loads((self.state / "update" / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def after_update(self, status):
        """Обновление завершилось: сервер — запустить, трей — перезапустить и выйти; mutex
        освобождается после выхода старого — новый стартует с ожиданием (ALICA_TRAY_TAKEOVER)."""
        self.note("Update " + status + "; relaunching server and tray")
        self.updating, self.update_deadline = False, 0
        self.launch()
        exe = python_executable()
        if os.name == "nt":
            exe = exe.with_name("pythonw.exe")
        subprocess.Popen([str(exe), str(self.root / "tools" / "start_emulator.pyw")], cwd=str(self.root),
                         stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         env={**os.environ, "ALICA_TRAY_TAKEOVER": "1"})
        self.exiting = True
        self.on_exit()

    def launch(self):
        log = self.logs / "server.log"
        rotate(log)
        with log.open("ab") as out:
            self.child = subprocess.Popen(
                [str(python_executable()), "-m", "server_emulator.server", "--state", str(self.state)],
                cwd=self.root, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "ALICA_LAUNCHER": "1"})
        self.note("Started server PID " + str(self.child.pid))

    def run(self):
        while True:
            try:
                self.iterate()
            except Exception as exc:  # supervisor не имеет права умирать: иначе иконка «жива», меню мертво
                try:
                    self.note("Supervisor iteration failed: " + type(exc).__name__ + ": " + str(exc)[:120])
                except Exception:
                    pass
            time.sleep(1)

    def iterate(self):
        while not self.commands.empty():
            command = self.commands.get_nowait()
            self.stop_error = False
            if command == "start":
                self.desired, self.retry_at = True, 0
            elif command == "stop":
                self.desired = False
            elif command == "restart":
                self.desired, self.stopping = True, False
                value = self.server.status()
                if value:
                    if self.own(value):
                        self.pid = value.get("pid")
                    self.stop_server(value)
                    if self.stop_error and self.pid:
                        self.kill_server(self.pid)
                        self.pid, self.stop_error = None, False
                self.retry_at = 0
            elif command == "exit":
                self.desired, self.exiting, self.stop_deadline = False, True, time.monotonic() + 30
        now = time.monotonic()
        self.supervise_stuck(now)   # до опроса: после принудительной остановки не слать ей stop ещё раз
        value = self.server.status()
        self.value_missing_since = (self.value_missing_since or now) if value is None else 0
        self.ready = bool(value and self.own(value))
        if self.ready:
            self.pid = value.get("pid")
        alive = (self.child is not None and self.child.poll() is None) or (self.child is None and process_alive(self.pid))
        if value:
            if not self.desired and not self.stop_error:
                self.stop_server(value)
            if not self.stopping and not self.stop_error:
                self.state_text = "Сервер работает" if self.own(value) else "Работает другой сервер"
            if self.open_panel and self.ready:
                self.opener(self.server.panel())
                self.open_panel = False
        elif alive:
            self.state_text = "Остановка…" if self.stopping else "Сервер запускается…"
        else:
            self.pid = None
            if self.child is not None:
                code, self.child = self.child.returncode, None
                if code == server_config.RESTART_CODE:
                    self.retry_at = 0
                    self.note("Server asked for restart")
                elif code == server_config.UPDATE_CODE:  # TASK-040: сервер ждёт updater, а не перезапуск
                    self.updating, self.update_deadline = True, time.monotonic() + 900
                    self.state_text = "Обновление…"
                    self.note("Server exited for self-update; waiting for plan")
                elif not self.stopping:
                    self.retry_at = time.monotonic() + 10
                    self.note("Server exited: " + str(code))
                    self.state_text = "Сервер завершился. Повторный запуск…"
            self.stopping = False
            if self.exiting:
                self.on_exit()
                return
            if self.updating:  # план пишется updater'ом; таймаут — updater умер, поднимаем сервер
                plan = self.update_plan()
                if (plan and plan.get("status") in ("ok", "failed")) or time.monotonic() > self.update_deadline:
                    self.after_update((plan or {}).get("status") or "timeout")
                    return
                self.safe_on_change()
                return
            if self.desired and time.monotonic() >= self.retry_at:
                try:
                    self.launch()
                    self.state_text = "Сервер запускается…"
                except OSError as exc:
                    self.state_text = "Ошибка запуска"
                    self.note(type(exc).__name__ + ": " + str(exc))
                    self.retry_at = time.monotonic() + 10
            elif not self.desired:
                self.state_text = "Сервер остановлен"
        self.safe_on_change()

    def safe_on_change(self):
        try:
            self.on_change()
        except Exception:
            pass  # сбой отрисовки иконки не должен убивать supervisor


def mutex_name(root=ROOT):
    import hashlib
    return "Local\\AliceEmule-" + hashlib.sha256(str(Path(root).resolve()).lower().encode()).hexdigest()[:20]


def single_instance(root=ROOT):
    """Handle mutex или None, если трей уже запущен для этой папки."""
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, mutex_name(root))
    if not handle or ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        if handle:
            kernel.CloseHandle(handle)
        return None
    return handle


def picture(color):
    """Иконка трея: логотип (tools/assets/alica.ico) с точкой цвета состояния в правом нижнем углу."""
    from PIL import Image, ImageDraw
    image = Image.open(ICON).convert("RGBA").resize((64, 64), Image.LANCZOS)
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 40, 63, 63), fill=color, outline="#0b0f14", width=3)
    return image


COLORS = {"Сервер работает": "#2f9e44"}


def write_icon(path):
    """Иконка ярлыков (автозапуск, рабочий стол) — логотип без точки состояния."""
    import shutil
    shutil.copyfile(ICON, path)
    return path


def autostart_enabled():
    return AUTOSTART.is_file()


def set_autostart(enabled):
    script = ROOT / "tools" / "install_autostart.ps1"
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    subprocess.run(args + ([] if enabled else ["-Remove"]), capture_output=True, check=False,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run_tray(open_panel=False):
    if os.name != "nt":
        raise RuntimeError("Трей — только Windows; на Linux: python -m server_emulator.server")
    import pystray
    write_icon(STATE / "alica.ico")
    icon = pystray.Icon("alica-emule", picture("#e8a33d"), "Алиса — эмулятор · запуск…")
    controller = Controller(on_change=lambda: update(), on_exit=icon.stop, open_panel=open_panel)
    shown = {"color": None, "menu": None}

    def update():
        color = COLORS.get(controller.state_text, "#c92a2a" if "Ошибка" in controller.state_text
                           or "завершился" in controller.state_text else "#e8a33d")
        if color != shown["color"]:
            icon.icon, shown["color"] = picture(color), color
        icon.title = ("Алиса — эмулятор · " + controller.state_text + " · " + controller.server.url())[:127]
        # pystray пересобирает меню с DestroyMenu: открытое меню закрывается и клик теряется. Поэтому
        # пересборка — только когда меняется видимое в нём, а не на каждом шаге supervisor (раз в секунду).
        menu = (controller.state_text, controller.ready, controller.stopping, controller.desired)
        if menu != shown["menu"]:
            icon.update_menu()
            shown["menu"] = menu

    def command(name):
        return lambda *args: controller.request(name)

    def open_path(path):
        return lambda *args: os.startfile(str(path))

    def toggle_autostart(*args):
        set_autostart(not autostart_enabled())
        icon.update_menu()  # галочка: меню само не пересобирается, пока не изменится состояние сервера

    icon.menu = pystray.Menu(
        pystray.MenuItem("Открыть панель", lambda *args: webbrowser.open(controller.server.panel()), default=True),
        pystray.MenuItem(lambda item: controller.state_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Перезапустить", command("restart"), enabled=lambda item: controller.ready and not controller.stopping),
        pystray.MenuItem("Остановить", command("stop"), visible=lambda item: controller.desired),
        pystray.MenuItem("Запустить", command("start"), visible=lambda item: not controller.desired),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Автозапуск", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem("Папка данных", open_path(controller.state)),
        pystray.MenuItem("Журнал сервера", open_path(controller.logs / "server.log")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", command("exit")))

    def setup(tray):
        tray.visible = True
        threading.Thread(target=controller.run, name="alica-supervisor", daemon=True).start()

    icon.run(setup=setup)


def main(open_panel=True):
    STATE.mkdir(parents=True, exist_ok=True)
    # TASK-040: перезапуск трея после обновления — старый процесс ещё держит mutex, ждём его выхода
    deadline = time.monotonic() + 20 if os.environ.get("ALICA_TRAY_TAKEOVER") else 0
    handle = single_instance()
    while handle is None and time.monotonic() < deadline:
        time.sleep(0.5)
        handle = single_instance()
    if handle is None:   # трей уже запущен — только открыть панель
        webbrowser.open(Server(STATE).panel())
        return
    try:
        run_tray(open_panel=open_panel)
    finally:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


if __name__ == "__main__":
    main()
