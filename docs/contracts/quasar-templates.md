---
title: "Формат шаблонов и слоёв Quasar-конфига"
status: draft
date: "2026-10-25"
module: "server_emulator/quasar_config.py"
---

# Формат шаблонов и слоёв Quasar-конфига (предложение)

Сборка ответа устройству — слои поверх шаблонов (слой аккаунта — см. `quasar-config.md`):

    common.json (общий конфиг, всегда есть, не удаляется)
      ← merge-patch платформы (state/quasar/platforms/<platform>.json — только патч)
      ← переопределения аккаунта владельца (quasar_overrides пользователя, config.account_config.*)
      ← переопределения устройства (quasar_overrides, патч по путям)
      → разрешение ссылок $template (выбранные варианты — «настройки»)
      → разрешение динамики {{токены}} / db / run
      → готовый sync

## 1. Шаблоны: определения и ссылки

Синтаксис — конвенция $ref (JSON Schema/OpenAPI), распространённая и знакомая:
определения лежат в `templates`, в теле — ссылка с выбранным вариантом.

Определение (в любом слое; платформа дополняет/переопределяет по имени):

    "templates": {
      "subscription": {
        "type": "selector",
        "label": "Подписка",
        "hint": "Вариант подставляет объект целиком",
        "options": {
          "on":  {"label": "Включена",
                  "value": {"enabled": true, "mode": "subscription", "ttl": 0}},
          "off": {"label": "Выключена (invalid_subscription_user)",
                  "value": {"enabled": false, "mode": "subscription", "ttl": 0,
                            "disabled_reason": "invalid_subscription_user"}}
        },
        "params": {
          "statusUrl": "https://plus.yandex.ru/module-bratt?deviceId={{device_id}}&screen={{param.screen_uid}}"
        }
      }
    }

Ссылка в теле (это и есть «настройка» — простой редактор меняет `select`/`params`):

    "sync": {
      "subscription": {"$template": "subscription", "select": "on",
                       "params": {"screen_uid": "wrong-uid"}}
    }

Разрешение: `options[select].value` ← deep-merge `params` из определения
(значения-по-умолчанию) ← `params` из ссылки (переопределение) → рендер
`{{токены}}` (запросные + `{{param.<имя>}}` + user/device/db — как в плане).
Итог для устройства X00REC при select="off":

    "subscription": {
      "enabled": false, "mode": "subscription", "ttl": 0,
      "statusUrl": "https://plus.yandex.ru/module-bratt?deviceId=X00T20002SV5KR&screen=wrong-uid",
      "disabled_reason": "invalid_subscription_user"
    }

Правила: ссылка — объект ровно с ключом `$template` (+`select`/`params`);
`select` без options у определения — ошибка валидации; шаблон можно «инлайнить»
в ссылку (`{"$template": {"options": {...}, "params": {...}}, "select": …}`) —
для разовых значений без имени.

## 2. Слои и патчи

- **common.json** — единственный, не удаляемый: полный `sync` со ссылками на
  шаблоны + `templates`. Путь: state/quasar/common.json.
