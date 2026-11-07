"""Карта архитектуры docs/architecture-map.svg: python tools/gen_architecture_map.py.
Раскладка: колонки с широкими коридорами, у каждой стрелки своя полоса; пересечения — с белым «мостиком»."""
from pathlib import Path
from xml.sax.saxutils import escape as x

W, H = 1800, 1190
COL = {"entry": (200, 220), "voice": (560, 220), "scn": (900, 220), "plg": (1220, 200), "ai": (1540, 200)}
NH, STEP, Y0 = 70, 110, 160
ROW = lambda r: Y0 + r * STEP
C = {  # обводка, заливка
    "ext": ("#5b6676", "#ffffff"), "entry": ("#2f6fd0", "#e8effb"), "voice": ("#16855a", "#e5f4ec"),
    "scn": ("#b2650b", "#fbf1e2"), "plg": ("#6a45c9", "#f0ebfb"), "ai": ("#b8326b", "#fbeaf1"),
    "store": ("#4a5566", "#eef1f4"),
}
FG, MUTED, CHIP, RULE, BG = "#18202b", "#5b6676", "#3d4a5c", "#d5dbe3", "#ffffff"
SANS = "Segoe UI, IBM Plex Sans, Arial, sans-serif"
MONO = "Consolas, IBM Plex Mono, Menlo, monospace"

nodes = {}
def node(nid, layer, x0, y0, w, h, title, sub, chips=None, dashed=False):
    nodes[nid] = dict(layer=layer, x=x0, y=y0, w=w, h=h, title=title, sub=sub, chips=chips, dashed=dashed)
def cnode(nid, col, r, title, sub, chips=None, dashed=False):
    x0, w = COL[col]; node(nid, col, x0, ROW(r), w, NH, title, sub, chips, dashed)

node("yandex", "ext", 200, 30, 220, 60, "Серверы Яндекса", "uniproxy · quasar · OTA")
node("speech", "ext", 560, 30, 220, 60, "Сервисы речи", "ASR и TTS: GPU/CPU или внешние API")
node("extapi", "ext", 1220, 30, 200, 60, "Внешние API плагинов", "Spotify, Open-Meteo, …")
node("llm", "ext", 1540, 30, 200, 60, "LLM-провайдеры", "облачные и локальные")
node("station", "ext", 20, ROW(0), 140, NH, "Яндекс Станции", "колонки в сети")
node("owner", "ext", 20, 830, 140, 64, "Владелец", "браузер")

cnode("front", "entry", 0, "front.py", "TLS-фронт, SNI", ["server.json"])
cnode("disp", "entry", 1, "dispatcher.py", "маршрут устройства")
cnode("http", "entry", 2, "HTTP Quasar", "quasar.py · endpoints", ["emulator", "quasar/", "misses"])
cnode("ota", "entry", 3, "Обновления OTA", "updates.py", ["updates"])
cnode("up", "entry", 4, "upstream/", "проход, журнал трафика", ["traffic"])
cnode("log", "entry", 5, "server_log.py", "журнал сервера (пишут все)", ["server_log"])

cnode("voice", "voice", 0, "voice.py", "AAPI по WebSocket")
cnode("asr", "voice", 1, "Распознавание", "ASR-адаптер → сервис речи")
cnode("router", "voice", 2, "Маршрутизатор", "кто ответит на реплику", ["misses"])
cnode("compose", "voice", 3, "Сборка ответа", "текст + директивы колонке")
cnode("tts", "voice", 4, "Синтез + кеш", "TTS-адаптер → сервис речи", ["tts_cache/"])
cnode("async", "voice", 5, "Отложенный ответ", "дольше 3 с: «Секунду…» → Push")

