"""Присмотр за локальными речевыми серверами: упавший поднимается, его код выхода и перезапуск —
в журнале сервера; остановленный вручную, занятый и неустановленный не трогаются; неудачные
перезапуски наращивают паузу; старт панели ждёт готовности сервера."""
import copy
import json
import time

import pytest

from server_emulator import voice_stack
from server_emulator.voice_stack import PLATFORM, VoiceStack


class Journal:
    """Записи журнала сервера без диска: [(level, source, message, detail)]."""

    def __init__(self):
        self.items = []

    def log(self, level, source, message, **detail):
        self.items.append((level, source, message, detail))

    def messages(self):
        return [item[2] for item in self.items]


class FakeRuntime:
    def __init__(self, directory):
        self.directory = directory
        self.settings = copy.deepcopy(voice_stack.DEFAULT_KEYS)
        self.settings["voice_stack"] = {"asr": "k:eng", "speak": True}
        self.server_log = Journal()
        self.lifecycle = type("Closed", (), {"closed": False})()

    async def save_settings(self, value):
        self.settings = value


def dead(code):
    """Popen завершённого процесса: poll() и returncode — код выхода."""

    class Process:
        pid = 4321
        returncode = code

        def poll(self):
            return code
    return Process()


@pytest.fixture
def stack(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    (packages / "eng.json").write_text(json.dumps({
        "id": "eng", "title": "Движок eng", "service": "asr", "adapter": "openai",
        "config": {"url": "http://127.0.0.1:{port}/v1/x"}, "defaults": {"port": 5099},
        "server": {"script": "tools/fake_server.py", "health": "/health"},
        "platforms": {PLATFORM: {"cpu": []}}}), encoding="utf-8")
    return VoiceStack(FakeRuntime(tmp_path / "state"), packages_dir=packages, speech_dir=tmp_path / "speech")


def statuses(stack, monkeypatch, *values):
    """LocalSpeech.state() по очереди отдаёт статусы (последний — дальше всегда)."""
    queue = list(values)

    async def state(package):
        return {"status": queue.pop(0) if len(queue) > 1 else queue[0]}
    monkeypatch.setattr(stack.local, "state", state)


def starters(stack, monkeypatch, outcome="healthy", error=None):
    """Запуск сервера: сколько раз звали и что отвечает wait_ready."""
    started = []

    async def start(package):
        started.append(package["id"])
        if error:
            raise ValueError(error)
        stack.local.processes[package["id"]] = dead(None)  # процесс жив, порт ответит

    async def wait_ready(package, timeout):
        return outcome
    monkeypatch.setattr(stack.local, "start", start)
    monkeypatch.setattr(stack.local, "wait_ready", wait_ready)
    return started


async def test_dead_server_is_restarted_and_reported(stack, monkeypatch):
    """Код падения и перезапуск — в журнале сервера, счётчик неудач снят, сервер поднят."""
    stack.local.processes["eng"] = dead(1)
    statuses(stack, monkeypatch, "stopped")
    started = starters(stack, monkeypatch)

    await stack._supervise_once()

    assert started == ["eng"]
    journal = stack.runtime.server_log.items
    assert journal[0][:3] == ("warning", "server", 'Речевой сервер «Движок eng» завершился с кодом 1 — перезапускаю')
    assert journal[0][3] == {"package": "eng", "exit_code": 1}
    assert journal[1][:2] == ("info", "server") and "снова работает" in journal[1][2]
    assert stack.supervised == {}


async def test_lost_process_without_code_is_reported_once(stack, monkeypatch):
    """Сервер поднят не этой панелью (кода выхода нет): причина — «перестал отвечать», а не «код None»."""
    statuses(stack, monkeypatch, "stopped")
    started = starters(stack, monkeypatch, outcome="loading")

    await stack._supervise_once()
    await stack._supervise_once()  # грузится модель: процесс жив, повторной записи нет

    assert started == ["eng"]
    messages = stack.runtime.server_log.messages()
    assert any("перестал отвечать" in m for m in messages) and not any("с кодом" in m for m in messages)


async def test_busy_and_loading_servers_are_not_touched(stack, monkeypatch):
    """Занятый распознаванием/синтезом и грузящий модель сервер порт принимает — не трогаем."""
    started = starters(stack, monkeypatch)
    for status in ("running", "starting"):
        statuses(stack, monkeypatch, status)
        await stack._supervise_once()
    assert started == [] and stack.runtime.server_log.items == [] and stack.supervised == {}


async def test_manually_stopped_server_is_not_revived(stack, monkeypatch):
    """«Остановить» — человек, а не сбой: присмотр такой сервер не поднимает."""
    stack.local.disabled.add("eng")
    statuses(stack, monkeypatch, "stopped")
    started = starters(stack, monkeypatch)

    await stack._supervise_once()

    assert started == [] and stack.runtime.server_log.items == []


async def test_absent_and_installing_packages_are_left_to_the_card(stack, monkeypatch):
    """Пакет не установлен или ставится — причина видна в карточке пакета, перезапусков нет."""
    started = starters(stack, monkeypatch)
    for status in ("absent", "installing", "unsupported", "error"):
        statuses(stack, monkeypatch, status)
        await stack._supervise_once()
    assert started == [] and stack.runtime.server_log.items == []


async def test_repeated_failures_wait_before_next_attempt(stack, monkeypatch):
    """Сервер падает снова: пауза между попытками растёт, в журнале — причина сбоя запуска."""
    statuses(stack, monkeypatch, "stopped")
    started = starters(stack, monkeypatch, error="процесс завершился с кодом 1")

    await stack._supervise_once()
    memory = stack.supervised["eng"]
    assert started == ["eng"] and memory["attempt"] == 1 and memory["due"] > time.monotonic()
    assert any("не запустился" in m for m in stack.runtime.server_log.messages())

    await stack._supervise_once()  # раньше паузы — новой попытки
    assert started == ["eng"] and memory["attempt"] == 1

    memory["due"] = 0.0  # пауза истекла: попытка есть, и она длиннее прежней
    await stack._supervise_once()
    assert started == ["eng", "eng"] and memory["attempt"] == 2
    assert memory["due"] - time.monotonic() > voice_stack.RESTART_DELAYS[0]


async def test_apply_in_progress_skips_the_pass(stack, monkeypatch):
    """Пока применение источника останавливает и запускает серверы, присмотр не вмешивается."""
    statuses(stack, monkeypatch, "stopped")
    started = starters(stack, monkeypatch)
    await stack.lifecycle.lock.acquire()
    try:
        await stack._supervise_once()
    finally:
        stack.lifecycle.lock.release()
    assert started == [] and stack.runtime.server_log.items == []


async def test_autostart_waits_for_readiness(stack, monkeypatch):
    """Старт панели поднимает применённые пакеты и ждёт готовности каждого — первый запрос
    колонки не попадает в грузящуюся модель; долгая загрузка — строка в журнале."""
    started, timeouts = [], []

    async def start(package):
        started.append(package["id"])

    async def wait_ready(package, timeout):
        timeouts.append(timeout)
        return "loading"
    monkeypatch.setattr(stack.local, "start", start)
    monkeypatch.setattr(stack.local, "wait_ready", wait_ready)

    await stack.autostart()

    assert started == ["eng"] and timeouts == [stack.lifecycle.ready_timeout]
    assert any("модель ещё загружается" in m for m in stack.runtime.server_log.messages())
