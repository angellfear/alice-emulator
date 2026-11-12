# LICENSE-NOTICE — сторонний код и происхождение решений

## Клиент аккаунта Яндекса (первичный конфигуратор, «Подтянуть из аккаунта»)

`server_emulator/account.py` — авторизация и обращения к iot-API перенесены по
образцу Home Assistant-интеграции **AlexxIT/YandexStation**
(<https://github.com/AlexxIT/YandexStation>), лицензия **MIT**
(<https://github.com/AlexxIT/YandexStation/blob/master/LICENSE>).

Перенесённые приёмы (файлы-источники на момент переноса, 2026-11-12):

- QR-вход (`core/yandex_session.py`: `get_qr`, `login_qr`) — страница
  `passport.yandex.ru/pwl-yandex` (CSRF), `api/passport/auth/password/submit`
  (создание трека), `api/passport/auth/magic/code` (ссылка QR-кода), опрос
  `magic/code/status` до `otp_auth_finished`, `api/passport/sessions/get_session`.
- Cookies → x_token (`login_cookies`) — `mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid`
  с заголовками `Ya-Client-Host`/`Ya-Client-Cookie`; публичные client_id/client_secret
  клиента Яндекса из того же файла.
- Проверка токена (`validate_token`) — `1/bundle/account/short_info/`.
- Вход по x_token в браузерную сессию (`login_token`) — `1/bundle/auth/x_token/`
  → `{passport_host}/auth/session/?track_id` (без редиректов, `Location` с `/auth/finish`).
- CSRF страницы iot и обращения к iot-API (`core/yandex_session.py: request`,
  `core/yandex_quasar.py`) — `yandex.ru/iot` (`csrfToken2`), заголовок `x-csrf-token`
  на изменяющих запросах, `iot.quasar.yandex.ru/m/v3/user/devices` (дом и устройства),
  `/m/v2/user/devices/<id>/configuration` (quasar_config колонки).

Перенос адаптирован под проект: aiohttp вместо HA-обвязки, секреты — только в
`state/account/`, ответы API — только статус, тексты ошибок без секретов.
Лицензия MIT распространяется на перенесённые приёмы; текст лицензии
YandexStation — по ссылке выше.