cnode("builtin", "scn", 0, "Встроенные сценарии", "время, таймеры, громкость…")
cnode("content", "scn", 1, "Сценарии контента", "музыка, погода, новости, свет")
cnode("engine", "scn", 2, "scenario_engine/", "триггеры hassil, executor", ["emulator", "scenarios/"])
cnode("sdk", "scn", 3, "sdk.py", "API py-сценариев (ctx.*)")
cnode("dict", "scn", 4, "Справочник фраз — план", "фразы и списки от плагинов", dashed=True)

cnode("prov", "plg", 0, "providers.py", "реестр поставщиков")
cnode("mgr", "plg", 1, "extensions/manager", "установка, вкл/выкл", ["extensions/", "emulator"])
cnode("bpl", "plg", 2, "Встроенные плагины", "server_emulator/plugins/")
cnode("epl", "plg", 3, "Внешние расширения", "state/extensions/<id>/")

cnode("intel", "ai", 0, "«Интеллект»", "scenarios · model · agent")
cnode("agent", "ai", 1, "agent/", "LangGraph, память", ["agent", "ops", "routines"])
cnode("mcp", "ai", 2, "mcp_hub.py", "внешние MCP-серверы", ["mcp.json"])
cnode("vstack", "ai", 3, "voice_stack/", "адаптеры ASR/TTS/LLM", ["voice_stack/"])

node("ui", "store", 200, 830, 580, 64, "Панель ui/ и ui/api/*", "vanilla JS · запись только с заголовком X-Emulator: 1",
     ["emulator", "traffic", "server_log", "tts_cache/", "misses", "quasar/", "mcp.json", "server.json"])

STORES1 = [
    ("emulator.sqlite3", "настройки, устройства, параметры", "пишет: state_store.py"),
    ("updates.sqlite3", "попытки OTA, URL прошивок", "пишет: updates.py"),
    ("traffic.sqlite3", "журнал трафика («Запись»)", "пишет: upstream/journal.py"),
    ("server_log.sqlite3", "события сервера", "пишет: server_log.py"),
    ("agent.sqlite3", "история диалогов, память", "пишет: agent/service, memory"),
    ("agent_ops.sqlite3", "прогоны и бюджет агента", "пишет: agent/ops.py"),
    ("routines.sqlite3", "задания по расписанию", "пишет: agent/routines.py"),
]
STORES2 = [
    ("tts_cache/", "готовые фразы Ogg", "пишет: синтез"),
    ("*_misses.json", "«Без ответа»", "пишет: miss_journal.py"),
    ("scenarios/", "свои сценарии", "пишет: панель «Сценарии»"),
    ("extensions/", "версии расширений", "пишет: extensions/manager"),
    ("quasar/", "конфиги колонки", "пишет: панель Quasar"),
    ("mcp.json", "MCP-серверы", "пишет: панель MCP"),
    ("voice_stack/", "пакеты адаптеров", "пишет: панель «Голос»"),
    ("server.json", "порты, сертификаты", "пишет: server_config.py"),
]

L = lambda n: nodes[n]["x"]
R = lambda n: nodes[n]["x"] + nodes[n]["w"]
T = lambda n: nodes[n]["y"]
Bm = lambda n: nodes[n]["y"] + nodes[n]["h"]
MX = lambda n: nodes[n]["x"] + nodes[n]["w"] // 2
CY = lambda n, d=0: nodes[n]["y"] + nodes[n]["h"] // 2 + d

edges, labels, badges = [], [], []
def edge(pts, layer, dashed=False):
    edges.append((pts, C[layer][0], layer, dashed))
def label(x0, y0, text, layer, anchor="start"):
    labels.append((x0, y0, text, C[layer][0], anchor))
def badge(x0, y0, n):
    badges.append((x0, y0, n))

