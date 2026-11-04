"""Update endpoint policies and durable discovery of original OTA URLs.

Расширения (TASK-032): папка updates/ включённого расширения добавляет правила
OTA (только чтение, в настройки БД не копируются): *.json — правило
{devices: {SN: политика}, platforms: {платформа: политика}, custom_devices/
custom_platforms: {объект: {updates: [...]}}}; политика — allow/deny/custom,
URL обновлений — http(s). Правило применяется к устройству/платформе, которые
пользователь не задал (пользовательские записи настроек сильнее); выключение
расширения убирает правила; некорректное правило — warning без падения раздела.
Слои — extensions/layers; callable ставит runtime (конструктор ниже).
"""
import asyncio
import copy
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import ClientSession, ClientTimeout, ClientError

log = logging.getLogger(__name__)


DEFAULT_ENDPOINTS = [
    {'id':'ota','host':'quasar.yandex.net','path':'/tv/api/v4/updates','method':'POST','kind':'list'},
    {'id':'legacy','host':'quasar.yandex.net','path':'/check_updates','method':'GET','kind':'check'},
    {'id':'allow','host':'quasar.yandex.net','path':'/allow_update','method':'GET','kind':'allow'},
    {'id':'confirm','host':'quasar.yandex.net','path':'/confirm_update','method':'GET','kind':'confirm'},
]

# Тип-символ SN → платформа (тот же формат, что DeviceKit в ui/devices_common.js)
SN_PLATFORMS = {'C':'chiron','X':'yandexstation_2','U':'yandexmidi','L':'blackthorn','T':'magritte',
                'M':'yandexmini_2','P':'yandexmicro'}
POLICIES = ('allow','deny','custom')
RULE_KEYS = ('devices','platforms','custom_devices','custom_platforms')  # правило updates/ расширения (TASK-032)


def platform_of(device):
    """Платформа устройства: профиль → модель из реестра → тип-символ SN; '' — не распознана."""
    if not device:
        return ''
    explicit = (device.get('profile') or {}).get('platform') or device.get('model')
    if explicit:
        return explicit
    sn = device.get('sn') or ''
    if sn.startswith('FF98'):
        return 'yandexmini'
    return SN_PLATFORMS.get(sn[:1], '') if len(sn) in (14, 32) else ''


def url_platforms(url,sns,devices):
    """Платформы прошивки: по устройствам, которым её выдал реальный сервер,
    иначе по сегменту пути CDN (…/chiron/ota/…)."""
    by_sn={d['sn']:d for d in devices}
    found=[platform_of(by_sn.get(sn) or {'sn':sn}) for sn in sns]
    found=list(dict.fromkeys(p for p in found if p))
    if not found:
        known=set(SN_PLATFORMS.values())|{'yandexmini'}
        found=[part for part in urlsplit(url).path.split('/') if part in known][:1]
    return found


