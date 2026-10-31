/* Вкладка «Quasar»: платформенные конфиги sync + переопределения устройств.
   Сайдбар: «Общие конфиги устройств» (общий конфиг + платформы-патчи; простой
   вид по инлайн-узлам / полный — граф-дерево со сворачиванием и редактор JSON
   на CodeMirror) и «Переопределение под устройство» (список устройств,
   переопределения, просмотр итогового конфига после сборки с происхождением
   значений — наведение показывает источник, клик создаёт переопределение). */
(() => {
  const $=id=>document.getElementById(id);
  let registry, section='platforms', selectedPlatform='', selectedSn='', mode='simple';
  let platformFile=null, dirty=false;
  let selectedBranch='';                 // выбранный путь ветви полного вида
  const foldState=new Map();             // путь → свёрнуто?; без записи — по умолчанию (глубина ≥ 2)
  const guard=()=>{if(dirty&&!confirm('Есть несохранённые изменения. Переключить и потерять их?'))return false;dirty=false;return true}
  const make=(tag,text)=>{const el=document.createElement(tag);if(text)el.textContent=text;return el};
  async function api(action,value){const response=await fetch('api/'+action,{method:value?'POST':'GET',headers:{'X-Emulator':'1','Content-Type':'application/json'},body:value?JSON.stringify(value):undefined});const result=await response.json();if(!response.ok)throw Error(result.error||response.statusText);return result}
  async function put(action,value){const response=await fetch('api/'+action,{method:'PUT',headers:{'X-Emulator':'1','Content-Type':'application/json'},body:JSON.stringify(value)});const result=await response.json();if(!response.ok)throw Error(result.error||response.statusText);return result}
  async function del(action){const response=await fetch('api/'+action,{method:'DELETE',headers:{'X-Emulator':'1'}});const result=await response.json();if(!response.ok)throw Error(result.error||response.statusText);return result}
  async function work(fn){try{await fn()}catch(e){$('notice').textContent=e.message}}

  // ── редактор кода: готовая библиотека CodeMirror (vendor/), запас — textarea
  function makeCM(container,{value='',readOnly=false,height='520px',onDirty=null}={}){
    if(typeof CodeMirror==='undefined'){
      const ta=make('textarea');ta.className='code';ta.value=value;ta.rows=18;ta.spellcheck=false;
      container.append(ta);
      if(onDirty&&!readOnly)ta.addEventListener('input',onDirty);
      return {isCM:false,getValue:()=>ta.value,setValue:v=>{ta.value=v},insert:t=>{const at=ta.selectionStart||0;ta.setRangeText(t,at,at,'end');ta.focus()},focus:()=>ta.focus(),refresh:()=>{}};
    }
    const cm=CodeMirror(container,{value,mode:'application/json',theme:'material-darker',
      lineNumbers:true,foldGutter:true,readOnly,indentUnit:2,tabSize:2,matchBrackets:true,
      gutters:['CodeMirror-linenumbers','CodeMirror-foldgutter']});
    cm.setSize('100%',height);
    setTimeout(()=>cm.refresh(),0);  // инициализация до вставки в DOM — пересчёт после монтирования
    let silent=false; // программная установка (выбор ветви) — не «грязное» изменение
    cm.on('change',()=>{if(!silent&&onDirty)onDirty()});
    return {isCM:true,cm,getValue:()=>cm.getValue(),
      setValue:v=>{silent=true;cm.setValue(v);cm.clearHistory();silent=false},
      insert:t=>{cm.replaceSelection(t);cm.focus()},focus:()=>cm.focus(),refresh:()=>cm.refresh()};
  }

  const INLINE_KEYS=['$select','$string','$number','$bool','$list','$param'];
  const inlineKindOf=node=>{
    if(!node||typeof node!=='object'||Array.isArray(node))return null;
    for(const key of Object.keys(node))if(INLINE_KEYS.includes(key))return key;
    return null;
  };
  function collectInline(node,found,path){
    if(Array.isArray(node)){node.forEach((v,i)=>collectInline(v,found,path+'.'+i));return}
    if(!node||typeof node!=='object')return;
    const kind=inlineKindOf(node);
    if(kind){found.push({node,kind,path});return}
    for(const [key,value] of Object.entries(node))collectInline(value,found,path?key+'.'+key:key);
  }
  const getByPath=(tree,p)=>{
    if(!p)return tree;
    let node=tree;
    for(const part of p.split('.')){
      if(node===null||typeof node!=='object')return undefined;
      node=node[part];
    }
    return node;
  };
  // RFC 7396 merge-patch в JS — «что увидит устройство до инлайн-узлов»
  function mergePatch(target,patch){
    if(patch===null||typeof patch!=='object'||Array.isArray(patch))return JSON.parse(JSON.stringify(patch));
    const base=(target&&typeof target==='object'&&!Array.isArray(target))?JSON.parse(JSON.stringify(target)):{};
    for(const [key,value] of Object.entries(patch)){
      if(value===null)delete base[key];else base[key]=mergePatch(base[key],value);
    }
    return base;
  }
  // JSON с отступом 2 + путь каждой строки (для хинтов происхождения)
  function formatJSON(value){
    const lines=[];
    const fmt=(v,path,prefix,indent,isLast)=>{
      const pad=' '.repeat(indent);
      if(v!==null&&typeof v==='object'){
        const isArr=Array.isArray(v);
        const entries=isArr?v.map((x,i)=>[String(i),x]):Object.entries(v);
        if(!entries.length){lines.push({text:pad+prefix+(isArr?'[]':'{}')+(isLast?'':','),path});return}
        lines.push({text:pad+prefix+(isArr?'[':'{'),path});
        entries.forEach(([key,child],i)=>{
          const childPath=path?path+'.'+key:key;
          fmt(child,childPath,isArr?'':JSON.stringify(key)+': ',indent+2,i===entries.length-1);
        });
        lines.push({text:pad+(isArr?']':'}')+(isLast?'':','),path});
      }else{
        lines.push({text:pad+prefix+JSON.stringify(v)+(isLast?'':','),path});
      }
    };
    fmt(value,'','',0,true);
    return lines;
  }
  const dictPathOf=p=>{
    const parts=(p||'').split('.');
    while(parts.length&&/^\d+$/.test(parts[parts.length-1]))parts.pop();
    return parts.join('.');
  };
  const originFor=(path,origins)=>{
    if(path&&origins[path])return origins[path];
    if(path){
      const parts=path.split('.');
      for(let n=parts.length-1;n>0;n--){
        const ancestor=parts.slice(0,n).join('.');
        if(origins[ancestor])return origins[ancestor]+' — унаследовано с '+ancestor;
      }
    }
    return origins['']||'общий конфиг (common.json)';
  };
  let cmTip=null; // единственный тултип происхождения на страницу
  const tipElement=()=>{
    if(cmTip===null){cmTip=make('div');cmTip.className='cm-tip';cmTip.hidden=true;document.body.append(cmTip)}
    return cmTip;
  };

  // ── каркас: сайдбар + контент ─────────────────────────────────────────
  async function render(){registry=await api('identities');const root=$('quasar-http');root.replaceChildren();
    const layout=make('div');layout.className='quasar-layout';
    const side=make('nav');side.className='quasar-side';side.setAttribute('aria-label','Разделы Quasar');
    const bPlatforms=make('button','Общие конфиги устройств');bPlatforms.type='button';
    const bDevices=make('button','Переопределение под устройство');bDevices.type='button';
    bPlatforms.onclick=()=>{if(guard()){section='platforms';render()}};
    bDevices.onclick=()=>{if(guard()){section='devices';render()}};
    (section==='platforms'?bPlatforms:bDevices).classList.add('active');
    side.append(bPlatforms,bDevices);
    const content=make('div');content.className='quasar-content';content.id='quasar-content';
    layout.append(side,content);root.append(layout);
    if(section==='platforms')await renderPlatforms(content);else await renderDevices(content);
    const actionsRoot=$('device-actions');
    if(actionsRoot)await actions(actionsRoot);
  }

  // ── Общие конфиги: общий конфиг (не удаляемый) + платформы-патчи ──────
  let overview=null; // {common, platforms, tokens, inline, sources} из API
  async function renderPlatforms(root){
    root.replaceChildren();  // единый редактор: никакого накопления экземпляров
    overview=await api('quasar_platforms');
    if(!selectedPlatform)selectedPlatform='common';
    const tabs=make('div');tabs.className='sc-toolbar';
    const commonB=make('button','Общий конфиг');commonB.type='button';commonB.title='База для всех платформ — не удаляется';
    if(selectedPlatform==='common')commonB.classList.add('active');
    commonB.onclick=()=>{if(guard()){selectedPlatform='common';mode='simple';selectedBranch='';work(render)}};
    tabs.append(commonB);
    for(const meta of overview.platforms){
      const b=make('button',meta.platform+(meta.default?' · по умолчанию':''));b.type='button';
      if(meta.platform===selectedPlatform)b.classList.add('active');
      b.title=meta.title+' · патч '+meta.patch_bytes+' Б';
      b.onclick=()=>{if(guard()){selectedPlatform=meta.platform;mode='simple';selectedBranch='';work(render)}};
      tabs.append(b);
    }
    const add=make('button','＋ платформа');add.type='button';add.className='secondary';
    add.onclick=()=>work(async()=>{const name=prompt('Идентификатор платформы (например station_max):');
      if(!name)return;await put('quasar_platforms',{platform_file:{platform:name,title:'Платформа '+name,
        description:'',patch:{}}});selectedPlatform=name;await render()});
    const delB=make('button','Удалить платформу');delB.type='button';delB.className='secondary danger';
    delB.onclick=()=>work(async()=>{if(selectedPlatform==='common'||!confirm('Удалить платформу '+selectedPlatform+'?'))return;
      await del('quasar_platforms?platform='+encodeURIComponent(selectedPlatform));
      selectedPlatform='common';await render()});
    tabs.append(add,delB);root.append(tabs);
    const file=selectedPlatform==='common'?overview.common
      :await api('quasar_platforms?platform='+encodeURIComponent(selectedPlatform));
    platformFile=file;
    const modes=make('div');modes.className='seg';modes.style.margin='10px 0';
    const simple=make('button','Простой');simple.type='button';
    const full=make('button','Полный (JSON + граф)');full.type='button';
    (mode==='simple'?simple:full).classList.add('active');
    simple.onclick=()=>{if(mode!=='simple'&&guard()){mode='simple';work(render)}};
    full.onclick=()=>{if(mode!=='full'&&guard()){mode='full';work(render)}};
    const hint=make('p',selectedPlatform==='common'
      ?'Общий конфиг — база всех устройств. Инлайн-узлы ($-ключи) превращаются в контролы простого вида; полный вид — граф-дерево и редактор JSON.'
      :'Платформа — merge-patch (RFC 7396) поверх общего конфига: только отличия; null удаляет ключ. Инлайн-узлы патчатся напрямую.');
    hint.className='hint';
    modes.append(simple,full);root.append(hint,modes);
    const save=async()=>{if(selectedPlatform==='common')await put('quasar_platforms',{common_file:file});
      else await put('quasar_platforms',{platform_file:{platform:selectedPlatform,patch:file}});
      dirty=false};
    if(mode==='simple')renderSimple(root,file,save);else renderFull(root,file,save);
  }

  // Простой вид: контролы по инлайн-узлам ($select/$bool/$string/$number/$list/$param)
  function renderSimple(root,file,save){
    const found=[];collectInline(file,found,'');
    const box=make('div');box.className='endpoint-tree';
    if(!found.length){const p=make('p','Инлайн-узлов нет — простой вид пуст. Добавьте узел в полном редакторе: например {"$select":"Вкл","Вкл":{…},"Выкл":{…},"$name":"Подпись"}.');p.className='hint';box.append(p)}
    for(const {node,kind,path} of found){
      const row=make('div');row.className='tree-leaf';row.style.flexWrap='wrap';
      row.append(make('span',node.$name||path));row.title=(node.$hint||'')+' · '+path;
      const apply=async fn=>{fn();await save();$('notice').textContent='Применено: '+path;await render()};
      if(kind==='$select'){
        const select=make('select');
        for(const key of Object.keys(node)){
          if(key.startsWith('$'))continue;
          const o=make('option',key);o.value=key;
          if(node.$select===key)o.selected=true;select.append(o)}
        select.onchange=()=>apply(()=>{node.$select=select.value});
        row.append(select);
      }else if(kind==='$bool'){
        const cb=make('input');cb.type='checkbox';cb.checked=!!node.$bool;
        cb.onchange=()=>apply(()=>{node.$bool=cb.checked});
        row.append(cb);
      }else if(kind==='$list'){
        const input=make('input');input.type='text';input.value=Array.isArray(node.$list)?node.$list.join(', '):'';
        input.onchange=()=>apply(()=>{node.$list=input.value.split(',').map(x=>x.trim()).filter(Boolean)});
        row.append(input);
      }else if(kind==='$param'){
        const input=make('input');input.type='text';input.value=node.$param;
        input.title='Ключ из каталога параметров (раздел «Параметры») или device.sn / user.name…';
        input.onchange=()=>apply(()=>{node.$param=input.value});
        row.append(input);
      }else{ // $string | $number
        const input=make('input');input.type=kind==='$number'?'number':'text';input.value=node[kind]??'';
        input.onchange=()=>apply(()=>{node[kind]=kind==='$number'?Number(input.value):input.value});
        row.append(input);
      }
      box.append(row);
    }
    root.append(box);
  }

  // Полный вид: граф-дерево (сворачивание +/-, шаблоны целиком) + редактор
  // выбранной ветви на CodeMirror (подсветка, фолдинг), чипы-подсказки внизу
  function renderFull(root,file,save){
    const split=make('div');split.className='split';
    const tree=make('div');tree.className='quasar-tree';
    const editor=make('div');editor.className='quasar-editor';
    const branchLabel=make('p','Ветвь: корень файла');branchLabel.className='hint';
    const edHolder=make('div');
    const ed=makeCM(edHolder,{height:'540px',onDirty:()=>{dirty=true}});
    const applyB=make('button','Сохранить ветвь');applyB.type='button';
    const chips=make('div');chips.className='chips-below';
    const hints=[['{{now_iso}}','время ответа'],['{{device_id}}','DeviceId'],['{{device_name}}','имя из реестра'],
      ['{{user.name}}','имя владельца'],['{{device.room}}','комната'],['{"db":"user.location"}','поле из БД эмулятора'],
      ['{"run":"script.bat"}','запуск скрипта (state/quasar/scripts)'],
      ['{"$select":"Вкл","Вкл":{…},"Выкл":{…},"$name":"Подпись"}','инлайн-селектор'],
      ['{"$bool":true,"$name":"Флаг"}','вкл/выкл'],['{"$string":"{{device_name}}"}','строка с токеном'],
      ['{"$param":"user.name"}','параметр из каталога']];
    const chipTitle=make('span','Вставить в позицию курсора:');chipTitle.style.color='var(--faint)';chipTitle.style.fontSize='11.5px';
    chips.append(chipTitle);
    for(const [text,title] of hints){const chip=make('code',text);chip.title=title;
      chip.onclick=()=>ed.insert(text);chips.append(chip)}
    applyB.onclick=()=>work(async()=>{
      let value;try{value=JSON.parse(ed.getValue())}catch{throw Error('Ветвь: некорректный JSON')}
      if(!selectedBranch){for(const key of Object.keys(file))delete file[key];Object.assign(file,value)}
      else{
        const parts=selectedBranch.split('.');
        let node=file;
        for(const part of parts.slice(0,-1)){
          let child=Array.isArray(node)?node[Number(part)]:node[part];
          if(!child||typeof child!=='object'){child={};if(Array.isArray(node))node[Number(part)]=child;else node[part]=child}
          node=child;
        }
        const last=parts[parts.length-1];
        if(Array.isArray(node))node[Number(last)]=value;else node[last]=value;
      }
      await save();$('notice').textContent='Ветвь сохранена: '+(selectedBranch||'файл')});
    editor.append(branchLabel,edHolder,applyB,chips);
    const rows={};
    const select=(node,path)=>{
      selectedBranch=path;
      ed.setValue(JSON.stringify(node,null,2));
      branchLabel.textContent='Ветвь: '+(path||'корень файла');
      tree.querySelectorAll('.quasar-branch.selected').forEach(x=>x.classList.remove('selected'));
      if(rows[path])rows[path].classList.add('selected');
      setTimeout(()=>ed.refresh(),0);
    };
    const buildNode=(host,node,path,label,depth)=>{
      const row=make('div');row.className='quasar-branch'+(path===selectedBranch?' selected':'');
      row.style.paddingLeft=(depth*12)+'px';
      const kind=inlineKindOf(node);
      if(kind){ // шаблон: содержимое не раскрываем — целиком в редактор справа
        const btn=make('button','Шаблон');btn.type='button';btn.className='tpl-btn';
        row.append(btn,make('span',(node.$name||label)+(kind==='$select'&&node.$select?' · '+node.$select:'')));
        row.title=(node.$hint||'инлайн-узел '+kind)+' — '+path;
        row.onclick=()=>select(node,path);
        host.append(row);rows[path]=row;return;
      }
      const isObj=node!==null&&typeof node==='object';
      const entries=isObj?(Array.isArray(node)?node.map((v,i)=>[String(i),v]):Object.entries(node)):[];
      if(!entries.length){ // пустой объект/массив или скаляр-лист
        if(!isObj)row.classList.add('leaf');
        row.textContent=isObj?(label+(Array.isArray(node)?' []':' {}')):(label+': '+JSON.stringify(node));
        row.title=path;
        row.onclick=()=>select(node,path);
        host.append(row);rows[path]=row;return;
      }
      const tw=make('span');tw.className='tw';tw.textContent='▾';
      const head=make('span',label+(Array.isArray(node)?' []':' {}'));head.style.flex='1';
      const count=make('span',String(entries.length));count.className='count';count.title='элементов';
      const kids=make('div');
      const folded=foldState.has(path)?foldState.get(path):depth>=2;
      kids.hidden=folded;tw.textContent=folded?'▸':'▾';
      tw.onclick=e=>{e.stopPropagation();
        const now=!kids.hidden;foldState.set(path,now);kids.hidden=now;tw.textContent=now?'▸':'▾'};
      row.append(tw,head,count);
      row.title=path;
      row.onclick=()=>select(node,path);
      rows[path]=row;
      host.append(row,kids);
      for(const [key,child] of entries)buildNode(kids,child,path?path+'.'+key:key,key,depth+1);
    };
    buildNode(tree,file,'','файл',0);
    // листья-скаляры: отдельные строки «ключ: значение»
    split.append(tree,editor);root.append(split);
    const existing=getByPath(file,selectedBranch);
    select(existing===undefined?file:(existing===null?null:existing),existing===undefined?'':selectedBranch);
  }

  // ── Переопределение под устройство ────────────────────────────────────
  async function renderDevices(root){
    root.replaceChildren();
    const split=make('div');split.className='split';
    const list=make('div');list.className='list';
    if(!registry.devices.length)list.append(make('p','Устройств нет — колонка попадёт в реестр сама при первом запросе.'));
    for(const device of registry.devices){
      const card=make('div');card.className='quasar-device'+(device.sn===selectedSn?' selected':'');
      const img=make('img');img.className='thumb';img.alt='';
      window.DeviceKit.applyPicture(img,window.DeviceKit.pictureChain(window.DeviceKit.parseSn(device.sn)));
      const about=make('div');
      const parsed=window.DeviceKit.parseSn(device.sn);
      about.append(make('b',parsed?parsed.name:'Устройство'),make('small','SN '+device.sn));
      const session=registry.sessions.find(s=>s.device_sn===device.sn);
      const activity=(registry.activity||{})[device.sn];
      const status=session?'онлайн · WS':(activity&&Date.now()/1000-activity.ts<600?'виден недавно':'офлайн');
      about.append(make('small',' · '+status+(activity&&activity.ip?' · '+activity.ip:'')));
      card.append(img,about);
      card.onclick=()=>{if(guard()){selectedSn=device.sn;work(render)}};
      list.append(card);
    }
    const panel=make('div');panel.style.flex='1';
    const device=registry.devices.find(d=>d.sn===selectedSn);
    if(!device)panel.append(make('p','Выберите устройство слева.'));
    else await renderOverrides(panel,device);
    split.append(list,panel);root.append(split);
  }

  let selectedPlatformBody=null;
  // Источник конфига устройства (TASK-052): Генерируемый · Напрямую · Свой.
  // Переключатель в строке заголовка (на 400px переносится под него), смена
  // сохраняется сразу; при маршруте устройства Прокси/Запись неактивен.
  const SOURCE_LABEL={generated:'Генерируемый',direct:'Напрямую',custom:'Свой'};
  const SOURCE_HINT={
    generated:'Колонка получает конфиг, собранный эмулятором: платформа → переопределения → инлайн-узлы. Ниже — переопределения и итоговая сборка.',
    direct:'Колонка получает ответ настоящего quasar.yandex.net. Ниже — последний полученный ответ, только чтение. Переопределения сохраняются, но не применяются.',
    custom:'Колонка получает сохранённый ответ как есть: без слоёв и инлайн-узлов. Заполните редактор вручную, генерацией или загрузкой с оригинального сервера и сохраните.'};
  async function renderOverrides(root,device){
    root.replaceChildren();
    let source;
    try{source=await api('quasar_source?sn='+encodeURIComponent(device.sn))}
    catch(e){const pe=make('p','Ошибка загрузки: '+e.message);pe.className='hint';root.append(pe);return}
    const routed=device.route_mode||registry.default_route_mode||'emulate';
    const locked=routed==='proxy'||routed==='record';
    const headRow=make('div');headRow.className='src-head';
    headRow.append(make('h3','Переопределения · '+device.sn));
    if(locked){
      const badge=make('span','маршрут: '+(routed==='proxy'?'Прокси':'Запись'),'chip');
      headRow.append(badge);
    }else{
      const seg=make('div');seg.className='segmented';
      for(const [value,label] of Object.entries(SOURCE_LABEL)){
        const b=make('button',label);b.type='button';
        if(source.mode===value)b.classList.add('active');
        b.onclick=()=>work(async()=>{await put('quasar_source',{sn:device.sn,mode:value});await render()});
        seg.append(b);
      }
      headRow.append(seg);
    }
    root.append(headRow);
    const hint=make('p');hint.className='hint';
    hint.textContent=locked
      ?'Маршрут устройства «'+(routed==='proxy'?'Прокси':'Запись')+'»: все запросы и так уходят в Яндекс, источник конфига не применяется.'
      :SOURCE_HINT[source.mode];
    root.append(hint);
    if(locked)return;
    if(source.mode==='direct')return renderDirect(root,source);
    if(source.mode==='custom')return renderCustom(root,device,source);
    return renderGenerated(root,device);
  }

  // «Напрямую»: последний ответ Яндекса, только чтение
  function renderDirect(root,source){
    root.append(make('h3','Последний ответ Яндекса'));
    const last=source.direct_last;
    if(!last||!last.body){
      const p=make('p','Ответов от Яндекса ещё не было: колонка опросит конфиг сама, ответ появится здесь.');
      p.className='hint';root.append(p);return;
    }
    root.append(make('p','Получен: '+new Date(last.ts*1000).toLocaleString('ru-RU')+' · статус '+last.status,'hint'));
    const holder=make('div');holder.className='panel';
    makeCM(holder,{value:JSON.stringify(last.body,null,2),readOnly:true,height:'520px'});
    root.append(holder);
    setTimeout(()=>[...holder.querySelectorAll('.CodeMirror')].forEach(el=>el.CodeMirror.refresh()),50);
  }

  // «Свой»: редактор ответа + генерация эмулятором и загрузка с оригинального сервера
  function renderCustom(root,device,source){
    root.append(make('h3','Свой ответ'));
    const holder=make('div');holder.className='panel';
    const ed=makeCM(holder,{value:source.custom?JSON.stringify(source.custom,null,2):'',height:'460px',onDirty:()=>{dirty=true}});
    const row=make('div');row.className='sc-toolbar';row.style.margin='8px 0';
    const save=make('button','Сохранить');save.type='button';
    const gen=make('button','Сгенерировать эмулятором');gen.type='button';gen.className='secondary';
    gen.title='Заполнить редактор ответом, который собрал бы эмулятор (не сохраняется)';
    const fetchB=make('button','Получить с оригинального сервера');fetchB.type='button';fetchB.className='secondary';
    fetchB.disabled=!source.can_fetch;
    fetchB.title=source.can_fetch?'Заполнить редактор ответом Яндекса на последний опрос этой колонки':'Колонка ещё не запрашивала конфиг после запуска сервера';
    const errLine=make('p');errLine.className='sc-err';errLine.hidden=true;
    const act=fn=>async()=>{errLine.hidden=true;try{await fn()}catch(e){errLine.textContent=e.message;errLine.hidden=false}};
    const saveJSON=async value=>{
      let parsed;try{parsed=JSON.parse(value||'null')}catch{throw Error('Ответ: корректный JSON')}
      if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw Error('Ответ: JSON-объект с объектом config');
      if(!parsed.config||typeof parsed.config!=='object')throw Error('Ответ: нужен объект config (пока его нет, колонка ответит 409 quasar_custom_not_set)');
      await put('quasar_source',{sn:device.sn,custom:parsed});
      dirty=false;$('notice').textContent='Свой ответ сохранён — колонка получит его при следующем опросе.';
    };
    save.onclick=act(()=>saveJSON(ed.getValue()));
    gen.onclick=act(async()=>{const r=await api('quasar_source',{sn:device.sn,action:'generate'});
      ed.setValue(JSON.stringify(r,null,2));dirty=true;setTimeout(()=>ed.refresh(),0)});
    fetchB.onclick=act(async()=>{const r=await api('quasar_source',{sn:device.sn,action:'fetch'});
      if(r.body==null)throw Error('Ответ сервера не JSON (статус '+r.status+')');
      ed.setValue(JSON.stringify(r.body,null,2));dirty=true;setTimeout(()=>ed.refresh(),0)});
    row.append(save,gen,fetchB);
    root.append(holder,row,errLine);
    setTimeout(()=>ed.refresh(),50);
  }

  // «Генерируемый»: прежнее содержимое панели — переопределения и итоговая сборка
  async function renderGenerated(root,device){
    const intro=make('p','Берётся файл платформы устройства, применяются только эти значения. Источник: вариант селектора либо своё значение/шаблон/база/скрипт.');
    intro.className='hint';root.append(intro);
    const platformName=(device.profile||{}).platform;
    selectedPlatformBody=platformName?await api('quasar_platforms?platform='+encodeURIComponent(platformName)):null;
    const overrides=(device.state||{}).quasar_overrides||[];
    const box=make('div');box.className='endpoint-tree';
    if(!overrides.length){const p=make('p','Переопределений нет: устройство получает базовый конфиг платформы'+(platformName?' '+platformName:' (платформа не задана — используется default)')+'.');p.className='hint';box.append(p)}
    for(let i=0;i<overrides.length;i++){
      const item=overrides[i];
      const row=make('div');row.className='tree-leaf';
      row.append(make('span',item.path));
      const summary=make('code',JSON.stringify(item.source).slice(0,80));summary.style.flex='1';row.append(summary);
      const edit=make('button','Править');edit.type='button';edit.className='secondary';
      edit.onclick=()=>overrideDialog(device,platformName,overrides,i);
      const delB=make('button','✕');delB.type='button';delB.className='icon-btn';delB.title='Удалить';
      delB.onclick=()=>work(async()=>{const state=JSON.parse(JSON.stringify(device.state));
        state.quasar_overrides=overrides.filter((_,j)=>j!==i);
        await api('identities',{operation:'state',sn:device.sn,state});
        $('notice').textContent='Переопределение удалено';await render()});
      row.append(edit,delB);box.append(row);
    }
    root.append(box);
    const add=make('button','＋ добавить значение');add.type='button';
    add.onclick=()=>overrideDialog(device,platformName,overrides,null);
    root.append(add);
    const finalHead=make('h3','Итоговый конфиг устройства (после сборки)');
    const finalHint=make('p','Только просмотр: общий → платформа'+(platformName?' '+platformName:' (default)')+' → переопределения → инлайн-узлы → динамика. Наведите на строку — подсказка, откуда значение; клик по строке — создать переопределение для этого пути.');
    finalHint.className='hint';
    const viewer=make('div');viewer.className='panel';
    root.append(finalHead,finalHint,viewer);
    await loadComposed(viewer,device);
  }

  // Просмотр итогового конфига: read-only CodeMirror, хинты происхождения,
  // клик — диалог переопределения с предзаполненным путём и текущим значением
  async function loadComposed(root,device){
    root.replaceChildren();
    let data;
    try{data=await api('quasar_composed?device='+encodeURIComponent(device.sn))}
    catch(e){root.append(make('p','Ошибка загрузки: '+e.message));return}
    if(data.error){root.append(make('p','Конфиг не собирается: '+data.error));return}
    if(data.legacy){root.append(make('p','Устройство на legacy-конфиге state.quasar (до платформ).'));return}
    const value=(data.config&&typeof data.config==='object')?data.config:{};
    const lines=formatJSON(value);
    const holder=make('div');
    makeCM(holder,{value:lines.map(l=>l.text).join('\n'),readOnly:true,height:'540px'});
    root.append(holder);
    const eachCM=()=>[...holder.querySelectorAll('.CodeMirror')].forEach(el=>el.CodeMirror.refresh());
    setTimeout(eachCM,50);  // создан вне DOM — обязателен пересчёт после вставки
    requestAnimationFrame(eachCM);
    if(typeof CodeMirror==='undefined')return;
    const cm=holder.querySelector('.CodeMirror').CodeMirror;
    const tip=tipElement();
    const wrapper=cm.getWrapperElement();
    wrapper.addEventListener('mousemove',e=>{
      const pos=cm.coordsChar({left:e.clientX,top:e.clientY});
      if(!pos||pos.line<0||pos.line>=lines.length){tip.hidden=true;return}
      const path=lines[pos.line].path;
      tip.textContent=(path||'корень')+'\n→ '+originFor(path,data.origins||{});
      tip.style.left=Math.min(e.clientX+14,window.innerWidth-360)+'px';
      tip.style.top=(e.clientY+18)+'px';
      tip.hidden=false;
    });
    wrapper.addEventListener('mouseleave',()=>{tip.hidden=true});
    cm.on('mousedown',(cmInstance,e)=>{
      if(e.target.closest&&e.target.closest('.CodeMirror-gutters'))return; // не мешаем фолдингу
      const pos=cmInstance.coordsChar({left:e.clientX,top:e.clientY});
      if(!pos||pos.line<0||pos.line>=lines.length)return;
      const path=dictPathOf(lines[pos.line].path);
      if(!path)return;
      e.preventDefault();
      overrideDialog(device,(device.profile||{}).platform,
        (device.state||{}).quasar_overrides||[],null,
        {path,value:getByPath(value,path)});
    });
  }

  // Диалог переопределения: путь (инлайн-узлы или вручную) + источник
  function overrideDialog(device,platformName,overrides,index,prefill=null){
    const dlg=document.createElement('dialog');dlg.className='catalog-dialog';
    const form=document.createElement('form');dlg.append(form);document.body.append(dlg);
    const editing=index!==null&&index!==undefined?overrides[index]:null;
    const merged=mergePatch((overview&&overview.common)||{},selectedPlatformBody||{});
    const nodes=[];collectInline(merged,nodes,'');
    const fromList=make('select');
    fromList.append(new Option('— путь вручную —',''));
    for(const {node,kind,path} of nodes)
      fromList.append(new Option((node.$name||path)+' ['+kind+'] · '+path,path));
    const pathInput=make('input');pathInput.value=editing?editing.path:(prefill&&prefill.path)||'';
    pathInput.placeholder='config.device_config.name';
    fromList.onchange=()=>{if(fromList.value)pathInput.value=fromList.value;sync()};
    if(editing)fromList.disabled=true;
    const sourceType=make('select');
    for(const [value,title] of Object.entries({literal:'своё значение (JSON)',expr:'шаблон {{токены}}',
      db:'из БД эмулятора (user./device.)',run:'запуск скрипта (белый список)',
      select:'переключить селектор (вариант $select)',delete:'удалить ключ'}))sourceType.append(new Option(title,value));
    const sourceValue=make('input');sourceValue.placeholder='{{user.name}} / user.location / script.bat';
    const literalLabel=make('label','Значение (JSON)');const literalHolder=make('div');literalLabel.append(literalHolder);
    const templatePick=make('select');
    templatePick.title='Варианты селектора по выбранному пути';
    function sync(){
      const isSelect=sourceType.value==='select';
      const isLiteral=sourceType.value==='literal';
      const isDelete=sourceType.value==='delete';
      templatePick.hidden=!isSelect;literalLabel.hidden=!isLiteral;
      sourceValue.hidden=isSelect||isLiteral||isDelete;
      if(isSelect){
        templatePick.replaceChildren(new Option('— вариант —',''));
        const found=nodes.find(x=>x.path===pathInput.value.trim());
        if(found&&found.kind==='$select')
          for(const key of Object.keys(found.node)){if(key.startsWith('$'))continue;templatePick.append(new Option(key,key))}
      }
    }
    const literal=makeCM(literalHolder,{height:'140px'});
    const fill=()=>{
      const source=(editing&&typeof editing.source==='object'&&editing.source!==null)?editing.source:{};
      if('delete' in source)sourceType.value='delete';
      else if('$select' in source){sourceType.value='select';}
      else if('expr' in source)sourceType.value='expr';
      else if('db' in source)sourceType.value='db';
      else if('run' in source)sourceType.value='run';
      else sourceType.value='literal';
      if('expr' in source)sourceValue.value=source.expr;
      if('db' in source)sourceValue.value=source.db;
      if('run' in source)sourceValue.value=source.run;
      if(sourceType.value==='literal')
        literal.setValue(JSON.stringify(editing?editing.source:(prefill&&prefill.value!==undefined?prefill.value:''),null,2));
      sync();
      if('$select' in source)templatePick.value=source.$select;
    };
    sourceType.onchange=()=>{if(sourceType.value==='literal')
      literal.setValue(JSON.stringify(editing?editing.source:(prefill&&prefill.value!==undefined?prefill.value:''),null,2));sync()};
    pathInput.oninput=sync;
    fill();
    const save=make('button','Сохранить');save.type='submit';
    form.onsubmit=e=>{e.preventDefault();work(async()=>{
      const path=pathInput.value.trim();
      if(!path)throw Error('Укажите путь');
      let source;
      if(sourceType.value==='delete')source={delete:true};
      else if(sourceType.value==='literal'){
        try{source=JSON.parse(literal.getValue()||'null')}catch{throw Error('Значение: корректный JSON')}
      }else if(sourceType.value==='select'){
        if(!templatePick.value)throw Error('Выберите вариант селектора (путь должен указывать на $select-узел)');
        source={'$select':templatePick.value};
      }else{
        const v=sourceValue.value.trim();
        if(!v)throw Error('Укажите значение');
        source={[sourceType.value]:v};
      }
      const state=JSON.parse(JSON.stringify(device.state));state.quasar_overrides=state.quasar_overrides||[];
      const item={path,source};
      if(index!==null&&index!==undefined)state.quasar_overrides[index]=item;else state.quasar_overrides.push(item);
      await api('identities',{operation:'state',sn:device.sn,state});
      dlg.close();dlg.remove();
      $('notice').textContent='Переопределение сохранено: '+path;await render()})};
    const pathLabel=make('label','Путь (инлайн-узлы или вручную)');pathLabel.append(fromList,pathInput);
    const typeLabel=make('label','Источник');typeLabel.append(sourceType);
    const valueLabel=make('label','Значение');valueLabel.append(sourceValue,templatePick);
    const cancel=make('button','Отмена');cancel.type='button';cancel.className='secondary';
    cancel.onclick=()=>{dlg.close();dlg.remove()};
    const actions=make('div');actions.className='row';actions.append(cancel,save);
    form.append(pathLabel,typeLabel,valueLabel,literalLabel,actions);
    dlg.showModal();
    dlg.addEventListener('close',()=>dlg.remove());
    setTimeout(()=>literal.refresh(),60);
  }

  async function actions(root){root.replaceChildren(make('h2','Действия устройства · WS Push'),make('p','Выберите конкретное активное соединение. ACK подтверждает доставку; выполнение показывается как неподтверждённое.'));
    const inventory=await api('actions'),form=make('form');root.append(form);const sl=make('label','Сессия устройства'),sessions=make('select');sl.append(sessions);form.append(sl);
    for(const s of registry.sessions.filter(s=>s.device_sn&&s.user_id)){const o=make('option',`${s.device_sn} · ${s.connection_id.slice(0,8)}`);o.value=s.connection_id;o.dataset.sn=s.device_sn;sessions.append(o)}
    const al=make('label','Команда'),action=make('select');al.append(action);form.append(al);for(const a of inventory.catalog){const o=make('option',a.name+' · '+a.id);o.value=a.id;action.append(o)}
    const label=make('label','Громкость (0–10)'),level=make('input');level.type='number';level.min=0;level.max=10;level.step=0.1;level.value=5;label.append(level);form.append(label);action.onchange=()=>label.hidden=action.value!=='sound_set_level';action.onchange();
    const send=make('button','Отправить на выбранное устройство');send.type='submit';send.disabled=!sessions.options.length;form.append(send);form.onsubmit=e=>{e.preventDefault();work(async()=>{send.disabled=true;try{await api('actions',{connection_id:sessions.value,device_sn:sessions.selectedOptions[0].dataset.sn,action:action.value,arguments:action.value==='sound_set_level'?{level:Number(level.value)}:{}});$('notice').textContent='Команда отправлена. Выполнение ещё не подтверждено.';await render()}finally{send.disabled=!sessions.options.length}})};
    if(!sessions.options.length)root.append(make('p','Нет активных привязанных WS-соединений эмулятора. В режиме проксирования команды через этот адаптер не отправляются.'));
    const refresh=make('button','Обновить сессии и доставку');refresh.onclick=()=>work(render);root.append(refresh);for(const h of inventory.history.slice(-15).reverse()){const line=make('p',`${h.device_sn} · ${h.action} · доставка: ${h.delivery} · выполнение: ${h.execution}`);root.append(line)}
  }
  window.addEventListener('hashchange',()=>{if(['#quasar-http','#device-actions'].includes(location.hash))work(render)});
  work(render);
})();