# голосовой ход
edge([(R("station"), CY("front")), (L("front") - 2, CY("front"))], "voice"); badge(R("station") - 4, T("station") - 4, 1)
edge([(R("front"), CY("front")), (L("voice") - 2, CY("front"))], "voice"); badge(490, CY("front") - 16, 2)
edge([(MX("voice"), Bm("voice")), (MX("voice"), T("asr") - 2)], "voice"); badge(MX("voice") + 18, Bm("voice") + 20, 3)
edge([(MX("asr"), Bm("asr")), (MX("asr"), T("router") - 2)], "voice"); badge(MX("asr") + 18, Bm("asr") + 20, 4)
edge([(R("router"), CY("router", -10)), (L("engine") - 2, CY("router", -10))], "voice"); badge(870, CY("router", -10) - 16, 5)
edge([(R("router"), CY("router", -26)), (810, CY("router", -26)), (810, 110), (1500, 110), (1500, CY("intel")), (L("intel") - 2, CY("intel"))], "voice")
badge(1000, 110, 6)
edge([(L("engine"), CY("engine", 25)), (850, CY("engine", 25)), (850, CY("compose")), (R("compose") + 2, CY("compose"))], "voice")
badge(870, CY("compose") - 16, 7)
edge([(MX("compose"), Bm("compose")), (MX("compose"), T("tts") - 2)], "voice"); badge(MX("compose") + 18, Bm("compose") + 20, 8)
edge([(L("tts"), CY("tts")), (540, CY("tts")), (540, CY("voice", 18)), (L("voice") - 2, CY("voice", 18))], "voice")
badge(540, CY("ota"), 9)

# вход и маршруты
edge([(MX("front"), Bm("front")), (MX("front"), T("disp") - 2)], "entry"); label(MX("front") + 8, Bm("front") + 25, "HTTP", "entry")
edge([(R("disp"), CY("disp")), (450, CY("disp")), (450, CY("up")), (R("up") + 2, CY("up"))], "entry")
edge([(450, CY("http")), (R("http") + 2, CY("http"))], "entry")
edge([(450, CY("ota")), (R("ota") + 2, CY("ota"))], "entry")
label(458, CY("http") + 4, "emulate", "entry"); label(458, CY("ota") + 4, "OTA", "entry"); label(458, CY("up") - 8, "hybrid /", "entry")
label(458, CY("up") + 8, "proxy", "entry")
edge([(L("up"), CY("up")), (180, CY("up")), (180, CY("yandex")), (L("yandex") - 2, CY("yandex"))], "entry")
label(172, CY("up") - 40, "проход", "entry", "end"); label(172, CY("up") - 24, "к Яндексу", "entry", "end")
edge([(R("owner"), CY("ui")), (L("ui") - 2, CY("ui"))], "entry")

# сценарии и плагины
edge([(R("content"), CY("content")), (1170, CY("content")), (1170, CY("prov", -10)), (L("prov") - 2, CY("prov", -10))], "plg")
label(1145, CY("content") - 8, "данные", "plg", "middle")
edge([(R("sdk"), CY("sdk")), (1195, CY("sdk")), (1195, CY("prov", 14)), (L("prov") - 2, CY("prov", 14))], "plg")
label(1157, CY("sdk") - 8, "ctx.music", "plg", "middle")
edge([(MX("mgr"), Bm("mgr")), (MX("mgr"), T("bpl") - 2)], "plg"); label(MX("mgr") + 8, Bm("mgr") + 25, "sync, вкл/выкл", "plg")
edge([(R("bpl"), CY("bpl")), (1450, CY("bpl")), (1450, CY("prov")), (R("prov") + 2, CY("prov"))], "plg")
edge([(R("epl"), CY("epl")), (1450, CY("epl")), (1450, CY("bpl"))], "plg")
label(1458, CY("mgr") + 4, "регистрация", "plg")
edge([(MX("prov"), T("prov")), (MX("prov"), Bm("extapi") + 2)], "plg"); label(MX("prov") + 8, 140, "HTTP", "plg")
edge([(MX("epl"), Bm("epl")), (MX("epl"), CY("dict")), (R("dict") + 2, CY("dict"))], "plg", dashed=True)
label(MX("epl") + 8, Bm("epl") + 30, "фразы (план)", "plg")

