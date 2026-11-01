"""Скриншоты панели для README (TASK-075): Playwright, демо-данные из tools/demo_state.py.

Запуск:
    python tools/demo_state.py <папка>            # один раз
    python tools/readme_screenshots.py --state <папка> --out docs/img

Зависимости (в pyproject не входят): pip install playwright pillow;
playwright install chromium. Сервер поднимается сам на 127.0.0.1:18090
(порты 80/443 не занимаются), снимаются вкладки на демо-данных.
Время в странице фиксируется на момент генерации демо-данных (tools/demo.json),
чтобы повторный запуск давал те же кадры. Схемы протокола — server_emulator/schemas
разработчика (или ALICA_PROTOCOL_SOURCE); картинки устройств — каталог разработчика.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORT = 18090
BASE_URL = f"http://127.0.0.1:{PORT}"

# (файл, вкладка, ожидаемый текст данных, подготовка)
SHOTS = [
    ("devices", "#devices", "Гостиная", None),
    ("users", "#accounts", "Дача", None),
    ("updates", "#updates", "Запретить", None),
    ("scenarios", "#scenarios", "Включи мультики", None),
    ("http", "#endpoints", "HTTP без эндпоинта", None),
    ("ws", "#ws", "Входящие", None),
    ("quasar", "#quasar-http", "Гостиная", "quasar-overrides"),
    ("params", "#params", "Определения", None),
    ("catalog", "#app-catalog", "Палитра", None),
    ("journal", "#traffic", "Гостиная", "expand-journal-record"),
    ("voice", "#voice-stack", "Голосовой стек", None),
    ("agent", "#agent", "Агент", None),
    ("extensions", "#extensions", "Расширения", None),
    ("server-log", "#server-log", "Лог", None),
    ("mobile", "#devices", "Гостиная", "mobile"),
]


def wait_for_server(seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/emulator/api/status", timeout=2) as answer:
                if answer.status == 200:
                    return
        except OSError:
            time.sleep(0.4)
    raise SystemExit("Сервер не поднялся за 30 секунд")


def freeze_time_script(now_epoch):
    """Date в странице фиксирован на момент генерации демо-данных — кадры воспроизводимы."""
    return ("(() => { const FIXED = %d; const Real = Date;" % (now_epoch * 1000)) + """
      class Frozen extends Real {
        constructor(...args) { args.length === 0 ? super(FIXED) : super(...args); }
        static now() { return FIXED; }
      }
      window.Date = Frozen;
    })();"""


def wait_ready(page):
    """Данные загружены: нет «Загрузка…», тосты закрыты."""
    page.wait_for_function("() => !document.body.textContent.includes('Загрузка…')", timeout=15000)
    page.wait_for_timeout(400)
    page.evaluate("document.querySelectorAll('.toast, #toasts, dialog[open]').forEach(n => n.remove())")


def prepare(page, action):
    if action == "expand-journal-record":
        # группа с ответом Алисы (кадры с расшифровкой) → карточка записи
        page.locator("#traffic details.traffic-group", has_text="AliceResponse").first             .locator("> summary").click()
        page.wait_for_timeout(500)
        page.locator("#traffic details.traffic-group", has_text="AliceResponse")             .locator("button.tr-item").first.click()
        page.wait_for_timeout(900)  # карточка: заголовки, тела, расшифровка protobuf
    elif action == "quasar-overrides":
        # «Переопределение под устройство» → карточка станции Гостиной (источник «Напрямую»)
        station = page.evaluate("(async () => (await (await fetch('api/identities')).json())"
                                ".devices.find(d => d.model === 'yandexstation_2').sn)()")
        page.locator("#quasar-http button", has_text="Переопределение под устройство").wait_for(timeout=8000)
        page.locator("#quasar-http button", has_text="Переопределение под устройство").click()
        page.wait_for_timeout(400)
        page.locator("#quasar-http .quasar-device", has_text=station).click()
        page.wait_for_timeout(600)


ERROR_MARKS = ("Произошла ошибка", "Не удалось загрузить", "Неизвестная модель",
               "Ошибка загрузки", "SN не сходится")


def assert_no_errors(page, name):
    """На снимке не должно быть ошибок интерфейса и расшифровки SN."""
    body = page.locator("main").inner_text()
    for mark in ERROR_MARKS:
        if mark in body:
            raise SystemExit(f"{name}: на снимке ошибка «{mark}» — исправьте демо-данные")


def settle(page, name, action, out_dir):
    wait_ready(page)
    if action == "mobile":
        page.set_viewport_size({"width": 400, "height": 860})
        page.wait_for_timeout(400)
    prepare(page, action)
    assert_no_errors(page, name)
    # один разворот: видимая область 1280×800 (мобильный — 400×860), без длинной простыни
    page.screenshot(path=str(out_dir / f"{name}.png"))


def optimize(path, limit_kb=300):
    from PIL import Image
    image = Image.open(path)
    image.save(path, optimize=True)
    while path.stat().st_size > limit_kb * 1024:
        image = image.convert("P", palette=Image.Palette.ADAPTIVE, colors=192)
        image.save(path, optimize=True)
        if path.stat().st_size > limit_kb * 1024:  # дальнейшее сжатие — уменьшение цветов
            colors = max(64, 128)
            image = image.convert("P", palette=Image.Palette.ADAPTIVE, colors=colors)
            image.save(path, optimize=True)
        break


def main():
    parser = argparse.ArgumentParser(description="Скриншоты панели для README (TASK-075)")
    parser.add_argument("--state", required=True, help="папка демо-данных из tools/demo_state.py")
    parser.add_argument("--out", default="docs/img", help="куда писать PNG")
    args = parser.parse_args()
    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = ROOT / "tools" / "demo.json"
    if not marker.exists():
        raise SystemExit("Нет tools/demo.json — сначала запустите tools/demo_state.py <папка>")
    now_epoch = json.loads(marker.read_text(encoding="utf-8")).get("generated", time.time())

    schemas = os.environ.get("ALICA_PROTOCOL_SOURCE") or str(ROOT / "server_emulator" / "schemas")
    if not Path(schemas).exists():
        raise SystemExit(f"Схемы протокола не найдены: {schemas} (нужны для расшифровки AAPI в журнале)")

    server = subprocess.Popen(
        [sys.executable, "-m", "server_emulator.server", "--host", "127.0.0.1",
         "--port", str(PORT), "--no-ssl", "--http-port", "0", "--state", args.state],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_server()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1280, "height": 800},
                                          device_scale_factor=1, locale="ru-RU")
            context.add_init_script(freeze_time_script(now_epoch))
            page = context.new_page()
            page.goto(BASE_URL + "/emulator/")
            wait_ready(page)
            for name, tab, expected, action in SHOTS:
                page.evaluate(f"window.goTab('{tab.lstrip('#')}')")
                page.wait_for_timeout(600)
                if expected and page.locator("main", has_text=expected).count() == 0 \
                        and not page.locator("main", has_text=expected).first.is_visible():
                    print(f"  пропуск {name}: на вкладке нет данных ({expected})")
                    continue
                settle(page, name, action, out_dir)
                size = (out_dir / f"{name}.png").stat().st_size
                print(f"  {name}.png ({size // 1024} КиБ)")
            context.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    for shot in out_dir.glob("*.png"):
        optimize(shot)
    print(f"Готово: {out_dir} ({len(list(out_dir.glob('*.png')))} снимков)")


if __name__ == "__main__":
    main()
