---
title: "Контракт: сценарный движок, каталог и реестр"
status: stable
date: "2026-11-07"
module: "server_emulator/scenario_engine/"
---

# Контракт: сценарный движок, каталог и реестр

Дата: 2026-11-07. Всё, что ниже — CODE_TESTED (автотесты в `tests/`); физическая приёмка на колонке не проводилась.

## 1. Порядок ответа (приоритет)

```
входящая реплика/событие
  └─ агент ждёт «да/нет» на своё действие? → ответ агенту (движок graph, docs/contracts/agent.md)
  └─ ожидающий диалог (ask-уточнение)? → продолжение своего сценария (continuation раньше роутинга)
  └─ probe (side-effect-free, H02): supported / unsupported / ambiguous / rejected + reason code
       └─ supported → executor сценария (без обращения к AI-модели)
       └─ иначе → AI-модель (chat) с tool_calls alice_action (если settings.model_actions)
```

Агент (движок graph) знает каталог: индекс сценариев в системном промпте, инструменты
`scenario_list/get/probe/run`. Поле сценария `risk` (`read|device|external|config|destructive`,
по умолчанию `device`) задаёт, нужно ли подтверждение, когда сценарий запускает агент.

Каталог builtin включается `settings.builtin_scenarios`, отдельные встроенные сценарии — `settings.builtin_disabled` (аддитивная миграция дефолтов доносит ключи в старые БД). Выключенные (`enabled:false` / id в `builtin_disabled`) из роутинга исключаются.

Шаблонные кандидаты упорядочены каталогом (приоритет); ранжирование точности
(сущность из закрытого списка сильнее свободного хвоста, больше литеральных кусков
текста сильнее — метрики hassil) выбирает самое точное совпадение **внутри**
сценария и для записей справочника фраз плагинов (`contributes.providers[].phrases`,
запись с `handler: core:<сценарий>` — кандидат в сценарий-обработчик). Споры записей
разных плагинов при равной точности — ручной порядок `settings["phrase_order"]`
(порядок видов и плагинов в виде; «Прочее» — плагины без вида); сухой прогон
(«Проверка») показывает порядок кандидатов (`candidates`).

## 1a. Поставщики данных для колбэков

Служебные колбэки колонки маршрутизируются во внутренние события (`informers.requested`,
`timesync.requested`, `callbacks.ack`, … — aapi_handlers.py), ответ собирает сценарий-поставщик
с триггером `{"kind": "provide", "event": …}`. Пользовательский сценарий с тем же событием
перекрывает встроенного поставщика; поставщика нет — колбэк отвечает умолчанием (тихое
подтверждение). Триггер `kind: event` сохранён для расширений и сценариев из промахов.

## 2. Классы сценариев

Класс задаёт, откуда сценарий берёт фразы и данные (обзор — `docs/architecture.md`,
«Сценарии: встроенные и от плагинов»). Колонка «Сейчас» — где фразы живут сегодня.

**Встроенные** — фразы и логика в ядре, плагин не нужен; данные — из запроса колонки
(`EnvironmentStateRaw`, `ctx.player`) и эмулятора:

