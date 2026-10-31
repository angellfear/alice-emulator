# alica_emule

Эмулятор сервера Яндекс Алисы: принимает HTTP/WebSocket-трафик колонок (Quasar, UniProxy, AAPI),
ведёт голосовой диалог (ASR, сценарии, TTS), поддерживает расширения и py-сценарии на SDK.

## Быстрый старт

Python 3.11+.

```powershell
python -m pip install -e ".[test]"
python -m server_emulator.server
```

На Windows — с иконкой в трее: `start.bat` в корне (Python из `.venv`, иначе `pythonw` из PATH);
ярлык на рабочем столе — `powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Desktop`. UI: **http://127.0.0.1/emulator/**. Порты по
умолчанию — 443 (TLS) и 80, настраиваются в «Дополнительно → Сервер» (`state/server.json`). Каталог данных —
`--state <папка>` (по умолчанию `server_emulator/state/`, не попадает в Git).

### Первый запуск

Описания протокола Алисы и картинки устройств в репозиторий не входят. При первом запуске UI открывает окно
«Настройка эмулятора»:

1. **Картинки устройств** (необязательно) — скачиваются с сайта умного дома Яндекса только после галочки
   «ознакомлен(а): права на картинки принадлежат ООО «Яндекс»»; без них показываются силуэты.
2. **Протокол** (обязательно) — `libquasar_daemons.so` из APK станции (`centaur-app-launcher.apk`, папка
   `lib/arm64-v8a/`) или zip-архив с `.proto` / `.pb`.

Протокол разбирается локально, файл никуда не отправляется и после разбора удаляется. Без UI:
`python -m server_emulator.protocol_import <файл>` и `python -m server_emulator.device_images --accept`.
Подробнее — [docs/setup.md](docs/setup.md).

На устройстве `quasar.local`, `uniproxy.local`, `rpc.local` должны разрешаться в LAN IP компьютера
с эмулятором. Подробнее: [docs/setup.md](docs/setup.md), [server_emulator/README.md](server_emulator/README.md).

## Тесты

```powershell
python -m pytest tests/ -q
```

Тесты используют только локальные серверы, без обращения к Яндексу. Тестам протокола нужны схемы: задайте
`ALICA_PROTOCOL_SOURCE=<libquasar_daemons.so или zip>` (схемы соберутся один раз) — без них такие тесты пропускаются.

## Документация

- [Архитектура](docs/architecture.md) — подсистемы, потоки данных, зависимости
- [Установка и настройка](docs/setup.md)
- [Контракты](docs/contracts/) — спецификации протоколов, сценариев, расширений и API

## Лицензия и атрибуция

Атрибуция v3rt3p/quasar-server (MIT): [server_emulator/NOTICE.md](server_emulator/NOTICE.md).
