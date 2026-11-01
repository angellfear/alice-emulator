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


class Catalog:
    def __init__(self,root,extension_dirs=None):
        self.directory=Path(root)/'data'/'catalog';self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'catalog.json'
        self.extension_dirs=extension_dirs  # callable → [(origin, Path)] — слои catalog/, TASK-032
        self.value=json.loads(self.path.read_text('utf8')) if self.path.exists() else {'enabled':False,'public_base_url':'http://rpc.alice.yandex.net','apps':[]}
        if self.value.get('public_base_url')==LEGACY_BASE_URL:  # *.local → настоящий домен
            self.value['public_base_url']='http://rpc.alice.yandex.net';self.save()

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
                                'apk_url':apk,'origin':origin}
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

    def patch(self,data,base_url=None):
        apps=self.apps()  # пользовательские + приложения расширений (TASK-032)
        if not self.value['enabled'] or not apps:return data
        # Category 0 is the observed 'Other' enum; avoid inventing an unsupported enum.
        base_url=(base_url or self.value['public_base_url']).rstrip('/')
        group=b''
        for app in apps:
            effective={**app,'icon':base_url+app['icon'] if app.get('icon','').startswith('/__catalog/') else app.get('icon','')}
            item=b''.join(textfield(n,str(effective.get(key,''))) for n,key in [(1,'id'),(2,'name'),(3,'description'),(4,'icon'),(5,'package'),(7,'version')])
            # APK: загруженный пользователем раздаётся эмулятором, приложение расширения — прямой ссылкой
            item+=textfield(6,base_url+'/__catalog/apk/'+app['sha256'] if app.get('sha256') else app.get('apk_url',''))
            item+=vint(64)+vint(app['version_code'])
            group+=textfield(2,item)
        result=[];merged=False
        for n,k,value,raw in fields(data):
            if n==1 and k==2 and not merged:
                category=next((v for f,t,v,_ in fields(value) if f==1 and t==0),0)
                if category==0:
                    result.append(textfield(1,value+group));merged=True;continue
            result.append(raw)
        if not merged:result.append(textfield(1,textfield(3,'Другое')+group))
        return b''.join(result)

    def observed(self):
        import time,gzip
        if time.monotonic()-getattr(self,'observed_at',-1000)<60:return self.observed_cache
        groups=[]
        if hasattr(self,'store'):
            with self.store.reader() as db:
                row=db.execute("SELECT ep.hash FROM events e JOIN event_payloads ep ON ep.event_id=e.id WHERE e.url='/gproxy/get_app_catalog' AND ep.role='response_original' AND e.status=200 ORDER BY e.ts DESC LIMIT 1").fetchone()
            if row:
                raw=self.store.payload(row[0])
                if raw.startswith(b'\x1f\x8b'):
                    import zlib
                    inflater=zlib.decompressobj(31);raw=inflater.decompress(raw,8*1024**2+1)
                    if len(raw)>8*1024**2 or not inflater.eof:raise ValueError('Каталог превышает лимит')
                # Захват не обязан быть каталогом: например, локальный ответ эмулятора
                # с JSON-ошибкой тоже пишется как response_original со статусом 200.
                try:
                    groups=catalog_view(raw)
                except ValueError:
                    groups=[]
                else:
                    self.observed_raw=raw
        self.observed_at=time.monotonic();self.observed_cache=groups
        return groups

    async def get(self,request):
        import asyncio
        return web.json_response({**self.value,'apps':self.apps(),'observed':await asyncio.to_thread(self.observed)})

    async def settings(self,request):
        value=await request.json()
        self.value['enabled']=bool(value.get('enabled'));self.save()
        return web.json_response(self.value)

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
        updates={k:str(value[k]).strip() for k in ('name','package','version','description','icon') if k in value}
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
