---
title: "Параметры: БД, разделы UI, роли"
status: draft
date: "2026-10-25"
module: "server_emulator/state_store.py"
---

# Параметры: БД, разделы UI, роли (план)

Задача (2026-10-25): параметры (wifi_hash, гео, будущие серверные/девайсные) —
данные в БД с каталогом, а не ad-hoc поля (сейчас users.fields×2 + location).
Разделы UI: пользовательский (ручной ввод своих) и продвинутый (серверные и
девайсные). Роли: админ правит всё у всех; пользователь в своей веб-морде —
только разрешённое (не все серверные/девайсные параметры доступны пользователю).

## Каркас данных (2026-10-27, для сценариев)

| scope | раздел (category) | что | владелец |
|---|---|---|---|
| user | profile | о пользователе: name, location.latitude/longitude/city/city_in, timezone | `user:<id>` |
| user | service | служебное пользователя: wifi_hash | `user:<id>` |
| device | info | о колонке: name, aliases (имена для команд между колонками), room, location.*, timezone | `device:<sn>` |
| device | service | служебное колонки (glagol и пр.) | `device:<sn>` |
| platform | info / service | по платформе (`platform.title`, `platform.display`) | `platform:<name>` |
| server | common / service | общее: timezone, weather.url, weather.max_age_hours, weather.refresh_timeout | `server` |

Цепочка разрешения: **device → user → platform → server → default_value** по
ключу без префикса области. Сценариям — `params.<scope>.*` и `params.effective.*`
(`ParamStore.context`), секреты в контекст не попадают. Старые разделы
(user/device/glagol/server) мигрируются при сиде (`LEGACY_CATEGORIES`).
Данные сценариев и плагинов — отдельная таблица `scenario_data`
(docs/contracts/scenario-json-v2.md). UI каркаса — позже.

## Принцип

Параметр = запись каталога (определение: ключ/тип/роль/подпись) + значение у
владельца. Редакторы рендерятся по определениям (как инлайн-узлы конфигов).
`$param` в шаблонах Quasar читает этот стор — единая точка resolve_param уже есть.

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

Разрешение `$param(key, device, user)`: param_values по цепочке
**device → user → server → default_value** (наследование: устройство
перекрывает пользователя, пользователь — серверный дефолт).

Миграция: users.location/users.fields → param_values(user:…) + defs из сида;
legacy-поля реестра остаются (не ломать), но источник правды — param_values.
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

## Этапы

1. Таблицы + ParamStore (CRUD/валидация) + resolve_param через стор (цепочка
   device→user→server→default) + сид-миграция + тесты.
2. Админ-API (params/param_values) + тесты ролей (admin ok, user 403, публичный).
3. UI админа: параметры в карточке пользователя + раздел «Параметры».
4. /api/me/* + страница кабинета (/me) + интеграция ролей AuthStore + тесты.

## Вопросы (см. чат)

1. Каталог defs пополняет только код — или админ заводит параметры через UI?
2. Пользовательская морда — страница этого же сервера (/me)?
3. Секретные параметры (токены): маскирование `__KEEP__` как у api_key?

## Статус реализации (2026-10-25 18:15)

Готово, 429 тестов зелёные, живая цепочка параметр→шаблон→колонка подтверждена
(X00REC sync 200; wifi_hash/гео собираются из param_values):
- server_emulator/params.py: ParamStore — каталог param_defs (валидация ключей/
  типов/select-вариантов/числовых пределов), значения param_values, секреты
  (маска __KEEP__ при чтении, запись маски сохраняет прежнее), разрешение
  $param цепочкой device → user → server → default_value, идемпотентный сид
  (каталог + перенос legacy location/fields).
- resolve_param: сначала стор параметров, затем legacy-реестр; шаблон common
  переведён на канонический ключ $param user.wifi_hash.
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

## Дополнение (2026-10-26): сертификаты Glagol

- device.glagol_cert / device.glagol_key — scope=device, type=secret,
  category=glagol; значения всех известных станций перенесены из реальных
  ответов облака (get_sync_info → glagol.security). Ссылки в common.json —
  `$param`. Алгоритм генерации новых сертификатов — будущая работа
  (см. docs/contracts/quasar-config.md).
