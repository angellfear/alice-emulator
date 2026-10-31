"""Local ownership registry. Transport identifiers are aliases, never serial numbers."""
import copy
import json
import secrets
import time
import uuid
from pathlib import Path

from .settings_service import RECORD_MODES, ROUTE_MIGRATION_KEY, ROUTE_MODES
from .state_store import StateStore


class DeviceMismatch(ValueError):
    """Разные DeviceId из независимых транспортных источников одного запроса."""


def collect_device_id(*candidates):
    """B03: единый resolver транспортных DeviceId.

    candidates — последовательность (source, value) в порядке приоритета.
    Возвращает (device_id, report). Пустые/None пропускаются; report.mismatch
    возникает, когда независимые источники дали разные ID. Решение остаётся за
    вызывающим: WS при header/sync расхождении закрывает соединение, HTTP
    разрешает по приоритету и журналирует расхождение.
    """
    present = []
    for source, value in candidates:
        text = str(value).strip() if value not in (None, "") else ""
        if text:
            present.append((source, text))
    ids = list(dict.fromkeys(value for _, value in present))
    report = {"sources": dict(present), "ids": ids, "mismatch": len(ids) > 1,
              "resolved_from": present[0][0] if present else None}
    return (ids[0] if ids else ""), report


class IdentityRegistry:
    def __init__(self, directory, store=None):
        # B02: единственное хранилище — StateStore (WAL, FK, транзакции).
        self.store = store or StateStore(Path(directory) / "emulator.sqlite3")
        self.data = self._load()
        # Оперативная активность известных устройств: sn → {'ts','ip','source'}.
        # Живёт в памяти (не в БД): каждый HTTP-обмен пишет сюда, персистить нечего.
        self.activity = {}

    GROUPS_META_KEY = "identity_groups"
    USER_QUASAR_META_KEY = "identity_user_quasar"  # {user_id: [{path, source}]} — слой аккаунта Quasar

    def _load(self):
        """Реестр из БД + группы (стереопары). Группы лежат в meta-таблице StateStore
        одним JSON-списком: схема таблиц identities остаётся прежней."""
        data = self.store.load_identity_data()
        raw = self.store.meta(self.GROUPS_META_KEY)
        try:
            groups = json.loads(raw) if raw else []
        except ValueError:
            groups = []
        data["groups"] = groups if isinstance(groups, list) else []
        # Переопределения Quasar-конфига пользователя (слой аккаунта, TASK-047): тоже в meta —
        # таблица users не меняется; в представлении — поле quasar_overrides пользователя.
        raw = self.store.meta(self.USER_QUASAR_META_KEY)
        try:
            overrides = json.loads(raw) if raw else {}
        except ValueError:
            overrides = {}
        if not isinstance(overrides, dict):
            overrides = {}
        for user in data["users"]:
            items = overrides.get(user["id"])
            if isinstance(items, list) and items:
                user["quasar_overrides"] = items
        return data

    def commit(self, value):
        self.store.save_identity_data(value)  # транзакция; конфликт → откат без изменения кэша
        groups = value.get("groups")
        if groups is not None:
            self.store.set_meta(self.GROUPS_META_KEY, json.dumps(groups, ensure_ascii=False))
        overrides = {u["id"]: u["quasar_overrides"] for u in value.get("users", []) if u.get("quasar_overrides")}
        if overrides or self.store.meta(self.USER_QUASAR_META_KEY):
            self.store.set_meta(self.USER_QUASAR_META_KEY, json.dumps(overrides, ensure_ascii=False))
        self.data = self._load()  # кэш зеркалит БД (вкл. метки pending)

    def public(self):
        return copy.deepcopy(self.data)

    @staticmethod
    def text(value, label):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(label + ': требуется непустая строка до 256 символов')
        return value.strip()

    @staticmethod
    def profile(value):
        if not isinstance(value, dict) or set(value) - {'system_prompt', 'device_context', 'tools', 'aapi_model', 'aapi_voice', 'aapi_tts', 'platform'}:
            raise ValueError('Профиль: допустимы system_prompt, device_context, tools, aapi_model, aapi_voice, aapi_tts, platform')
        if 'platform' in value and (not isinstance(value['platform'], str) or not value['platform'].strip()):
            raise ValueError('platform: непустая строка (модель устройства, например chiron)')
        if 'system_prompt' in value and not isinstance(value['system_prompt'], str):
            raise ValueError('system_prompt должен быть строкой')
        if 'device_context' in value and not isinstance(value['device_context'], dict):
            raise ValueError('device_context должен быть объектом')
        if 'tools' in value and not isinstance(value['tools'], list):
            raise ValueError('tools должен быть массивом')
        if any(k in value and not isinstance(value[k], bool) for k in ('aapi_model', 'aapi_voice', 'aapi_tts')):
            raise ValueError('Переключатели профиля должны быть bool')
        return copy.deepcopy(value)

    @staticmethod
    def location(value):
        """Гео пользователя для сценариев (погода берёт координаты отсюда)."""
        if value in (None, {}):
            return {}
        if not isinstance(value, dict) or set(value) - {'latitude', 'longitude', 'city'}:
            raise ValueError('Локация: {latitude, longitude, city}')
        out = {}
        for key in ('latitude', 'longitude'):
            item = value.get(key)
            if item is None:
                continue
            limit = 90.0 if key == 'latitude' else 180.0
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not -limit <= float(item) <= limit:
                raise ValueError(f'{key}: число в допустимых пределах')
            out[key] = float(item)
        if not out or set(out) != {'latitude', 'longitude'}:
            raise ValueError('Локация требует и latitude, и longitude')
        if value.get('city') is not None:
            out['city'] = IdentityRegistry.text(value['city'], 'city')
        return out

    @staticmethod
    def room(value):
        """Комната размещения колонки (контекст света: «включи свет» без уточнения)."""
        if value in (None, ''):
            return ''
        return IdentityRegistry.text(value, 'Комната')

    @staticmethod
    def fields(value):
        """Произвольные доп. поля для сценариев: JSON-объект без исполняемого
        содержимого, до 16 КиБ. Значения читаются bindings по точечному пути."""
        if value in (None, {}):
            return {}
        if not isinstance(value, dict):
            raise ValueError('Доп. поля: объект JSON')
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            raise ValueError('Доп. поля: сериализуемый JSON')
        if len(encoded) > 16 * 1024:
            raise ValueError('Доп. поля: до 16 КиБ')
        for key in value:
            if not isinstance(key, str) or not key or key.lower() in ('code', 'script', 'eval', 'import', 'exec'):
                raise ValueError('Доп. поля: имена — непустые строки без исполняемых ключей')
        return copy.deepcopy(value)

    @staticmethod
    def route_mode(value):
        """Маршрут устройства (TASK-044): None — наследовать default_route_mode."""
        if value in (None, ''):
            return None
        if value not in ROUTE_MODES:
            raise ValueError('Маршрут: ' + ' | '.join(ROUTE_MODES))
        return value

    @staticmethod
    def record_option(value):
        """Опция «Запись» устройства (TASK-054): off | on | misses; пусто — off."""
        if value in (None, ''):
            return 'off'
        if value not in RECORD_MODES:
            raise ValueError('Запись: ' + ' | '.join(RECORD_MODES))
        return value

    def record_mode(self, sn):
        """Опция записи устройства по SN; неопознанное устройство (нет SN) — off.
        Читается на каждом обмене и кадре: смена действует сразу."""
        if not sn:
            return 'off'
        device = next((d for d in self.data['devices'] if d['sn'] == sn), None)
        return (device or {}).get('record') or 'off'

    def pending_route_migration(self):
        """Очередь миграции emulation_excluded → route_mode=proxy (meta StateStore)."""
        raw = self.store.meta(ROUTE_MIGRATION_KEY)
        try:
            pending = json.loads(raw) if raw else {}
        except ValueError:
            pending = {}
        return [sn for sn in pending.get("excluded", []) if isinstance(sn, str)]

    def apply_pending_route(self, sn):
        """Известный реестру SN из очереди миграции: route_mode=proxy и снятие с очереди."""
        if not self._take_pending_route(sn):
            return False
        self.mutate({'operation': 'route_mode', 'sn': sn, 'mode': 'proxy'})
        return True

    def _take_pending_route(self, sn):
        """SN из очереди миграции → route_mode=proxy (однократно), очередь короче."""
        pending = self.pending_route_migration()
        if sn not in pending:
            return False
        pending.remove(sn)
        self.store.set_meta(ROUTE_MIGRATION_KEY, json.dumps({"excluded": pending}))
        return True

    GROUP_ROLES = ('leader', 'follower')

    @classmethod
    def group(cls, request, value, old=None):
        """Стереопара: ровно 2 устройства одного владельца, ровно один leader.
        Устройство состоит не более чем в одной группе."""
        old = old or {}
        items = request.get('devices', old.get('devices'))
        if not isinstance(items, list) or len(items) != 2:
            raise ValueError('Группа: ровно 2 устройства [{sn, role}]')
        members = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Группа: устройство — объект {sn, role}')
            sn = cls.text(item.get('sn'), 'SN')
            role = item.get('role')
            if role not in cls.GROUP_ROLES:
                raise ValueError('Группа: роль — leader или follower')
            members.append({'sn': sn, 'role': role})
        if members[0]['sn'] == members[1]['sn']:
            raise ValueError('Группа: устройства должны различаться')
        if sorted(m['role'] for m in members) != ['follower', 'leader']:
            raise ValueError('Группа: ровно один leader и один follower')
        owners = set()
        for member in members:
            device = next((d for d in value['devices'] if d['sn'] == member['sn']), None)
            if device is None:
                raise ValueError('Группа: SN ' + member['sn'] + ' не найден')
            owners.add(device.get('user_id'))
        if len(owners) != 1 or None in owners:
            raise ValueError('Группа: устройства должны принадлежать одному владельцу')
        identifier = request.get('id', old.get('id'))
        if identifier is None:
            identifier = max([g['id'] for g in value['groups'] if isinstance(g.get('id'), int)] + [0]) + 1
        identifier = cls.group_id(identifier)
        taken = {m['sn'] for g in value['groups'] if g['id'] != identifier for m in g['devices']}
        if taken & {m['sn'] for m in members}:
            raise ValueError('Устройство уже состоит в другой группе')
        secret = request.get('secret', old.get('secret')) or secrets.token_hex(8)
        return {'id': identifier, 'name': cls.text(request.get('name', old.get('name')), 'Название группы'),
                'secret': cls.text(secret, 'secret'), 'devices': members}

    @staticmethod
    def group_id(identifier):
        if isinstance(identifier, str) and identifier.strip().isdigit():
            identifier = int(identifier)
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise ValueError('Группа: id — положительное целое')
        return identifier

    @staticmethod
    def prune_groups(value):
        """Группа живёт, пока оба устройства есть в реестре и у одного владельца."""
        devices = {d['sn']: d for d in value['devices']}
        keep = []
        for group in value['groups']:
            owners = {(devices.get(m['sn']) or {}).get('user_id') for m in group['devices']}
            if len(group['devices']) == 2 and all(m['sn'] in devices for m in group['devices']) \
                    and len(owners) == 1 and None not in owners:
                keep.append(group)
        value['groups'] = keep

    def group_of(self, sn):
        """Группа (стереопара), в которую входит устройство, или None."""
        group = next((g for g in self.data.get('groups', []) if any(m['sn'] == sn for m in g['devices'])), None)
        return copy.deepcopy(group)

    def resolve_by_ip(self, ip):
        """Устройство по последнему наблюдённому IP. Неизвестный IP или неоднозначность
        (несколько устройств на одном IP) — None."""
        if not ip:
            return None
        matches = [sn for sn, seen in self.activity.items() if seen.get('ip') == ip]
        known = [d for d in self.data['devices'] if d['sn'] in matches]
        return copy.deepcopy(known[0]) if len(known) == 1 else None

    def mutate(self, request):
        value = self.public()
        value.setdefault('groups', [])
        operation = request.get('operation')
        if operation == 'user':
            identifier = request.get('id') or uuid.uuid4().hex
            old = next((u for u in value['users'] if u['id'] == identifier), {})
            user = {'id': identifier, 'name': self.text(request.get('name'), 'Имя'), 'profile': self.profile(request.get('profile', old.get('profile', {}))),
                    'location': self.location(request.get('location', old.get('location', {}))),
                    'fields': self.fields(request.get('fields', old.get('fields', {})))}
            if old.get('quasar_overrides'):
                user['quasar_overrides'] = old['quasar_overrides']  # слой аккаунта не теряется при правке имени
            value['users'] = [u for u in value['users'] if u['id'] != identifier] + [user]
        elif operation == 'user_quasar_overrides':
            # Слой аккаунта Quasar-конфига: {id, quasar_overrides: [{path, source}]};
            # пути — только config.account_config.*; пустой список очищает слой.
            from .quasar_config import QuasarConfigError, validate_account_overrides
            user = next((u for u in value['users'] if u['id'] == request.get('id')), None)
            if user is None:
                raise ValueError('Пользователь не найден')
            try:
                items = validate_account_overrides(request.get('quasar_overrides', []))
            except QuasarConfigError as exc:
                raise ValueError(str(exc))
            if items:
                user['quasar_overrides'] = items
            else:
                user.pop('quasar_overrides', None)
        elif operation == 'user_delete':
            # Раздел «Пользователи»: удаление отвязывает устройства (user_id=None),
            # сами устройства и их состояние остаются; защита от живых сессий — в API.
            identifier = request.get('id')
            if not any(u['id'] == identifier for u in value['users']):
                raise ValueError('Пользователь не найден')
            value['users'] = [u for u in value['users'] if u['id'] != identifier]
            for device in value['devices']:
                if device.get('user_id') == identifier:
                    device['user_id'] = None
        elif operation == 'device':
            sn = self.text(request.get('sn'), 'SN')
            user_id = request.get('user_id') or None
            if user_id and not any(u['id'] == user_id for u in value['users']):
                raise ValueError('Пользователь не найден')
            aliases = request.get('device_ids', [])
            if not isinstance(aliases, list):
                raise ValueError('device_ids должен быть массивом')
            aliases = list(dict.fromkeys(self.text(a, 'DeviceId') for a in aliases))
            if any(set(d['device_ids']) & set(aliases) for d in value['devices'] if d['sn'] != sn):
                raise ValueError('DeviceId уже привязан к другому SN')
            old = next((d for d in value['devices'] if d['sn'] == sn), {})
            if old.get('user_id') != user_id and old.get('user_id'):
                if not request.get('reset_state'):
                    raise ValueError('При смене владельца нужно явно сбросить состояние устройства')
                state, previous_profile = {}, {}
            else:
                state, previous_profile = old.get('state', {}), old.get('profile', {})
            device = {'sn': sn, 'name': self.text(request.get('name') or sn, 'Название'), 'user_id': user_id,
                      'device_ids': aliases, 'profile': self.profile(request.get('profile', previous_profile)), 'state': state,
                      'room': self.room(request.get('room', old.get('room', ''))),
                      'model': (request.get('model', old.get('model', '')) or '').strip().lower()[:32],
                      'fields': self.fields(request.get('fields', old.get('fields', {}))),
                      'route_mode': self.route_mode(request.get('route_mode', old.get('route_mode'))),
                      'record': self.record_option(request.get('record', old.get('record')))}
            if not old and self._take_pending_route(sn):  # TASK-044: SN из emulation_excluded
                device['route_mode'] = 'proxy'
            value['devices'] = [d for d in value['devices'] if d['sn'] != sn] + [device]
            value['unassigned'] = [d for d in value['unassigned'] if d['device_id'] not in aliases]
        elif operation == 'route_mode':
            # TASK-044: смена маршрута одного устройства; mode=None — снова наследовать default
            sn = self.text(request.get('sn'), 'SN')
            device = next((d for d in value['devices'] if d['sn'] == sn), None)
            if device is None:
                raise ValueError('SN не найден')
            mode = self.route_mode(request.get('mode'))
            if mode:
                device['route_mode'] = mode
            else:
                device.pop('route_mode', None)
        elif operation == 'record':
            # TASK-054: опция записи одного устройства (off | on | misses)
            sn = self.text(request.get('sn'), 'SN')
            device = next((d for d in value['devices'] if d['sn'] == sn), None)
            if device is None:
                raise ValueError('SN не найден')
            device['record'] = self.record_option(request.get('mode'))
        elif operation == 'device_delete':
            # Удаление = отвязка (если была) + строка уходит из списка. Без игноров:
            # как только устройство снова даст о себе знать (/get_sync_info), оно тут же
            # вернётся в список — уже непривязанным. Отвязка отдельно — только user_id=None.
            sn = self.text(request.get('sn'), 'SN')
            if not any(d['sn'] == sn for d in value['devices']):
                raise ValueError('SN не найден')
            value['devices'] = [d for d in value['devices'] if d['sn'] != sn]
        elif operation == 'state':
            device = next((d for d in value['devices'] if d['sn'] == request.get('sn')), None)
            if device is None:
                raise ValueError('SN не найден')
            if not isinstance(request.get('state'), dict):
                raise ValueError('Состояние должно быть объектом')
            device['state'] = copy.deepcopy(request['state'])
        elif operation == 'pair':
            # B04: pairing pending→assigned — явная привязка наблюдаемого DeviceId
            # из очереди unassigned к SN владельца; требует confirm и уникальность.
            device_id = self.text(request.get('device_id'), 'DeviceId')
            pending = next((d for d in value['unassigned'] if d['device_id'] == device_id), None)
            if pending is None:
                raise ValueError('DeviceId не найден в очереди на привязку')
            if not request.get('confirm'):
                raise ValueError('Pairing требует confirm=true')
            sn = self.text(request.get('sn'), 'SN')
            user_id = request.get('user_id') or None
            if user_id and not any(u['id'] == user_id for u in value['users']):
                raise ValueError('Пользователь не найден')
            if any(set(d['device_ids']) & {device_id} for d in value['devices'] if d['sn'] != sn):
                raise ValueError('DeviceId уже привязан к другому SN')
            old = next((d for d in value['devices'] if d['sn'] == sn), {})
            device = {'sn': sn, 'name': self.text(request.get('name') or sn, 'Название'),
                      'user_id': user_id, 'device_ids': list(dict.fromkeys([device_id] + old.get('device_ids', []))),
                      'profile': old.get('profile', {}), 'state': old.get('state', {}),
                      'record': old.get('record') or 'off'}
            if not old and self._take_pending_route(sn):  # TASK-044: SN из emulation_excluded
                device['route_mode'] = 'proxy'
            value['devices'] = [d for d in value['devices'] if d['sn'] != sn] + [device]
            value['unassigned'] = [d for d in value['unassigned'] if d['device_id'] != device_id]
        elif operation == 'group':
            # Стереопары (TASK-048): создать (без id) или изменить (с id) группу.
            old = None
            if request.get('id') is not None:
                identifier = self.group_id(request['id'])
                old = next((g for g in value['groups'] if g['id'] == identifier), None)
                if old is None:
                    raise ValueError('Группа не найдена')
                request = {**request, 'id': identifier}
            group = self.group(request, value, old)
            value['groups'] = [g for g in value['groups'] if g['id'] != group['id']] + [group]
        elif operation == 'group_delete':
            identifier = self.group_id(request.get('id'))
            if not any(g['id'] == identifier for g in value['groups']):
                raise ValueError('Группа не найдена')
            value['groups'] = [g for g in value['groups'] if g['id'] != identifier]
        else:
            raise ValueError('Неизвестная операция реестра')
        if operation not in ('group', 'group_delete'):
            self.prune_groups(value)  # удаление/перепривязка устройства разрушает его пару
        self.commit(value)
        return self.public()

    def observe(self, device_id, source=None, ip=None, register=False):
        """Наблюдение DeviceId. register=False (по умолчанию) — resolve-only:
        известные устройства обновляют activity, реестр не пополняется.
        register=True — колонка представилась (/get_sync_info): неизвестное
        устройство сразу попадает в список БЕЗ владельца (временно, до активации).
        IP и источник — метаданные наблюдения (activity, память), не ownership."""
        device_id = self.text(device_id, 'DeviceId')
        device = self.resolve(device_id)
        if device:
            # Активность известного устройства — для индикатора «виден» и последнего IP.
            previous = self.activity.get(device['sn'], {})
            self.activity[device['sn']] = {'ts': time.time(), 'ip': ip or previous.get('ip'),
                                           'source': source or previous.get('source')}
        if not device and register:
            value = self.public()
            device_row = {'sn': device_id, 'name': device_id, 'user_id': None,
                          'device_ids': [device_id], 'profile': {}, 'state': {}}
            if self._take_pending_route(device_id):  # TASK-044: SN из emulation_excluded
                device_row['route_mode'] = 'proxy'
            value['devices'].append(device_row)
            self.commit(value)
            device = self.resolve(device_id)
        return device

    def resolve(self, device_id):
        return copy.deepcopy(next((d for d in self.data['devices'] if device_id in d['device_ids']), None))

    def settings_for(self, defaults, device):
        result = copy.deepcopy(defaults)
        if device and device.get('user_id'):
            user = next((u for u in self.data['users'] if u['id'] == device['user_id']), None)
            if user:
                result.update(copy.deepcopy(user['profile']))
                result.update(copy.deepcopy(device['profile']))
                result['device_context'] = {**result.get('device_context', {}), 'sn': device['sn'], 'user_id': user['id'], 'state': copy.deepcopy(device['state'])}
                return result
        # Unclaimed devices can synchronize, but cannot consume an owner's AI profile.
        result.update(system_prompt='', device_context={}, tools=[], aapi_model=False, aapi_voice=False, aapi_tts=False)
        # Маркер для стандартных ответов: сервер отвечает репликой, а не молчанием
        result['unclaimed'] = True
        return result
