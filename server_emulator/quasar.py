"""Quasar HTTP compatibility, informed by v3rt3p/quasar-server (see NOTICE)."""
import base64
import copy
import hashlib
import hmac
import json
import secrets
import time


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


class QuasarBackend:
    def __init__(self, runtime):
        self.runtime = runtime
        path = runtime.directory/'quasar-secret.json'
        if path.exists():
            self.key = bytes.fromhex(json.loads(path.read_text())['key'])
        else:
            self.key = secrets.token_bytes(32)
            runtime.write(path.name, {'key':self.key.hex()})

    def token(self, device_id, user_id):
        now = int(time.time())
        head = b64(b'{"alg":"HS256","typ":"JWT"}')
        body = b64(json.dumps({'sub':device_id,'uid':user_id,'iss':'quasar-backend','aud':'glagol','iat':now,'exp':now+86400},separators=(',',':')).encode())
        data = head+'.'+body
        return data+'.'+b64(hmac.new(self.key,data.encode(),hashlib.sha256).digest())

    def verify(self, token, device):
        try:
            head, body, signature = token.decode().split('.')
            if not hmac.compare_digest(signature,b64(hmac.new(self.key,(head+'.'+body).encode(),hashlib.sha256).digest())):
                return False
            header = json.loads(base64.urlsafe_b64decode(head+'='*(-len(head)%4)))
            claims = json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)))
            return header.get('alg')=='HS256' and claims.get('aud')=='glagol' and claims.get('iss')=='quasar-backend' and isinstance(claims.get('exp'),(int,float)) and claims['exp']>time.time() and claims.get('uid')==device['user_id'] and claims.get('sub') in device['device_ids']
        except (ValueError,TypeError,KeyError,UnicodeError):
            return False

    ROUTES = {('GET','/check_updates'),('GET','/get_sync_info'),('GET','/glagol/device_list'),('GET','/glagol/token'),('POST','/glagol/check_token'),('POST','/glagol/v2.0/check_token'),('POST','/update_device_state'),('POST','/set_device_capability')}

    def list_caller(self, client_ip):
        """Вызывающий /glagol/device_list без маркера устройства: устройство по IP
        клиента (может оказаться непривязанным) либо {'user_id'} единственного владельца
        среди известных устройств. Неопределим (владельцев 0 или несколько) — None."""
        identities = self.runtime.identities
        caller = identities.resolve_by_ip(client_ip)
        if caller is not None:
            return caller
        owners = {d['user_id'] for d in identities.data['devices'] if d.get('user_id')}
        return {'user_id':next(iter(owners))} if len(owners)==1 else None

    @staticmethod
    def platform_of(device):
        return (device.get('state') or {}).get('platform') or (device.get('profile') or {}).get('platform') or 'unknown'

    def group_payload(self, device):
        """Стереопара устройства из реестра: {id, name, secret, devices:[{id, platform, role}]}
        (формат syncd; id участника — первый из device_ids). Вне группы — None."""
        identities = self.runtime.identities
        group = identities.group_of(device['sn'])
        if not group:
            return None
        known = {d['sn']: d for d in identities.data['devices']}
        members = []
        for member in group['devices']:
            other = known.get(member['sn'])
            if other is None:
                return None
            members.append({'id':next(iter(other['device_ids']),other['sn']),'platform':self.platform_of(other),'role':member['role']})
        return {'id':group['id'],'name':group['name'],'secret':group['secret'],'devices':members}

    def glagol_security(self, device):
        """server_certificate/server_private_key устройства из параметров
        device.glagol_cert / device.glagol_key (контекст этого устройства). Пара неполна — {}."""
        from .quasar_config import QuasarConfigError, context_for, resolve_param
        context = context_for(self.runtime, device)
        security = {}
        for field, param in (('server_certificate','device.glagol_cert'),('server_private_key','device.glagol_key')):
            try:
                value = resolve_param(param, self.runtime, device, context)
            except QuasarConfigError:
                return {}
            if not isinstance(value, str) or not value:
                return {}
            security[field] = value
        return security

    def device_list(self, user_id):
        """Элементы /glagol/device_list: устройства одного владельца. Как в реальном ответе,
        glagol.security есть у каждого устройства списка (у каждого — свои параметры)."""
        devices = []
        for other in self.runtime.identities.data['devices']:
            if other['user_id'] != user_id:
                continue
            own_state = other['state']
            item = {'id':next(iter(other['device_ids']),other['sn']),'name':other['name'],'platform':own_state.get('platform','unknown'),
                    'activation_code':own_state.get('activation_code',''),'activation_region':own_state.get('activation_region',''),
                    'promocode_activated':bool(own_state.get('promocode_activated',False)),
                    'networkInfo':own_state.get('networkInfo',{}),'config':own_state.get('quasar',{}).get('config',{}).get('device_config',{}),'tags':[]}
            security = self.glagol_security(other)
            if security:
                item['glagol'] = {'security':security}
            group = self.group_payload(other)
            if group:
                item['group'] = {'devices':group['devices']}
            devices.append(item)
        return devices

    def handle(self, method, path, device, device_id, body, caller=None, caller_id=None, client_ip=None):
        routes = self.ROUTES
        if (method,path) not in routes:
            return None
        if path=='/check_updates':
            return 200, {'hasUpdate':False}
        if path=='/glagol/token':
            # v9 §8: query device_id — ЦЕЛЕВОЕ устройство (токен «для него»), вызывающий
            # определяется заголовками (или, без заголовков, сам запрашивает для себя).
            if caller_id:
                # вызывающий известен по заголовкам: непривязан — отказ, фолбэка на цель нет
                if not caller or not caller.get('user_id'):
                    return 403, {'error':'caller_not_assigned','hint':'Calling device must be bound to an owner'}
            else:
                caller = caller or device  # без заголовков: устройство запрашивает для себя
            if device_id and device is None:
                return 404, {'error':'target_unknown','hint':'device_id does not resolve to a known device'}
            if device is not None and device.get('user_id') != caller.get('user_id'):
                return 403, {'error':'forbidden','hint':'Glagol token is issued only for devices of the same owner'}
            if device is not None:
                sub, uid = next(iter(device['device_ids']), device_id), device['user_id']
            else:  # нет query — токен вызывающего для самого себя
                sub, uid = next(iter(caller['device_ids']), ''), caller['user_id']
            return 200, {'status':'ok','token':self.token(sub, uid)}
        # Вызывающий неопределим (запрос без маркеров устройства — только OAuth
        # аккаунта): builtin не знает, чьи данные отдавать, — уступает каталогу
        # эндпоинтов (захваченный реальный ответ). Известное, но непривязанное
        # устройство — по-прежнему честный 403.
        if device is None and path == '/glagol/device_list':
            # Без маркера устройства: сначала привязка по IP клиента (B03, activity реестра),
            # затем единственный владелец среди известных устройств; иначе — каталог/404.
            who = self.list_caller(client_ip)
            if who is None:
                return None
            if not who.get('user_id'):
                return 403, {'error':'device_not_assigned','hint':'Bind DeviceId to SN and owner in emulator UI'}
            return 200, {'status':'ok','devices':self.device_list(who['user_id'])}
        if device is None and path.endswith('/check_token'):
            return None
        if not device or not device.get('user_id'):
            return 403, {'error':'device_not_assigned','hint':'Bind DeviceId to SN and owner in emulator UI'}
        state = copy.deepcopy(device['state'])
        if path=='/get_sync_info':
            # Слоёная модель: common → merge-patch платформы → переопределения
            # аккаунта владельца → переопределения устройства → инлайн-узлы → динамика.
            common, platform_patch = self.runtime.quasar_platforms.resolve_files(device)
            if common is not None:
                try:
                    from .quasar_config import account_layer, compose
                    account_overrides, account_name = account_layer(self.runtime, device)
                    body = compose(common, platform_patch,
                                   state.get('quasar_overrides'), self.runtime, device,
                                   account_overrides=account_overrides, account_name=account_name)
                except Exception as exc:  # битый источник: колонке нужен внятный ответ
                    return 409, {'error':'quasar_config_source','hint':str(exc)[:200]}
                if not isinstance(body, dict) or not isinstance(body.get('config'), dict):
                    return 409, {'error':'quasar_config_no_config','hint':'В файле платформы нет sync.config'}
                result = {'status':'ok', **body}
                group = self.group_payload(device)
                if group:
                    result['group'] = group  # реестр стереопар главнее узла шаблона
                return 200, result
            # Legacy-путь (до миграции на платформы): полный конфиг в state.quasar
            quasar = state.get('quasar',{})
            if not isinstance(quasar.get('config'),dict):
                return 409, {'error':'device_config_not_set','hint':'Set state.quasar.config for this SN'}
            result = {'status':'ok','config':quasar['config'],'tags':quasar.get('tags',[])}
            for key in ('glagol','subscription','alice_pro_subscription'):
                if key in quasar:
                    result[key] = quasar[key]
            group = self.group_payload(device)
            if group:
                result['group'] = group
            return 200, result
        if path=='/glagol/device_list':
            return 200, {'status':'ok','devices':self.device_list(device['user_id'])}
        if path=='/glagol/token':
            return 200, {'status':'ok','token':self.token(device_id,device['user_id'])}
        if path.endswith('/check_token'):
            valid = bool(self.verify(body,device))
            return 200, {'status':'ok',**({'owner':valid,'guest':False} if '/v2.0/' in path else {'valid':valid})}
        try:
            value=json.loads(body)
        except (ValueError,UnicodeError):
            if path=='/set_device_capability':
                # станции шлют capabilities protobuf-телом (не JSON): сохранить нечего,
                # ack-ответ отдаёт каталог эндпоинтов (захваченный 200)
                return None
            return 400, {'error':'expected_json'}
        if not isinstance(value,dict):
            return 400, {'error':'expected_object'}
        if path=='/update_device_state':
            state['networkInfo'] = value
        else:
            state['reported_capabilities'] = value
        self.runtime.identities.mutate({'operation':'state','sn':device['sn'],'state':state})
        return 200, {'status':'ok'}
