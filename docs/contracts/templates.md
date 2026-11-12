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
и метаданные рядом (`"proves"` — источник-запись, `"name"` — директива конверта,
`"analytics_type"`); реестр нормализует обе формы к `{id, title, name, analytics_type, body}`.

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
(карта отказа «открой часы»), `player_pause_animation` (анимация паузы плеера).

Применение: `app_directives.youtube_launch_directive()`/`not_here_directive()` и
`media_directives.draw_player_pause()` собирают директивы через реестр — правка шаблона
в UI меняет ответ сценария `device.app.open` и `device.player.pause`.

## API «Шаблоны» (`ui/api/templates.py`, вкладка «Эмулятор → Шаблоны»)

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

UI (`ui/templates.js`): список (шаблон/директива/переменные/источник), редактор JSON
(CodeMirror), значения переменных, «Предпросмотр» и «Отправить на колонку».

## Предпросмотр (DivKit web)

Готовый рендерер **DivKit web — `@divkitframework/divkit` 33.11.0**, браузерный бандл
`dist/client.min.js` (461 323 байта, sha256 `98c444b1ba62c48082246ca9b31d0c94a5bbf74052fcc78e7531d7a8f3370fd0`),
лицензия **Apache-2.0**; файл — `ui/vendor/divkit.min.js`, подключается в предпросмотре
через `<script>` (CommonJS-глобал: `window.exports` shim) без сборки, версия и контрольная
сумма зафиксированы в этом разделе. Предпросмотр рендерит `{templates, card}` из
`StringBody` в sandbox-iframe (`allow-scripts`); ограничения: `div-action://` и
`centaur://` колбэки станции в вебе не исполняются — предпросмотр показывает визуал.
