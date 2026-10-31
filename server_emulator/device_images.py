"""Картинки устройств — с публичной веб-версии умного дома Яндекса (без входа).

В репозитории картинок нет. Страница `https://iot.quasar.yandex.ru/iot/` отдаёт без авторизации HTML со
ссылкой на `main@iot-app.css`; в CSS каждое устройство — правило
`.device-image__icon_type_<тип>[.device-image__icon_color_<цвет>]{background-image:image-set(url(…webp)2x,url(…webp)3x)}`.
Модуль разбирает CSS в таблицу `{тип: {цвет|'-': url}}`, сопоставляет её с именами картинок UI
(`iot_station_chiron_black` и т. п. — `DEVICE_IMAGES`) и кладёт webp в каталог картинок, откуда их отдаёт UI.
Берётся вариант без `_on` («выключено»), 3x. Нет сети или правила — UI показывает силуэт.

Картинки принадлежат Яндексу: скачиваются только по явному согласию пользователя (галочка в окне настройки,
`accept` в `POST api/setup/images`, `--accept` в CLI), фоновой загрузки нет.

Безопасность: только https и только хосты `ALLOWED_HOSTS`, редиректы проверяются на каждом шаге,
ограничены размеры ответов и время запроса.

CLI: `python -m server_emulator.device_images --accept`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

from .protocol_import import write_atomic

log = logging.getLogger(__name__)

BASE_URL = "https://iot.quasar.yandex.ru/iot/"
ALLOWED_HOSTS = ("iot.quasar.yandex.ru", "pudya.yastatic.net")
DEFAULT_IMAGES = Path(__file__).resolve().parent / "ui" / "img" / "devices"
INDEX_NAME = "_index.json"
MAX_HTML = 8 * 1024 ** 2
MAX_CSS = 16 * 1024 ** 2
MAX_IMAGE = 4 * 1024 ** 2
TIMEOUT = ClientTimeout(total=45, connect=10)
PARALLEL = 6
MAX_REDIRECTS = 3

# Имя нашей картинки → (тип сайта, цвет сайта | None). Тип сайта — devices.types… без префикса, точки и «_» → «-».
# Сопоставление проверено сравнением картинок; у станций Лайт 2 («no_clock») на сайте тип prune, у «street» — mango.
DEVICE_IMAGES = {
    "iot_intercom": ("openable-intercom", None),
    "iot_station_2_beige": ("station-midi", "beige"),
    "iot_station_bergamot_black": ("station-bergamot", "black"),
    "iot_station_bergamot_gray": ("station-bergamot", "gray"),
    "iot_station_bergamot_violet": ("station-bergamot", "violet"),
    "iot_station_chiron_biege": ("station-chiron", "beige"),
    "iot_station_chiron_black": ("station-chiron", "black"),
    "iot_station_chiron_green": ("station-chiron", "green"),
    "iot_station_chiron_red": ("station-chiron", "red"),
    "iot_station_fiero": ("fiero-blackthorn", None),
    "iot_station_lite2_blue_no_clock_connection": ("station-prune", "blue"),
    "iot_station_lite2_coral_no_clock_connection": ("station-prune", "copper"),
    "iot_station_lite2_gray_no_clock_connection": ("station-prune", "black"),
    "iot_station_lite2_green_no_clock_connection": ("station-prune", "green"),
    "iot_station_lite2_lilac_no_clock_connection": ("station-prune", "violet"),
    "iot_station_lite2_pink_no_clock_connection": ("station-prune", "pink"),
    "iot_station_magritte": ("media-device-tv-yandex-magritte", None),
    "iot_station_malevich": ("media-device-tv-yandex-malevich", None),
    "iot_station_max_beige": ("station-2", "beige"),
    "iot_station_max_black": ("station-2", "black"),
    "iot_station_max_green": ("station-2", "green"),
    "iot_station_max_turquoise": ("station-2", "cyan"),
    "iot_station_mini3_pro_black_clock_led": ("station-pickle", "black"),
    "iot_station_mini3_pro_blue_clock_led": ("station-pickle", "blue"),
    "iot_station_mini3_pro_gray_clock_led": ("station-pickle", "white"),
    "iot_station_mini3_pro_green_clock_led": ("station-pickle", "green"),
    "iot_station_mini3_pro_violet_clock_led": ("station-pickle", "violet"),
    "iot_station_monet": ("media-device-tv-yandex-monet", None),
    "iot_station_orion_black": ("station-orion", "black"),
    "iot_station_orion_gray": ("station-orion", "gray"),
    "iot_station_orion_violet": ("station-orion", "violet"),
    "iot_station_plum_blue": ("station-plum", "blue"),
    "iot_station_plum_copper": ("station-plum", "copper"),
    "iot_station_plum_gray": ("station-plum", "black"),
    "iot_station_plum_green": ("station-plum", "green"),
    "iot_station_plum_pink": ("station-plum", "pink"),
    "iot_station_plum_violet": ("station-plum", "violet"),
    "iot_station_quinglong": ("station-quinglong", None),
    "iot_station_street_black_led": ("station-mango", "black"),
    "iot_station_street_gray_led": ("station-mango", "gray"),
    "iot_station_street_green_led": ("station-mango", "green"),
    "iot_station_street_orange_led": ("station-mango", "copper"),
    "iot_station_street_violet_led": ("station-mango", "violet"),
}

_TYPE_PREFIX = "device-image__icon_type_"
_COLOR_PREFIX = "device-image__icon_color_"
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SELECTOR = re.compile(r"(?:\.[\w-]+)+")
_CLASS = re.compile(r"\.([\w-]+)")
_IMAGE_URL = re.compile(r"url\(\s*['\"]?([^)'\"\s]+)['\"]?\s*\)\s*(2x|3x)")
_CSS_LINK = re.compile(r"((?:https?:)?//[A-Za-z0-9._~:\-/]+/)(main|chunk-extended)@iot-app\.css")


class ImageFetchError(ValueError):
    """Понятная пользователю ошибка получения картинок (по-русски)."""


def images_dir():
    """Каталог картинок: `ALICA_IMAGES_DIR` или `server_emulator/ui/img/devices/` (в Git не входит)."""
    override = os.environ.get("ALICA_IMAGES_DIR")
    return Path(override) if override else DEFAULT_IMAGES


def parse_css(text, allowed=ALLOWED_HOSTS, insecure=False):
    """CSS сайта → `{тип: {цвет|'-': url}}`: правила без состояний (`_on`, `_mod-…`, размеров), 3x, иначе 2x."""
    table = {}
    for rule in _RULE.finditer(text):
        selectors, body = rule.group(1), rule.group(2)
        if _TYPE_PREFIX not in selectors or "image-set" not in body:
            continue
        urls = {scale: url for url, scale in _IMAGE_URL.findall(body)}
        url = urls.get("3x") or urls.get("2x")
        if not url or not _url_allowed(url, allowed, insecure):
            continue
        for selector in selectors.split(","):
            selector = selector.strip()
            if not _SELECTOR.fullmatch(selector):
                continue
            classes = _CLASS.findall(selector)
            if not classes[0].startswith(_TYPE_PREFIX):
                continue
            color, plain = "-", True
            for extra in classes[1:]:
                if extra.startswith(_COLOR_PREFIX) and color == "-":
                    color = extra[len(_COLOR_PREFIX):]
                else:
                    plain = False   # `_on`, `_mod-…`, `_file-resolution_…`: другое состояние
            if plain:
                table.setdefault(classes[0][len(_TYPE_PREFIX):], {}).setdefault(color, url)
    return table


def resolve(table):
    """Таблица сайта → `{имя нашей картинки: url}`; цвета нет — правило без цвета; правила нет — имени нет."""
    found = {}
    for name, (kind, color) in DEVICE_IMAGES.items():
        colors = table.get(kind) or {}
        url = (colors.get(color) if color else None) or colors.get("-")
        if url:
            found[name] = url
    return found


def css_links(html, base_url):
    """Ссылки на `main@iot-app.css` (и `chunk-extended@iot-app.css`, если есть) из HTML страницы."""
    html = html.replace("\\u002F", "/").replace("\\/", "/")
    links = []
    for prefix, name in _CSS_LINK.findall(html):
        link = urljoin(base_url, prefix + name + "@iot-app.css")
        if link not in links:
            links.append(link)
    links.sort(key=lambda link: "main@" not in link)   # main первым: без него таблицы нет
    return links


def _url_allowed(url, allowed, insecure):
    parts = urlsplit(url)
    if parts.scheme != "https" and not (insecure and parts.scheme == "http"):
        return False
    if parts.hostname not in allowed:
        return False
    return insecure or parts.port in (None, 443)


class DeviceImages:
    def __init__(self, directory=None, base_url=BASE_URL, allowed_hosts=ALLOWED_HOSTS, insecure=False):
        self._directory = Path(directory) if directory else None
        self.base_url = base_url
        self.allowed = tuple(allowed_hosts)
        self.insecure = insecure   # только для тестов: http и произвольный порт локального сервера
        self.error = ""
        self.busy = False
        self._lock = asyncio.Lock()

    @property
    def directory(self):
        return self._directory or images_dir()

    def count(self):
        return sum(1 for name in DEVICE_IMAGES if (self.directory / (name + ".webp")).is_file())

    def status(self):
        return {"images": self.count(), "images_total": len(DEVICE_IMAGES), "images_busy": self.busy,
                "images_error": self.error}

    # ── сеть ────────────────────────────────────────────────────────────────

    async def _get(self, session, url, limit):
        for _ in range(MAX_REDIRECTS + 1):
            if not _url_allowed(url, self.allowed, self.insecure):
                raise ImageFetchError("Адрес вне разрешённых хостов умного дома Яндекса: запрос отклонён")
            async with session.get(url, allow_redirects=False) as response:
                if 300 <= response.status < 400 and response.headers.get("Location"):
                    url = urljoin(url, response.headers["Location"])
                    continue
                if response.status != 200:
                    raise ImageFetchError(f"Сайт ответил {response.status}")
                data = bytearray()
                async for chunk in response.content.iter_chunked(1 << 16):
                    data += chunk
                    if len(data) > limit:
                        raise ImageFetchError("Ответ сайта слишком большой")
                return bytes(data)
        raise ImageFetchError("Слишком много перенаправлений")

    async def _download(self, session, semaphore, name, url):
        async with semaphore:
            payload = await self._get(session, url, MAX_IMAGE)
        if payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
            raise ImageFetchError("Сайт вернул не webp")
        write_atomic(self.directory / (name + ".webp"), payload)

    def _index(self):
        try:
            value = json.loads((self.directory / INDEX_NAME).read_text(encoding="utf-8"))
            return value.get("files") if isinstance(value, dict) and isinstance(value.get("files"), dict) else {}
        except (OSError, ValueError):
            return {}

    async def refresh(self):
        """Таблица с сайта → недостающие и изменившиеся webp. Возвращает `status()`; ошибка — ImageFetchError."""
        if self._lock.locked():
            raise ImageFetchError("Загрузка картинок уже идёт")
        async with self._lock:
            self.busy = True
            try:
                await self._refresh()
                self.error = ""
            except ImageFetchError as exc:
                self.error = str(exc)
                raise
            except (ClientError, asyncio.TimeoutError, OSError) as exc:
                self.error = "Нет связи с сайтом умного дома Яндекса" + (": " + str(exc) if str(exc) else "")
                raise ImageFetchError(self.error) from exc
            finally:
                self.busy = False
        return self.status()

    async def _refresh(self):
        async with ClientSession(timeout=TIMEOUT, headers={"User-Agent": "alica-emulator"}) as session:
            html = (await self._get(session, self.base_url, MAX_HTML)).decode("utf-8", "replace")
            links = css_links(html, self.base_url)
            if not links:
                raise ImageFetchError("На странице умного дома не найден main@iot-app.css: сайт изменился")
            table = {}
            for number, link in enumerate(links):
                try:
                    text = (await self._get(session, link, MAX_CSS)).decode("utf-8", "replace")
                except ImageFetchError:
                    if number == 0:
                        raise
                    continue   # дополнительный CSS необязателен
                for kind, colors in parse_css(text, self.allowed, self.insecure).items():
                    for color, url in colors.items():
                        table.setdefault(kind, {}).setdefault(color, url)
            wanted = resolve(table)
            if not wanted:
                raise ImageFetchError("В CSS умного дома нет нужных картинок: сайт изменился")
            known, semaphore = self._index(), asyncio.Semaphore(PARALLEL)
            todo = {name: url for name, url in wanted.items()
                    if known.get(name) != url or not (self.directory / (name + ".webp")).is_file()}
            results = await asyncio.gather(*(self._download(session, semaphore, name, url) for name, url in todo.items()),
                                           return_exceptions=True)
            failed = [item for item in results if isinstance(item, BaseException)]
            done = {name: url for (name, url), item in zip(todo.items(), results) if not isinstance(item, BaseException)}
            files = {}
            for name, url in wanted.items():
                exists = (self.directory / (name + ".webp")).is_file()
                if name in done or (name not in todo and exists):
                    files[name] = url
                elif exists and name in known:
                    files[name] = known[name]   # не скачалась, остаётся прежняя
            write_atomic(self.directory / INDEX_NAME, json.dumps({"fetched": int(time.time()), "files": files},
                                                                 ensure_ascii=False, indent=1).encode("utf-8"))
            if failed and not done and todo:
                raise ImageFetchError("Картинки не скачались: " + str(failed[0]))


NOTICE = ("Картинки устройств скачиваются с сайта умного дома Яндекса (iot.quasar.yandex.ru); права на них "
          "принадлежат ООО «Яндекс». Они нужны только для показа в интерфейсе эмулятора.")


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="python -m server_emulator.device_images", description=NOTICE)
    parser.add_argument("--accept", action="store_true", help="подтвердить, что вы ознакомлены с условием выше")
    args = parser.parse_args(argv)
    if not args.accept:
        print(NOTICE + " Чтобы скачать, запустите с --accept.", file=sys.stderr)
        return 2
    try:
        info = asyncio.run(DeviceImages().refresh())
    except ImageFetchError as exc:
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1
    print(f"Картинки: {info['images']} из {info['images_total']} → {images_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
