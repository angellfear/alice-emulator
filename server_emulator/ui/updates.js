(() => {
  // Обновления · OTA: политика на трёх уровнях — общая → платформа → устройство (SN).
  // Сверху две полосы карточек DeviceCards: платформы (класс устройств; ничего не
  // выбрано — общая политика) и устройства выбранной платформы. Выбор карточки
  // задаёт область: её политику, свой ответ, найденные URL и журнал попыток.
  const root=document.getElementById('updates');
  let inventory,registry,draft,scope={kind:'all'};
  const POLICY_TITLES={allow:'Разрешить · реальный ответ',deny:'Запретить · обновлений нет',custom:'Свой апдейт'};
  const POLICY_BADGES={allow:{text:'Разрешено',tone:'ok'},deny:{text:'Запрет',tone:'danger'},custom:{text:'Свой',tone:'accent'}};
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=text;if(cls)n.className=cls;return n};
  async function api(action,value){const r=await fetch('api/'+action,{method:value?'POST':'GET',headers:{'X-Emulator':'1','Content-Type':'application/json'},body:value?JSON.stringify(value):undefined});const v=await r.json();if(!r.ok)throw Error(v.error||r.statusText);return v}
  const notice=text=>{document.getElementById('notice').textContent=text};
  async function work(fn){try{await fn()}catch(e){notice(e.message)}}
  function field(parent,title,value){const label=el('label',title),input=el('input');input.value=value||'';label.append(input);parent.append(label);return input}
  function choice(parent,title,options,value){const label=el('label',title),s=el('select');s.setAttribute('aria-label',title);for(const [v,t] of options){const o=el('option',t);o.value=v;s.append(o)}s.value=value;label.append(s);parent.append(label);return s}

  const query=()=>scope.kind==='device'?'?sn='+encodeURIComponent(scope.sn):scope.kind==='platform'?'?platform='+encodeURIComponent(scope.platform):'';
  async function load(){[inventory,registry]=await Promise.all([api('updates'+query()),api('identities')]);draft=structuredClone(inventory.settings);draft.platforms??={};draft.custom_platforms??={};draft.custom_devices??={};render()}
  const platformOf=sn=>(inventory.device_platforms||{})[sn]||'';
  // Политика уровня выше выбранного — то, что «наследуется»
  const inherited=()=>scope.kind==='device'&&draft.platforms[platformOf(scope.sn)]?{policy:draft.platforms[platformOf(scope.sn)],from:'платформы'}:{policy:draft.policy,from:'общей политики'};
  const ownPolicy=()=>scope.kind==='device'?draft.devices[scope.sn]:scope.kind==='platform'?draft.platforms[scope.platform]:draft.policy;
  const ownCustom=()=>(scope.kind==='device'?draft.custom_devices[scope.sn]:scope.kind==='platform'?draft.custom_platforms[scope.platform]:null)||draft.custom;

  function cardsBlock(){
    const devices=registry.devices.map(d=>({...d,platform:platformOf(d.sn),status:DeviceCards.status(d,registry)}));
    const platforms=[...new Set([...devices.map(d=>d.platform),...Object.keys(draft.platforms)])]
      .sort((a,b)=>DeviceKit.platformInfo(a).name.localeCompare(DeviceKit.platformInfo(b).name,'ru'));
    const selectedPlatform=scope.kind==='platform'?scope.platform:scope.kind==='device'?platformOf(scope.sn):null;
    const pick=next=>{scope=next;work(load)};
    const box=el('div',null,'upd-cards');
    box.append(el('h3','Платформы'),DeviceCards.grid([
      DeviceCards.all({title:'Все платформы',subtitle:devices.length+' устр',selected:scope.kind==='all',
        badge:POLICY_BADGES[draft.policy],onClick:()=>pick({kind:'all'})}),
      ...platforms.map(p=>DeviceCards.platform(p,{subtitle:devices.filter(d=>d.platform===p).length+' устр',
        selected:scope.kind==='platform'&&scope.platform===p,badge:POLICY_BADGES[draft.platforms[p]],
        onClick:()=>pick(scope.kind==='platform'&&scope.platform===p?{kind:'all'}:{kind:'platform',platform:p})}))],{strip:true}));
    const shown=selectedPlatform===null?devices:devices.filter(d=>d.platform===selectedPlatform);
    box.append(el('h3','Устройства'+(selectedPlatform!==null?' · '+DeviceKit.platformInfo(selectedPlatform).name:'')));
    if(!shown.length)box.append(el('p','Устройств этой платформы в реестре нет.','hint'));
    else box.append(DeviceCards.grid(shown.map(d=>DeviceCards.device(d,{subtitle:'SN '+d.sn,
      selected:scope.kind==='device'&&scope.sn===d.sn,badge:POLICY_BADGES[draft.devices[d.sn]],
      onClick:()=>pick(scope.kind==='device'&&scope.sn===d.sn?{kind:'platform',platform:d.platform}:{kind:'device',sn:d.sn})})),{strip:true}));
    return box;
  }

  function scopeTitle(){
    if(scope.kind==='device'){const d=registry.devices.find(x=>x.sn===scope.sn);return 'Устройство · '+(d?d.name:scope.sn)}
    if(scope.kind==='platform')return 'Платформа · '+DeviceKit.platformInfo(scope.platform).name;
    return 'Все устройства · общая политика';
  }

  function policyPanel(){
    const panel=el('div',null,'panel upd-policy');panel.append(el('h3',scopeTitle()));
    const parent=inherited();
    const options=[...(scope.kind!=='all'?[['inherit','Наследовать от '+parent.from+' · '+POLICY_TITLES[parent.policy]]]:[]),...Object.entries(POLICY_TITLES)];
    const policy=choice(panel,'Что выдавать устройству',options,ownPolicy()||'inherit');
    const label=el('label','Свой ответ с обновлением (JSON)'),custom=el('textarea');custom.rows=10;custom.className='code';custom.spellcheck=false;
    custom.value=JSON.stringify(ownCustom(),null,2);label.append(custom);panel.append(label);
    const format=el('p','Формат: {"updates":[{"url":"https://…/ota.zip","version_code":5000001246,"version_name":"…","size":123,"type":"ota","priority":"OPTIONAL"}]}. Содержимое и контрольная сумма должны соответствовать файлу; панель ничего не скачивает и не устанавливает.','hint');
    panel.append(format);
    const syncCustom=()=>{label.hidden=format.hidden=policy.value!=='custom'};syncCustom();policy.onchange=syncCustom;
    const save=el('button','Сохранить');save.type='button';
    save.onclick=()=>work(async()=>{
      const value=policy.value==='custom'?JSON.parse(custom.value):null;
      const [policies,customs,key]=scope.kind==='device'?[draft.devices,draft.custom_devices,scope.sn]:scope.kind==='platform'?[draft.platforms,draft.custom_platforms,scope.platform]:[null,null,null];
      if(!policies){draft.policy=policy.value;if(value)draft.custom=value}
      else if(policy.value==='inherit'){delete policies[key];delete customs[key]}
      else{policies[key]=policy.value;if(value)customs[key]=value;else delete customs[key]}
      await api('updates',draft);notice('Политика обновлений сохранена: '+scopeTitle());await load()});
    const actions=el('div',null,'actions');actions.append(save);panel.append(actions);
    return {panel,useUrl:(u)=>{custom.value=JSON.stringify({updates:[{...u.metadata,url:u.url}]},null,2);policy.value='custom';syncCustom();
      panel.scrollIntoView({behavior:'smooth',block:'start'});custom.focus({preventScroll:true});notice('Свой апдейт подставлен — проверьте и нажмите «Сохранить»')}};
  }

  function endpointsBlock(){
    const details=el('details'),summary=el('summary','Эндпойнты проверки · '+draft.endpoints.length);details.append(summary);const table=el('div');details.append(table);
    function routes(){table.replaceChildren();for(const e of draft.endpoints){const row=el('div');row.className='fields';const enabled=el('input');enabled.type='checkbox';enabled.checked=e.enabled!==false;const l=el('label','Включён');l.prepend(enabled);row.append(l);enabled.onchange=()=>e.enabled=enabled.checked;const host=field(row,'Домен',e.host),path=field(row,'Путь',e.path);host.oninput=()=>e.host=host.value;path.oninput=()=>e.path=path.value;const method=choice(row,'Метод',[['GET','GET'],['POST','POST']],e.method),kind=choice(row,'Тип ответа',[['list','Список updates'],['check','Проверка hasUpdate'],['allow','Разрешение allow'],['confirm','Подтверждение']],e.kind);method.onchange=()=>e.method=method.value;kind.onchange=()=>e.kind=kind.value;const upstream=field(row,'Реальный сервер (необязательно)',e.upstream||'');upstream.oninput=()=>{if(upstream.value)e.upstream=upstream.value;else delete e.upstream};const remove=el('button','Удалить эндпойнт');remove.onclick=()=>{draft.endpoints=draft.endpoints.filter(x=>x!==e);routes()};row.append(remove);table.append(row)}}routes();
    const add=el('button','Добавить эндпойнт');add.onclick=()=>{draft.endpoints.push({host:'quasar.yandex.net',path:'/new_updates',method:'GET',kind:'list'});routes()};
    const saveEndpoints=el('button','Сохранить эндпойнты');saveEndpoints.onclick=()=>work(async()=>{await api('updates',draft);notice('Эндпойнты сохранены');await load()});
    details.append(add,saveEndpoints,el('p','Список общий для всех областей. Добавьте маршрут домена в прокси, если его ещё нет.','hint'));
    return details;
  }

  // ── Каталог прошивок: реальные ответы серверов копятся в базе; группа — платформа,
  // карточка — версия (DeviceCards.card), по нажатию — ссылка, хеш и выдача своим апдейтом
  const versionOf=m=>{const parts=String(m.version_name||'').split('.');return parts.length>=4?parts.slice(0,4).join('.'):(m.version_name||String(m.version_code||'—'))};
  const buildDate=m=>{const d=String(m.version_name||'').split('.').find(x=>/^20\d{6}$/.test(x));return d?d.slice(6,8)+'.'+d.slice(4,6)+'.'+d.slice(0,4):''};
  const sizeOf=bytes=>!bytes?'':bytes>=1e9?(bytes/1e9).toFixed(2)+' ГБ':(bytes/1e6).toFixed(1)+' МБ';
  const issued=url=>(ownCustom().updates||[]).some(u=>u.url===url)&&(ownPolicy()||inherited().policy)==='custom';

  function firmwareDialog(item,useUrl){
    const dialog=el('dialog',null,'upd-firmware'),m=item.metadata;
    const head=el('header');head.append(el('h3',DeviceKit.platformInfo(item.platforms[0]||'').name+' · '+versionOf(m)));
    const close=el('button','✕','icon-btn');close.type='button';close.setAttribute('aria-label','Закрыть');close.onclick=()=>dialog.close();head.append(close);
    const facts=el('dl');
    const fact=(k,v)=>{if(v===undefined||v===null||v==='')return;facts.append(el('dt',k),el('dd',String(v)))};
    fact('Версия',m.version_name);fact('version_code',m.version_code);fact('Размер',sizeOf(m.size));fact('Тип',m.type);fact('Приоритет',m.priority);
    fact('update_id',m.update_id);fact('MD5',m.md5_hash);fact('Платформы',item.platforms.join(', ')||'не определены');
    fact('Устройства',item.sns.map(sn=>{const d=registry.devices.find(x=>x.sn===sn);return d?d.name+' ('+sn+')':sn}).join(', '));
    fact('Встречена',item.hits+' раз · впервые '+new Date(item.first_seen*1000).toLocaleString('ru-RU')+' · последний '+new Date(item.last_seen*1000).toLocaleString('ru-RU'));
    const link=el('a',item.url,'upd-link');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';
    const actions=el('div',null,'actions');
    const copy=el('button','Скопировать ссылку','secondary');copy.type='button';
    copy.onclick=()=>work(async()=>{await navigator.clipboard.writeText(item.url);notice('Ссылка скопирована')});
    const use=el('button','Выдать как свой апдейт · '+scopeTitle());use.type='button';
    use.onclick=()=>{dialog.close();useUrl(item)};
    actions.append(use,copy);
    dialog.append(head,link,facts,actions);document.body.append(dialog);
    dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
  }

  function catalogBlock(useUrl){
    const box=el('div',null,'upd-catalog');
    const selected=scope.kind==='platform'?scope.platform:scope.kind==='device'?platformOf(scope.sn):null;
    const items=inventory.catalog.filter(i=>selected===null||i.platforms.includes(selected));
    const head=el('div',null,'sc-toolbar');
    head.append(el('h3','Каталог прошивок'+(selected!==null?' · '+DeviceKit.platformInfo(selected).name:'')),el('span',items.length+' версий','hint'));
    box.append(head);
    if(!items.length){box.append(el('p','Прошивки ещё не найдены: каталог пополняется из реальных ответов сервера при проверке обновлений устройством.','hint'));return box}
    const groups=new Map();
    for(const item of items)for(const p of (item.platforms.length?item.platforms:['']))if(selected===null||p===selected){if(!groups.has(p))groups.set(p,[]);groups.get(p).push(item)}
    for(const [platform,list] of [...groups].sort((a,b)=>DeviceKit.platformInfo(a[0]).name.localeCompare(DeviceKit.platformInfo(b[0]).name,'ru'))){
      if(selected===null)box.append(el('h4',DeviceKit.platformInfo(platform).name,'upd-group'));
      list.sort((a,b)=>(b.metadata.version_code||0)-(a.metadata.version_code||0));
      box.append(DeviceCards.grid(list.map(item=>DeviceCards.card({kind:'platform',platform,title:versionOf(item.metadata),
        subtitle:[buildDate(item.metadata),sizeOf(item.metadata.size),item.metadata.type].filter(Boolean).join(' · '),
        badge:issued(item.url)?{text:'Выдаётся',tone:'accent'}:item.metadata.priority==='FORCED'?{text:'Принудительная'}:null,
        hint:item.url,onClick:()=>firmwareDialog(item,useUrl)}))));
    }
    return box;
  }

  // ── Журнал проверок: окно-консоль, новые строки снизу, обновляется само
  const deviceLabel=a=>{const d=registry.devices.find(x=>x.sn===a.device_sn);return d?d.name:(a.device_sn||a.device_id||'—')};
  function consoleLines(target){
    target.replaceChildren();
    const rows=[...inventory.attempts].reverse();
    if(!rows.length){target.append(el('div','— проверок пока не было','log-console-empty'));return}
    for(const a of rows){
      const line=el('div',null,'upd-log-line'+(a.error||a.status>=400?' bad':''));
      const time=new Date(a.ts*1000);
      line.append(el('span',time.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'})+' '+time.toLocaleTimeString('ru-RU'),'t'),
        el('span',deviceLabel(a),'d'),el('span',a.endpoint,'e'),el('span',String(a.status),'s'),
        el('span',a.policy,'p '+a.policy),el('span',a.error||'','x'));
      target.append(line);
    }
    target.scrollTop=target.scrollHeight;
  }
  function logBlock(){
    const box=el('div',null,'upd-log-wrap');
    const head=el('div',null,'sc-toolbar');
    const refresh=el('button','Обновить','secondary');refresh.type='button';refresh.onclick=()=>work(load);
    head.append(el('h3','Журнал проверок · '+scopeTitle()),refresh);
    const log=el('div',null,'log-console');log.setAttribute('role','log');
    box.append(head,log);consoleLines(log);
    return box;
  }

  function render(){
    root.replaceChildren(el('h2','Обновления · OTA'),el('p','При каждой проверке устройство обращается к реальному серверу: исходные URL сохраняются в базе независимо от выдаваемого ответа. Политика устройства перекрывает политику платформы, та — общую.'));
    root.append(cardsBlock());
    const {panel,useUrl}=policyPanel();
    root.append(panel,catalogBlock(useUrl),logBlock(),endpointsBlock());
  }
  // Пока вкладка открыта — журнал подтягивается каждые 10 с без перерисовки остального
  setInterval(()=>{if(location.hash!=='#updates'||document.hidden||!inventory)return;
    api('updates'+query()).then(v=>{inventory.attempts=v.attempts;const log=root.querySelector('.log-console');if(log){const atEnd=log.scrollHeight-log.scrollTop-log.clientHeight<8;const keep=log.scrollTop;consoleLines(log);if(!atEnd)log.scrollTop=keep}}).catch(()=>{})},10000);
  window.addEventListener('hashchange',()=>{if(location.hash==='#updates')work(load)});work(load);
})();