# интеллект
edge([(1640, Bm("intel")), (1640, T("agent") - 2)], "ai"); label(1648, Bm("intel") + 25, "agent", "ai")
edge([(1640, Bm("agent")), (1640, T("mcp") - 2)], "ai"); label(1648, Bm("agent") + 25, "инструменты", "ai")
edge([(R("intel"), CY("intel")), (1770, CY("intel")), (1770, CY("vstack")), (R("vstack") + 2, CY("vstack"))], "ai")
labels.append((1784, CY("mcp"), "model", C["ai"][0], "middle", 90))
edge([(1700, T("intel")), (1700, Bm("llm") + 2)], "ai"); label(1708, 140, "LLM API", "ai")

out = []
o = out.append
o(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{SANS}">')
o('<title>Карта эмулятора Алисы</title>')
o('<desc>Компоненты по слоям, голосовой ход по номерам, файлы состояния каждого блока и кто в них пишет.</desc>')
o('<defs>')
for layer in ("voice", "entry", "plg", "ai"):
    o(f'<marker id="m-{layer}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
      f'<path d="M0,0 L10,5 L0,10 z" fill="{C[layer][0]}"/></marker>')
o('</defs>')
o(f'<rect width="{W}" height="{H}" fill="{BG}"/>')
o(f'<rect x="188" y="124" width="1600" height="676" rx="14" fill="#fafbfc" stroke="{RULE}" stroke-dasharray="4 4"/>')
o(f'<text x="1776" y="790" text-anchor="end" font-size="12" fill="{MUTED}">процесс эмулятора: Python, один asyncio-цикл, aiohttp; runtime.py собирает компоненты</text>')
for title, col in [("ВХОД", "entry"), ("ГОЛОС", "voice"), ("СЦЕНАРИИ", "scn"), ("ПЛАГИНЫ", "plg"), ("ИНТЕЛЛЕКТ", "ai")]:
    o(f'<text x="{COL[col][0]}" y="150" font-size="12" font-weight="700" letter-spacing="1.4" fill="{C[col][0]}">{title}</text>')

# узлы
for nid, n in nodes.items():
    stroke, fill = C[n["layer"]]
    if n["dashed"]:
        fill = BG
    dash = ' stroke-dasharray="7 5"' if n["dashed"] else ""
    o(f'<rect x="{n["x"]}" y="{n["y"]}" width="{n["w"]}" height="{n["h"]}" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')
    o(f'<text x="{n["x"] + 14}" y="{n["y"] + 23}" font-size="14" font-weight="700" fill="{FG}">{x(n["title"])}</text>')
    o(f'<text x="{n["x"] + 14}" y="{n["y"] + 41}" font-size="11.5" fill="{MUTED}">{x(n["sub"])}</text>')
    if n["chips"]:
        o(f'<text x="{n["x"] + 14}" y="{n["y"] + 59}" font-size="11" font-family="{MONO}" fill="{CHIP}">▸ {x(" · ".join(n["chips"]))}</text>')

# связи: сначала белая подложка (мостик на пересечениях), потом линия
for pts, color, layer, dashed in edges:
    d = "M" + " L".join(f"{a},{b}" for a, b in pts)
    o(f'<path d="{d}" fill="none" stroke="{BG}" stroke-width="7" stroke-linejoin="round"/>')
for pts, color, layer, dashed in edges:
    d = "M" + " L".join(f"{a},{b}" for a, b in pts)
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    o(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" stroke-linejoin="round"{dash} marker-end="url(#m-{layer})"/>')
for lab in labels:
    x0, y0, text, color, anchor = lab[:5]
    rot = f' transform="rotate({lab[5]} {x0} {y0})"' if len(lab) > 5 else ""
    o(f'<text x="{x0}" y="{y0}"{rot} font-size="12" font-weight="600" fill="{color}" text-anchor="{anchor}" '
      f'stroke="{BG}" stroke-width="4" paint-order="stroke">{x(text)}</text>')
for x0, y0, n in badges:
    o(f'<circle cx="{x0}" cy="{y0}" r="11" fill="{C["voice"][0]}" stroke="{BG}" stroke-width="2"/>'
      f'<text x="{x0}" y="{y0 + 4.5}" font-size="12" font-weight="700" fill="#fff" text-anchor="middle">{n}</text>')

# хранилища
o(f'<text x="20" y="935" font-size="12" font-weight="700" letter-spacing="1.4" fill="{C["store"][0]}">ХРАНИЛИЩА state/</text>')
o(f'<text x="170" y="935" font-size="12" fill="{MUTED}">у каждой подсистемы свой файл; чужие файлы читаются только через API владельца; ▸ в блоке выше — какие файлы он использует</text>')
def store_row(items, y0, w, gap):
    for i, (name, what, who) in enumerate(items):
        sx = 20 + i * (w + gap)
        o(f'<rect x="{sx}" y="{y0}" width="{w}" height="62" rx="9" fill="{C["store"][1]}" stroke="{C["store"][0]}" stroke-width="1.3"/>')
        o(f'<text x="{sx + 12}" y="{y0 + 20}" font-size="13" font-weight="700" font-family="{MONO}" fill="{FG}">{x(name)}</text>')
        o(f'<text x="{sx + 12}" y="{y0 + 37}" font-size="11.5" fill="{MUTED}">{x(what)}</text>')
        o(f'<text x="{sx + 12}" y="{y0 + 53}" font-size="11.5" fill="{CHIP}">{x(who)}</text>')
store_row(STORES1, 950, 228, 14)
store_row(STORES2, 1026, 199, 13)

# легенда
ly = 1122
o(f'<line x1="20" y1="{ly - 22}" x2="{W - 20}" y2="{ly - 22}" stroke="{RULE}"/>')
o(f'<text x="20" y="{ly + 5}" font-size="13" font-weight="700" fill="{FG}">Голосовой ход</text>')
steps = ["колонка → фронт, TLS 443/80", "WebSocket uni.ws", "PCM на распознавание", "текст", "есть сценарий",
         "промах → «Интеллект»", "текст и директивы", "фраза на синтез", "Ogg → колонке"]
sx = 140
for i, s in enumerate(steps, 1):
    o(f'<circle cx="{sx + 11}" cy="{ly}" r="11" fill="{C["voice"][0]}"/>'
      f'<text x="{sx + 11}" y="{ly + 4.5}" font-size="12" font-weight="700" fill="#fff" text-anchor="middle">{i}</text>')
    o(f'<text x="{sx + 28}" y="{ly + 5}" font-size="12" fill="{FG}">{x(s)}</text>')
    sx += 46 + int(len(s) * 6.9)
legend = [("entry", "вход и маршруты"), ("voice", "голос"), ("scn", "сценарии"), ("plg", "плагины"),
          ("ai", "«Интеллект»"), ("store", "UI и хранилища"), ("ext", "вне процесса")]
sx = 20
for layer, name in legend:
    o(f'<rect x="{sx}" y="{ly + 26}" width="16" height="13" rx="2" fill="{C[layer][1]}" stroke="{C[layer][0]}"/>')
    o(f'<text x="{sx + 22}" y="{ly + 37}" font-size="12" fill="{FG}">{name}</text>')
    sx += 44 + int(len(name) * 7)
o(f'<text x="{sx + 10}" y="{ly + 37}" font-size="12" fill="{FG}">пунктир — запланировано</text>')
o(f'<text x="20" y="{ly + 60}" font-size="12" fill="{MUTED}">Ответ модели идёт в сборку ответа так же, как ответ сценария; дольше 3 с — через отложенный ответ (Push). '
  'Приоритет имён: пользовательское &gt; расширение &gt; встроенное.</text>')
o('</svg>')
open(Path(__file__).resolve().parent.parent / "docs" / "architecture-map.svg", "w", encoding="utf-8").write("\n".join(out))
print("ok")
