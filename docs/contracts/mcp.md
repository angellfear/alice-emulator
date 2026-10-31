---
title: "MCP: инструменты модели"
status: stable
date: "2026-10-28"
module: "server_emulator/mcp_hub.py"
---

# MCP: инструменты модели

Вкладка «MCP» панели эмулятора. Код: `server_emulator/mcp_hub.py` (mcp.json,
транспорт, встроенные инструменты, базовый контекст, вызов), `server_emulator/ui/api/mcp.py`
(API), `server_emulator/ui/mcp.js` (вкладка), цикл инструментов — `Runtime.chat`.

## Встроенные инструменты (источник `alice`)

Модель получает их всегда, исполняются автоматически в цикле `Runtime.chat`.

| Инструмент | Аргументы | Что делает |
|---|---|---|
| `speakers_list` | — | колонки пользователя: sn, название, комната, платформа, `online`, `current` |
| `speaker_command` | `action`, `sn?`, `level?`, `frames?` | push-команда из `actions.CATALOG` в живую WS-сессию колонки; без `sn` — текущая колонка |
| `db_query` | `sql` | один `SELECT`/`WITH` (SQLite, только чтение) по снимку данных, до 200 строк |
| `web_search` | `query`, `max_results?` (1..10, 5), `region?` | веб-поиск без ключей (пакет `ddgs`): `{results: [{title, url, snippet}]}` |
| `web_fetch` | `url`, `max_chars?` (до 20000, 6000), `start?` | страница по URL → основной текст (httpx + `trafilatura`), `total_chars`, `truncated`; продолжение — `start` |

Веб-инструменты — `server_emulator/web_tools.py`, класс риска `read` (без подтверждения).
`web_fetch` отклоняет не-http(s) и адреса loopback/private/link-local (в т.ч. после
редиректов), читает не более 3 МБ.

Снимок для `db_query` — in-memory SQLite, собирается на каждый вызов: в голосовой
сессии — только данные владельца текущей колонки, из панели — все. Таблицы:
`speakers`, `users`, `params` (секреты — `***`), `timers` (время — локальное ISO),
`device_state` (без служебного `quasar_legacy`), `scenario_data`. Таблицы хранилища
(`settings`, `meta`, ключи провайдеров) в снимок не попадают; запись запрещена авторизатором.

## Базовый контекст

`mcp_hub.base_context` добавляется к системному промпту каждого запроса модели:
текущие дата, время и день недели в поясе колонки/пользователя/сервера, имя
пользователя и город, текущая колонка (с которой говорят), список колонок с
комнатами и статусом связи, подсказка про инструменты. Свой `device_context`
профиля передаётся без служебных `sn`, `user_id`, `state`.

## Внешние серверы — `state/mcp.json`

Стандартный формат клиентов MCP:

```json
{
  "mcpServers": {
    "homeassistant": { "type": "http", "url": "http://192.0.2.110:9583/private_…" },
    "files": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/data"], "env": {} },
    "old": { "type": "http", "url": "https://…/mcp", "headers": { "Authorization": "Bearer …" }, "disabled": true }
  }
}
```

| Поле | Смысл |
|---|---|
| `type` | `http` (MCP streamable HTTP; `streamable-http` — синоним) или `stdio`; без `type` — по `url`/`command` |
| `url`, `headers` | для http; ответ принимается `application/json` и `text/event-stream` |
| `command`, `args`, `env` | для stdio: постоянный процесс на сервер |
| `disabled` | `true` — сервер и его инструменты модели не передаются |

Транспорт и протокол — официальный MCP SDK (пакет `mcp`, `ClientSession` + `streamable_http_client` / `stdio_client`); http — новая MCP-сессия на каждый вызов. Старый двухканальный SSE-транспорт не поддержан. Имя сервера — `[A-Za-z0-9_.-]`, не `alice`.
При первом запуске файл создаётся: перенос прежних `settings.mcp_servers` или
заготовка `homeassistant` (выключена, URL-заглушка). Сохранение из вкладки
проверяет файл, удаляет выбор инструментов исчезнувших серверов и опрашивает
`tools/list` у каждого включённого сервера (ошибка одного не мешает остальным).

## Выбор инструментов внешних серверов

`settings.tools[]`: `{name, server_id, server_tool, description, inputSchema, enabled, mode}`,
имя — `<сервер>__<инструмент>` (до 64 символов). Обновление списка сохраняет `enabled`/`mode`.

Движок `graph` (по умолчанию, [agent.md](agent.md)) вместо `mode` использует класс риска
`risk`: `read`/`device` — выполняется сразу, `external`/`config`/`destructive` — после
подтверждения (голосом или в панели), результат возвращается модели. Без `risk` класс
выводится из `mode`. Таблица ниже — движок `legacy`.

| mode | Поведение в `Runtime.chat` (legacy) |
|---|---|
| `auto` | модель вызывает, хаб исполняет, результат уходит модели сообщением `tool`; до 5 кругов |
| `confirm` | вызов возвращается как предложение (`pending_calls`), запуск — вручную во вкладке «Проверка» |

Если в ответе модели смешаны `auto` и прочие вызовы (`confirm`, `alice_action`),
`auto` исполняются, остальные возвращаются предложениями, цикл останавливается.
Ошибка инструмента не роняет ответ — модель получает её текстом. Журнал вызовов
(модель и панель) — `runtime.tool_log`, последние 100.

## API (`/emulator/api/mcp`)

- `GET` → `{builtin: {id, title, tools}, config (текст mcp.json), path, servers: [{id, type, target, disabled, error}], tools, log}`
- `POST {op: 'config_save', text}` → `{saved, servers: {id: {count} | {error}}}`
- `POST {op: 'discover', server_id}` → `{count}`
- `POST {op: 'tool', name, enabled?, mode?, risk?}` (`risk` ставит и `mode`; `mode` снимает `risk`)
- `POST {op: 'call', name, arguments, sn?, user_id?}` → `{result, run}` — пробный вызов из панели

## Home Assistant (ha-mcp)

Аддон [ha-mcp](https://github.com/homeassistant-ai/ha-mcp) слушает порт 9583 в
локальной сети; адрес с секретным путём (`http://<IP HA>:9583/private_…`)
пишется в лог аддона при первом старте. Токен не нужен — секрет в пути.
Снаружи порт закрыт (удалённо — через аддоны Webhook Proxy или Cloudflared).
