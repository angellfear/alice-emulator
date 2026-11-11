# quasar_defaults — встроенный дефолт /get_sync_info (источник generated)

Собран разовым скриптом из исходников https://github.com/v3rt3p/quasar-server (MIT,
атрибуция в NOTICE.md), commit `fefcd58803012c61f8a9f8c88351dabff1305c81`.

- `common.json` — полный дефолт ответа без захвата: config (account_config /
  device_config / system_config) + alice_pro_subscription + subscription + tags.
  Персональных значений нет: device_config.name — токен `{{device.name}}`, заполняется
  динамикой compose; wifi_hash и координаты — примеры исходного шаблона.
- `platforms/<платформа>.json` — merge-patch поверх common.json: chiron/magritte —
  квирк supported_frames-объект и удаление hybrid_voice_inputs, остальные платформы —
  заготовки `{}`.
- `rules/common.json`, `rules/platforms/<платформа>.json` — поставляемые замены поверх шаблона
  (`{"rules": [...]}`, формат и правки поверх них из UI — docs/contracts/quasar-config.md). В
  общих — только одинаковое для всех платформ по ответам настоящего сервера; значения одной
  платформы — в её файле; значения колонок и аккаунтов сюда не попадают. Скриптом не
  пересобираются.

Пересборка и сверка порта с эталонами оригинального кода —
`python tools/build_quasar_defaults.py <клон>` (шаги в docstring скрипта; клон во
временной папке вне репозитория, зависимости ставятся только с `--ignore-scripts`).
