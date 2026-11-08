/* Вкладка «Quasar»: слои конфига + переопределения устройств.
   Стек слоёв сборки слева: встроенная база (только чтение) → общий →
   платформа → под устройство (раздел переопределений в той же панели).
   Панель слоя — настройки по инлайн-узлам, JSON слоя с деревом, поиском по
   путям и редактором ветви, полоса несохранённых изменений; «Итог для
   платформы» — просмотр собранного конфига с происхождением значений. */
(() => {
  const $=id=>document.getElementById(id);
  let registry, selectedSn='';
  let dirty=false;                    // несохранённый редактор «Свой ответ» (слой устройств)
  // слои конфига
  let layer='common';                 // base | common | platform | devices
  let basePlatform='';                // платформа встроенной базы ('' — общий дефолт)
  let selectedPlatform='';            // платформа патча
  let overview=null;                  // кэш общего GET quasar_platforms
  let workingFile=null;               // правимая копия тела активного слоя
  let layerKey='';                    // какое тело загружено ('base:x'|'common'|'platform:x'|'devices')
  let changes=0;                      // несохранённые изменения активного слоя
  let saveErr='';                     // ошибка сохранения — в полосе изменений
  let jsonOpen=false;                 // состояние <details> «JSON слоя»
  let branchFilter='';                // фильтр «найти ключ»
  let selectedBranch='';              // выбранный путь ветви JSON-раздела
  const foldState=new Map();          // путь → свёрнуто?; без записи — по умолчанию (глубина ≥ 2)
  const readOnlyLayer=()=>layer==='base';
  const ruPlural=(n,forms)=>{const m10=n%10,m100=n%100;
    return forms[(m10===1&&m100!==11)?0:(m10>=2&&m10<=4&&(m100<12||m100>14))?1:2]};
  const make=(tag,text)=>{const el=document.createElement(tag);if(text)el.textContent=text;return el};
  async function api(action,value){const response=await fetch('api/'+action,{method:value?'POST':'GET',headers:{'X-Emulator':'1','Content-Type':'application/json'},body:value?JSON.stringify(value):undefined});const result=await response.json();if(!response.ok)throw Error(result.error||response.statusText);return result}
  async function put(action,value){const response=await fetch('api/'+action,{method:'PUT',headers:{'X-Emulator':'1','Content-Type':'application/json'},body:JSON.stringify(value)});const result=await response.json();if(!response.ok)throw Error(result.error||response.statusText);return result}
  async function del(action){const response=await fetch('api/'+action,{method:'DELETE',headers:{'X-Emulator':'1'}});const result=await response.json();if(!response.ok)throw Error(result.error||response.statusText);return result}
  async function work(fn){try{await fn()}catch(e){notify(e.message,{error:true})}}

  // ── диалоги вместо confirm()/prompt(): askDialog → Promise значения кнопки ──
  function askDialog(text,actions,title){
    return new Promise(resolve=>{
      const dlg=document.createElement('dialog');dlg.className='catalog-dialog';
      dlg.setAttribute('aria-label',title||'Подтверждение');
      const form=document.createElement('form');dlg.append(form);document.body.append(dlg);
      if(title)form.append(make('h3',title));
      const p=make('p',text);p.className='hint';form.append(p);
      const row=make('div');row.className='row';form.append(row);
      for(const a of actions){
        const b=make('button',a.label);b.type='button';
        if(a.className)b.className=a.className;
        b.onclick=()=>{dlg.close();resolve(a.value)};
        row.append(b);
      }
      dlg.addEventListener('close',()=>{dlg.remove();resolve(null)});
      dlg.showModal();
    });
  }
  const confirmDiscard=message=>askDialog(message,[
    {label:'Остаться',className:'secondary',value:false},{label:'Переключить',value:true}],
    'Несохранённые изменения');
  const leaveLayers=()=>askDialog('Что сделать с несохранёнными изменениями слоя?',[
    {label:'Отмена',className:'secondary',value:'cancel'},
    {label:'Не сохранять',className:'secondary',value:'discard'},
    {label:'Сохранить',value:'save'}],'Несохранённые изменения');
  function askText(title,text,placeholder,value=''){
    return new Promise(resolve=>{
      const dlg=document.createElement('dialog');dlg.className='catalog-dialog';
      dlg.setAttribute('aria-label',title);
      const form=document.createElement('form');dlg.append(form);document.body.append(dlg);
      form.append(make('h3',title));
      if(text){const p=make('p',text);p.className='hint';form.append(p)}
      const input=make('input');input.value=value;input.placeholder=placeholder||'';input.spellcheck=false;
      const label=make('label','Имя');label.append(input);form.append(label);
      const row=make('div');row.className='row';
      const cancel=make('button','Отмена');cancel.type='button';cancel.className='secondary';cancel.onclick=()=>dlg.close();
      const save=make('button','Сохранить');save.type='submit';
      row.append(cancel,save);form.append(row);
      form.onsubmit=e=>{e.preventDefault();dlg.close();resolve(input.value.trim())};
      dlg.addEventListener('close',()=>{dlg.remove();resolve(null)});
      dlg.showModal();input.focus();
    });
  }

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
  const originFor=(path,origins,fallback)=>{
    if(path&&origins[path])return origins[path];
    if(path){
      const parts=path.split('.');
      for(let n=parts.length-1;n>0;n--){
        const ancestor=parts.slice(0,n).join('.');
        if(origins[ancestor])return origins[ancestor]+' — унаследовано с '+ancestor;
      }
    }
    return origins['']||fallback||'общий конфиг (common.json)';
  };
  let cmTip=null; // единственный тултип происхождения на страницу
  const tipElement=()=>{
    if(cmTip===null){cmTip=make('div');cmTip.className='cm-tip';cmTip.hidden=true;document.body.append(cmTip)}
    return cmTip;
  };

  // ── каркас: стек слоёв + панель активного слоя ────────────────────────
  async function render(){registry=await api('identities');
    await renderLayers($('quasar-http'));}
  async function renderLayers(root){
    root.replaceChildren();
    if(!overview)overview=await api('quasar_platforms');
    if(!selectedPlatform)selectedPlatform=(overview.platforms[0]||{}).platform||'';
    const key=layer==='base'?'base:'+basePlatform:layer==='common'?'common'
      :layer==='devices'?'devices':'platform:'+selectedPlatform;
    if(layer!=='devices'&&(key!==layerKey||workingFile===null)){
      saveErr='';
      if(layer==='base'){
        const data=await api('quasar_platforms?builtin='+encodeURIComponent(basePlatform));
        workingFile=data.config;
      }else if(layer==='common'){
        workingFile=JSON.parse(JSON.stringify(overview.common));
      }else if(selectedPlatform){
        workingFile=await api('quasar_platforms?platform='+encodeURIComponent(selectedPlatform));
      }else workingFile=null;
      layerKey=key;changes=0;selectedBranch='';branchFilter='';
    }
    const wrap=make('div');wrap.className='quasar-layers';
    wrap.append(layerStack());
    if(layer==='devices')wrap.append(await devicesPanel());
    else wrap.append(await layerPanel());
    root.append(wrap);
  }

  function layerStack(){
    const stack=make('div');stack.className='quasar-stack';
    const counts=overview.builtin_counts||{};
    const button=(title,short,small,active,onclick)=>{
      const b=make('button');b.type='button';
      const full=make('b',title);full.className='full';
      const sh=make('span',short);sh.className='short';
      b.append(full,sh);
      if(small)b.append(make('small',small));
      if(active)b.classList.add('active');
      b.onclick=()=>work(onclick);
      stack.append(b);return b};
    button('Встроенная база','База',
      'ключей '+(counts[basePlatform]??'—')+' · '+(basePlatform||'общий дефолт'),
      layer==='base',async()=>switchLayer('base'));
    button('Общий','Общий',
      'ключей '+overview.common_keys+' · настроек '+overview.common_inline,
      layer==='common',async()=>switchLayer('common'));
    const meta=overview.platforms.find(p=>p.platform===selectedPlatform);
    button('Платформа',selectedPlatform||'Платформа',
      (selectedPlatform?selectedPlatform+' · ':'')+(selectedPlatform?'ключей '+(meta?meta.keys:0):'нет файлов'),
      layer==='platform',async()=>switchLayer('platform'));
    button('Под устройство','Колонки','устройств '+registry.devices.length,
      layer==='devices',async()=>switchLayer('devices'));
    return stack;
  }

  // переход между слоями: слой устройств — потерять правки «Своего ответа»,
  // конфиг-слои — «Сохранить / Не сохранять / Отмена»
  async function switchLayer(next){
    if(next===layer)return;
    if(layer==='devices'){
      if(dirty&&!await confirmDiscard('В редакторе «Свой ответ» есть несохранённые изменения. Переключить и потерять их?'))return;
      dirty=false;
    }else if(!await guardLayerSwitch())return;
    layer=next;selectedBranch='';work(render);
  }

  // переход между слоями/платформами: несохранённое — «Сохранить / Не сохранять / Отмена»
  async function guardLayerSwitch(){
    if(!changes)return true;
    const act=await leaveLayers();
    if(act==='cancel')return false;
    if(act==='save'){const ok=await saveLayer();if(!ok)return false}
    if(act==='discard'){layerKey='';workingFile=null}  // правки теряются — тело перечитать
    changes=0;saveErr='';return true;
  }
  async function switchLayer(next){
    if(next===layer)return;
    if(!await guardLayerSwitch())return;
    layer=next;selectedBranch='';work(render);
  }
  async function switchPlatform(name){
    if(name===selectedPlatform)return;
    if(!await guardLayerSwitch())return;
    selectedPlatform=name;selectedBranch='';work(render);
  }
  async function switchBasePlatform(name){ // база только для чтения — без сохранений
    basePlatform=name;selectedBranch='';work(render);
  }

  async function saveLayer(){
    try{
      if(layer==='common')await put('quasar_platforms',{common_file:workingFile});
      else await put('quasar_platforms',{platform_file:{platform:selectedPlatform,patch:workingFile}});
      changes=0;saveErr='';overview=null;notify('Слой сохранён');return true;
    }catch(e){saveErr=e.message;refreshSavebar();return false}
  }
  function fillSavebar(bar){
    bar.replaceChildren();
    bar.append(make('span',changes+' '+ruPlural(changes,['изменение','изменения','изменений'])));
    const undo=make('button','Отменить');undo.type='button';undo.className='secondary';
    undo.onclick=()=>work(async()=>{changes=0;saveErr='';layerKey='';work(render)});
    const save=make('button','Сохранить');save.type='button';
    save.onclick=()=>work(async()=>{const ok=await saveLayer();if(ok)work(render)});
    const err=make('span');err.className='sc-err';err.textContent=saveErr;err.hidden=!saveErr;
    bar.append(undo,save,err);
    bar.hidden=!changes&&!saveErr;
  }
  function refreshSavebar(){const bar=$('quasar-savebar');if(bar)fillSavebar(bar)}
  function markChange(){changes++;refreshSavebar()}

  async function layerPanel(){
    const panel=make('div');panel.className='quasar-panel-layer';
    const head=make('div');head.className='layer-head';
    head.append(make('h3',{base:'Встроенная база',common:'Общий',platform:'Платформа'}[layer]));
    let composeName=layer==='base'?basePlatform:selectedPlatform;
    if(layer==='base'){
      const select=make('select');select.className='layer-select';
      select.title='Платформа встроенной базы: база собирается из общего дефолта плюс квирк платформы';
      select.append(new Option('общий дефолт',''));
      for(const name of overview.builtin_platforms||[])select.append(new Option(name,name));
      select.value=basePlatform;
      select.onchange=()=>switchBasePlatform(select.value);
      const lock=make('span','только чтение');lock.className='quasar-lock';
      head.append(select,lock);
    }
    if(layer==='platform'){
      const select=make('select');select.className='layer-select';select.title='Платформа-патч';
      if(!selectedPlatform)select.append(new Option('— платформа не выбрана —',''));
      for(const meta of overview.platforms)
        select.append(new Option(meta.platform+(meta.default?' · по умолчанию':''),meta.platform));
      if(selectedPlatform)select.value=selectedPlatform;
      select.onchange=()=>switchPlatform(select.value);
      const menu=make('button','⋯');menu.type='button';menu.className='icon-btn';
      menu.title='Платформы: добавить, переименовать, удалить';
      menu.onclick=()=>platformMenu();
      head.append(select,menu);
    }
    const composeB=make('button',composeName?'Итог для '+composeName:'Итог (без платформы)');
    composeB.type='button';composeB.className='secondary';
    composeB.title='Собранный конфиг без устройства: встроенная база → общий → патч платформы';
    composeB.onclick=()=>work(()=>composeDialog(composeName));
    head.append(composeB);
    panel.append(head);
    const hint=make('p');hint.className='hint';
    hint.textContent=readOnlyLayer()
      ?'Только чтение. Правки — в «Общий» и «Платформа».'
      :layer==='common'
        ?'Действует на все колонки поверх встроенной базы.'
        :'Только отличия от общего; null удаляет ключ.';
    panel.append(hint);
    if(!workingFile){
      panel.append(make('p','Платформ ещё нет — добавьте через меню «⋯» рядом с выбором платформы.'));
      return panel;
    }
    const ro=readOnlyLayer();
    if(!ro)panel.append(settingsSection(workingFile,ro));
    panel.append(jsonSection(workingFile,ro));
    if(!ro){const bar=make('div');bar.className='layer-savebar';bar.id='quasar-savebar';fillSavebar(bar);panel.append(bar)}
    return panel;
  }

  // «Настройки»: инлайн-узлы слоя строками «подпись — контрол справа»
  function settingsSection(file,ro){
    const box=make('div');
    box.append(make('h4','Настройки'));
    const found=[];collectInline(file,found,'');
    if(!found.length){const p=make('p','Инлайн-узлов ($-ключей) в слое нет — добавить можно в JSON слоя.');p.className='hint';box.append(p);return box}
    const rows=make('div');rows.className='set-rows';
    for(const {node,kind,path} of found)rows.append(settingRow(node,kind,path,ro));
    box.append(rows);
    return box;
  }
  function settingRow(node,kind,path,ro){
    const row=make('div');row.className='set-row';
    row.append(make('span',node.$name||path));
    row.title=(node.$hint?node.$hint+' · ':'')+path;
    const commit=fn=>{fn();markChange()};
    if(kind==='$bool'){
      const sw=make('label');sw.className='ui-switch';
      const cb=make('input');cb.type='checkbox';cb.checked=!!node.$bool;cb.disabled=ro;
      cb.onchange=()=>commit(()=>{node.$bool=cb.checked});
      sw.append(cb,make('i'));row.append(sw);
    }else if(kind==='$select'){
      const select=make('select');select.className='set-ctl';select.disabled=ro;
      for(const key of Object.keys(node)){
        if(key.startsWith('$'))continue;
        const o=make('option',key);o.value=key;
        if(node.$select===key)o.selected=true;select.append(o)}
      select.onchange=()=>commit(()=>{node.$select=select.value});
      row.append(select);
    }else if(kind==='$param'){
      const btn=make('button',node.$param||'— задать параметр —');btn.type='button';
      btn.className='set-param';btn.disabled=ro;btn.title='Параметр узла: '+path;
      btn.onclick=()=>paramDialog(node,path,btn);
      row.append(btn);
    }else{ // $string | $number | $list
      const input=make('input');
      input.type=kind==='$number'?'number':'text';input.className='set-ctl';input.disabled=ro;
      input.value=kind==='$list'?(Array.isArray(node.$list)?node.$list.join(', '):''):node[kind]??'';
      input.onchange=()=>commit(()=>{node[kind]=kind==='$list'
        ?input.value.split(',').map(x=>x.trim()).filter(Boolean)
        :(kind==='$number'?Number(input.value):input.value)});
      row.append(input);
    }
    return row;
  }
  // ключ $param: выбор из известных путей или ввод — в <dialog>
  function paramDialog(node,path,btn){
    const dlg=document.createElement('dialog');dlg.className='catalog-dialog';
    dlg.setAttribute('aria-label','Параметр узла');
    const form=document.createElement('form');dlg.append(form);document.body.append(dlg);
    form.append(make('h3','Параметр узла'));
    const p=make('p','Ключ из каталога параметров (раздел «Параметры») или путь реестра: device.sn, user.name…');
    p.className='hint';form.append(p);
    const input=make('input');input.value=node.$param||'';input.spellcheck=false;
    const list=make('datalist');list.id='quasar-param-list';
    for(const name of ['device.sn','device.id','device.name','device.room','device.platform',
      'user.id','user.name','session.sn'])list.append(new Option(name));
    input.setAttribute('list',list.id);
    form.append(list,input);
    const row=make('div');row.className='row';
    const cancel=make('button','Отмена');cancel.type='button';cancel.className='secondary';cancel.onclick=()=>dlg.close();
    const save=make('button','Сохранить');save.type='submit';
    row.append(cancel,save);form.append(row);
    form.onsubmit=e=>{e.preventDefault();node.$param=input.value.trim();
      btn.textContent=node.$param||'— задать параметр —';
      dlg.close();dlg.remove();markChange()};
    dlg.addEventListener('close',()=>dlg.remove());
    dlg.showModal();input.focus();
  }

  // «JSON слоя»: поиск по путям + дерево + редактор ветви (справа на широком,
  // под деревом на узком); у базы всё только для чтения
  function jsonSection(file,ro){
    const det=make('details');det.className='layer-json';det.open=jsonOpen;
    det.addEventListener('toggle',()=>{jsonOpen=det.open});
    det.append(make('summary','JSON слоя'));
    const body=make('div');
    const filter=make('input');filter.type='search';filter.className='json-filter';
    filter.placeholder='найти ключ (путь)';filter.value=branchFilter;filter.spellcheck=false;
    const split=make('div');split.className='quasar-json';
    const tree=make('div');tree.className='quasar-tree';
    const editor=make('div');editor.className='quasar-editor';
    const branchLabel=make('p','Ветвь: корень файла');branchLabel.className='hint';
    const edHolder=make('div');
    const ed=makeCM(edHolder,{readOnly:ro,height:'420px'});
    split.append(tree,editor);body.append(filter,split);
    if(!ro){
      const applyB=make('button','Сохранить ветвь');applyB.type='button';
      applyB.onclick=()=>work(async()=>{
        let value;try{value=JSON.parse(ed.getValue())}catch{throw Error('Ветвь: некорректный JSON')}
        writeBranch(file,selectedBranch,value);
        markChange();jsonOpen=true;work(render);  // настройки и дерево — по новому телу
      });
      const chipsDet=make('details');chipsDet.className='layer-chips';
      chipsDet.append(make('summary','Вставки'));
      const chips=make('div');chips.className='chips-below';
      const chipTitle=make('span','Вставить в позицию курсора:');chips.append(chipTitle);
      for(const [text,title] of CHIP_HINTS){
        const chip=make('code',text);chip.title=title;chip.onclick=()=>ed.insert(text);chips.append(chip)}
      chipsDet.append(chips);
      editor.append(branchLabel,edHolder,applyB,chipsDet);
    }else editor.append(branchLabel,edHolder);
    filter.oninput=()=>{branchFilter=filter.value.trim().toLowerCase();draw()};
    const visible=()=>visiblePathsFor(file,branchFilter);
    function draw(){
      const vis=visible();
      const rows={};
      tree.replaceChildren();
      const select=(node,path)=>{
        selectedBranch=path;
        ed.setValue(JSON.stringify(node,null,2));
        branchLabel.textContent='Ветвь: '+(path||'корень файла');
        tree.querySelectorAll('.quasar-branch.selected').forEach(x=>x.classList.remove('selected'));
        if(rows[path])rows[path].classList.add('selected');
        setTimeout(()=>ed.refresh(),0);
      };
      const buildNode=(host,node,path,label,depth)=>{
        if(vis&&!vis.has(path))return;
        const row=make('div');row.className='quasar-branch'+(path===selectedBranch?' selected':'');
        row.style.paddingLeft=(depth*12)+'px';
        const kind=inlineKindOf(node);
        if(kind){ // шаблон: содержимое не раскрываем — целиком в редактор
          const btn=make('button','Шаблон');btn.type='button';btn.className='tpl-btn';
          row.append(btn,make('span',(node.$name||label)+(kind==='$select'&&node.$select?' · '+node.$select:'')));
          row.title=(node.$hint||'инлайн-узел '+kind)+' — '+path;
          row.onclick=()=>select(node,path);
          host.append(row);rows[path]=row;return;
        }
        const isObj=node!==null&&typeof node==='object';
        const entries=isObj?(Array.isArray(node)?node.map((v,i)=>[String(i),v]):Object.entries(node)):[];
        if(!entries.length){ // пустой объект/массив или скаляр
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
        const folded=vis?false:(foldState.has(path)?foldState.get(path):depth>=2);
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
      const existing=getByPath(file,selectedBranch);
      select(existing===undefined?file:(existing===null?null:existing),existing===undefined?'':selectedBranch);
    }
    draw();
    det.append(body);
    return det;
  }
  const CHIP_HINTS=[
    ['{{now_iso}}','время ответа'],['{{device_id}}','DeviceId'],['{{device_name}}','имя из реестра'],
    ['{{user.name}}','имя владельца'],['{{device.room}}','комната'],['{"db":"user.location"}','поле из БД эмулятора'],
    ['{"run":"script.bat"}','запуск скрипта (state/quasar/scripts)'],
    ['{"$select":"Вкл","Вкл":{…},"Выкл":{…},"$name":"Подпись"}','инлайн-селектор'],
    ['{"$bool":true,"$name":"Флаг"}','вкл/выкл'],['{"$string":"{{device_name}}"}','строка с токеном'],
    ['{"$param":"user.name"}','параметр из каталога']];
  function writeBranch(file,path,value){
    if(!path){for(const key of Object.keys(file))delete file[key];Object.assign(file,value);return}
    const parts=path.split('.');
    let node=file;
    for(const part of parts.slice(0,-1)){
      let child=Array.isArray(node)?node[Number(part)]:node[part];
      if(!child||typeof child!=='object'){child={};if(Array.isArray(node))node[Number(part)]=child;else node[part]=child}
      node=child;
    }
    const last=parts[parts.length-1];
    if(Array.isArray(node))node[Number(last)]=value;else node[last]=value;
  }
  // пути, видимые при фильтре «найти ключ»: совпавшие + все их предки (null — без фильтра)
  function visiblePathsFor(file,filter){
    if(!filter)return null;
    const found=new Set();
    const walk=(node,path)=>{
      if(path&&path.toLowerCase().includes(filter))found.add(path);
      if(inlineKindOf(node))return;
      if(Array.isArray(node))node.forEach((v,i)=>walk(v,path?path+'.'+i:String(i)));
      else if(node&&typeof node==='object')
        for(const [key,child] of Object.entries(node))walk(child,path?path+'.'+key:key);
    };
    walk(file,'');
    const visible=new Set(['']);
    for(const path of found){
      visible.add(path);
      const parts=path.split('.');
      for(let i=1;i<parts.length;i++)visible.add(parts.slice(0,i).join('.'));
    }
    return visible;
  }

  // меню «⋯»: платформы — добавить / переименовать / удалить (все через <dialog>)
  function platformMenu(){
    const actions=[{label:'Добавить платформу',run:addPlatformDialog}];
    if(selectedPlatform){
      actions.push({label:'Переименовать платформу',run:()=>renamePlatformDialog(selectedPlatform)});
      actions.push({label:'Удалить платформу',run:()=>deletePlatformDialog(selectedPlatform)});
    }
    const dlg=document.createElement('dialog');dlg.className='catalog-dialog';
    dlg.setAttribute('aria-label','Платформы');
    const form=document.createElement('form');dlg.append(form);document.body.append(dlg);
    form.append(make('h3','Платформы'));
    const list=make('div');list.className='list';
    for(const a of actions){
      const b=make('button',a.label);b.type='button';
      b.onclick=()=>{dlg.close();dlg.remove();work(a.run)};
      list.append(b);
    }
    form.append(list);
    const row=make('div');row.className='row';
    const cancel=make('button','Отмена');cancel.type='button';cancel.className='secondary';cancel.onclick=()=>dlg.close();
    row.append(cancel);form.append(row);
    dlg.showModal();
    dlg.addEventListener('close',()=>dlg.remove());
  }
  async function addPlatformDialog(){
    if(!await guardLayerSwitch())return;
    const name=await askText('Новая платформа',
      'Идентификатор платформы: латиница, цифры, точка, подчёркивание, дефис (например station_max).',
      'station_max');
    if(!name)return;
    await put('quasar_platforms',{platform_file:{platform:name,patch:{}}});
    overview=null;selectedPlatform=name;layerKey='';changes=0;
    notify('Платформа добавлена: '+name);await render();
  }
  async function renamePlatformDialog(oldName){
    if(!await guardLayerSwitch())return;
    const name=await askText('Переименовать платформу','Новое имя платформы '+oldName+'.','новое-имя',oldName);
    if(!name||name===oldName)return;
    const patch=await api('quasar_platforms?platform='+encodeURIComponent(oldName));
    await put('quasar_platforms',{platform_file:{platform:name,patch}});
    await del('quasar_platforms?platform='+encodeURIComponent(oldName));
    overview=null;selectedPlatform=name;layerKey='';changes=0;
    notify('Платформа переименована: '+oldName+' → '+name);await render();
  }
  async function deletePlatformDialog(name){
    if(!await guardLayerSwitch())return;
    const ok=await askDialog('Удалить платформу '+name+'? Патч платформы будет удалён безвозвратно.',
      [{label:'Отмена',className:'secondary',value:false},{label:'Удалить',className:'danger',value:true}],
      'Удаление платформы');
    if(!ok)return;
    await del('quasar_platforms?platform='+encodeURIComponent(name));
    overview=null;selectedPlatform='';layerKey='';changes=0;
    notify('Платформа удалена: '+name);await render();
  }

  // «Итог для <платформа>»: собранный конфиг без устройства (база → общий → патч),
  // read-only CodeMirror с подсказкой происхождения значений
  async function composeDialog(platformName){
    const data=await api('quasar_platforms?compose='+encodeURIComponent(platformName||''));
    const dlg=document.createElement('dialog');dlg.className='catalog-dialog compose-dialog';
    const head=make('div');head.className='dialog-head';
    head.append(make('h3',platformName?'Итог для '+platformName:'Итог без платформы'));
    const closeB=make('button','✕');closeB.type='button';closeB.className='icon-btn';
    closeB.setAttribute('aria-label','Закрыть');closeB.onclick=()=>dlg.close();
    head.append(closeB);dlg.append(head);
    const hint=make('p','Собранный конфиг без устройства: встроенная база → общий → патч платформы. '
      +'Инлайн-узлы разрешены по выбранным вариантам; {{токены}} и маркеры (db/run) остаются как есть. '
      +'Наведение на строку — происхождение значения.');
    hint.className='hint';dlg.append(hint);
    const holder=make('div');
    const value=data.config&&typeof data.config==='object'?data.config:{};
    const lines=formatJSON(value);
    makeCM(holder,{value:lines.map(l=>l.text).join('\n'),readOnly:true,height:'540px'});
    dlg.append(holder);
    document.body.append(dlg);
    dlg.showModal();
    dlg.addEventListener('close',()=>dlg.remove());
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
      tip.textContent=(path||'корень')+'\n→ '+originFor(path,data.origins||{},'встроенная база (quasar_defaults)');
      tip.style.left=Math.min(e.clientX+14,window.innerWidth-360)+'px';
      tip.style.top=(e.clientY+18)+'px';
      tip.hidden=false;
    });
    wrapper.addEventListener('mouseleave',()=>{tip.hidden=true});
  }

  // ── Слой «Под устройство»: переопределения в панели справа ───────────
  async function devicesPanel(){
    const root=make('div');root.className='quasar-panel-layer';
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
      card.onclick=()=>work(async()=>{if(await confirmDiscard('Есть несохранённые изменения. Переключить и потерять их?')){selectedSn=device.sn;work(render)}});
      list.append(card);
    }
    const panel=make('div');panel.style.flex='1';
    const device=registry.devices.find(d=>d.sn===selectedSn);
    if(!device)panel.append(make('p','Выберите устройство слева.'));
    else await renderOverrides(panel,device);
    split.append(list,panel);root.append(split);
    return root;
  }

  let selectedPlatformBody=null;
  // Источник конфига устройства (TASK-052): Генерируемый · Напрямую · Свой.
  // Переключатель в строке заголовка (на 400px переносится под него), смена
  // сохраняется сразу и в маршруте «Прозрачно» (там применяется только «Напрямую» с галочкой, TASK-065).
  const SOURCE_LABEL={generated:'Генерируемый',direct:'Напрямую',custom:'Свой'};
  const SOURCE_HINT={
    generated:'Колонка получает конфиг, собранный эмулятором: встроенные дефолты → ваш общий конфиг → платформа → переопределения → инлайн-узлы. Ниже — переопределения и итоговая сборка.',
    direct:'Колонка получает ответ настоящего сервера. Ниже — последний полученный ответ, только чтение. Переопределения сохраняются, но не применяются.',
    custom:'Колонка получает сохранённый ответ как есть: без слоёв и инлайн-узлов. Заполните редактор вручную, генерацией или загрузкой с оригинального сервера и сохраните.'};
  async function renderOverrides(root,device){
    root.replaceChildren();
    let source;
    try{source=await api('quasar_source?sn='+encodeURIComponent(device.sn))}
    catch(e){const pe=make('p','Ошибка загрузки: '+e.message);pe.className='hint';root.append(pe);return}
    const routed=device.route_mode||'emulate';
    const locked=routed==='proxy';
    const headRow=make('div');headRow.className='src-head';
    headRow.append(make('h3','Переопределения · '+device.sn));
    // push: принудительный перезапрос конфига (push_update_config) в живую сессию —
    // колонка сделает get_sync_info, правила замен применятся на свежем ответе
    const pushBtn=make('button','Перезапросить');pushBtn.type='button';pushBtn.className='secondary';
    pushBtn.title='Принудительный перезапрос конфига: колонка сделает get_sync_info';
    pushBtn.onclick=()=>work(async()=>{
      pushBtn.disabled=true;
      try{
        await api('quasar_source',{sn:device.sn,action:'push'});
        notify('Перезапрос конфига отправлен.');
      }catch(e){notify(e.message,{error:true})}
      finally{pushBtn.disabled=false}
    });
    headRow.append(pushBtn);
    if(locked)headRow.append(make('span','маршрут: Прозрачно','chip'));
    const seg=make('div');seg.className='segmented';
    for(const [value,label] of Object.entries(SOURCE_LABEL)){
      const b=make('button',label);b.type='button';
      if(source.mode===value)b.classList.add('active');
      b.onclick=()=>work(async()=>{await put('quasar_source',{sn:device.sn,mode:value});await render()});
      seg.append(b);
    }
    headRow.append(seg);
    root.append(headRow);
    const hint=make('p');hint.className='hint';
    if(locked)hint.textContent='Маршрут устройства «Прозрачно»: все запросы уходят на настоящий сервер. '
      +(source.mode==='direct'
        ?'Ответ get_sync_info правится заменами, только пока включена галочка «И в прозрачном маршруте»; без неё замены в этом маршруте не применяются.'
        :'Генерируемый и свой ответ в этом маршруте не применяются; чтобы править ответ заменами, выберите «Напрямую».');
    else if(source.mode==='generated'&&source.builtin_base)
      hint.textContent='Генерируемый (встроенный): конфиг собирается из встроенных дефолтов платформы — переопределения и инлайн-узлы поверх. Ниже — переопределения и итоговая сборка.';
    else hint.textContent=SOURCE_HINT[source.mode];
    root.append(hint);
    if(locked&&source.mode!=='direct')return;
    if(source.mode==='direct')return renderDirect(root,device,source);
    if(source.mode==='custom')return renderCustom(root,device,source);
    return renderGenerated(root,device);
  }

  // «Напрямую»: кнопка «Замены» (regexp в ответе upstream) и последний ответ, только чтение
  function renderDirect(root,device,source){
    const rules=source.replace||[];
    const enabled=rules.filter(r=>r.enabled).length;
    const bar=make('div');bar.className='sc-toolbar';
    const replaceB=make('button','Замены'+(enabled?' · '+enabled:''));replaceB.type='button';replaceB.className='secondary';
    replaceB.onclick=()=>replaceDialog(device,rules);
    const transparent=make('label');transparent.className='check';
    const flag=make('input');flag.type='checkbox';flag.checked=source.transparent===true;
    flag.onchange=()=>work(async()=>{
      try{await put('quasar_source',{sn:device.sn,transparent:flag.checked})}
      catch(e){flag.checked=!flag.checked;throw e}
      notify(flag.checked?'Замены применяются и в маршруте «Прозрачно»':'В маршруте «Прозрачно» замены не применяются');
    });
    transparent.append(flag,document.createTextNode('И в прозрачном маршруте'));
    bar.append(replaceB,transparent);root.append(bar);
    root.append(make('h3','Последний ответ'));
    const last=source.direct_last;
    if(!last||!last.body){
      const p=make('p','Ответов ещё не было: колонка опросит конфиг сама, ответ появится здесь.');
      p.className='hint';root.append(p);return;
    }
    const info=make('p','Получен: '+new Date(last.ts*1000).toLocaleString('ru-RU')+' · статус '+last.status
      +(last.body_sent?' · замен сработало: '+last.replaced+(last.preview?' (превью)':''):'')
      +(last.replace_error?' · ошибка замен: '+last.replace_error:''));
    info.className='hint';root.append(info);
    const holder=make('div');holder.className='panel';
    const ed=makeCM(holder,{value:JSON.stringify(last.body_sent||last.body,null,2),readOnly:true,height:'520px'});
    if(last.body_sent){
      const seg=make('div');seg.className='segmented';seg.style.margin='0 0 8px';
      for(const [label,value] of [['После замен'+(last.preview?' (превью)':''),last.body_sent],['До замен',last.body]]){
        const b=make('button',label);b.type='button';
        if(value===last.body_sent)b.classList.add('active');
        b.onclick=()=>{seg.querySelectorAll('button').forEach(x=>x.classList.toggle('active',x===b));
          ed.setValue(JSON.stringify(value,null,2));setTimeout(()=>ed.refresh(),0)};
        seg.append(b);
      }
      root.append(seg);
    }
    root.append(holder);
    setTimeout(()=>[...holder.querySelectorAll('.CodeMirror')].forEach(el=>el.CodeMirror.refresh()),50);
  }

  // Окно «Замены»: правила regexp устройства (вкл/выкл, pattern, replace); ошибка сервера «Строка N: …»
  // показывается у своей строки
  function replaceDialog(device,rules){
    const dlg=document.createElement('dialog');dlg.className='catalog-dialog replace-dialog';
    const form=document.createElement('form');dlg.append(form);document.body.append(dlg);
    const head=make('h3','Замены в ответе · '+device.sn);
    const hint=make('p','Python regex, правила применяются сверху вниз к тексту ответа перед отправкой колонке; группы — \\1, \\g<name>. Обычные символы . ? [ ( экранируются обратной косой чертой. Ответ — компактный JSON, без пробелов: «subscription» матчится, «subscription»: — нет. Правила при сохранении сразу прогоняются по последнему оригинальному ответу — «После замен» обновится без запроса колонки.');
    hint.className='hint';
    const list=make('div');list.className='rp-list';
    const generalErr=make('p');generalErr.className='sc-err';generalErr.hidden=true;
    const addRow=(rule={enabled:true,pattern:'',replace:''})=>{
      const row=make('div');row.className='rp-row';
      const on=make('input');on.type='checkbox';on.checked=rule.enabled!==false;on.title='Включено';on.className='rp-on';
      const pattern=make('input');pattern.type='text';pattern.className='code rp-pattern';pattern.value=rule.pattern||'';
      pattern.placeholder='pattern';pattern.spellcheck=false;
      const replace=make('input');replace.type='text';replace.className='code rp-replace';replace.value=rule.replace||'';
      replace.placeholder='replace';replace.spellcheck=false;
      const del=make('button','✕');del.type='button';del.className='icon-btn rp-del';del.title='Удалить правило';
      del.onclick=()=>row.remove();
      const err=make('p');err.className='sc-err rp-err';err.hidden=true;
      for(const el of [pattern,replace])el.oninput=()=>{err.hidden=true};
      row.append(on,pattern,replace,del,err);list.append(row);return row;
    };
    for(const rule of rules)addRow(rule);
    const add=make('button','＋ Добавить правило');add.type='button';add.className='secondary';
    add.onclick=()=>{const row=addRow();row.querySelector('.rp-pattern').focus()};
    const save=make('button','Сохранить');save.type='submit';
    const cancel=make('button','Отмена');cancel.type='button';cancel.className='secondary';
    cancel.onclick=()=>{dlg.close()};
    const actions=make('div');actions.className='row';actions.append(cancel,save);
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
    form.append(head,hint,list,add,generalErr,actions);
    dlg.addEventListener('close',()=>dlg.remove());
    dlg.showModal();
    if(!rules.length)add.click();
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
    fetchB.title=source.can_fetch?'Заполнить редактор ответом оригинального сервера на последний опрос этой колонки':'Колонка ещё не запрашивала конфиг после запуска сервера';
    const errLine=make('p');errLine.className='sc-err';errLine.hidden=true;
    const act=fn=>async()=>{errLine.hidden=true;try{await fn()}catch(e){errLine.textContent=e.message;errLine.hidden=false}};
    const saveJSON=async value=>{
      let parsed;try{parsed=JSON.parse(value||'null')}catch{throw Error('Ответ: корректный JSON')}
      if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw Error('Ответ: JSON-объект с объектом config');
      if(!parsed.config||typeof parsed.config!=='object')throw Error('Ответ: нужен объект config (пока его нет, колонка ответит 409 quasar_custom_not_set)');
      await put('quasar_source',{sn:device.sn,custom:parsed});
      dirty=false;notify('Свой ответ сохранён — колонка получит его при следующем опросе.');
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
        notify('Переопределение удалено');await render()});
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
      notify('Переопределение сохранено: '+path);await render()})};
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

  window.addEventListener('hashchange',()=>{if(['#quasar-http'].includes(location.hash))work(render)});
  work(render);
})();
