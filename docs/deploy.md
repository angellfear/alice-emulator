# Развёртывание

Два способа запускать эмулятор: на своей машине (как в [setup.md](setup.md)) и службой на
Linux-сервере, чтобы он работал постоянно. Этот документ — про второй.

Эмулятор нужен станции в любой момент: колонка обращается к облаку сама, и если эмулятор не
запущен, она идёт в Яндекс мимо него. Поэтому «запустил из терминала и закрыл» не годится — либо
трей с автозапуском на десктопе (Windows), либо служба на сервере (Linux).

## Кому что подходит

| Сценарий | Как запускать |
|---|---|
| Своя машина: Windows, macOS, десктопный Linux | `install.*` + `start.*`; Windows — трей и автозапуск, см. [setup.md](setup.md) |
| Linux-сервер, мини-ПК, Raspberry Pi: эмулятор работает всегда | systemd, этот документ |

Служба ставится на тот же Linux-хост, на который вы перенаправляете трафик станции (DNS или
роутер). Порты по умолчанию — 443 (TLS для станции) и 80 (панель), поэтому на хосте они должны
быть свободны: если их уже занял nginx или другой веб-сервер, его нужно остановить или
перенастроить.

## Linux + systemd

### 1. Пользователь и код

Служба работает не от root — отдельный системный пользователь, которому принадлежат клон и папка
данных. Право слушать порты 80/443 выдаётся юнитом (см. ниже), а не правами root.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin alice-emulator
sudo mkdir -p /opt/alice-emulator
sudo chown alice-emulator: /opt/alice-emulator
sudo -u alice-emulator git clone https://github.com/angellfear/alice-emulator.git /opt/alice-emulator
cd /opt/alice-emulator
sudo -u alice-emulator python3 -m venv .venv
sudo -u alice-emulator .venv/bin/python -m pip install -e ".[emulator]"
```

Нужны `python3.11+`, `python3-venv` и `git` (git — только для первого клона: обновления работают
без него). Установка **editable** обязательна: при обновлении сервер переключает рабочую копию на
новый тег, и код подхватывается без переустановки.

Если у вас уже есть клон и `.venv` (ставили через `install.sh`), можно обойтись без шагов с
`git clone`/`venv` — важно только, чтобы клон и `.venv` принадлежали пользователю службы.

### 2. Юнит

```bash
sudo cp /opt/alice-emulator/deploy/systemd/alice-emulator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alice-emulator
```

В юните поправьте под себя, если ставили не в `/opt/alice-emulator`:

- `WorkingDirectory` и `ExecStart` — путь клона и его `.venv`;
- `User` и `Group` — владелец клона и папки данных;
- `Documentation` — путь к этому файлу.

Папку данных (`--state /var/lib/alice-emulator`) создаёт сам systemd по `StateDirectory=`,
владелец — `User`, права 0750. Менять путь можно в `ExecStart`; тогда поправьте `StateDirectory`.

**Почему `ExecStart` — `server_emulator.service`, а не `server_emulator.server`.** При обновлении
сервер завершается кодом 76 и ждёт, пока отдельный процесс `self_update/updater.py` сделает бэкап
`state/`, переключит клон на новый тег и при необходимости обновит зависимости. Под systemd
поднимать сервер должен менеджер служб — поэтому сервер видит `INVOCATION_ID` и не запускает себя
сам. Но `Restart=always` на месте сервера поднял бы **старую** версию сразу после выхода, пока
updater ещё переключает файлы. `server_emulator.service` — маленький супервизор: запускает сервер
дочерним процессом, после кода 76 ждёт итог в `/var/lib/alice-emulator/update/plan.json` и
запускает сервер снова, уже с новым кодом. Подробности — в «Самообновлении»
[architecture.md](architecture.md).

### 3. Первый запуск

```bash
systemctl status alice-emulator
journalctl -u alice-emulator -n 30
curl -s http://127.0.0.1/api/status
```

Панель — `http://<адрес-сервера>/emulator/`. Дальше как в [setup.md](setup.md):

1. **Сертификат на имена Яндекса** — «Дополнительно → Сервер» (файл загружается в панели) или
   `--cert`/`--key` в `ExecStart`. Без сертификата сервер поднимается без TLS: панель работает,
   станция по TLS — нет, в журнале будет ошибка про порт 443.
2. **Протокол** — `libquasar_daemons.so` из APK станции: окно настройки в панели или
   `sudo -u alice-emulator /opt/alice-emulator/.venv/bin/python -m server_emulator.protocol_import <файл>`.
   Пока протокола нет, WS и протокольные HTTP-обработчики отвечают `setup_required`.
3. **Перенаправление трафика станции** на этот хост (DNS/роутер) и доверие сертификату на станции —
   это вне эмулятора, см. [setup.md](setup.md).

