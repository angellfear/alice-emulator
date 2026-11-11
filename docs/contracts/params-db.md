---
title: "Параметры: БД, разделы UI, роли"
status: stable
date: "2026-11-11"
module: "server_emulator/state_store.py"
---

# Параметры: БД, разделы UI, роли

Параметры (wifi_hash, гео, серверные/девайсные) — данные в БД с каталогом,
а не ad-hoc поля.
Разделы UI: пользовательский (ручной ввод своих) и продвинутый (серверные и
девайсные). Роли: админ правит всё у всех; пользователь в своей веб-морде —
только разрешённое (не все серверные/девайсные параметры доступны пользователю).

## Хранение: память и зеркало в файл

`StateStore` (`state/emulator.sqlite3`; `ParamStore`, реестр устройств, executor и данные сценариев
работают поверх его соединения) читает и пишет в памяти: при открытии файл копируется в
`sqlite3 :memory:`, запросы идут туда синхронно и без дискового ввода-вывода — цикл событий не
блокируется, API вызывающего кода не async. Каждая закоммиченная транзакция памяти ставится в
очередь, воркер на `aiosqlite` применяет её к файлу (WAL, `foreign_keys=ON`, один writer, порядок
сохраняется). Вне цикла событий (старт процесса до `asyncio.run`, инструменты, sync-тесты) транзакция
пишется в файл сразу на sync-соединении. `await store.flush()` дожидается очереди, `await store.close()`
дорабатывает её и закрывает соединения; аварийный выход теряет только ещё не записанные транзакции
(миллисекунды). `sqlite3.connect` в модуле — два места: снимок файла при открытии и запись вне цикла.

## Каркас данных (2026-10-27, для сценариев)

| scope | раздел (category) | что | владелец |
|---|---|---|---|
| user | profile | о пользователе: name, location.latitude/longitude/city/city_in, timezone | `user:<id>` |
| user | service | служебное пользователя: wifi_hash | `user:<id>` |
| device | info | о колонке: name, aliases (имена для команд между колонками), room, location.*, timezone | `device:<sn>` |
| device | service | служебное колонки (glagol и пр.) | `device:<sn>` |
| platform | info / service | по платформе (`platform.title`, `platform.display`) | `platform:<name>` |
| server | common / service | общее: timezone, weather.max_age_hours (свежесть записи), weather.refresh_timeout (ожидание поставщика) | `server` |

Цепочка разрешения: **device → user → platform → server → default_value** по
ключу без префикса области. Сценариям — `params.<scope>.*` и `params.effective.*`
(`ParamStore.context`), секреты в контекст не попадают.
Данные сценариев и плагинов — отдельная таблица `scenario_data`
(docs/contracts/scenario-json-v2.md). UI каркаса — позже.

## Принцип

Параметр = запись каталога (определение: ключ/тип/роль/подпись) + значение у
владельца. Редакторы рендерятся по определениям каталога. Значения каталога
вкладываются в корни подстановок Quasar-конфига (`context_for` в
quasar_config.py: `user.wifi_hash`, `device.glagol_cert`, `server.*` — ключ
через точку; пути, занятые реестром, не перезаписываются).
Поставляемые замены Quasar-конфига ссылаются на `user.wifi_hash`,
`user.location.latitude/longitude`: параметр
с определением, но без значения на всех уровнях цепочки — замена пропускается,
в шапке UI закреплённое уведомление «не заполнен параметр X», пока значение
не задано (docs/contracts/quasar-config.md, «Новая установка»).

## БД (additive, emulator.sqlite3)

    param_defs(                 -- каталог: ЧТО есть параметр (админ/система)
      key TEXT PRIMARY KEY,     -- 'user.wifi_hash' | 'device.lamp.scheme' | 'server.timezone'
      scope TEXT NOT NULL,      -- user | device | server
      category TEXT NOT NULL,   -- группа UI: 'user' | 'server' | 'device' | произвольная
      user_editable INT DEFAULT 0,  -- роль: пользователь может править в своей морде
      type TEXT NOT NULL,       -- string | number | bool | select | text-list | secret
      title TEXT, hint TEXT,    -- подпись/пояснение (ru) для форм
      options TEXT,             -- JSON: варианты select, min/max/pattern
      default_value TEXT        -- JSON: значение по умолчанию
    )
    param_values(               -- значения: ГДЕ установлен
      owner TEXT NOT NULL,      -- 'user:<id>' | 'device:<sn>' | 'server'
      key TEXT NOT NULL,
      value TEXT NOT NULL,      -- JSON
      updated_ts REAL,
      PRIMARY KEY(owner, key)
    )

