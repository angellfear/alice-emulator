/* Вкладка «Quasar»: конфиг колонок.
   Сборка: встроенный шаблон → общие замены → замены платформы → замены устройства → итог.
   Слева — шаги сборки; справа — панель шага. Замена — строка «имя · узел JSON ·
   значение · ⋯»: флаг и выбор переключаются прямо в строке, остальное — в окне правки.
   Каждое действие сохраняется сразу (PUT quasar_rules), несохранённого состояния нет —
   кроме редактора «Свой ответ» устройства. */
(() => {
  const $=id=>document.getElementById(id);
  let registry=null, overview=null;
  let step='common';                  // template | common | platform | devices | final
  let templatePlatform='';            // платформа встроенного шаблона ('' — общий)
  let rulesPlatform='';               // платформа замен
  let finalPlatform='';               // платформа итога
  let selectedSn='';
  let dirty=false;                    // несохранённый редактор «Свой ответ»
  const composedCache=new Map();      // платформа → итог без устройства (для путей и подстановки)

  const make=(tag,text,className)=>{const el=document.createElement(tag);if(text)el.textContent=text;if(className)el.className=className;return el};
  async function request(method,action,value){
    const response=await fetch('api/'+action,{method,headers:{'X-Emulator':'1','Content-Type':'application/json'},
      body:value===undefined?undefined:JSON.stringify(value)});
    const result=await response.json();
    if(!response.ok)throw Error(result.error||result.hint||response.statusText);
    return result;
  }
  const api=(action,value)=>request(value===undefined?'GET':'POST',action,value);
  const put=(action,value)=>request('PUT',action,value);
  const del=action=>request('DELETE',action);
  async function work(fn){try{await fn()}catch(e){notify(e.message,{error:true})}}
  const q=encodeURIComponent;

  // ── диалоги вместо confirm()/prompt() ─────────────────────────────────
  function dialogShell(title,className=''){
    const dlg=document.createElement('dialog');dlg.className='catalog-dialog '+className;
    dlg.setAttribute('aria-label',title);
    const form=document.createElement('form');form.method='dialog';dlg.append(form);
    form.append(make('h3',title));
    document.body.append(dlg);
    dlg.addEventListener('close',()=>dlg.remove());
    return {dlg,form};
  }
  function askDialog(text,actions,title){
    return new Promise(resolve=>{
      const {dlg,form}=dialogShell(title||'Подтверждение');
      form.append(make('p',text,'hint'));
      const row=make('div','','row');form.append(row);
      for(const a of actions){
        const b=make('button',a.label,a.className);b.type='button';
        b.onclick=()=>{resolve(a.value);dlg.close()};
        row.append(b);
      }
      dlg.addEventListener('close',()=>resolve(null));
      dlg.showModal();
    });
  }
  function askText(title,text,placeholder,value=''){
    return new Promise(resolve=>{
      const {dlg,form}=dialogShell(title);
      if(text)form.append(make('p',text,'hint'));
      const input=make('input');input.value=value;input.placeholder=placeholder||'';input.spellcheck=false;
      const label=make('label','Имя');label.append(input);form.append(label);
      const row=make('div','','row');
      const cancel=make('button','Отмена','secondary');cancel.type='button';cancel.onclick=()=>dlg.close();
      const save=make('button','Сохранить');save.type='submit';
      row.append(cancel,save);form.append(row);
      form.onsubmit=e=>{e.preventDefault();resolve(input.value.trim());dlg.close()};
      dlg.addEventListener('close',()=>resolve(null));
      dlg.showModal();input.focus();
    });
  }
  // меню действий строки/платформы: список кнопок в <dialog>
  function menuDialog(title,actions){
    const {dlg,form}=dialogShell(title,'qr-menu-dialog');
    const list=make('div','','list');
    for(const a of actions){
      const b=make('button',a.label,a.className||'secondary');b.type='button';
      b.onclick=()=>{dlg.close();work(a.run)};
      list.append(b);
    }
    const row=make('div','','row');
    const cancel=make('button','Отмена','secondary');cancel.type='button';cancel.onclick=()=>dlg.close();
    row.append(cancel);form.append(list,row);
    dlg.showModal();
  }

  // ── редактор кода: CodeMirror (vendor/), запас — textarea ─────────────
  function makeCM(container,{value='',readOnly=false,height='520px',onDirty=null}={}){
    if(typeof CodeMirror==='undefined'){
      const ta=make('textarea','','code');ta.value=value;ta.rows=12;ta.spellcheck=false;ta.readOnly=readOnly;
      container.append(ta);
      if(onDirty&&!readOnly)ta.addEventListener('input',onDirty);
      return {cm:null,getValue:()=>ta.value,setValue:v=>{ta.value=v},refresh:()=>{},onChange:fn=>ta.addEventListener('input',fn)};
    }
    const cm=CodeMirror(container,{value,mode:'application/json',theme:'material-darker',
      lineNumbers:true,foldGutter:true,readOnly,indentUnit:2,tabSize:2,matchBrackets:true,lineWrapping:false,
      gutters:['CodeMirror-linenumbers','CodeMirror-foldgutter']});
    cm.setSize('100%',height);
    let silent=false;
    cm.on('change',()=>{if(!silent&&onDirty)onDirty()});
    const refresh=()=>cm.refresh();
    setTimeout(refresh,0);setTimeout(refresh,60);
    return {cm,getValue:()=>cm.getValue(),
      setValue:v=>{silent=true;cm.setValue(v);cm.clearHistory();silent=false},
      refresh,onChange:fn=>cm.on('change',fn)};
  }

  // ── пути: JSON Pointer (RFC 6901) ────────────────────────────────────
  const escPart=s=>String(s).replace(/~/g,'~0').replace(/\//g,'~1');
  const unescPart=s=>s.replace(/~1/g,'/').replace(/~0/g,'~');
  const pointerParts=p=>p.split('/').slice(1).map(unescPart);
  // ввод пользователя: путь через «/», ведущий «/» необязателен
  const toPointer=text=>{
    const t=(text||'').trim();
    if(!t)return '';
    return t.startsWith('/')?t:'/'+t;
  };
  const valueAt=(tree,pointer)=>{
    let node=tree;
    for(const part of pointerParts(pointer)){
      if(node===null||typeof node!=='object'||!(part in node))return undefined;
      node=node[part];
    }
    return node;
  };
  function allPointers(tree,limit=6000){
    const out=[];
    const walk=(node,prefix)=>{
      if(out.length>=limit||node===null||typeof node!=='object'||Array.isArray(node))return;
      for(const [key,child] of Object.entries(node)){
        const p=prefix+'/'+escPart(key);out.push(p);walk(child,p);
      }
    };
    walk(tree,'');
    return out;
  }
  async function composedFor(platform){
    if(!composedCache.has(platform))
      composedCache.set(platform,api('quasar_platforms?compose='+q(platform)).then(r=>r.config||{}));
    return composedCache.get(platform);
  }
  const invalidate=()=>{composedCache.clear();overview=null};

  // ── отображение значений: {{токены}} и параметры — теги ──────────────
  const tag=(text,kind='')=>make('span',text,'qtag'+(kind?' '+kind:''));
  function withTokens(text){
    const box=make('span','','qr-tpl');
    const re=/\{\{\s*([^}]+?)\s*\}\}/g;let last=0,m;
    while((m=re.exec(text))){
      if(m.index>last)box.append(document.createTextNode(text.slice(last,m.index)));
      box.append(tag(m[1]));last=re.lastIndex;
    }
    if(last<text.length)box.append(document.createTextNode(text.slice(last)));
    return box;
  }
  const short=(v,n=48)=>{const s=JSON.stringify(v);return s===undefined?'':s.length>n?s.slice(0,n-1)+'…':s};
  // значение состояния: строка — текст с тегами подстановок, маркеры db/run — теги, иначе JSON
  function stateView(value){
    if(typeof value==='string')return value.includes('{{')?withTokens(value):make('code',short(value),'qr-val');
    if(value&&typeof value==='object'&&!Array.isArray(value)){
      const keys=Object.keys(value);
      if(keys.length===1&&keys[0]==='db'){const t=tag(String(value.db),'db');t.prepend(make('b','БД'));return t}
      if(keys.length===1&&keys[0]==='run'){const t=tag(String(value.run),'run');t.prepend(make('b','run'));return t}
    }
    return make('code',short(value),'qr-val');
  }
  const statesOf=rule=>Object.keys(rule.options||{});

  // ── каркас ───────────────────────────────────────────────────────────
  const STEPS=[
    ['template','Шаблон','Шаблон'],
    ['common','Общие замены','Общие'],
    ['platform','Замены платформы','Платф.'],
    ['devices','Замены устройств','Устр.'],
    ['final','Итог','Итог']];
  async function render(){
    const root=$('quasar-http');
    [registry,overview]=await Promise.all([api('identities'),overview?overview:api('quasar_platforms')]);
    const platforms=platformNames();
    if(!rulesPlatform||!platforms.includes(rulesPlatform))
      rulesPlatform=(overview.platforms[0]||{}).platform||platforms[0]||'';
    if(!finalPlatform)finalPlatform=rulesPlatform;
    const wrap=make('div','','quasar-layers');
    wrap.append(stepNav());
    const panel=make('div','','quasar-panel-layer');
    wrap.append(panel);
    root.replaceChildren(wrap);
    if(step==='template')await templatePanel(panel);
    else if(step==='common')await rulesPanel(panel,'common');
    else if(step==='platform')await rulesPanel(panel,'platform');
    else if(step==='devices')await devicesPanel(panel);
    else await finalPanel(panel);
  }
  const platformNames=()=>[...new Set([...(overview.builtin_platforms||[]),...overview.platforms.map(p=>p.platform)])].sort();
  const platformRules=name=>(overview.platforms.find(p=>p.platform===name)||{}).rules||0;

  function stepNav(){
    const nav=make('nav','','quasar-stack');nav.setAttribute('aria-label','Шаги сборки конфига');
    const counts={template:templatePlatform||'общий',common:'замен '+overview.common_rules,
      platform:rulesPlatform?rulesPlatform+' · '+platformRules(rulesPlatform):'—',
      devices:'устройств '+overview.devices,final:finalPlatform||'общий'};
    for(const [id,full,shortName] of STEPS){
      const b=make('button');b.type='button';
      b.append(make('b',full,'full'),make('span',shortName,'short'),make('small',counts[id]));
      if(step===id){b.classList.add('active');b.setAttribute('aria-current','step')}
      b.onclick=()=>work(()=>goStep(id));
      nav.append(b);
    }
    return nav;
  }
  async function goStep(id){
    if(id===step)return;
    if(dirty&&!await askDialog('В редакторе «Свой ответ» есть несохранённые изменения. Уйти и потерять их?',
      [{label:'Остаться',className:'secondary',value:false},{label:'Уйти',value:true}],'Несохранённые изменения'))return;
    dirty=false;step=id;await render();
  }
  function platformSelect(value,onchange,withCommon){
    const select=make('select','','layer-select');select.setAttribute('aria-label','Платформа');
    if(withCommon)select.append(new Option('общий',''));
    for(const name of platformNames())select.append(new Option(name,name));
    select.value=value;
    select.onchange=()=>work(()=>onchange(select.value));
    return select;
  }
  function head(title,...extra){
    const h=make('div','','layer-head');h.append(make('h3',title),...extra);return h;
  }

  // ── Шаблон: встроенный, только чтение ────────────────────────────────
  async function templatePanel(panel){
    panel.append(head('Шаблон',platformSelect(templatePlatform,async v=>{templatePlatform=v;await render()},true)));
    panel.append(make('p','Встроенный шаблон эмулятора: общий дефолт плюс особенности платформы. Только чтение — меняется заменами.','hint'));
    const data=await api('quasar_platforms?builtin='+q(templatePlatform));
    const holder=make('div');panel.append(holder);
    makeCM(holder,{value:JSON.stringify(data.config,null,2),readOnly:true,height:'560px'});
  }

  // ── Общие замены / замены платформы ──────────────────────────────────
  async function rulesPanel(panel,kind){
    let layer='common',platform=rulesPlatform;
    if(kind==='platform'){
      if(!rulesPlatform){
        panel.append(head('Замены платформы'));
        panel.append(make('p','Платформ нет.','hint'));
        return;
      }
      layer='platform:'+rulesPlatform;
      const more=make('button','⋯','icon-btn');more.type='button';more.setAttribute('aria-label','Платформы');
      more.onclick=()=>platformMenu();
      panel.append(head('Замены платформы',platformSelect(rulesPlatform,async v=>{rulesPlatform=v;finalPlatform=v;await render()}),more));
      panel.append(make('p','Применяются после общих замен к колонкам этой платформы, сверху вниз: нижняя сильнее.','hint'));
    }else{
      panel.append(head('Общие замены'));
      panel.append(make('p','Применяются ко всем колонкам поверх встроенного шаблона, сверху вниз: нижняя сильнее.','hint'));
      platform=rulesPlatform||'';
    }
    const data=await api('quasar_rules?layer='+q(layer));
    for(const group of data.inherited||[]){
      if(!group.rules.length)continue;
      panel.append(make('h4','Из расширения '+group.origin));
      panel.append(ruleList(group.rules,{readOnly:true}));
    }
    if((data.inherited||[]).some(g=>g.rules.length))panel.append(make('h4','Свои'));
    panel.append(ruleList(data.rules,{layer,platform}));
  }

  // список замен слоя: строки + «＋ Замена»
  function ruleList(rules,{layer,platform,readOnly=false,device=null,current=null}){
    const box=make('div','','qr-box');
    const list=make('div','','qr-list');
    if(!rules.length)list.append(make('p','Замен нет.','qr-empty'));
    else{
      const headRow=make('div','','qr-row qr-head');headRow.setAttribute('aria-hidden','true');
      headRow.append(make('span','Вкл','qr-on'),make('span','Имя','qr-name'),make('span','Узел JSON','qr-path'),
        make('span','Значение','qr-ctl'),make('span','','qr-acts'));
      list.append(headRow);
    }
    const save=async next=>{
      await put('quasar_rules',{layer,rules:next});
      invalidate();await render();
    };
    rules.forEach((rule,index)=>list.append(ruleRow(rule,index,rules,{layer,platform,readOnly,save,device,current})));
    box.append(list);
    if(!readOnly){
      const add=make('button','＋ Замена','secondary qr-add');add.type='button';
      add.onclick=()=>work(()=>ruleDialog({rules,index:null,layer,platform,device,current,save}));
      box.append(add);
    }
    return box;
  }
  // иконки действий строки (inline SVG, цвет — currentColor)
  const ICONS={
    edit:'M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4',
    up:'M12 19V5M6 11l6-6 6 6',
    down:'M12 5v14M6 13l6 6 6-6',
    reset:'M1 4v6h6M3.51 15a9 9 0 1 0 2.13-9.36L1 10',
    del:'M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13M10 11v6M14 11v6'};
  function iconButton(icon,label,onclick,extra=''){
    const b=make('button','','icon-btn qr-icon'+(extra?' '+extra:''));b.type='button';
    b.setAttribute('aria-label',label);b.title=label;
    b.innerHTML='<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="'+ICONS[icon]+'"/></svg>';
    if(onclick)b.onclick=()=>work(onclick);else b.disabled=true;
    return b;
  }
  function ruleRow(rule,index,rules,ctx){
    const row=make('div','','qr-row'+(rule.enabled===false||rule.removed?' off':''));
    const label=rule.name||rule.path;
    const on=make('input','','qr-on');on.type='checkbox';on.checked=rule.enabled!==false&&!rule.removed;
    on.setAttribute('aria-label','Включена: '+label);on.title=on.checked?'Включена':'Отключена';
    on.disabled=!!ctx.readOnly||!!rule.removed;
    on.onchange=()=>work(async()=>{
      try{await ctx.save(rules.map((r,i)=>i===index?{...r,enabled:on.checked}:r))}
      catch(e){on.checked=!on.checked;throw e}
    });
    const name=make('span',rule.name||pointerParts(rule.path).slice(-1)[0],'qr-name');
    if(rule.shipped){ // поставляемый набор: бейдж у имени, правки state — «изменено/снято»
      const badge=make('span',rule.removed?'снято':rule.modified?'изменено':'поставляемое','chip');
      badge.title=rule.removed?'Поставляемое правило снято правкой state — кнопка «Сбросить» возвращает его'
        :rule.modified?'Правка state поверх поставляемого правила — «Сбросить» возвращает исходное'
        :'Правило поставляемого набора (в пакете); правка сохранится в state';
      name.append(' ',badge);
    }
    const path=make('code',rule.path,'qr-path');
    row.append(on,name,path);
    const ctl=make('div','','qr-ctl');
    const editable=!ctx.readOnly&&rule.enabled!==false;
    const change=value=>work(async()=>{
      const next=rules.map((r,i)=>i===index?{...r,value}:r);
      await ctx.save(next);
      notify((rule.name||rule.path)+': сохранено');
    });
    const states=statesOf(rule);
    if(rule.kind==='delete')ctl.append(tag('удалить','del'));
    else if(states.length===2){  // два состояния — переключатель: первое — «вкл»
      const sw=make('label','','ui-switch');sw.setAttribute('aria-label',label);
      sw.title=states.join(' / ');
      const cb=make('input');cb.type='checkbox';cb.checked=rule.value===states[0];cb.disabled=!editable;
      cb.onchange=()=>change(cb.checked?states[0]:states[1]);
      sw.append(cb,make('i'));ctl.append(sw);
    }else if(states.length>2){
      const select=make('select','','qr-select');select.disabled=!editable;
      select.setAttribute('aria-label',label);
      for(const key of states)select.append(new Option(key,key));
      select.value=rule.value;
      select.onchange=()=>change(select.value);
      ctl.append(select);
    }else{  // одно состояние — значение узла
      const view=stateView((rule.options||{})[rule.value]);
      if(!ctx.readOnly){
        const b=make('button','','qr-value');b.type='button';b.append(view);
        b.setAttribute('aria-label','Изменить: '+label);
        b.onclick=()=>work(()=>ruleDialog({...ctx,rules,index}));
        ctl.append(b);
      }else ctl.append(view);
    }
    row.append(ctl);
    const acts=make('div','','qr-acts');
    if(rule.removed){  // снятое поставляемое — только «сбросить к поставляемому»
      acts.append(iconButton('reset','Сбросить к поставляемому: '+label,
        ()=>ctx.save(rules.map((r,i)=>i===index?r.base:r))));
      row.append(acts);
      return row;
    }
    if(!ctx.readOnly){
      const reset=rule.modified?()=>ctx.save(rules.map((r,i)=>i===index?r.base:r)):null;
      acts.append(
        iconButton('edit','Редактировать: '+label,()=>ruleDialog({...ctx,rules,index})),
        iconButton('up','Выше',!rule.shipped&&index>0&&!rules[index-1].shipped?()=>ctx.save(move(rules,index,-1)):null),
        iconButton('down','Ниже',!rule.shipped&&index<rules.length-1&&!rules[index+1].shipped?()=>ctx.save(move(rules,index,1)):null),
        ...(reset?[iconButton('reset','Сбросить к поставляемому: '+label,reset)]:[]),
        iconButton('del','Удалить: '+label,async()=>{
          if(await askDialog('Удалить замену «'+label+'»?',
            [{label:'Отмена',className:'secondary',value:false},{label:'Удалить',className:'danger',value:true}],'Удаление замены'))
            await ctx.save(rules.filter((_,i)=>i!==index));
        },'danger-icon'));
    }
    row.append(acts);
    return row;
  }
  const move=(rules,index,delta)=>{const next=rules.slice();const [item]=next.splice(index,1);next.splice(index+delta,0,item);return next};

  // ── окно замены: имя, узел, вид, значение ────────────────────────────
  const BUILTIN_PARAMS=['device.sn','device.id','device.name','device.room','device.platform','user.id','user.name','session.sn'];
  let paramKeys=null;
  async function knownParams(){
    if(paramKeys===null){
      try{const r=await api('params');paramKeys=[...new Set([...BUILTIN_PARAMS,...(r.defs||[]).map(d=>d.key)])]}
      catch{paramKeys=BUILTIN_PARAMS.slice()}
    }
    return paramKeys;
  }
  function datalist(id,values){
    let list=document.getElementById(id);
    if(!list){list=make('datalist');list.id=id;document.body.append(list)}
    list.replaceChildren(...values.map(v=>new Option(v)));
    return id;
  }
  // текст поля значения ⇄ JSON: строка показывается без кавычек; при сохранении текст,
  // похожий на JSON (объект, список, число, true/false/null, "строка"), разбирается как JSON
  const valueToText=v=>v===undefined?'':typeof v==='string'?v:JSON.stringify(v,null,2);
  function textToValue(text){
    const t=text.trim();
    if(/^[\[{"]/.test(t)||/^(true|false|null|-?\d+(\.\d+)?([eE][+-]?\d+)?)$/.test(t)){
      try{return JSON.parse(t)}catch{if(/^[\[{]/.test(t))throw Error('Значение «'+(t.length>30?t.slice(0,29)+'…':t)+'»: некорректный JSON')}
    }
    return text;
  }
  // начальные состояния новой замены по текущему значению узла
  const defaultStates=v=>typeof v==='boolean'
    ?{options:{'Вкл':true,'Выкл':false},value:v?'Вкл':'Выкл'}
    :{options:{'Значение':v===undefined?'':v},value:'Значение'};
  async function ruleDialog({rules,index,layer,platform,device,current,save,prefill=null}){
    const editing=index!==null&&index!==undefined?rules[index]:null;
    const tree=current||await composedFor(platform||'');
    const tokens=[...new Set(['device_name','device_id','device.room','user.name','now_iso','uuid',...await knownParams()])];
    const {dlg,form}=dialogShell(editing?'Замена':'Новая замена','qr-dialog');
    const field=(text,control)=>{const l=make('label',text);l.append(control);return l};
    const name=make('input');name.value=editing?editing.name:'';name.placeholder='Подпись';
    const path=make('input','','code');path.spellcheck=false;
    path.value=editing?editing.path:(prefill&&prefill.path)||'';
    path.placeholder='/config/device_config/…';
    path.setAttribute('list',datalist('qr-paths',allPointers(tree)));
    const currentValue=()=>{const p=toPointer(path.value);return p?valueAt(tree,p):undefined};
    let kind=editing&&editing.kind==='delete'?'delete':'select';
    const seg=make('div','','segmented qr-kind');seg.setAttribute('role','radiogroup');seg.setAttribute('aria-label','Вид');
    const statesBox=make('div','','qr-states');
    const kindButtons=[['select','Состояния'],['delete','Удалить ключ']].map(([value,label])=>{
      const b=make('button',label);b.type='button';b.setAttribute('role','radio');
      b.onclick=()=>{kind=value;syncKind()};seg.append(b);return [value,b];
    });
    function syncKind(){
      for(const [value,b] of kindButtons){b.classList.toggle('active',kind===value);b.setAttribute('aria-checked',String(kind===value))}
      statesBox.hidden=kind!=='select';
    }
    // состояния: [текущее ◉] [имя] [значение] [удалить]
    const list=make('div','','qr-state-list');
    const radioName='qr-cur-'+Math.random().toString(36).slice(2);
    let lastValueInput=null;
    const grow=ta=>{ta.rows=Math.min(8,Math.max(1,ta.value.split('\n').length))};
    function addState(stateName='',value='',checked=false){
      const row=make('div','','qr-state');
      const cur=make('input');cur.type='radio';cur.name=radioName;cur.checked=checked;cur.setAttribute('aria-label','Текущее состояние');
      const n=make('input','','qr-state-name');n.value=stateName;n.placeholder='Имя';n.setAttribute('aria-label','Имя состояния');
      const v=make('textarea','','code');v.value=valueToText(value);v.spellcheck=false;v.placeholder='значение';
      v.setAttribute('aria-label','Значение состояния');grow(v);
      const mark=()=>{lastValueInput=v};v.oninput=()=>{grow(v);mark()};
      for(const ev of ['focus','pointerdown','keyup','select'])v.addEventListener(ev,mark);
      const rm=iconButton('del','Удалить состояние',()=>{row.remove();
        if(!list.querySelector('input[type=radio]:checked')){const f=list.querySelector('input[type=radio]');if(f)f.checked=true}});
      row.append(cur,n,v,rm);list.append(row);lastValueInput=lastValueInput||v;return row;
    }
    function fillStates(spec){
      list.replaceChildren();lastValueInput=null;
      for(const [key,value] of Object.entries(spec.options))addState(key,value,key===spec.value);
    }
    fillStates(editing&&editing.kind==='select'?{options:editing.options,value:editing.value}:defaultStates(currentValue()));
    const add=make('button','＋ Состояние','secondary');add.type='button';
    add.onclick=()=>{const r=addState('','',!list.children.length);r.querySelector('.qr-state-name').focus()};
    const chips=make('div','','qr-chips');
    for(const t of tokens){
      const c=make('button','','qtag-btn');c.type='button';c.append(tag(t));c.title='Вставить {{'+t+'}}';
      c.onclick=()=>{const v=lastValueInput||list.querySelector('textarea');if(!v)return;
        const at=v.selectionStart??v.value.length;v.setRangeText('{{'+t+'}}',at,v.selectionEnd??at,'end');grow(v);v.focus()};
      chips.append(c);
    }
    statesBox.append(make('p','Одно состояние — значение узла, два — переключатель (первое — «вкл»), больше — выбор. Значение — текст или JSON; подстановки {{…}} — кнопками ниже.','hint'),
      list,add,chips);
    const enabled=make('label','','ui-switch');
    const enabledBox=make('input');enabledBox.type='checkbox';enabledBox.checked=editing?editing.enabled!==false:true;
    enabled.append(enabledBox,make('i'),document.createTextNode('Включена'));
    const err=make('p','','sc-err');err.hidden=true;
    path.onchange=()=>{if(!editing){
      if(!name.value.trim())name.value=pointerParts(toPointer(path.value)).slice(-1)[0]||'';
      fillStates(defaultStates(currentValue()));
    }};
    syncKind();
    const row=make('div','','row');
    const cancel=make('button','Отмена','secondary');cancel.type='button';cancel.onclick=()=>dlg.close();
    const ok=make('button','Сохранить');ok.type='submit';
    row.append(cancel,ok);
    form.append(field('Имя',name),field('Узел JSON',path),seg,statesBox,enabled,err,row);
    form.onsubmit=async e=>{
      e.preventDefault();err.hidden=true;
      try{
        const pointer=toPointer(path.value);
        if(!pointer)throw Error('Укажите узел JSON');
        const rule={...(editing||{}),name:name.value.trim(),path:pointer,kind,enabled:enabledBox.checked};
        if(kind==='select'){
          const options={};let chosen=null;
          for(const r of list.children){
            const key=r.querySelector('.qr-state-name').value.trim();
            if(!key)throw Error('У состояния нет имени');
            if(key in options)throw Error('Имена состояний повторяются: '+key);
            options[key]=textToValue(r.querySelector('textarea').value);
            if(r.querySelector('input[type=radio]').checked)chosen=key;
          }
          if(!Object.keys(options).length)throw Error('Нужно хотя бы одно состояние');
          rule.options=options;rule.value=chosen??Object.keys(options)[0];
        }else{delete rule.options;rule.value=null}
        const next=rules.slice();
        if(editing)next[index]=rule;else next.push(rule);
        ok.disabled=true;
        await save(next);
        dlg.close();notify('Замена сохранена: '+(rule.name||pointer));
      }catch(ex){
        err.textContent=ex.message.replace(/^Строка \d+:\s*/,'');err.hidden=false;
      }finally{ok.disabled=false}
    };
    dlg.showModal();
    (editing?name:path).focus();
  }

  // ── платформы: добавить / переименовать / удалить ────────────────────
  function platformMenu(){
    const own=overview.platforms.some(p=>p.platform===rulesPlatform);
    menuDialog('Платформы',[
      {label:'Добавить платформу',run:async()=>{
        const name=await askText('Новая платформа','Латиница, цифры, точка, подчёркивание, дефис.','station_max');
        if(!name)return;
        await api('quasar_platforms',{action:'create',platform:name});
        invalidate();rulesPlatform=name;notify('Платформа добавлена: '+name);await render();
      }},
      ...(own?[{label:'Переименовать '+rulesPlatform,run:async()=>{
        const name=await askText('Переименовать платформу','',rulesPlatform,rulesPlatform);
        if(!name||name===rulesPlatform)return;
        await api('quasar_platforms',{action:'rename',platform:rulesPlatform,to:name});
        invalidate();rulesPlatform=name;notify('Платформа переименована');await render();
      }},{label:'Удалить замены '+rulesPlatform,className:'danger',run:async()=>{
        if(!await askDialog('Удалить все замены платформы '+rulesPlatform+'? Встроенный шаблон платформы останется.',
          [{label:'Отмена',className:'secondary',value:false},{label:'Удалить',className:'danger',value:true}],'Удаление замен'))return;
        await del('quasar_platforms?platform='+q(rulesPlatform));
        invalidate();notify('Замены платформы удалены');await render();
      }}]:[])]);
  }

  // ── просмотр собранного конфига с происхождением значений ────────────
  function formatJSON(value){
    const lines=[];
    const fmt=(v,path,prefix,indent,isLast)=>{
      const pad=' '.repeat(indent);
      if(v!==null&&typeof v==='object'){
        const isArr=Array.isArray(v);
        const entries=isArr?v.map((x,i)=>[String(i),x]):Object.entries(v);
        if(!entries.length){lines.push({text:pad+prefix+(isArr?'[]':'{}')+(isLast?'':','),path});return}
        lines.push({text:pad+prefix+(isArr?'[':'{'),path});
        entries.forEach(([key,child],i)=>fmt(child,path+'/'+escPart(key),isArr?'':JSON.stringify(key)+': ',indent+2,i===entries.length-1));
        lines.push({text:pad+(isArr?']':'}')+(isLast?'':','),path});
      }else lines.push({text:pad+prefix+JSON.stringify(v)+(isLast?'':','),path});
    };
    fmt(value,'','',0,true);
    return lines;
  }
  const originFor=(path,origins)=>{
    if(path&&origins[path])return origins[path];
    const parts=pointerParts(path||'');
    for(let n=parts.length-1;n>0;n--){
      const ancestor='/'+parts.slice(0,n).map(escPart).join('/');
      if(origins[ancestor])return origins[ancestor];
    }
    return 'встроенный шаблон';
  };
  let tipEl=null;
  const tipElement=()=>{if(!tipEl){tipEl=make('div','','cm-tip');tipEl.hidden=true;document.body.append(tipEl)}return tipEl};
  // read-only просмотр; onPick(pointer, value) — клик по строке (создать замену)
  function composedViewer(host,config,origins,onPick){
    const lines=formatJSON(config);
    const holder=make('div');host.append(holder);
    const ed=makeCM(holder,{value:lines.map(l=>l.text).join('\n'),readOnly:true,height:'560px'});
    if(!ed.cm)return;
    const cm=ed.cm,tip=tipElement(),wrapper=cm.getWrapperElement();
    const lineAt=e=>{const pos=cm.coordsChar({left:e.clientX,top:e.clientY});return pos&&pos.line>=0&&pos.line<lines.length?lines[pos.line]:null};
    wrapper.addEventListener('mousemove',e=>{
      const line=lineAt(e);if(!line){tip.hidden=true;return}
      tip.textContent=(line.path||'корень')+'\n← '+originFor(line.path,origins);
      tip.style.left=Math.min(e.clientX+14,window.innerWidth-370)+'px';tip.style.top=(e.clientY+18)+'px';tip.hidden=false;
    });
    wrapper.addEventListener('mouseleave',()=>{tip.hidden=true});
    if(onPick)cm.on('mousedown',(_,e)=>{
      if(e.target.closest&&e.target.closest('.CodeMirror-gutters'))return;
      const line=lineAt(e);if(!line||!line.path)return;
      const parts=pointerParts(line.path);
      while(parts.length&&/^\d+$/.test(parts[parts.length-1]))parts.pop(); // элемент списка → сам список
      if(!parts.length)return;
      e.preventDefault();tip.hidden=true;
      onPick('/'+parts.map(escPart).join('/'));
    });
  }

  // ── Итог без устройства ──────────────────────────────────────────────
  async function finalPanel(panel){
    panel.append(head('Итог',platformSelect(finalPlatform,async v=>{finalPlatform=v;await render()},true)));
    panel.append(make('p','Шаблон → общие замены → замены платформы, без устройства: параметры и {{токены}} остаются тегами. Наведение на строку — откуда значение.','hint'));
    const data=await api('quasar_platforms?compose='+q(finalPlatform));
    composedViewer(panel,data.config||{},data.origins||{},null);
  }

  // ── Замены устройств ─────────────────────────────────────────────────
  async function devicesPanel(panel){
    panel.append(head('Замены устройств'));
    const split=make('div','','qd-split');
    const list=make('div','','qd-list');
    if(!registry.devices.length)list.append(make('p','Устройств нет — колонка попадёт в реестр при первом запросе.','hint'));
    if(!selectedSn&&registry.devices.length)selectedSn=registry.devices[0].sn;
    for(const device of registry.devices){
      const card=make('button','','quasar-device'+(device.sn===selectedSn?' selected':''));card.type='button';
      const img=make('img','','thumb');img.alt='';
      const parsed=window.DeviceKit.parseSn(device.sn);
      window.DeviceKit.applyPicture(img,window.DeviceKit.pictureChain(parsed));
      const about=make('span');
      const session=registry.sessions.find(s=>s.device_sn===device.sn);
      const activity=(registry.activity||{})[device.sn];
      const status=session?'онлайн':(activity&&Date.now()/1000-activity.ts<600?'недавно':'офлайн');
      about.append(make('b',parsed?parsed.name:'Устройство'),make('small',device.sn+' · '+status));
      card.append(img,about);
      card.onclick=()=>work(async()=>{
        if(device.sn===selectedSn)return;
        if(dirty&&!await askDialog('В редакторе «Свой ответ» есть несохранённые изменения. Переключить и потерять их?',
          [{label:'Остаться',className:'secondary',value:false},{label:'Переключить',value:true}],'Несохранённые изменения'))return;
        dirty=false;selectedSn=device.sn;await render();
      });
      list.append(card);
    }
    const body=make('div','','qd-body');
    split.append(list,body);panel.append(split);
    const device=registry.devices.find(d=>d.sn===selectedSn);
    if(device)await devicePanel(body,device);
  }

  // Источник конфига устройства: Генерируемый · Напрямую · Свой (сохраняется сразу)
  const SOURCE_LABEL={generated:'Генерируемый',direct:'Напрямую',custom:'Свой'};
  const SOURCE_HINT={
    generated:'Колонка получает собранный конфиг: шаблон → общие замены → замены платформы → замены устройства.',
    direct:'Колонка получает ответ настоящего сервера; ниже — последний ответ. Замены устройства хранятся, но не применяются.',
    custom:'Колонка получает сохранённый ответ как есть, без замен.'};
  async function devicePanel(root,device){
    const source=await api('quasar_source?sn='+q(device.sn));
    const locked=(device.route_mode||'emulate')==='proxy';
    const top=make('div','','src-head');
    const seg=make('div','','segmented');seg.setAttribute('role','radiogroup');seg.setAttribute('aria-label','Источник конфига');
    for(const [value,label] of Object.entries(SOURCE_LABEL)){
      const b=make('button',label);b.type='button';b.setAttribute('role','radio');
      b.setAttribute('aria-checked',String(source.mode===value));
      if(source.mode===value)b.classList.add('active');
      b.onclick=()=>work(async()=>{
        if(source.mode===value)return;
        if(dirty&&!await askDialog('В редакторе «Свой ответ» есть несохранённые изменения. Переключить и потерять их?',
          [{label:'Остаться',className:'secondary',value:false},{label:'Переключить',value:true}],'Несохранённые изменения'))return;
        dirty=false;await put('quasar_source',{sn:device.sn,mode:value});await render();
      });
      seg.append(b);
    }
    const push=make('button','Перезапросить','secondary');push.type='button';
    push.title='Колонка сразу запросит конфиг заново (get_sync_info)';
    push.onclick=()=>work(async()=>{push.disabled=true;
      try{await api('quasar_source',{sn:device.sn,action:'push'});notify('Перезапрос конфига отправлен.')}
      finally{push.disabled=false}});
    top.append(seg,push);
    if(locked)top.append(make('span','маршрут: Прозрачно','chip'));
    root.append(top);
    const hint=make('p','','hint');
    if(locked)hint.textContent='Маршрут «Прозрачно»: запросы уходят на настоящий сервер. '
      +(source.mode==='direct'?'Ответ правится заменами, только пока включено «И в прозрачном маршруте».'
        :'Генерируемый и свой ответ здесь не применяются; для правки ответа выберите «Напрямую».');
    else hint.textContent=SOURCE_HINT[source.mode];
    root.append(hint);
    if(locked&&source.mode!=='direct')return;
    if(source.mode==='direct')return renderDirect(root,device,source);
    if(source.mode==='custom')return renderCustom(root,device,source);
    return renderGenerated(root,device);
  }

  // «Генерируемый»: замены устройства + итог устройства (клик по строке — новая замена)
  async function renderGenerated(root,device){
    const layer='device:'+device.sn;
    const [data,composed,full]=await Promise.all([api('quasar_rules?layer='+q(layer)),
      api('quasar_composed?raw=1&device='+q(device.sn)),
      api('quasar_composed?device='+q(device.sn))]);
    const platform=data.platform||'';
    const current=composed.config&&typeof composed.config==='object'?composed.config:null;
    const save=async next=>{await put('quasar_rules',{layer,rules:next});invalidate();await render()};
    root.append(make('h4','Замены'+(platform?' · '+platform:'')));
    root.append(ruleList(data.rules,{layer,platform,device,current}));
    if((full.errors||[]).length){ // неразрешённые подстановки/db/скрипты: правило пропущено — нижний слой стоит
      const box=make('div','','sc-err');
      for(const item of full.errors)
        box.append(make('div',(item.layer||'замены')+' «'+item.rule+'»: '+item.error));
      root.append(box);
    }
    root.append(make('h4','Итог устройства'));
    const viewer=make('div');root.append(viewer);
    if(composed.error){viewer.append(make('p','Конфиг не собирается: '+composed.error,'sc-err'));return}
    composedViewer(viewer,current||{},composed.origins||{},
      pointer=>work(()=>ruleDialog({rules:data.rules,index:null,layer,platform,device,current,save,prefill:{path:pointer}})));
  }

  // «Напрямую»: кнопка «Замены» (regexp в ответе upstream) и последний ответ, только чтение
  function renderDirect(root,device,source){
    const rules=source.replace||[];
    const enabled=rules.filter(r=>r.enabled).length;
    const bar=make('div','','sc-toolbar');
    const replaceB=make('button','Замены в тексте'+(enabled?' · '+enabled:''),'secondary');replaceB.type='button';
    replaceB.onclick=()=>replaceDialog(device,rules);
    const transparent=make('label','','check');
    const flag=make('input');flag.type='checkbox';flag.checked=source.transparent===true;
    flag.onchange=()=>work(async()=>{
      try{await put('quasar_source',{sn:device.sn,transparent:flag.checked})}
      catch(e){flag.checked=!flag.checked;throw e}
      notify(flag.checked?'Замены применяются и в маршруте «Прозрачно»':'В маршруте «Прозрачно» замены не применяются');
    });
    transparent.append(flag,document.createTextNode('И в прозрачном маршруте'));
    bar.append(replaceB,transparent);root.append(bar);
    root.append(make('h4','Последний ответ'));
    const last=source.direct_last;
    if(!last||!last.body){root.append(make('p','Ответов ещё не было: колонка опросит конфиг сама.','hint'));return}
    root.append(make('p','Получен: '+new Date(last.ts*1000).toLocaleString('ru-RU')+' · статус '+last.status
      +(last.body_sent?' · замен сработало: '+last.replaced+(last.preview?' (превью)':''):'')
      +(last.replace_error?' · ошибка замен: '+last.replace_error:''),'hint'));
    const holder=make('div','','panel');
    let ed=null;
    if(last.body_sent){
      const seg=make('div','','segmented');seg.style.margin='0 0 8px';
      for(const [label,value] of [['После замен'+(last.preview?' (превью)':''),last.body_sent],['До замен',last.body]]){
        const b=make('button',label);b.type='button';
        if(value===last.body_sent)b.classList.add('active');
        b.onclick=()=>{seg.querySelectorAll('button').forEach(x=>x.classList.toggle('active',x===b));
          ed.setValue(JSON.stringify(value,null,2));ed.refresh()};
        seg.append(b);
      }
      root.append(seg);
    }
    root.append(holder);
    ed=makeCM(holder,{value:JSON.stringify(last.body_sent||last.body,null,2),readOnly:true,height:'520px'});
  }

  // Окно «Замены в тексте»: regexp-правила ответа upstream; ошибка «Строка N: …» — у своей строки
  function replaceDialog(device,rules){
    const {dlg,form}=dialogShell('Замены в ответе · '+device.sn,'replace-dialog');
    form.append(make('p','Python regex по компактному тексту ответа, сверху вниз; группы — \\1, \\g<name>. При сохранении правила прогоняются по последнему ответу.','hint'));
    const list=make('div','','rp-list');
    const generalErr=make('p','','sc-err');generalErr.hidden=true;
    const addRow=(rule={enabled:true,pattern:'',replace:''})=>{
      const row=make('div','','rp-row');
      const on=make('input','','rp-on');on.type='checkbox';on.checked=rule.enabled!==false;on.setAttribute('aria-label','Включено');
      const pattern=make('input','','code rp-pattern');pattern.value=rule.pattern||'';pattern.placeholder='pattern';pattern.spellcheck=false;
      const replace=make('input','','code rp-replace');replace.value=rule.replace||'';replace.placeholder='replace';replace.spellcheck=false;
      const remove=make('button','✕','icon-btn rp-del');remove.type='button';remove.setAttribute('aria-label','Удалить правило');
      remove.onclick=()=>row.remove();
      const err=make('p','','sc-err rp-err');err.hidden=true;
      for(const el of [pattern,replace])el.oninput=()=>{err.hidden=true};
      row.append(on,pattern,replace,remove,err);list.append(row);return row;
    };
    for(const rule of rules)addRow(rule);
    const add=make('button','＋ Правило','secondary');add.type='button';
    add.onclick=()=>addRow().querySelector('.rp-pattern').focus();
    const save=make('button','Сохранить');save.type='submit';
    const cancel=make('button','Отмена','secondary');cancel.type='button';cancel.onclick=()=>dlg.close();
    const actions=make('div','','row');actions.append(cancel,save);
    form.onsubmit=async e=>{
      e.preventDefault();
      const rows=[...list.children];
      rows.forEach(r=>{r.querySelector('.rp-err').hidden=true});generalErr.hidden=true;
      const replace=rows.map(r=>({enabled:r.querySelector('.rp-on').checked,
        pattern:r.querySelector('.rp-pattern').value,replace:r.querySelector('.rp-replace').value}));
      save.disabled=true;
      try{
        await put('quasar_source',{sn:device.sn,replace});
        dlg.close();notify('Замены сохранены: '+replace.filter(r=>r.enabled).length+' вкл. из '+replace.length);
        await render();
      }catch(err){
        const m=/^Строка (\d+):\s*([\s\S]*)$/.exec(err.message);
        const row=m&&rows[Number(m[1])-1];
        if(row){const line=row.querySelector('.rp-err');line.textContent=m[2];line.hidden=false;row.scrollIntoView({block:'nearest'})}
        else{generalErr.textContent=err.message;generalErr.hidden=false}
      }finally{save.disabled=false}
    };
    form.append(list,add,generalErr,actions);
    dlg.showModal();
    if(!rules.length)add.click();
  }

  // «Свой»: редактор ответа + генерация эмулятором и загрузка с оригинального сервера
  function renderCustom(root,device,source){
    root.append(make('h4','Свой ответ'));
    const holder=make('div','','panel');
    const row=make('div','','sc-toolbar');row.style.margin='8px 0';
    const save=make('button','Сохранить');save.type='button';
    const gen=make('button','Сгенерировать эмулятором','secondary');gen.type='button';
    gen.title='Заполнить редактор ответом, который собрал бы эмулятор (не сохраняется)';
    const fetchB=make('button','Получить с оригинального сервера','secondary');fetchB.type='button';
    fetchB.disabled=!source.can_fetch;
    fetchB.title=source.can_fetch?'Ответ оригинального сервера на последний опрос этой колонки':'Колонка ещё не запрашивала конфиг после запуска сервера';
    const errLine=make('p','','sc-err');errLine.hidden=true;
    root.append(holder,row,errLine);
    const ed=makeCM(holder,{value:source.custom?JSON.stringify(source.custom,null,2):'',height:'460px',onDirty:()=>{dirty=true}});
    const act=fn=>async()=>{errLine.hidden=true;try{await fn()}catch(e){errLine.textContent=e.message;errLine.hidden=false}};
    save.onclick=act(async()=>{
      let parsed;try{parsed=JSON.parse(ed.getValue()||'null')}catch{throw Error('Ответ: некорректный JSON')}
      if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw Error('Ответ: JSON-объект с объектом config');
      if(!parsed.config||typeof parsed.config!=='object')throw Error('Ответ: нужен объект config (пока его нет, колонка получит 409 quasar_custom_not_set)');
      await put('quasar_source',{sn:device.sn,custom:parsed});
      dirty=false;notify('Свой ответ сохранён — колонка получит его при следующем опросе.');
    });
    gen.onclick=act(async()=>{const r=await api('quasar_source',{sn:device.sn,action:'generate'});
      ed.setValue(JSON.stringify(r,null,2));dirty=true;ed.refresh()});
    fetchB.onclick=act(async()=>{const r=await api('quasar_source',{sn:device.sn,action:'fetch'});
      if(r.body==null)throw Error('Ответ сервера не JSON (статус '+r.status+')');
      ed.setValue(JSON.stringify(r.body,null,2));dirty=true;ed.refresh()});
    row.append(save,gen,fetchB);
  }

  window.addEventListener('hashchange',()=>{if(location.hash==='#quasar-http')work(render)});
  work(render);
})();