class Updates:
    def __init__(self, directory, extension_dirs=None):
        self.path = directory/'updates.sqlite3'
        self.extension_dirs = extension_dirs  # callable → [(origin, Path)] — слои updates/, TASK-032
        self._poll_task = None
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, ts REAL, endpoint TEXT, device_sn TEXT, user_id TEXT, device_id TEXT, policy TEXT, status INTEGER, error TEXT);
                CREATE TABLE IF NOT EXISTS urls (url TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, hits INTEGER, metadata TEXT);
                CREATE TABLE IF NOT EXISTS sightings (attempt_id TEXT, url TEXT, PRIMARY KEY(attempt_id,url));
                CREATE INDEX IF NOT EXISTS attempts_ts ON attempts(ts);
                CREATE TABLE IF NOT EXISTS last_requests (endpoint TEXT, device_sn TEXT, method TEXT, target TEXT,
                    headers TEXT, body BLOB, ts REAL, PRIMARY KEY(endpoint, device_sn));
            ''')
            try:  # миграция: IP устройства в попытках (для старых баз колонки нет)
                db.execute('ALTER TABLE attempts ADD COLUMN ip TEXT')
            except sqlite3.OperationalError:
                pass
            if not db.execute('SELECT 1 FROM settings').fetchone():
                db.execute('INSERT INTO settings VALUES (1,?)',(json.dumps({'policy':'deny','devices':{},'custom':{'updates':[]},'endpoints':DEFAULT_ENDPOINTS}),))

    def connect(self):
        db = sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        return db

    # --- слой расширений (TASK-032) ------------------------------------------------------
    def _extension_layers(self):
        """Слои updates/ включённых расширений: [(origin, Path)] (callable из runtime —
        extensions/layers.source_layers). Слой не задан или сбой — пусто, раздел работает."""
        if self.extension_dirs is None:
            return []
        try:
            return [(origin,Path(path)) for origin,path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать раздел
            return []

    def extension_rules(self):
        """Правила OTA из расширений: [{'origin','path','rules'}] — файл = правило
        {devices{SN: allow|deny|custom}, platforms{платформа: …}, custom_devices/
        custom_platforms{объект: {updates: [...]}}}; URL обновлений — http(s).
        Некорректное правило — warning, раздел работает. В настройки БД не копируются:
        применяются при обработке запроса только к тому, что пользователь не задал."""
        from .extensions.layers import layer_records
        def warn(message):log.warning('Расширения (обновления): %s',message)
        result=[]
        for origin,path,value in layer_records(self._extension_layers(),warn=warn):
            rules=self._rule_of(path,value,warn)
            if rules is not None:
                result.append({'origin':origin,'path':str(path),'rules':rules})
        return result

    def _rule_of(self,path,value,warn):
        """Файл правила → проверенный {devices, platforms, custom_devices,
        custom_platforms} (пустые разделы опускаются) или None — warning уже отдан."""
        if not isinstance(value,dict):
            warn(f'{path}: нужен JSON-объект с правилом');return None
        unknown=sorted(set(value)-set(RULE_KEYS))
        if unknown:warn(f'{path}: неизвестные поля правила: {unknown}');return None
        rules={}
        for key in ('devices','platforms'):
            policies=value.get(key) or {}
            if not isinstance(policies,dict) or any(p not in POLICIES for p in policies.values()):
                warn(f'{path}: {key} — объект SN/платформа → ' + ', '.join(POLICIES));return None
            if policies:rules[key]={str(sn):p for sn,p in policies.items()}
        for key in ('custom_devices','custom_platforms'):
            customs=value.get(key) or {}
            if not isinstance(customs,dict):
                warn(f'{path}: {key} должен быть объектом');return None
            checked={}
            for owner,custom in customs.items():
                updates=custom.get('updates') if isinstance(custom,dict) else None
                if not isinstance(updates,list) or any(not isinstance(u,dict) for u in updates):
                    warn(f'{path}: {key}.{owner}: updates — массив объектов');return None
                for update in updates:
                    try:self.validate_url(update.get('url',''))
                    except ValueError as exc:
                        warn(f'{path}: {key}.{owner}: {exc}');return None
                checked[str(owner)]=custom
            if checked:rules[key]=checked
        return rules

    def extension_overlay(self):
        """Правила расширений одним слоем: {devices, platforms, custom_devices,
        custom_platforms} — поздний слой перекрывает ранний (пустые поля не влияют)."""
        overlay={key:{} for key in RULE_KEYS}
        for item in self.extension_rules():
            for key,values in item['rules'].items():
                overlay[key].update(values)
        return overlay

    def with_extension_rules(self, settings):
        """Настройки + правила расширений (TASK-032, только чтение): устройства и
        платформы, не заданные пользователем, получают политику/свой ответ расширения;
        пользовательская запись побеждает; settings не мутируются."""
        overlay=self.extension_overlay()
        if not any(overlay.values()):
            return settings
        merged=dict(settings)
        for key in RULE_KEYS:
            merged[key]={**overlay[key],**(settings.get(key) or {})}
        return merged

    def settings(self):
        with self.connect() as db:
            return json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])

    @staticmethod
    def validate_url(value):
        u=urlsplit(value)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.fragment:
            raise ValueError('Требуется http(s) URL без credentials и fragment')

    def save(self, value):
        if value.get('policy') not in ('allow','deny','custom') or not isinstance(value.get('devices'),dict):
            raise ValueError('Политика: allow, deny или custom; devices — объект SN → политика')
        if any(p not in POLICIES for p in value['devices'].values()):
            raise ValueError('Неизвестная политика устройства')
        if not isinstance(value.setdefault('platforms',{}),dict) or any(p not in POLICIES for p in value['platforms'].values()):
            raise ValueError('platforms — объект платформа → allow/deny/custom')
        if not isinstance(value.setdefault('custom_platforms',{}),dict):raise ValueError('custom_platforms должен быть объектом')
        if not isinstance(value.get('custom'),dict) or not isinstance(value['custom'].get('updates'),list):
            raise ValueError('Свой ответ должен содержать updates: []')
        if not isinstance(value.get('custom_devices',{}),dict):raise ValueError('custom_devices должен быть объектом')
        for custom in [value['custom'],*value.get('custom_devices',{}).values(),*value['custom_platforms'].values()]:
            if not isinstance(custom,dict) or not isinstance(custom.get('updates'),list):raise ValueError('Свой ответ: updates должен быть массивом')
            for update in custom['updates']:
                if not isinstance(update,dict):raise ValueError('Обновление должно быть объектом')
                self.validate_url(update.get('url',''))
        endpoints=value.get('endpoints')
        if not isinstance(endpoints,list) or len(endpoints)>100:
            raise ValueError('Нужен список до 100 эндпойнтов')
        keys=set()
        for e in endpoints:
            e.setdefault('id',uuid.uuid4().hex)
            if not e.get('host') or e.get('method') not in ('GET','POST') or e.get('kind') not in ('list','check','allow','confirm'):
                raise ValueError('Эндпойнт: host, method GET/POST, kind list/check/allow/confirm')
            if not e.get('path','').startswith('/') or '?' in e['path'] or '#' in e['path']:
                raise ValueError('path должен начинаться с / и не содержать query/fragment')
            self.validate_url(e.get('upstream') or 'https://'+e['host'])
            key=(e['host'].lower(),e['path'],e['method'])
            if key in keys:raise ValueError('Повторяющийся эндпойнт')
            keys.add(key)
            e['host']=e['host'].lower()
        with self.connect() as db:
            db.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(value,ensure_ascii=False),))
        return self.settings()

    def match(self,host,path,method):
        return next((copy.deepcopy(e) for e in self.settings()['endpoints'] if e.get('enabled',True) and e['host']==host.lower() and e['path']==path and e['method']==method),None)

    @staticmethod
    async def fetch(url,method,headers,body):
        async with ClientSession(timeout=ClientTimeout(total=20)) as client:
            async with client.request(method,url,headers=headers,data=body,allow_redirects=False) as response:
                data=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data)>8*1024**2:raise ValueError('Ответ проверки обновлений превышает 8 MiB')
                return response.status,dict(response.headers),bytes(data)

    def record(self,attempt,payload):
        found={}
        def walk(value,metadata=None):
            if isinstance(value,dict):
                meta={k:v for k,v in value.items() if k in ('type','priority','size','version_code','version_name','update_id','md5_hash','sha256')}
                for k,v in value.items():
                    if isinstance(v,str) and (k.lower() in ('url','download_url','update_url','firmware_url') or k.lower().endswith('_url')):
                        try:self.validate_url(v)
                        except ValueError:continue
                        found[v]=meta
                    elif isinstance(v,(dict,list)):walk(v,meta)
            elif isinstance(value,list):
                for v in value:walk(v,metadata)
        walk(payload)
        with self.connect() as db:
            inserted=db.execute('INSERT OR IGNORE INTO attempts (id,ts,endpoint,device_sn,user_id,device_id,policy,status,error,ip) VALUES (?,?,?,?,?,?,?,?,?,?)',tuple(attempt.get(k) for k in ('id','ts','endpoint','device_sn','user_id','device_id','policy','status','error','ip')))
            if not inserted.rowcount:
                db.execute('UPDATE attempts SET device_id=COALESCE(device_id,?),device_sn=COALESCE(device_sn,?),user_id=COALESCE(user_id,?),ip=COALESCE(ip,?) WHERE id=?',(attempt['device_id'],attempt['device_sn'],attempt['user_id'],attempt.get('ip'),attempt['id']))
                return
            for url,meta in found.items():
                db.execute('INSERT INTO urls VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen,hits=urls.hits+1,metadata=excluded.metadata',(url,attempt['ts'],attempt['ts'],1,json.dumps(meta,ensure_ascii=False)))
                db.execute('INSERT INTO sightings VALUES (?,?)',(attempt['id'],url))

    async def handle(self,endpoint,query,headers,body,device,device_id,fetch=None,client_ip=None):
        if not self._poll_task:  # суточный опрос сервера, когда устройство молчит
            self._poll_task=asyncio.create_task(self.poll_loop())
        settings=self.with_extension_rules(self.settings())  # + правила updates/ расширений (TASK-032)
        policy,custom=self.effective(settings,device)
        target=(endpoint.get('upstream') or 'https://'+endpoint['host']).rstrip('/')+endpoint['path']+('?' + query if query else '')
        # Forward the real request, including its authentication, but never persist credentials.
        excluded={'host','connection','content-length','transfer-encoding','accept-encoding','x-forwarded-for','x-real-ip','forwarded','x-alice-update-probe'}
        real_headers={k:v for k,v in headers.items() if k.lower() not in excluded}
        real_headers['X-Alice-Update-Probe']='1'
        status,raw,reply_headers,error=502,b'',{'Content-Type':'application/json'},None
        payload=None
        try:
            status,reply_headers,raw=await (fetch or self.fetch)(target,endpoint['method'],real_headers,body)
            try:payload=json.loads(raw)
            except (ValueError,UnicodeError):error='non_json_response' if raw else None
        except (ClientError,OSError,asyncio.TimeoutError,ValueError) as exc:
            error=type(exc).__name__
        attempt={'id':uuid.uuid4().hex,'ts':time.time(),'endpoint':endpoint['id'],'device_sn':device['sn'] if device else None,'user_id':device.get('user_id') if device else None,'device_id':device_id or None,'policy':policy,'status':status,'error':error,'ip':client_ip}
        if device_id or (device and device.get('sn')):  # последний запрос устройства — источник суточного опроса
            await asyncio.to_thread(self.note_request,endpoint,target,endpoint['method'],headers,body,
                                    device['sn'] if device else device_id)
        await asyncio.to_thread(self.record,attempt,payload)
        original=raw
        if policy=='deny':
            result={'updates':[]} if endpoint['kind']=='list' else {'hasUpdate':False} if endpoint['kind']=='check' else {'allow':False} if endpoint['kind']=='allow' else {'status':'blocked'}
            status,raw=200,json.dumps(result).encode()
            reply_headers={'Content-Type':'application/json'}
        elif policy=='custom':
            result=custom if endpoint['kind']=='list' else {'hasUpdate':bool(custom['updates']),**({'update':custom['updates'][0]} if custom['updates'] else {})} if endpoint['kind']=='check' else {'allow':bool(custom['updates'])} if endpoint['kind']=='allow' else {'status':'ok'}
            status,raw=200,json.dumps(result,ensure_ascii=False).encode()
            reply_headers={'Content-Type':'application/json'}
        elif error and not original:
            raw=b'{"error":"update_upstream_unavailable"}'
        return status,raw,{k:v for k,v in reply_headers.items() if k.lower() in ('content-type','retry-after')},original,attempt

    def note_request(self,endpoint,target,method,headers,body,device_sn):
        """Последний реальный запрос устройства к OTA-эндпойнту: из него суточный
        таймер переспрашивает реальный сервер, когда устройство молчит."""
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO last_requests VALUES (?,?,?,?,?,?,?)',
                       (endpoint['id'],device_sn,method,target,json.dumps(dict(headers)),
                        sqlite3.Binary(body or b''),time.time()))

    def due_requests(self):
        """Запросы устройств, не стучавшихся к OTA-эндпойнту более суток."""
        now=time.time()
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM last_requests') if now-r['ts']>=86400]

    def mark_polled(self,row):
        with self.connect() as db:
            db.execute('UPDATE last_requests SET ts=? WHERE endpoint=? AND device_sn=?',(time.time(),row['endpoint'],row['device_sn']))

    async def poll_loop(self):
        """Раз в 10 минут: если устройство не обращалось к эндпойнту более суток —
        эмулятор сам повторяет его последний запрос к реальному серверу и
        пополняет каталог ответом (независимо от политики устройства)."""
        while True:
            await asyncio.sleep(600)
            try:
                for row in await asyncio.to_thread(self.due_requests):
                    try:
                        status,reply,raw=await self.fetch(row['target'],row['method'],json.loads(row['headers'] or '{}'),bytes(row['body'] or b''))
                        try:payload=json.loads(raw)
                        except (ValueError,UnicodeError):payload=None
                        attempt={'id':uuid.uuid4().hex,'ts':time.time(),'endpoint':row['endpoint'],
                                 'device_sn':row['device_sn'],'user_id':None,'device_id':None,
                                 'policy':'auto','status':status,'error':None,'ip':None}
                        await asyncio.to_thread(self.record,attempt,payload)
                        await asyncio.to_thread(self.mark_polled,row)
                        log.info('Суточный опрос обновлений: %s (%s) — ответ записан в каталог',row['device_sn'],row['endpoint'])
                    except Exception as exc:
                        log.warning('Суточный опрос обновлений не удался (%s): %s',row['device_sn'],exc)
            except Exception:
                log.exception('Цикл суточного опроса обновлений')

    @staticmethod
    def effective(settings,device):
        """(политика, свой ответ) для устройства: SN → платформа → общая."""
        platform=platform_of(device)
        policy,custom=settings['policy'],settings['custom']
        if platform in settings.get('platforms',{}):
            policy=settings['platforms'][platform]
            custom=settings.get('custom_platforms',{}).get(platform,custom)
        if device and device['sn'] in settings['devices']:
            policy=settings['devices'][device['sn']]
            custom=settings.get('custom_devices',{}).get(device['sn'],custom)
        return policy,custom

    def inventory(self,sn=None,sns=None):
        with self.connect() as db:
            if sn:
                urls=db.execute('SELECT u.*,COUNT(*) scoped_hits FROM urls u JOIN sightings s ON s.url=u.url JOIN attempts a ON a.id=s.attempt_id WHERE a.device_sn=? GROUP BY u.url ORDER BY u.last_seen DESC LIMIT 200',(sn,)).fetchall()
                attempts=db.execute('SELECT * FROM attempts WHERE device_sn=? ORDER BY ts DESC LIMIT 100',(sn,)).fetchall()
            elif sns is not None:
                marks=','.join('?'*len(sns)) or "''"
                urls=db.execute(f'SELECT u.*,COUNT(*) scoped_hits FROM urls u JOIN sightings s ON s.url=u.url JOIN attempts a ON a.id=s.attempt_id WHERE a.device_sn IN ({marks}) GROUP BY u.url ORDER BY u.last_seen DESC LIMIT 200',tuple(sns)).fetchall()
                attempts=db.execute(f'SELECT * FROM attempts WHERE device_sn IN ({marks}) ORDER BY ts DESC LIMIT 100',tuple(sns)).fetchall()
            else:
                urls=db.execute('SELECT * FROM urls ORDER BY last_seen DESC LIMIT 200').fetchall()
                attempts=db.execute('SELECT * FROM attempts ORDER BY ts DESC LIMIT 100').fetchall()
            # Каталог — все найденные URL (коллекция не зависит от области) и SN, у кого встречены
            catalog=db.execute('SELECT u.*,GROUP_CONCAT(DISTINCT a.device_sn) sns FROM urls u LEFT JOIN sightings s ON s.url=u.url LEFT JOIN attempts a ON a.id=s.attempt_id GROUP BY u.url ORDER BY u.last_seen DESC LIMIT 500').fetchall()
            # Версии, предложенные в каждой попытке (sightings → urls → metadata.version_code)
            offered={}
            ids=[a['id'] for a in attempts]
            if ids:
                marks=','.join('?'*len(ids))
                for s in db.execute(f'SELECT s.attempt_id aid,u.metadata FROM sightings s JOIN urls u ON u.url=s.url WHERE s.attempt_id IN ({marks})',ids):
                    try:vc=json.loads(s['metadata']).get('version_code')
                    except (ValueError,TypeError):vc=None
                    if vc:offered.setdefault(s['aid'],[]).append(int(vc))
            return {'settings':self.settings(),'urls':[{**dict(r),'metadata':json.loads(r['metadata'])} for r in urls],'attempts':[dict(r) for r in attempts],
                    'catalog':[{**dict(r),'metadata':json.loads(r['metadata']),'sns':[x for x in (r['sns'] or '').split(',') if x]} for r in catalog],
                    'offered_versions':{k:sorted(set(v),reverse=True) for k,v in offered.items()},
                    'extensions':self.extension_rules()}  # правила updates/ расширений (TASK-032)
