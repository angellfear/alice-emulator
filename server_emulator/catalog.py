"""Lossless wire editing for the observed Centaur catalog protocol.

Расширения (TASK-032): папка catalog/ включённого расширения добавляет приложения
в каталог (только чтение): *.json — запись {name, package, version, version_code,
description, icon, apk_url}; id без префикса получает <ext-id>.; источник APK —
apk_url (http(s), станция качает напрямую; локальная выдача /__catalog/apk —
только для загруженных пользователем); пользовательская запись с тем же id
побеждает; выключение расширения убирает записи; некорректная запись — warning
без падения раздела. Слои — extensions/layers; callable ставит runtime.
"""
import hashlib
import json
import logging
import re
import time
import uuid
import zipfile
from pathlib import Path
from aiohttp import web

log = logging.getLogger(__name__)


def vint(n):
    out=bytearray()
    while n>127: out.append((n&127)|128); n>>=7
    return bytes(out)+bytes([n])


def fields(data):
    pos=0
    def read():
        nonlocal pos
        value=0
        for shift in range(0,70,7):
            if pos>=len(data): raise ValueError('Оборванный protobuf')
            b=data[pos];pos+=1;value|=(b&127)<<shift
            if b<128:return value
        raise ValueError('Некорректный varint')
    while pos<len(data):
        start=pos;tag=read();kind=tag&7;number=tag>>3
        if not number:raise ValueError('Некорректное поле')
        if kind==2:
            length=read();value=data[pos:pos+length];pos+=length
        elif kind==0:value=read()
        elif kind in (1,5):
            length=8 if kind==1 else 4;value=data[pos:pos+length];pos+=length
        else:raise ValueError('Неизвестный wire type')
        if pos>len(data):raise ValueError('Оборванное поле')
        yield number,kind,value,data[start:pos]


def textfield(n,value):
    b=value.encode() if isinstance(value,str) else value
    return vint(n*8+2)+vint(len(b))+b


def catalog_view(data):
    groups=[]
    for n,k,v,_ in fields(data):
        if n!=1 or k!=2:continue
        group={'apps':[]}
        for f,t,b,_ in fields(v):
            if f==1 and t==0:group['id']=b
            elif f==3 and t==2:group['title']=b.decode('utf8')
            elif f==2 and t==2:
                names={1:'id',2:'name',3:'description',4:'icon',5:'package',6:'apk_url',7:'version',8:'version_code'}
                group['apps'].append({names[x]:y.decode('utf8') if z==2 else y for x,z,y,_ in fields(b) if x in names})
        groups.append(group)
    return groups


LEGACY_BASE_URL = 'http://rpc.local'  # старый дефолт catalog.json (*.local → настоящий домен)
MODES = ('direct', 'merged', 'own')


