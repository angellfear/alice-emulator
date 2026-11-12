# Шаблоны экранных директив

Экранные директивы ответа (`divui_show_view`, `draw_animation_directive`, `divui_hide_view`)
собираются из **шаблонов реестра** (`server_emulator/screen_templates.py`): сценарий вместо
жёстко зашитого JSON берёт шаблон по id. Встроенные шаблоны — образцы настоящего сервера
в `server_emulator/app_cards/*.json`; пользовательские и переопределения встроенных —
`state/screen_templates/<id>.json` (в репозиторий не попадают).

## Формат

Тело шаблона — JSON-документ payload директивы (wire: `PayloadRaw = encode(proto, fields)`):

```json
{"Name": "divui_show_view",
 "Div2Card": {"CardName": "youtube", "Id": {"CardName": "youtube", "CardId": "youtube_key_static_key"},
              "StringBody": {"templates": {}, "card": {"log_id": "…", "states": [ … ]}}},
 "InactivityTimeout": "Infinity", …}
```

Образцы настоящего сервера (`app_cards/*.json`) несут тот же payload в ключе `"directive"`
и метаданные рядом (`"title"` — короткое название для вкладки, `"proves"` — запись-источник
образца, `"name"` — директива конверта, `"analytics_type"`); реестр нормализует обе формы
к `{id, title, name, analytics_type, body}` — `proves` остаётся в файле образца и в
интерфейсе не показывается.

- **Имя конверта** (`name`) — директива ответа: `divui_show_view` →
  `NAlice.TLayeredDivUICapability.TDivUIShowViewDirective`, `draw_animation_directive` →
  `NAlice.TAnimationCapability.TDrawAnimationDirective`, `divui_hide_view` →
  `NAlice.TLayeredDivUICapability.TDivUIHideViewDirective` (proto payload, схема «centaur»).
- **Подстановки** `{{имя}}` — значениями при сборке (`Registry.build(id, values)`);
  неизвестная переменная — ошибка сборки (опечатка не уходит на провод). `<uuid>` —
  свежий request_id (в образце ютуба `log_id` карты == `request_id` webview — один uuid;
  из seeded random — детерминированность эталонов). Переменные собираются из тела
  автоматически; `Div2Card.StringBody` при сборке пересобирается компактной JSON-строкой
  (формат провода образца).

## Приоритет и сброс

**Переопределение > встроенный** (тот же id в `state/screen_templates`); «сбросить»
удаляет переопределение и возвращает образец. Новый id — пользовательский шаблон,
«сбросить» удаляет его целиком. Встроенные: `youtube` (запуск ютуба), `clock_refusal`
(карта отказа «открой часы»), `player_pause_animation` (анимация паузы плеера),
`weather` (карточка погоды weather_day).

Применение: `app_directives.youtube_launch_directive()`/`not_here_directive()` и
`media_directives.draw_player_pause()` собирают директивы через реестр — правка шаблона
в UI меняет ответ сценария `device.app.open` и `device.player.pause`.

## API «Шаблоны» (`ui/api/templates.py`, вкладка «Эмулятор → Шаблоны divui»)

- `GET api/templates` — список `{templates: [{id, name, title, analytics_type, body?…, source, variables}]}`.
- `GET api/templates?id=<id>` — шаблон целиком.
- `PUT api/templates` `{id, body, title?, name?, analytics_type?}` — сохранение с валидацией:
  JSON-структура по имени директивы (`Div2Card.Id.CardId`, `StringBody.card.log_id`,
  непустой `states`; у `draw_animation_directive` — непустой `Animations[{S3Directory{Bucket,Path}}]`),
  ошибки — с путём в JSON. Встроенный id → переопределение, новый → пользовательский.
- `POST api/templates` `{op: "reset", id}` — сброс к встроенному.
- `POST api/templates` `{op: "send", id, sn, values}` — сборка директивы и Push в живую
  сессию SN (`actions.push_directive`, `runtime.send_message`): проверка шаблона на реальной
  станции; ответ `{delivery: "sent"|"no_session"}`.

UI (`ui/templates.js`): список — шаблон (id и короткое название), директива, переменные,
источник и кнопка «Изменить» у каждой строки. Редактор — один диалог за раз (повторное
открытие заменяет его, закрытие удаляет из DOM): JSON шаблона (CodeMirror) рядом с
живым предпросмотром DivKit — правка кода или значений переменных перерисовывает
карточку; переменные, выбор колонки, ошибки и кнопки («Сохранить», «Сбросить» у
переопределения, «Отправить на колонку») — в закреплённом низу диалога.

## Предпросмотр (DivKit web)

Готовый рендерер **DivKit web — `@divkitframework/divkit` 33.11.0**, браузерная сборка
`dist/browser/client.js` (471 822 байта, sha256 `b79ff49bdf1d6e16b1fbe6947bcc4721a92a7fa925309a50f7f44fdd76a6b3e6`),
лицензия **Apache-2.0**; файл — `ui/vendor/divkit.min.js`, подключается в предпросмотре
через `<script>` без сборки, глобал — `window.Ya.DivKit` (метод `render({id, target, json})`,
`json` — конверт `{templates, card}`; сборка `dist/client.js` — CommonJS для сборщиков,
в браузере не работает). Версия и контрольная сумма зафиксированы в этом разделе.
Предпросмотр рендерит `{templates, card}` из `StringBody` (глобальные шаблоны иконок
`GlobalTemplates` подмешиваются в `templates`) в sandbox-iframe (`allow-scripts`);
скрипты — в конце `body` (в `head` `document.body` ещё нет — target получал undefined),
без `dist/client.css` (он схлопывает корень рендера до 1px); первый рендер — после
раскладки диалога (DivKit измеряет target при старте). Канва превью — экран станции
720×1280, масштабируется в панель. Ограничения: `div-action://` и `centaur://` колбэки
станции в вебе не исполняются, webview-карты (ютуб) в sandbox-iframe не рисуются —
предпросмотр показывает визуал карточки.