| id | фразы | исполнение | тесты |
|---|---|---|---|
| informers.clock | «сколько времени», «который час» | local_time_phrase, карточка | test_builtin_scenarios |
| informers.clock.date | «какое сегодня число», «какой день недели» | дата в таймзоне колонки | test_chat_scenarios |
| timers.timer.set | «поставь таймер на N…» | `open_screen_timer_info_directive` (или `open_screen_timer_list_directive{ForceAutoClose}`, если список уже открыт) + `timer_set_directive` по образцу; уточнение при отсутствии длительности | test_time_scenarios |
| timers.timer.list | «сколько осталось до таймера», «какие таймеры установлены» | остаток из состояния колонки (`Duration − (now − StartTimestamp)`), `open_screen_timer_list_directive{TargetTimerIds}` | test_time_scenarios |
| timers.timer.cancel | «отмени таймер» | `open_screen_timer_list_directive{ForceAutoClose}` (если список открыт) + `timer_cancel_directive{TimerId}` | test_time_scenarios |
| timers.alarm.set | «поставь будильник на HH:MM» | `open_screen_alarm_list_directive{ForceAutoClose}` + `alarm_add_directive` (одна дата активации, таймзона колонки, SnoozeTime 10) | test_time_scenarios |
| timers.ringing.stop | «хватит» | у колонки `CurrentlyPlaying`/истёкший активный таймер → `timer_stop_directive`/`alarm_stop_directive{StopMedia}` + закрытие экрана; не звенит — отказ (реплику берёт `device.player.pause`) | test_time_scenarios |
| device.volume | «громче/тише/громкость на N» (шкала 0–10) | `sound_set_level`/louder/quiter: директива ответа или Push в сессию колонки | test_simple_commands |
| device.player.pause | «пауза», «останови музыку» | директивы настоящего сервера | test_builtin_templates |
| device.bluetooth.on/off | «включи/выключи блютуз» | директивы по образцу | test_simple_commands |
| device.led.emotion | «подмигни», «покажи глазки» | LED-директива | test_led_emotion |
| device.apps.list | «какие приложения установлены?» | перечень из внутреннего списка `runtime.installed_apps` формулировкой записи .85 (текст + карточка); пустой список — честно о состоянии эмулятора | test_device_apps |
| device.app.open | «открой/запусти X» | резолв по внутреннему списку `runtime.installed_apps` (пока пуст — автоматического получения нет); запуск ютуба — тихий ответ с divui_show_view и webview-картой (app_cards/youtube.json, запись .85 2026-11-10), прочие приложения — честная карточка «запуск не поддержан» (образцов запуска нет) | test_device_apps |
| system.* | служебные события, «забудь всё», «функция недоступна» | timesync, callbacks.ack, dialog.reset, agent_say, unavailable | test_builtin_templates |

Таймеры и будильники ведёт колонка: сервер не хранит и не считает их (своего планировщика
нет), ответы — штатные директивы «Часов» (`tests/fixtures/directives/` — обезличенные
образцы журнала).

**Ядро по поставщику** — фразы и ответ колонке в ядре, данные — от поставщика вида
(`docs/contracts/providers.md`):

| id | вид | сейчас | тесты |
|---|---|---|---|
| informers.weather, informers.weather.widget | weather | без поставщика отвечает «Для этого нет источника данных.» | test_weather, test_weather_provider |
| informers.news | news | без поставщика — честный отказ | test_news_scenario |

**Экспорт плагина** — фразы объявляет плагин по своим возможностям, ответ колонке собирает
ядро:

| id | плагин (вид / возможность) | сейчас | тесты |
|---|---|---|---|
| media.music | music; возможности `fairy_tales`, `podcasts`, `audiobooks` | фразы в ядре (`scenarios/media/music.json`), включая голый шаблон «включи {query}» и отсечку `media_other_domain` | test_music_provider |
| кино/сериалы | video | вида нет; фразы — в `system.unavailable` (честный отказ) | — |
| smart_home.light | умный дом (`builtin.home-assistant`) | фразы в файле плагина (`phrases/light.json`: закрытый список ламп из датасета `ha.lights`, падежи — pymorphy3 и словарь `room`; лампы одной площадки — одной командой), сценарий тоже даёт плагин: яркость → `brightness_pct`, исполнение — действие `ha.call_service` (сервисы `light.turn_on`/`turn_off` HA); плагин не настроен или ламп нет — фраз нет; сбой HA при вызове — понятная фраза | test_home_assistant_light |
| lists.shopping | список покупок (`builtin.shopping-list`) | фразы и сценарий даёт плагин (`phrases/lists.json`, handler `core:lists.shopping`, у сценария `"triggers": []`); список хранится по пользователю (`ctx.data`), владелец записи — id сценария, поэтому перенос уже сохранённых списков не трогает; плагин выключен — фраз списка нет | test_chat_scenarios |
| games.games | игры (`builtin.games`) | фразы и сценарий даёт плагин (`phrases/games.json`, handler `core:games.games`, у сценария `"triggers": []`); ответ случайный — монетка, кубик, предсказание; плагин выключен — фраз игр нет; свою игру внешний плагин добавляет своими фразами и сценарием (docs/contracts/extensions.md) | test_chat_scenarios |
| chat.chat | болталка (`builtin.chat`) | фразы и сценарий даёт плагин (`phrases/chat.json`, handler `core:chat.chat`, у сценария `"triggers": []`); плагин выключен — фраз болталки нет | test_chat_scenarios |
| knowledge.knowledge | знания (`builtin.knowledge`) | фразы и сценарий даёт плагин; данные — поставщик вида knowledge с вкладки «Поставщики данных» (контракт docs/contracts/datasets.md), без поставщика — честный отказ; плагин выключен — фраз знаний нет | test_knowledge_scenarios |