class Catalog:
    def __init__(self,root,extension_dirs=None):
        self.directory=Path(root)/'data'/'catalog';self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'catalog.json'
        self.extension_dirs=extension_dirs  # callable → [(origin, Path)] — слои catalog/, TASK-032
        value=json.loads(self.path.read_text('utf8')) if self.path.exists() else {}
        self.value=self._migrate(value)

    @staticmethod
    def _migrate(value):
        """catalog.json: выключатель `enabled` → режим `mode` (true → merged, false → direct);
        `http://rpc.local` → настоящий домен; разделы и режим присутствуют всегда."""
        if 'enabled' in value:
            value['mode']='merged' if value.pop('enabled') else 'direct'
        value['mode']=value.get('mode') if value.get('mode') in MODES else 'direct'
        value['sections']=[s for s in value.get('sections') or [] if isinstance(s,dict) and s.get('id')]
        value.setdefault('public_base_url','http://rpc.alice.yandex.net')
        value.setdefault('apps',[])
        if value.get('public_base_url')==LEGACY_BASE_URL:
            value['public_base_url']='http://rpc.alice.yandex.net'
        return value

    # --- слой расширений (TASK-032) ----------------------------------------------------
    def _extension_layers(self):
        """Слои catalog/ включённых расширений: [(origin, Path)] (callable из runtime —
        extensions/layers.source_layers). Слой не задан или сбой — пусто, раздел работает."""
        if self.extension_dirs is None:
            return []
        try:
            return [(origin,Path(path)) for origin,path in self.extension_dirs()]
        except Exception:  # noqa: BLE001 — расширения не должны ломать раздел
            return []

    def extension_apps(self):
        """Приложения каталога из расширений: {id: запись} — *.json файла {name, package,
        version, version_code, description, icon, apk_url}; id без префикса получает
        <ext-id>.; обязательны name/package/version_code и apk_url (http(s) — станция
        качает напрямую); некорректная запись — warning, раздел работает. Поздний слой
        перекрывает ранний."""
        from .extensions.layers import layer_records
        def warn(message):log.warning('Расширения (каталог приложений): %s',message)
        result={}
        for origin,path,value in layer_records(self._extension_layers(),warn=warn):
            ext_id=origin.split(':',1)[1] if str(origin).startswith('extension:') else ''
            if not isinstance(value,dict):
                warn(f'{path}: нужен JSON-объект с записью приложения');continue
            value=dict(value)
            identifier=value.get('id') or path.stem
            if ext_id and not str(identifier).startswith(ext_id+'.'):
                identifier=f'{ext_id}.{identifier}'
            name=str(value.get('name') or '').strip()
            package=str(value.get('package') or '').strip()
            if not name:warn(f'{path}: укажите название приложения');continue
            if not re.fullmatch(r'[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+',package,re.ASCII):
                warn(f'{path}: некорректный package: {package!r}');continue
            try:code=int(value.get('version_code'))
            except (TypeError,ValueError):code=0
            if not 0<code<2**63:warn(f'{path}: некорректный versionCode');continue
            apk=value.get('apk_url') or ''
            if not re.match(r'https?://',apk):
                warn(f'{path}: нужен apk_url http(s) (локальная выдача APK — только для загруженных)');continue
            result[identifier]={'id':identifier,'name':name,'package':package,
                                'version':str(value.get('version') or ''),
                                'description':str(value.get('description') or ''),
                                'icon':str(value.get('icon') or ''),'version_code':code,
                                'apk_url':apk,'origin':origin,
                                'section':str(value.get('section') or '')}
        return result

    def apps(self):
        """Записи каталога: пользовательские (catalog.json) + приложения включённых
        расширений (только чтение; пользовательская запись с тем же id побеждает)."""
        user=self.value['apps']
        known={a.get('id') for a in user}
        extension=self.extension_apps()
        return [*user,*[app for identifier,app in extension.items() if identifier not in known]]

    def save(self):
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.value,ensure_ascii=False,indent=2),'utf8');tmp.replace(self.path)

    def _record(self,app,base_url):
        """Запись приложения в protobuf: поля 1–7 текстовые, поле 8 — varint versionCode
        (карта catalog_view из наблюдённого каталога; колонка сравнивает его с установленной
        версией — запись в чужом поле заставляла приложение вечно показывать «Обновить»)."""
        effective={**app,'icon':base_url+app['icon'] if app.get('icon','').startswith('/__catalog/') else app.get('icon','')}
        item=b''.join(textfield(n,str(effective.get(key,''))) for n,key in [(1,'id'),(2,'name'),(3,'description'),(4,'icon'),(5,'package'),(7,'version')])
        # APK: загруженный пользователем раздаётся эмулятором, приложение расширения — прямой ссылкой
        item+=textfield(6,base_url+'/__catalog/apk/'+app['sha256'] if app.get('sha256') else app.get('apk_url',''))
        return item+vint(8*8+0)+vint(int(app['version_code']))

    def build_own(self,base_url):
        """Каталог только из своих приложений: группы своих разделов (по order) + «Другое».
        Свой раздел — группа без категории (enum 0 «Другое» наблюдённого каталога) со своим
        заголовком: колонка группирует по enum, новые значения enum не изобретаются."""
        apps=self.apps();base_url=(base_url or self.value['public_base_url']).rstrip('/')
        out=[]
        for section in sorted(self.value.get('sections') or [],key=lambda s:s.get('order',0)):
            members=[a for a in apps if a.get('section')==section['id']]
            if members:
                group=b''.join(textfield(2,self._record(a,base_url)) for a in members)
                out.append(textfield(1,textfield(3,str(section.get('title') or section['id']))+group))
        other=[a for a in apps if not a.get('section') or not any(a.get('section')==s['id'] for s in self.value.get('sections') or [])]
        out.append(textfield(1,textfield(3,'Другое')+(b''.join(textfield(2,self._record(a,base_url)) for a in other) if other else b'')))
        return b''.join(out)

    def patch(self,data,base_url=None):
        """Ответ get_app_catalog по режиму: direct — база как есть; merged — свои приложения
        в свои разделы (в группы Яндекса — по заголовку группы) и «Другое»; own — build_own
        без базы. Пустая база в merged — только свои."""
        mode=self.value.get('mode','direct')
        apps=self.apps()  # пользовательские + приложения расширений
        base_url=(base_url or self.value['public_base_url']).rstrip('/')
        if mode=='direct':return data
        if mode=='own' or (not apps and mode=='merged') or not data:
            return self.build_own(base_url) if mode=='own' or apps else data
        own_ids={s['id'] for s in self.value.get('sections') or []}
        own_titles={s['id']:str(s.get('title') or s['id']) for s in self.value.get('sections') or []}
        other=[a for a in apps if not a.get('section') or a.get('section') not in own_ids and a.get('section') not in {g['title'] for g in catalog_view(data) if g.get('title')}]
        result=[];merged_other=False
        for n,k,value,raw in fields(data):
            if n==1 and k==2:
                title=next((v.decode('utf8') for f,t,v,_ in fields(value) if f==3 and t==2),'')
                # свои приложения, чей раздел — заголовок группы Яндекса
                additions=b''.join(textfield(2,self._record(a,base_url)) for a in apps
                                   if a.get('section') and a.get('section') not in own_ids and a.get('section')==title)
                if additions:
                    result.append(textfield(1,value+additions));continue  # пересборка группы
                category=next((v for f,t,v,_ in fields(value) if f==1 and t==0),0)
                if category==0:
                    # «Другое»: свои приложения без раздела и с неизвестным разделом
                    result.append(textfield(1,value+b''.join(textfield(2,self._record(a,base_url)) for a in other)))
                    merged_other=True;continue
            result.append(raw)
        # свои разделы — после групп Яндекса, по order
        for section in sorted(self.value.get('sections') or [],key=lambda s:s.get('order',0)):
            members=[a for a in apps if a.get('section')==section['id']]
            if members:
                group=b''.join(textfield(2,self._record(a,base_url)) for a in members)
                result.append(textfield(1,textfield(3,own_titles[section['id']])+group))
        if not merged_other and other:
            result.append(textfield(1,textfield(3,'Другое')+b''.join(textfield(2,self._record(a,base_url)) for a in other)))
        return b''.join(result)

    def store_upstream(self,raw):
        """Последний удачный ответ Яндекса на get_app_catalog — в кеш (data/catalog/upstream.bin)."""
        (self.directory/'upstream.bin').write_bytes(raw)
        (self.directory/'upstream.json').write_text(json.dumps({'ts':time.time()}),'utf8')

    def upstream_cache(self):
        """Кеш ответа Яндекса: (protobuf, время получения) или (None, 0)."""
        try:
            raw=(self.directory/'upstream.bin').read_bytes()
            stamp=json.loads((self.directory/'upstream.json').read_text('utf8')).get('ts',0)
        except (OSError,ValueError):
            return None,0
        return raw,float(stamp or 0)

    def observed(self):
        """Каталог Яндекса для панели: группы из кеша последнего ответа (только чтение)."""
        if time.monotonic()-getattr(self,'observed_at',-1000)<60:return self.observed_cache
        raw,stamp=self.upstream_cache()
        groups=[]
        if raw:
            try:
                groups=catalog_view(raw)
            except ValueError:
                groups=[]
            else:
                self.observed_raw=raw;self.observed_ts=stamp
        self.observed_at=time.monotonic();self.observed_cache=groups
        return groups

    async def get(self,request):
        import asyncio
        raw,stamp=await asyncio.to_thread(self.upstream_cache)
        groups=[]
        if raw:
            try:groups=catalog_view(raw)
            except ValueError:groups=[]
        return web.json_response({'mode':self.value.get('mode','direct'),'sections':self.value.get('sections') or [],
                                  'apps':self.apps(),'yandex':{'groups':groups,'ts':stamp,'cached':raw is not None}})

    async def settings(self,request):
        value=await request.json()
        if 'mode' in value:
            if value['mode'] not in MODES:raise ValueError('mode: ' + ' | '.join(MODES))
            self.value['mode']=value['mode']
        if 'sections' in value:
            sections=[]
            for item in value['sections'] or []:
                if not isinstance(item,dict) or not str(item.get('title') or '').strip():
                    raise ValueError('Раздел: объект с title')
                sections.append({'id':str(item.get('id') or uuid.uuid4())[:64],
                                 'title':str(item['title']).strip()[:100],
                                 'order':int(item.get('order') or 0)})
            known={s['id'] for s in sections}
            # раздел удалён — его приложения переходят в «Другое»
            for app in self.value['apps']:
                if app.get('section') and app['section'] not in known:app['section']=''
            self.value['sections']=sections
        self.save()
        return web.json_response({'mode':self.value.get('mode','direct'),'sections':self.value.get('sections') or []})

    async def upload(self,request):
        temp=self.directory/(uuid.uuid4().hex+'.upload');digest=hashlib.sha256();size=0
        try:
            with temp.open('wb') as out:
                async for chunk in request.content.iter_chunked(256*1024):
                    size+=len(chunk)
                    if size>512*1024**2:raise ValueError('Лимит APK: 512 MiB')
                    digest.update(chunk);out.write(chunk)
            import asyncio
            from .apk_metadata import read_apk
            try:metadata=await asyncio.to_thread(read_apk,temp)
            except Exception as exc:raise ValueError('Не удалось разобрать APK: '+str(exc)) from exc
            sha=digest.hexdigest();target=self.directory/(sha+'.apk')
            if target.exists():temp.unlink()
            else:temp.replace(target)
            icon=metadata.pop('icon_bytes')
            if icon:(self.directory/(sha+'.icon')).write_bytes(icon)
            previous=next((a for a in self.value['apps'] if a['package']==metadata['package']),{})
            app={**metadata,'id':previous.get('id',str(uuid.uuid4())),'icon':'/__catalog/icon/'+sha if icon else '', 'sha256':sha,'size':size}
            package=app['package']
            self.value['apps']=[a for a in self.value['apps'] if a['package']!=package]+[app];self.save()
            return web.json_response(app)
        finally:temp.unlink(missing_ok=True)

    async def edit(self,request):
        app=next((a for a in self.value['apps'] if a['id']==request.match_info['id']),None)
        if app is None:raise web.HTTPNotFound()
        value=await request.json()
        updates={k:str(value[k]).strip() for k in ('name','package','version','description','icon','section') if k in value}
        if not updates.get('name',app['name']):raise ValueError('Укажите название')
        package=updates.get('package',app['package'])
        if not re.fullmatch(r'[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+',package,re.ASCII):raise ValueError('Некорректный package')
        if any(a['id']!=app['id'] and a['package']==package for a in self.value['apps']):raise ValueError('Этот package уже есть в каталоге')
        if 'version_code' in value:
            code=int(value['version_code'])
            if not 0<code<2**63:raise ValueError('Некорректный versionCode')
            updates['version_code']=code
        app.update(updates);self.save()
        return web.json_response(app)

    async def icon(self,request):
        sha=request.match_info['hash']
        if not re.fullmatch('[0-9a-f]{64}',sha) or not (self.directory/(sha+'.icon')).exists():raise web.HTTPNotFound()
        path=self.directory/(sha+'.icon');magic=path.read_bytes()[:12]
        mime='image/png' if magic.startswith(b'\x89PNG') else 'image/webp' if magic[:4]==b'RIFF' else 'image/jpeg'
        return web.FileResponse(path,headers={'Content-Type':mime,'Cache-Control':'public, max-age=86400'})

    async def delete(self,request):
        self.value['apps']=[a for a in self.value['apps'] if a['id']!=request.match_info['id']];self.save()
        return web.json_response({'ok':True})

    async def download(self,request):
        sha=request.match_info['hash']
        if not re.fullmatch('[0-9a-f]{64}',sha) or not any(a['sha256']==sha for a in self.value['apps']):raise web.HTTPNotFound()
        return web.FileResponse(self.directory/(sha+'.apk'),headers={'Content-Type':'application/vnd.android.package-archive'})
