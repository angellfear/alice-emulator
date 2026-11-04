(() => {
  // Обновления · OTA: политика на трёх уровнях — общая → платформа → устройство (SN).
  // Сверху полосы карточек платформ/устройств и панель политики. Ниже секция
  // «Каталог прошивок»: слева платформы (только захваченные в каталоге, вертикально),
  // в центре — список прошивок (заголовок = version_code, свежие сверху; клик —
  // существующее окно), справа — журнал: последние 10 проверок, свежие сверху,
  // сгруппированы по устройству (IP, версия от Яндекса, версия на устройстве).
  // Каталог пополняется из реальных ответов Яндекса при любой политике, а если
  // устройство не стучится более суток — суточным опросом (updates.py, last_requests).
  const root=document.getElementById('updates');
  let inventory,registry,draft,scope={kind:'all'};
  const POLICY_TITLES={allow:'Разрешить · реальный ответ',deny:'Запретить · обновлений нет',custom:'Свой апдейт'};
  const POLICY_BADGES={allow:{text:'allow',tone:'ok'},deny:{text:'deny',tone:'danger'},custom:{text:'custom',tone:'accent'}};
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=text;if(cls)n.className=cls;return n};
  async function api(action,value){const r=await fetch('api/'+action,{method:value?'POST':'GET',headers:{'X-Emulator':'1','Content-Type':'application/json'},body:value?JSON.stringify(value):undefined});const v=await r.json();if(!r.ok)throw Error(v.error||r.statusText);return v}
  const notice=(text,error)=>{notify(text,{error})};
  async function work(fn){try{await fn()}catch(e){notice(e.message,true)}}
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

  // ── Каталог прошивок: реальные ответы серверов копятся в базе; список — version_code,
  // полная версия, дата сборки, размер; клик — окно (ссылка, md5, выдача своим апдейтом)
  const versionOf=m=>{const parts=String(m.version_name||'').split('.');return parts.length>=4?parts.slice(0,4).join('.'):''};
  const buildDate=m=>{const d=String(m.version_name||'').split('.').find(x=>/^20\d{6}$/.test(x));return d?d.slice(6,8)+'.'+d.slice(4,6)+'.'+d.slice(0,4):''};
  const sizeOf=bytes=>!bytes?'':bytes>=1e9?(bytes/1e9).toFixed(2)+' ГБ':(bytes/1e6).toFixed(1)+' МБ';
  const issued=url=>(ownCustom().updates||[]).some(u=>u.url===url)&&(ownPolicy()||inherited().policy)==='custom';

  function firmwareDialog(item,useUrl){
    const dialog=el('dialog',null,'upd-firmware'),m=item.metadata;
    const head=el('header');head.append(el('h3',DeviceKit.platformInfo(item.platforms[0]||'').name+' · '+(m.version_code||versionOf(m))));
    const close=el('button','✕','icon-btn');close.type='button';close.setAttribute('aria-label','Закрыть');close.onclick=()=>dialog.close();head.append(close);
    const facts=el('dl');
    const fact=(k,v)=>{if(v===undefined||v===null||v==='')return;facts.append(el('dt',k),el('dd',String(v)))};
    fact('version_code',m.version_code);fact('Версия',m.version_name);fact('Размер',sizeOf(m.size));fact('Тип',m.type);fact('Приоритет',m.priority);
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
    if(!items.length){box.append(el('p','Прошивки ещё не найдены: каталог пополняется из реальных ответов сервера при проверке обновлений устройством (или суточным опросом эмулятора).','hint'));return box}
    const row=item=>{
      const r=el('button',null,'upd-cat-row');r.type='button';r.title=item.url;
      r.append(el('span',String(item.metadata.version_code||'—'),'vc'),
        el('span',versionOf(item.metadata),'vn'),
        el('span',buildDate(item.metadata),'d'),
        el('span',sizeOf(item.metadata.size),'s'));
      if(item.metadata.priority==='FORCED')r.append(el('span','FORCED','tag'));
      if(issued(item.url))r.append(el('span','выдаётся','tag iss'));
      r.onclick=()=>firmwareDialog(item,useUrl);
      return r};
    const list=el('div',null,'upd-cat-list');
    if(selected!==null){
      [...items].sort((a,b)=>(b.metadata.version_code||0)-(a.metadata.version_code||0)).forEach(item=>list.append(row(item)));
    }else{
      const groups=new Map();
      for(const item of items)for(const p of (item.platforms.length?item.platforms:[''])){
        if(!groups.has(p))groups.set(p,[]);groups.get(p).push(item)}
      for(const [platform,glist] of [...groups].sort((a,b)=>DeviceKit.platformInfo(a[0]).name.localeCompare(DeviceKit.platformInfo(b[0]).name,'ru'))){
        list.append(el('h4',DeviceKit.platformInfo(platform).name,'upd-group'));
        [...glist].sort((a,b)=>(b.metadata.version_code||0)-(a.metadata.version_code||0)).forEach(item=>list.append(row(item)));
      }
    }
    box.append(list);
    return box;
  }

  // ── Журнал: последние 10 проверок, свежие сверху; подряд идущие записи одного
  // устройства сгруппированы (заголовок — имя устройства); в строке — дата/время,
  // IP устройства, версия от Яндекса (version_code), версия на устройстве, политика
  function consoleLines(target){
    target.replaceChildren();
    const rows=(inventory.attempts||[]).slice(0,10);
    if(!rows.length){target.append(el('div','— проверок пока не было','log-console-empty'));return}
    const offered=a=>{const v=(inventory.offered_versions||{})[a.id];return v&&v.length?String(v[0]):'—'};
    const fw=sn=>String((inventory.device_versions||{})[sn]||'').split('.').slice(0,4).join('.');
    let group=null;
    for(const a of rows){
      const key=a.device_sn||a.device_id||'';
      if(!group||group.key!==key){
        group={key,el:el('div',null,'upd-log-group')};
        const d=registry.devices.find(x=>x.sn===a.device_sn);
        group.el.append(el('div',d?d.name:(a.device_sn||a.device_id||'—'),'upd-log-dev'));
        target.append(group.el);
      }
      const line=el('div',null,'upd-log-line'+(a.error||a.status>=400?' bad':''));
      const time=new Date(a.ts*1000);
      const stamp=time.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'})+' '+
        time.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      line.append(el('span',stamp,'t'),
        el('span',a.ip||'—','ip'),
        el('span',offered(a),'v'),
        el('span',fw(a.device_sn)||'—','dv'),
        el('span',a.policy,'p '+a.policy));
      group.el.append(line);
    }
  }
  function logBlock(){
    const box=el('div',null,'upd-log-wrap');
    const head=el('div',null,'sc-toolbar');
    const refresh=el('button','Обновить','secondary');refresh.type='button';refresh.onclick=()=>work(load);
    head.append(el('h3','Журнал'),refresh);
    const log=el('div',null,'log-console upd-log-list');log.setAttribute('role','log');
    box.append(head,log);consoleLines(log);
    return box;
  }

  // ── Секция «Каталог прошивок»: платформы (только захваченные, вертикально) —
  // каталог-список — журнал
  function catPlatformsBlock(){
    const box=el('div',null,'upd-platforms');
    const pick=next=>{scope=next;work(load)};
    const row=(label,count,active,onClick)=>{
      const b=el('button',null,'upd-pl-row'+(active?' active':''));b.type='button';
      b.append(el('span',label,'n'));
      if(count)b.append(el('span',count,'c'));
      b.onclick=onClick;return b};
    const counts={};
    for(const item of (inventory.catalog||[]))for(const p of item.platforms)counts[p]=(counts[p]||0)+1;
    box.append(el('h3','Платформы'));
    box.append(row('Все прошивки',(inventory.catalog||[]).length||'',scope.kind==='all',()=>pick({kind:'all'})));
    for(const [p,c] of Object.entries(counts).sort((a,b)=>DeviceKit.platformInfo(a[0]).name.localeCompare(DeviceKit.platformInfo(b[0]).name,'ru')))
      box.append(row(DeviceKit.platformInfo(p).name,c,scope.kind==='platform'&&scope.platform===p,
        ()=>pick(scope.kind==='platform'&&scope.platform===p?{kind:'all'}:{kind:'platform',platform:p})));
    return box;
  }

  function catalogSection(useUrl){
    const section=el('div',null,'upd-cat-section');
    section.append(catPlatformsBlock(),catalogBlock(useUrl),logBlock());
    return section;
  }

  function render(){
    root.replaceChildren(el('h2','Обновления · OTA'));
    root.append(cardsBlock());
    const {panel,useUrl}=policyPanel();
    root.append(panel,catalogSection(useUrl),endpointsBlock());
  }
  // Пока вкладка открыта — журнал подтягивается каждые 10 с без перерисовки остального
  setInterval(()=>{if(location.hash!=='#updates'||document.hidden||!inventory)return;
    api('updates'+query()).then(v=>{inventory.attempts=v.attempts;inventory.offered_versions=v.offered_versions||{};inventory.device_versions=v.device_versions||{};
      const log=root.querySelector('.log-console');if(log){const atTop=log.scrollTop<8;consoleLines(log);if(!atTop)log.scrollTop=0}}).catch(()=>{})},10000);
  window.addEventListener('hashchange',()=>{if(location.hash==='#updates')work(load)});work(load);
})();