Каталог builtin включается `settings.builtin_scenarios`, отдельные встроенные сценарии —
`settings.builtin_disabled`.

Passport provenance — `scenario_engine/builtin.py::BUILTIN_PROVENANCE` (перенесено/адаптировано/неподдержано).

## 3. Executor / flow

Операции: `resolve/action/compose/ask/finish/error` (контракты `FLOW_OPS`, граф-валидация: ссылки next/on_failure, достижимость, выход к терминалу, fallthrough по списку). Ограничения: 64 шага, deadline 300 c, TTL ожидания. Action ID → идемпотентность side-effect. awaiting-диалоги персистентны (StateStore.executions), переживают рестарт. Композеры: `builtin.reply@1` (текст из слота/состояния — база для пользовательских сценариев-ответов).

## 3a. Возможности JSON-сценария (S01+)

**Триггеры phrase**: `name` (слово/предложение) и/или `phrases:[]` (список), режим `match`:
`contains` (по умолчанию — вхождение), `exact` (равенство), `mask` (глоб `*`/`?` по нормализованной
реплике целиком, `*погод*` — вхождение по маске). Матчинг идёт после нормализации ASR
(`normalize.py`: регистр/ё/wake-слово/разглей/однобуквенные огрехи).

**Guard — право отказа**: `guard` на сценарии — декларативное условие над контекстом:
`{all|any|not: …}` и листья `{exists|eq|ne|in: [binding, значение]}`. Условие не сошлось
(например, нет гео пользователя) — сценарий ОТКАЗЫВАЕТСЯ, реплика уходит следующему кандидату
по порядку каталога или в LLM; в отчёте reason `guard_declined:<id>`.

**Контекст запроса**: bindings `{"source":"user","field":"…"}` и `{"source":"device","field":"…"}`
(точечный путь: `location.latitude`, `fields.city`). У пользователя — `location`
({latitude, longitude, city}, профиль) и произвольный `fields` (≤16 КиБ); у устройства —
`room` (комната колонки: «включи свет» без уточнения) и `fields`. Погода берёт координаты
из гео пользователя, при его отсутствии — из параметров пользователя
(параметры погоды, поля широты/долготы).

**compose.reply — ответ без кода**: `{text|speech (строка|binding), speech_prefix, card,
directives:[{name, type?, analytics_type?, is_led_silent?, payload (Struct; в листьях
bindings), payload_raw (base64 — реплей перехваченного wire)}]}`. Реплика озвучивается
голосовым трактом (TTS); `HasVoiceResponse` поднимается только при реальном аудио, как облако.

## 4. Каталог API (`/emulator/api/scenarios`)

- GET → `{scenarios, builtin, builtin_enabled}`; пользовательские — полный рабочий объект (`_group` — путь каталога «clock/watches», любая глубина; `enabled`); `builtin` — полные определения + `title`/`description` (ru/en, `builtin.py::BUILTIN_META`) + `catalog:"builtin"` + индивидуальный `enabled` (главный флаг И id ∉ `builtin_disabled`) + `provenance`.
- PUT `{scenario}` → валидация (формат §4a) → сохранить (замена по id).
- PUT `{id, enabled}` → индивидуальный переключатель без редактора: пользовательские — флаг сценария; `builtin.*` — список `settings.builtin_disabled` (роутинг уважает оба уровня).
- DELETE `?id=` → удалить.
- POST `{text}` → dry-run без устройства: `{handled, verdict?, reason, scenario?, status?, trace}`.