Разрешение `ParamStore.resolve(key, user_id, sn, platform)`: param_values по
цепочке **device → user → platform → server → default_value** (наследование:
устройство перекрывает пользователя, пользователь — серверный дефолт).

Каталог-сид: user.wifi_hash, user.location.latitude/longitude (editable);
device.name/room (editable); server.* — стартовые серверные (задел, пополняет
админ). Таймзона дома, имя дома, часовой пояс отчётов — первые кандидаты server.

## API

Админ (existing admin-панель, X-Emulator):
- `GET/PUT/DELETE /api/params` — каталог определений (создание/правка defs);
- `GET /api/param_values?owner=user:local-alex` — значения любого владельца;
- `PUT /api/param_values {owner, key, value}` — установка с валидацией по def
  (тип, enum/min-max, secret-маска __KEEP__).

Пользовательский кабинет (роль user, привязка principal → identities_user уже
в AuthStore):
- `GET /api/me/params` — только defs c user_editable=1 своих scope'ов
  (user + свои device), со своими значениями; серверные и не-editable НЕ видны;
- `PUT /api/me/params {key, value}` — валидация + только свои owner.

Публичный режим (auth.json нет) — как сейчас: всё доступно («админ без входа»).

## UI

Админ-панель, «Эмулятор → Параметры» — две подвкладки (сегменты в шапке раздела):
- **«Определения»** — каталог ключей, сгруппированный по области (user/device/server,
  у группы одно пояснение: «Пользователь — у каждого пользователя своё значение» и т. п.);
  добавление и правка определения — `<dialog>`; значок «правит владелец» у user_editable;
- **«Значения»** — выбор владельца (пользователь / устройство / платформа / сервер + конкретный
  из списка), форма значений по определениям его области, контролы по типу; секреты — маска
  `__KEEP__`. Для устройства — колонки **«Итог»** и **«Откуда»**: итоговое значение каждого
  ключа и уровень цепочки, откуда оно взято (`GET /api/param_values?owner=device:<sn>&resolve=1`);
  клик по уровню переключает владельца.
«Пользователи» → карточка «Параметры» — быстрый доступ к тем же значениям (без изменений
поведения); кнопка «Все параметры» ведёт в «Значения» с этим владельцем.
Пользовательская морда (кабинет, /me):
- профиль: свои user-параметры (форма по type);
- «Мои устройства»: список (DeviceKit) с их editable-параметрами.

## Роли (матрица)

| | каталог defs | свои user/device editable | любые значения | server-scope |
|---|---|---|---|---|
| admin | CRUD | да | да | да |
| user | нет | да (только свои owner) | нет | не виден |

## Реализация

Живая цепочка параметр→шаблон→колонка подтверждена
(X00REC sync 200; wifi_hash/гео собираются из param_values):
- server_emulator/params.py: ParamStore — каталог param_defs (валидация ключей/
  типов/select-вариантов/числовых пределов), значения param_values, секреты
  (маска __KEEP__ при чтении, запись маски сохраняет прежнее), разрешение
  resolve цепочкой device → user → platform → server → default_value,
  идемпотентный сид каталога.
- Значения каталога — в корнях подстановок Quasar-конфига (context_for);
  glagol_security (quasar.py) читает device.glagol_cert/glagol_key напрямую
  из стора.
- Админ-API: params (GET/PUT/DELETE каталога), param_values (GET ?owner=,
  PUT с валидацией). Раздел «Параметры»: таблица определений, диалог
  создания/правки, серверные значения. Карточка пользователя («Пользователи»
  → «Параметры»): простая форма editable user-scope + «Продвинутое» — все
  user-параметры и параметры его устройств (замок на не-editable).
- Кабинет /me (server_emulator/ui/me.html): вход (AuthStore), свои editable
  user-параметры и параметры своих колонок; /api/me/params GET/PUT —
  только user_editable user/device, только свои owner, серверное — 403/невидимо;
  мидлварь пускает /api/me/* под любой ролью со сессией, админ-API для user
  по-прежнему 403.

## Сертификаты Glagol

- device.glagol_cert / device.glagol_key — scope=device, type=secret,
  category=glagol; значения всех известных станций перенесены из реальных
  ответов облака (get_sync_info → glagol.security); glagol_security (quasar.py)
  читает их напрямую из стора — заданные параметры сильнее автогена
  (state.glagol_security). Алгоритм генерации новых сертификатов облаком
  не восстановлен (см. docs/contracts/quasar-config.md).
