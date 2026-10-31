---
title: "Память агента"
status: stable
date: "2026-10-28"
module: "server_emulator/agent/memory.py"
---

# Память агента

Код: `server_emulator/agent/memory.py`, API: `server_emulator/ui/api/memory.py`, вкладка
«Память»: `server_emulator/ui/memory.js`. Работает в движке `graph` ([agent.md](agent.md)).

## Хранилище

LangGraph `AsyncSqliteStore` в `state/agent.sqlite3` (таблицы `store`, `store_vectors`).
Память принадлежит **владельцу колонки** (пользователю реестра): голос не различается, колонка
общая для семьи. Без `user_id` (непривязанная колонка, студия без устройства) памяти нет.

| Пространство | Что | Кто пишет |
|---|---|---|
| `("memories", user, "facts")` | факты о пользователе и доме, предпочтения | `manage_memory`, рефлексия, панель |
| `("memories", user, "episodes")` | сводки прошлых разговоров | рефлексия, панель (удаление) |
| `("memories", user, "instructions")`, ключ `main` | пожелания к ответам | `memory_instructions` (с подтверждением), панель |
| `("memories_meta",)` | подпись модели эмбеддингов, отметки рефлексии по тредам | сервис |

Значение записи: `{"kind": "Fact"|"Episode"|"Instructions", "content": {"text": …}}`;
`manage_memory` LangMem пишет `{"content": "…"}` — оба вида читаются.

## Подстановка в промпт

Перед прогоном в системный промпт добавляются факты (`recall_limit`), два эпизода и пожелания.
С эмбеддингами (`settings.memory.embed`) факты и эпизоды подбираются по смыслу реплики
(sqlite-vec, косинус), без них — последние записи (до `recall_limit × 3` фактов).
Сбой памяти не ломает ответ: прогон идёт без неё, ошибка — в `runtime.recent` (`kind: memory`).

## Инструменты агента

| Инструмент | Класс риска | Что |
|---|---|---|
| `manage_memory` (LangMem) | `device` | создать / обновить / удалить факт по просьбе пользователя |
| `search_memory` (LangMem) | `read` | поиск по фактам |
| `memory_instructions` | `config` — подтверждение | заменить пожелания к ответам |

## Фоновая рефлексия

После каждого прогона тред ставится в очередь: через `reflect_after_s` секунд тишины (новая
реплика переносит запуск) два менеджера LangMem (`create_memory_store_manager`) разбирают
**новые** реплики треда: факты (вставка, правка, удаление) и эпизод. Им идут только реплики
пользователя и ответы агента — без вызовов и результатов инструментов, так содержимое
MCP/Home Assistant не попадает в память. Отметка последнего разобранного сообщения —
`("memories_meta", "reflected")`, ключ — тред. Журнал последних 30 запусков — во вкладке.

Планирование — asyncio в процессе сервера (не `ReflectionExecutor` LangMem: тот работает
в своём потоке, а вызовы модели идут через общий aiohttp-клиент основного цикла).

## Эмбеддинги

OpenAI-совместимый `POST {url}` `{model, input: [...]}` → `data[].embedding` (Ollama
`/v1/embeddings`, LM Studio, OpenAI). Размерность определяется пробным запросом. Смена
URL/модели/размерности — записи переиндексируются при следующем открытии хранилища.
Сбой запроса — сервис не опрашивается 60 с (без задержки ответа на каждой реплике);
переиндексации при сбое нет.

## Настройки `settings.memory`

`{enabled, embed: {url, model, api_key}, recall_limit (1–20), reflect_after_s (10–3600)}`.
`api_key` в UI и `GET` — маска `__KEEP__`; `env:VAR` поддерживается.

## API (`/emulator/api/memory`)

- `GET ?user=` → `{available, settings, users, user, items: {facts, episodes, instructions}, log}`
- `POST {op: 'settings', enabled?, recall_limit?, reflect_after_s?, embed?}`
- `POST {op: 'put', user, kind, text, key?}` → `{key}`; `{op: 'delete', user, kind, key}`
- `POST {op: 'forget', user}` → `{removed}` — вся память пользователя
- `POST {op: 'reflect', user}` → `{records}` — рефлексия тредов `voice:<SN>` его колонок сейчас
- `POST {op: 'test_embed'}` → `{dims}`