- **Платформы** — только патч, формат RFC 7396 JSON Merge Patch (объектная
  рекурсивная мерджа, `null` удаляет ключ — как в Kubernetes/JSON Merge Patch):

      {
        "platform": "yandexstation_2",
        "title": "Яндекс Станция Макс",
        "patch": {
          "config": {"device_config": {"platform": "yandexstation_2", "name": "{{device_name}}"}},
          "sync.glagol.security": {"server_certificate": {"db": "device.fields.cert"}},
          "templates": {"subscription": {"params": {"screen_uid": "station"}}
        }
      }

- **Переопределения устройства** — как принято ранее (список path+source),
  где source тоже может быть ссылкой на шаблон:

      {"path": "subscription", "source": {"$template": "subscription", "select": "on"}}

## 3. Простой редактор

Рендерится по разрешимой структуре: каждая ссылка $template — селектор
(label/hint/options из определения) + поля params; выбор меняет select/params
словаря ссылки и сохраняет слой, где ссылка живёт (общий/платформа/устройство).

## 4. Пример common.json (фрагмент)

    {
      "common": true,
      "templates": {
        "subscription": {…как выше…},
        "alice_pro": {"type": "selector", "label": "Alice Pro",
          "options": {"on": {"label": "Вкл", "value": {"enabled": true}},
                      "off": {"label": "Выкл", "value": {"enabled": false}}}},
        "tags": {"type": "text-list", "label": "Теги", "value": []}
      },
      "sync": {
        "config": {"account_config": {}, "device_config": {"name": "{{device_name}}"},
                   "system_config": {}},
        "tags": {"$template": "tags"},
        "subscription": {"$template": "subscription", "select": "on"},
        "alice_pro_subscription": {"$template": "alice_pro", "select": "on"},
        "glagol": {"security": {"server_certificate": {"db": "device.fields.cert"}}}
      }
    }

## Статус реализации (2026-10-25 14:30)

Готово, 423 теста зелёные, живой трафик подтверждает (X00REC — 200 на слоёной
сборке, в ответе его deviceId в statusUrl):
- движок: merge_patch (RFC 7396), шаблоны $template (selector/toggle/text/
  number/text-list; params с {{param.*}}; вложенные и инлайн-определения),
  слои common → платформа (patch) → устройство (overrides, включая ссылки на
  шаблоны) → динамика ({{токены}}/db/run с белым списком);
- хранилище: state/quasar/common.json (не удаляется, скелет создаются через
  ensure_common при первом заходе UI), platforms/*.json — только патчи;
- API quasar_platforms: GET {common, platforms, templates(объединённые),
  tokens, sources}; GET ?platform=|common; PUT {common_file}|{platform_file};
  DELETE — только платформы (общий конфиг отказ);
- миграция v2 (выполнена в исходном проекте): общий конфиг из
  эталонной платформы со ссылками-настройками; chiron — пустой патч,
  yandexstation_2 — merge-patch отличий; переопределения устройств сохранены;
  statusUrl темплирован {{device_id}};
- UI: сайдбар «Общий конфиг» (закреплён) + платформы-патчи; простой вид
  рендерит ссылки $template (селекторы/тумблеры/поля + params), полный —
  граф-дерево файла + редактор ветви + чипы; диалог переопределений получил
  источник «вариант шаблона платформы».

## Финальная конструкция: инлайн-узлы (2026-10-25 16:20)

Утверждён вариант А + расширение типов. Шаблон живёт В ТЕЛЕ на месте значения:
один $-ключ типа со значением, рядом опционально $name/$hint (подпись/пояснение
для простого редактора):

    "subscription": {
      "$name": "Подписка", "$hint": "Состояние подписки устройства",
      "$select": "Включена",
      "Включена":  {"enabled": true,  "mode": "subscription", "ttl": 0,
                    "statusUrl": "…deviceId={{device_id}}"},
      "Выключена (invalid_subscription_user)": {"enabled": false, …}
    }
    "alice_pro_subscription": {"$name": "Alice Pro", "$bool": true}
    "tags":       {"$name": "Теги", "$list": ["a", "b"]}
    "device_label": {"$string": "{{device_name}}", "$name": "Подпись"}
    "volume":       {"$number": 5, "$name": "Громкость"}
    "owner":        {"$param": "user.name", "$name": "Владелец"}

Правила: у $select не-$ ключи = варианты (их ключи — подписи выпадающего
списка, значения проходят общий шаблонизатор); у прочих типов рядом только
$name/$hint. $param — заглушка будущей системы параметров (device.sn,
device.id/name/room/platform, user.id/name, session.sn — одна точка
расширения resolve_param). Переопределение устройства может переключать
селектор, не дублируя варианты: {"path": "subscription", "source":
{"$select": "Выключена (invalid_subscription_user)"}} — мерджится в узел.
templates.json упразднён (.bak): всё в теле. 423 теста; X00REC/chiron —
живые 200 на инлайн-модели.
