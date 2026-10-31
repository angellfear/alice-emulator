---
title: "Задания по расписанию"
status: stable
date: "2026-10-30"
module: "server_emulator/agent/routines.py"
---

# Задания по расписанию

Код: `server_emulator/agent/routines.py`, API: `server_emulator/ui/api/routines.py`, вкладка
«Задания»: `server_emulator/ui/routines.js`. Нужен движок `graph` ([agent.md](agent.md)) и APScheduler.

## Задание

```json
{"id": "r1a2b3c4d5", "title": "Утро", "owner": "<user_id>", "sn": "<SN колонки>",
 "kind": "agent", "prompt": "расскажи погоду",          // или kind=scenario + scenario_id/text
 "schedule": {"kind": "cron", "expr": "0 8 * * 1-5"},   // interval: секунды ≥ 60; once: ISO дата-время
 "tz": "Europe/Moscow", "delivery": "speak", "quiet": {"from": "22:00", "to": "08:00"},
 "enabled": true, "created_by": "user", "max_runs_per_day": 24}
```

Хранение — `state/routines.sqlite3` (таблицы `routines`, `routine_runs`, `inbox`; отдельный файл,
чтобы синхронные записи не ждали транзакций checkpointer'а `agent.sqlite3`). Задачи APScheduler
(`AsyncIOScheduler`, память) пересобираются из таблицы при старте и при каждой правке.
Планировщик запускается и в standalone, и в составе прокси.

## Запуск

| kind | Что |
|---|---|
| `agent` | прогон агента в треде `routine:<id>` с профилем колонки; в промпте — «фоновое задание, пользователь не ответит» |
| `scenario` | сценарий без модели (`scenario_run`: `scenario_id` и/или фраза `text`) |

Вызовы с подтверждением (класс `external`/`config`/`destructive`) в фоне не выполняются: статус
`needs_confirmation`, вызов — в `pending_calls` (подтверждение в студии), во входящих — заметка.
Лимит `max_runs_per_day` — затем `skipped_budget`. `once` после запуска выключается. AI агент выключен (вкладка «AI агент») — запуски не выполняются, статус `skipped_disabled`.

**Простой сервера:** пропущенный запуск не догоняется — запись `missed` в журнале (как у таймеров);
`once` в прошлом выключается.

## Доставка

| delivery | Колонка в сети, не тихие часы | Иначе |
|---|---|---|
| `speak` | Push `server_action @@mm_semantic_frame` (как напоминание облака, `async_reply.py`): колонка сама запрашивает реплику и получает её с голосом и карточкой; реплика добавляется в голосовой тред `voice:<SN>` ответом агента (`AgentService.note`) — ответ пользователя на вопрос задания понятен модели; тред, ждущий подтверждения, не трогается | `inbox` |
| `inbox` | `inbox` | `inbox` |
| `silent` | только журнал | — |

Непрочитанные входящие владельца подставляются в системный промпт следующего диалога («в начале
ответа коротко передай») и отмечаются прочитанными после ответа.

Проактивный ответ колонке идёт тем же путём, что срабатывание таймера; воспроизведение
физическим устройством не подтверждено (как и для таймеров).

## Инструменты агента

| Инструмент | Класс риска |
|---|---|
| `routine_list` | `read` |
| `routine_create` | `config` — подтверждение; владелец — пользователь колонки, `created_by: agent`, таймзона пользователя |
| `routine_update` | `config` — только свои задания |
| `routine_delete` | `destructive` — только свои задания |

## API (`/emulator/api/routines`)

- `GET` → `{available, routines (с next_run), runs (100), inbox, users, devices, scenarios}`
- `POST {op: 'save', routine}` — без `id` создать, с `id` — изменить переданные поля
- `POST {op: 'delete', id}`, `{op: 'run', id}` — запустить сейчас, `{op: 'read', ids}`
