"""Веб-инструменты модели: web_search и web_fetch (httpx + trafilatura).

web_search — конфигурируемый: если в settings.web_tools задан search_url (SearXNG, Brave,
Tavily и пр.), используется HTTP API; иначе ddgs как fallback.
web_fetch не ходит в локальную сеть: адреса loopback/private/link-local отклоняются.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

TOOLS = [
    {"name": "web_search",
     "description": "Поиск в интернете (без ключей): заголовок, адрес и фрагмент для каждого результата. "
                    "Затем при необходимости читай страницу через web_fetch.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "поисковый запрос"},
         "max_results": {"type": "integer", "description": "сколько результатов, 1..10 (по умолчанию 5)"},
         "region": {"type": "string", "description": "регион, например ru-ru, us-en; по умолчанию wt-wt"}},
         "required": ["query"]}},
    {"name": "web_fetch",
     "description": "Загрузить веб-страницу по URL и вернуть её основной текст (без разметки). "
                    "Длинный текст обрезается; start — смещение для чтения продолжения.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "http(s)://…"},
         "max_chars": {"type": "integer", "description": "сколько символов вернуть, до 20000 (по умолчанию 6000)"},
         "start": {"type": "integer", "description": "смещение в символах, по умолчанию 0"}},
         "required": ["url"]}},
]
NAMES = {t["name"] for t in TOOLS}
RISKS = {"web_search": "read", "web_fetch": "read"}
MAX_BYTES = 3_000_000
TIMEOUT = 20


def _int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _search_ddgs(query, limit, region):
    from ddgs import DDGS
    return DDGS(timeout=TIMEOUT).text(query, region=region, max_results=limit)


async def _search_api(search_url, query, limit, api_key=None):
    """HTTP API поиска (SearXNG JSON format: results[].{title, url, content})."""
    import httpx
    _check_url(search_url)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as http:
        resp = await http.get(search_url, params={"q": query, "format": "json", "count": str(limit)})
        resp.raise_for_status()
        data = resp.json()
    raw = data.get("results") or data.get("web", {}).get("results") or []
    return [{"title": r.get("title", ""), "url": r.get("url") or r.get("href", ""),
             "snippet": r.get("content") or r.get("body") or r.get("snippet", "")} for r in raw[:limit]]


async def web_search(arguments, config=None):
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("web_search: нужен query")
    limit = _int(arguments.get("max_results"), 5, 1, 10)
    cfg = config or {}
    search_url = cfg.get("search_url")
    if search_url:
        try:
            results = await _search_api(search_url, query.strip(), limit, cfg.get("search_api_key"))
        except Exception as exc:
            raise ValueError("web_search: API поиска недоступен — " + (str(exc) or type(exc).__name__))
    else:
        try:
            found = await asyncio.to_thread(_search_ddgs, query.strip(), limit, str(arguments.get("region") or "wt-wt"))
        except Exception as exc:
            if type(exc).__name__ == "DDGSException" and "No results" in str(exc):
                found = []
            else:
                raise ValueError("web_search: поиск недоступен — " + (str(exc) or type(exc).__name__))
        results = [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in found]
    return {"query": query, "results": results}


def _check_url(url):
    parts = urlparse(url) if isinstance(url, str) else None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("web_fetch: нужен адрес http(s)://…")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise ValueError("web_fetch: хост не найден — " + parts.hostname)
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("web_fetch: локальные адреса запрещены — " + parts.hostname)


def _extract(html, url):
    import trafilatura
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_recall=True)
    title = getattr(trafilatura.extract_metadata(html), "title", None)
    return title or "", text or ""


async def web_fetch(arguments):
    import httpx
    url = arguments.get("url")
    limit, start = _int(arguments.get("max_chars"), 6000, 200, 20000), _int(arguments.get("start"), 0, 0, 10 ** 9)
    await asyncio.to_thread(_check_url, url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AliceEmulator/1.0)", "Accept-Language": "ru,en;q=0.8"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, headers=headers) as http:
            for _ in range(5):  # редиректы вручную: каждый адрес проверяется на локальную сеть
                async with http.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = str(response.url.join(response.headers["location"]))
                        await asyncio.to_thread(_check_url, url)
                        continue
                    response.raise_for_status()
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    kind, encoding = response.headers.get("content-type", ""), response.encoding or "utf-8"
                    break
            else:
                raise ValueError("web_fetch: слишком много перенаправлений")
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"web_fetch: HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        raise ValueError("web_fetch: страница недоступна — " + (str(exc) or type(exc).__name__))
    html = body[:MAX_BYTES].decode(encoding, "replace")
    if "html" in kind or "xml" in kind or not kind:
        title, text = await asyncio.to_thread(_extract, html, url)
    elif kind.startswith("text/") or "json" in kind:
        title, text = "", html
    else:
        raise ValueError("web_fetch: не текстовая страница (" + kind.split(";")[0] + ")")
    chunk = text[start:start + limit]
    return {"url": url, "title": title, "total_chars": len(text), "start": start, "text": chunk,
            "truncated": start + limit < len(text)}


async def call(name, arguments, config=None):
    if name == "web_search":
        return await web_search(arguments, config)
    return await web_fetch(arguments)