## 4a. Файл на сценарий и поля определения

Каждый сценарий — отдельный JSON-файл `state/scenarios/<id>.json` (полный рабочий объект, включая `enabled`/`_group`). При старте непустой каталог файлов — источник правды (определения можно править при остановленном сервере); каждое сохранение/удаление синхронизирует файлы (запись изменённых, удаление лишних); существующие сценарии выгружаются в файлы при первом старте (миграция). Битые файлы пропускаются, остальные работают.

Опциональные поля определения (валидатор `scenario_packages/validate.py`; формат v4 §10 без изменений для format_version/id/version/triggers/slots/flow/dependencies/publication):

- `title`, `description` — строка или `{ru, en}`; пустая строка внутри = перевода нет; UI показывает ru → en → id/фразы.
- `executor` — декларативный выход: `{kind: reply|flow|mcp_stdio|script|app}` + `server/tool/args/ref/entrypoint/note`. Движок исполняет `reply`, `flow` и `script` (py-файл рядом с JSON, SDK — `docs/contracts/scripts-sdk.md`). `mcp_stdio` и `app` — только объявление привязки: она хранится и показывается, но не исполняется. Инлайн-код по-прежнему запрещён правилами пакета.

UI каталога: плоские группы-каталоги (`builtin`, путь `_group`, «без раздела») — без вложенных аккордеонов; галочка каждого сценария и группы (группа `builtin` — мастер-флаг; включение снимает индивидуальные отключения; смешанные состояния — indeterminate). «Редактировать» открывает окно-редактор (`<dialog>`); встроенного (и сценария расширения) — окно с черновиком копии `user.<имя>`: на диск и в настройки ничего не пишется, «Сохранить» записывает копию (с кодом `.py` у py-сценариев: `GET scenarios?script=<id>` → `PUT {scenario, script}`), затем выключает встроенный; «Отмена» без следов. Установка пакетов сценариев — «Дополнительно → Расширения» (кнопка-ссылка в панели каталога).

## 4b. Правка агентом

Код: `server_emulator/agent/scenario_edit.py`, API `scenario-drafts`, панель «Черновики и предложения
агента» во вкладке «Сценарии» (`ui/scenario_drafts.js`).

Цикл: `scenario_draft` (копия существующего или новый JSON; встроенный → `user.<id>`) →
`scenario_patch` (JSON Patch RFC 6902, ошибка валидатора возвращается модели) → `scenario_test`
(фразы черновика + фразы других сценариев с добавочным словом: не перехвачены и не стали
неоднозначными) → `scenario_activate` (класс `config`, подтверждение) → `scenario_history` /
`scenario_revert`.

Черновики — `state/scenario_drafts/<draft_id>.json` (в роутинг не попадают), версии —
`state/scenario_history/<id>/<n>.json` (прежнее определение перед каждой активацией и откатом).
Активация встроенного копией выключает встроенный (`builtin_disabled`).

Каталог общий для всех колонок: агент в диалоге с пользователем включает только **личный**
сценарий — с guard `{"eq": ["{{user.id}}", <user>]}` (по умолчанию `scenario_draft` ставит его
сам); общий получает статус `proposed` и включается администратором в панели. Откат общего —
только в панели. В студии (без пользователя) агент правит каталог целиком, после подтверждения.

API: `GET scenario-drafts` → `{drafts}`; `POST {op: test|activate|discard|history|revert}`.

## 5. Реестр пакетов — каталог расширений

Отдельного API `registry` больше нет: пакеты сценариев ставятся тем же механизмом, что и расширения
(`docs/contracts/extensions.md`, `server_emulator/extensions/`).