Права на порты: в юните `AmbientCapabilities=CAP_NET_BIND_SERVICE` и
`CapabilityBoundingSet=CAP_NET_BIND_SERVICE` — этого достаточно для 80/443 без root. Если порты
уже заняты, лучше освободить их, а не менять: станция ходит именно на 443.

### 4. Обновления

Кнопка «Обновление vX.Y.Z» в панели работает и под systemd, в том же ручном режиме, что и на
десктопе (автоматически ничего не ставится). Что происходит:

1. сервер пишет план, запускает updater и завершается с кодом 76;
2. updater ждёт выхода сервера, бэкапит `state/` в `state/backups/` (хранится 5), тянет теги,
   переключает клон на новый тег, при изменении `pyproject.toml` обновляет зависимости;
3. супервизор видит итог (`ok` или `failed` откат на прежний тег) и запускает сервер снова.

В журнале службы это видно построчно:

```text
service: сервер запущен (pid 1234)
service: идёт обновление — жду updater
service: обновление: готово — запускаю новую версию
service: сервер запущен (pid 2345)
```

`git pull` вручную не нужен; откат на предыдущую версию — кнопкой в том же окне. Если супервизор
не дождался итога (updater умер), он поднимает сервер как есть — прежнюю версию, и обновление можно
повторить из панели.

### 5. Данные и бэкап

Меняется только папка данных (`--state`): sqlite-базы, `certs/`, `scenarios/`, `extensions/`,
`endpoints/`, `animations/`, журналы, `quasar/`, `update/` и `backups/`. Код живёт в `/opt` и
меняется тегами.

Бэкап — короткая остановка и архив:

```bash
sudo systemctl stop alice-emulator
sudo tar czf alice-state-$(date +%F).tgz -C /var/lib alice-emulator
sudo systemctl start alice-emulator
```

Копировать sqlite «на горячую» нельзя — либо останавливать службу, либо `sqlite3 ... ".backup"`,
либо снапшот диска. Перед каждым обновлением бэкап `state/` делает сам updater.

### 6. Безопасность

- **Панель по умолчанию открыта.** Без файла `auth.json` вход не требуется, а порт 80 смотрит в
  сеть: включите вход, прежде чем ставить службу в общую сеть.

  ```bash
  sudo systemctl stop alice-emulator
  sudo -u alice-emulator /opt/alice-emulator/.venv/bin/python -m server_emulator.auth --state /var/lib/alice-emulator
  sudo systemctl start alice-emulator
  ```

  Команда один раз печатает пароль администратора; дальше панель требует вход.
- Не выставляйте порты наружу без VPN (WireGuard/Tailscale) или обратного прокси с авторизацией: в
  папке данных — трафик колонок, токены и данные аккаунтов. Это исследовательский инструмент, не
  публичный сервис.
- Сертификат на имена Яндекса — ваш; в репозитории его нет и не будет.
- Юнит уже ограничивает службу: отдельный пользователь, `PrivateTmp=yes`, `ProtectSystem=full`
  (`/usr`, `/boot`, `/etc` только на чтение; `/opt` и `/var/lib` — на запись, иначе не работало бы
  самообновление). Не включайте `ProtectSystem=strict` — оно сломает обновление.

### 7. Диагностика

| Симптом | Куда смотреть |
|---|---|
| Служба не стартует | `journalctl -u alice-emulator -n 50`: чаще всего `.venv` собран другим Python или клон не принадлежит `User` |
| `порт 443: [Errno 98] Address already in use` | порт занят (nginx, другой эмулятор); освободите его |
| `порт 443: [Errno 13] Permission denied` | с юнита сняли `AmbientCapabilities`/`CapabilityBoundingSet` (например, через `systemctl edit`) — верните их |
| Панель открывается, станция молчит | нет сертификата на имена Яндекса или трафик станции не перенаправлен на этот хост |
| `setup_required` в WS/HTTP | не загружен протокол (`protocol_import`) |
| «домен не покрыт сертификатом» в журнале | фронт туннелирует такой домен в облако — это штатное поведение, см. [architecture.md](architecture.md) |
| Обновление не применилось | `journalctl -u alice-emulator`, `/var/lib/alice-emulator/update/update.log` и `plan.json` |

Журнал сервера (ошибки, домены, промахи) — ещё и в панели: «Эмулятор → Журнал сервера».

## Windows и macOS

Служба systemd — только Linux. На десктопе проще локальный запуск:

- **Windows:** `install.bat`, `start.bat` (трей без консоли), автозапуск при входе —
  `tools/install_autostart.ps1`, ярлык на рабочий стол — `tools/install_autostart.ps1 -Desktop`.
- **macOS / десктопный Linux:** `./install.sh`, `./start.sh`. Автозапуск — средствами системы
  (launchd на macOS, автозагрузка в DE на Linux); для Linux-хоста, который работает без входа
  пользователя, используйте systemd — инструкция выше.
