---
title: "Версия, релизы и самообновление"
status: stable
date: "2026-11-07"
module: "server_emulator/self_update/"
---

# Версия, релизы и самообновление

Код: `server_emulator/self_update/`.

## Обзор

Версия рабочей копии, выпуск релизов и обновление сервера по клику.

## Версия и релизы

Версия рабочей копии — `server_emulator/version.py`: git-описание через dulwich (последний
semver-тег `vX.Y.Z`, коммиты после тега — `X.Y.Z+N.g<sha>`, изменённые отслеживаемые файлы —
`dirty`); без `.git` (zip-установка) — из `.release-please-manifest.json`. Показывается в API
статуса и во вкладке «Дополнительно → Сервер». Релизы ведёт release-please (GitHub Action) по
Conventional Commits: PR релиза, `CHANGELOG.md`, тег `vX.Y.Z`, GitHub Release с заметками.
Установка — `git clone` скриптами `install.bat`/`install.sh` (editable-установка: после переключения
тега код подхватывается без переустановки); данные пользователя — только в игнорируемых путях
(`server_emulator/state*/`), работа сервера оставляет `git status --porcelain` пустым.

## Самообновление

Обновление только по клику, автоматически ничего не ставится. Проверка — GitHub Releases API
(`self_update/check.py`: `/releases/latest` для stable, `/releases` для канала beta, ETag/If-None-Match,
ошибки сети — в статус, не в исключения; 404 — репозиторий не найден или релизов нет — тоже
статус, не исключение). Сравнение версий — `packaging.version.Version` против `version.py`. Цикл проверки
живёт на Runtime (`runtime.self_update`, старт с задержкой 45 с, затем раз в `interval_hours`).
Сбой проверки — в журнал сервера не чаще раза в сутки: метка последней записи — в `StateStore.meta`
(`self_update_error_logged_at`), переживает рестарт процесса, иначе каждый перезапуск при
недоступном репозитории писал бы ту же запись заново.

Возможность обновления (`self_update/repo.py`, dulwich): git-клон с origin, совпадающим с
настройкой `self_update.repo`, без правок в отслеживаемых файлах (untracked и ignored не мешают),
не контейнер. Обновление — отдельным процессом `self_update/updater.py` (копируется в
`state/update/`, чтобы замена файлов рабочей копии не задевала исполняемый код updater'а):
ждёт выхода сервера (код `UPDATE_CODE` = 76 в `server_config.py`), бэкапит данные `state/` в zip
с ротацией до 5, fetch тегов и переключение на тег (ветка релизов — fast-forward через
`reset --hard`, иначе checkout detached), при изменении pyproject между релизами —
`pip install -e ".[emulator]"`, запуск сервера и ожидание `/__control/status` с новой версией.
Сбой любого шага — возврат на прежний коммит, повтор шага зависимостей, запуск; итог ok/failed —
в `state/update/plan.json`, лог — `state/update/update.log`.

Кто поднимает сервер после обновления (`launch` в плане): под треем Windows (`ALICA_LAUNCHER`) —
трей: код UPDATE_CODE он не перезапускает, ждёт план, затем поднимает сервер и перезапускает себя
(новый трей стартует с `ALICA_TRAY_TAKEOVER` — ожидание освобождения mutex одного экземпляра);
под менеджером служб (настройка `restart: manager`, systemd `INVOCATION_ID`) — менеджер; иначе
updater сам. Менеджер для systemd — `server_emulator/service.py`: запускает сервер дочерним
процессом, после кода UPDATE_CODE ждёт в плане итог для pid этого сервера (`ok`/`failed`) и
запускает сервер снова; штатный выход 0 и падение с другим кодом дальше не перезапускаются
(`Restart=on-failure` юнита). Юнит — `deploy/systemd/alice-emulator.service`, инструкция —
[deploy.md](../deploy.md).

API `ui/api/self_update.py`: GET — статус (current/latest, can_update и причины, blocked_files,
last_check, error, last_update, previous, installed_kind git|zip); POST `{action}` — `check`,
`update`, `stash_update` (правки → refs/stash по клику), `rollback` (предыдущий тег тем же путём),
`skip` (пропустить версию), `snooze` («Позже» до следующей проверки). UI — кнопка в навигации при
новой версии (одна пульсация, `prefers-reduced-motion` её отключает) — ручной режим, от галочки
автообновления не зависит; окно `<dialog>` с заметками релиза (текстом, без markdown), причинами
блокировки и кнопками. В форме настроек («Дополнительно → Сервер», блок «Обновление») — только
галочка автообновления и канал; остальные поля `self_update` не редактируются (дефолты).