- Манифест прежнего формата `scenario-registry` — `{format, packages:[{id, version, name, path?, url, sha256?}]}` —
  читается каталогом расширений как обычный источник-каталог: пакет → запись каталога (`id`, `version` приводится к
  semver, `name` → описание). `path` (подраздел `_group`) слоем расширений не поддержан — раздел по `category`.
- Установка записи — пакет `scenario-package` (JSON или zip-бандл с `package.json`) разворачивается в data-расширение
  (`extension.json` + `scenarios/<category>/<id>.json`); дальше включение, обновление, откат, удаление — штатные.
  Защиты прежнего реестра сохранены: пакет ≤4 МБ **того же origin**, что и каталог, sha256-пин, traversal в zip.
- Миграция при старте (`extensions/legacy_registry.migrate_registry`, один раз): `settings.registry.url` → источник
  в `settings.extensions.sources` (если подходит: https или loopback), каждый пакет из `registry.installed` →
  расширение (версии журнала лежат на диске, последняя активна, предыдущая — для отката), нетронутые копии
  сценариев пакета убираются из пользовательских, изменённые и выключенные остаются и перекрывают расширение;
  ключ `registry` удаляется.
- `tools/make_registry.py` по-прежнему публикует каталог в этом формате; его URL добавляется как источник расширений.

## 6. Карточка диалога (каркас реальных ответов)

Каждый исходящий AliceResponse финализируется (`response_compose.finalize`): `Cards[i].CardId` (новый на карточку), `Header.SequenceNumber` (монотонный счётчик runtime), `Header.DialogId=""`, директива `tts_play_placeholder {channel:"Dialog"}`. Громкость из модельных действий — директива `sound_set_level {new_level: float}` (wire из перехвата).

## 7. NO_SPEECH (wire реального сервера, по захвату)

Клиент закрыл микрофон `CloseReason=NO_SPEECH` (шёпот: локальный VAD колонки) → сервер: финальный пустой AsrResult → закрытие канала **OK** → реплика «Извините, я не расслышала…» (карточка + голос/TTS). Серверный no-speech — тот же wire.

## 8. Не поддержано (честно)

- Исполнение `executor.kind = mcp_stdio|app` — поле декларативное, привязка хранится, но не запускается.
- Запуск приложений физически (TSendAndroidAppIntentDirective не восстановлен).
- Сигналит и снимает сигнал колонка: срабатываний на сервере больше нет,
  сценарий «сработало» снят. Snooze-перепостановка будильника, отмена и изменение
  будильника, дни недели будильника, пауза/продолжить таймера, таймер сна,
  напоминания, календарь — по образцам не восстановлены (БЛОКЕР, нужна запись).
- Прогноз погоды на дни (только current); локация по GPS устройства.

## 9. Медиа: штатный плеер колонки

Своей медиатеки, очереди и избранного у эмулятора нет. Музыку ищет и отдаёт поставщик
вида music (`media.music`, `docs/contracts/providers.md`), играет штатный плеер колонки
по стеку директив образца. «Стоп/пауза» — директивы настоящего сервера
(`device.player.pause`), колбэки `HollywoodMusic` — тихий `callbacks.ack`. Состояние
плеера — из запроса колонки (`ctx.player`, capability 46). Директивы, состояние и
список БЛОКЕРов — `docs/contracts/player.md`.

## 10. Не реализовано: перенос фраз контента в плагины

Классы из раздела 2 закреплены; для сценариев класса «Экспорт плагина» и «Ядро по
поставщику» не реализовано:

- Объявление плагином возможностей (`capabilities`) и своих фраз; ядро гасит фразы без
  возможности у выбранного поставщика.
- Признак «активен только при поставщике вида» у погоды и новостей (сейчас — отказ).
- Голая фраза «включи {X}»: опрос каталогов плагинов с поиском (выбранный поставщик
  первым, отвечает нашедший); после этого ручная отсечка `media_other_domain` для
  маршрутизации не нужна.
- Вид video.
