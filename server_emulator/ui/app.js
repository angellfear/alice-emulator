const embedded=new URLSearchParams(location.search).get('embedded')==='1';document.body.classList.toggle('embedded',embedded);
const $=id=>document.getElementById(id);let settings,objectUrl,standalone=false;
// Сообщения — тосты (toast.js): стек справа сверху, автоскрытие; errors — {error:true}
const message=(text,error=false)=>{notify(text,{error})};
async function api(action,method='GET',value){const r=await fetch('api/'+action,{method,headers:{'Content-Type':'application/json','X-Emulator':'1'},body:value===undefined?undefined:JSON.stringify(value)});const result=await readJson(r,action);if(!r.ok)throw Error(result.error||r.statusText);return result}
// Ответ не JSON: 404 от aiohttp («404: Not Found») — работающий сервер старее UI с диска (UI
// читается с диска, Python-код — только при запуске), поэтому вместо ошибки разбора — подсказка.
async function readJson(r,action){const text=await r.text();try{return text?JSON.parse(text):{}}catch(e){if(r.status===404)throw Error('Сервер не знает этот адрес (api/'+action.split('?')[0]+') — перезапустите сервер: интерфейс уже обновлён, а сервер работает со старым кодом.');throw Error('Ответ сервера не JSON ('+r.status+'): '+text.slice(0,120))}}
async function work(fn){const buttons=[...document.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);try{await fn()}catch(e){message(e.message,true)}finally{buttons.forEach(b=>b.disabled=false)}}
const fieldSpec={};
const labels={url:'Полный URL операции',model:'Модель',api_key:'API-ключ',temperature:'Температура',language:'Язык',voice:'Голос',response_format:'Формат аудио',stub_text:'Ответ заглушки (пусто — использовать AI)'};
function fields(){for(const [kind,keys] of Object.entries(fieldSpec)){const root=$(kind+'-fields');root.replaceChildren();for(const key of keys){const label=document.createElement('label');label.textContent=labels[key];const input=document.createElement('input');input.id=kind+'-'+key;input.type=key==='api_key'?'password':key==='temperature'?'number':'text';if(key==='temperature'){input.min='0';input.max='2';input.step='0.1'}input.value=settings[kind][key]??'';label.append(input);root.append(label)}}}
function setField(id,prop,value){const e=$(id);if(e)e[prop]=value}function readField(id,prop,fallback){const e=$(id);return e?e[prop]:fallback}function renderSettings(){fields();setField('system-prompt','value',settings.system_prompt);setField('device-context','value',JSON.stringify(settings.device_context,null,2));setField('aapi-model','checked',settings.aapi_model);setField('model-actions','checked',!!settings.model_actions);setField('aapi-voice','checked',!!settings.aapi_voice);setField('aapi-tts','checked',!!settings.aapi_tts);$('mcp-servers').value=JSON.stringify(settings.mcp_servers,null,2);$('tool-list').value=JSON.stringify(settings.tools,null,2)}
async function saveSettings(){const next=structuredClone(settings);next.system_prompt=readField('system-prompt','value',settings.system_prompt);next.device_context=$('device-context')?JSON.parse($('device-context').value):settings.device_context;next.aapi_model=readField('aapi-model','checked',settings.aapi_model);next.model_actions=readField('model-actions','checked',settings.model_actions);next.aapi_voice=readField('aapi-voice','checked',settings.aapi_voice);next.aapi_tts=readField('aapi-tts','checked',settings.aapi_tts);next.mcp_servers=JSON.parse($('mcp-servers').value);next.tools=JSON.parse($('tool-list').value);for(const [kind,keys] of Object.entries(fieldSpec))for(const key of keys)next[kind][key]=key==='temperature'?Number($(kind+'-'+key).value):$(kind+'-'+key).value;settings=await api('settings','PUT',next);message('Настройки сохранены и применены.');renderSettings()}
// Разделы по областям (вариант A): группа → подвкладки; min — с какого режима «Интеллект» виден пункт.
// Хеш — id подвкладки (#scenarios, #mcp…); у секции тот же id, поэтому переход — goTab (pushState
// + hashchange), а не location.hash: иначе браузер прокручивает страницу к секции как к якорю.
const LEVELS=['scenarios','model','agent'];
// Режим — сегменты справа в полосе групп; подсказка — на их title, в «Простом» она же —
// содержимое группы «Интеллект» (подвкладок нет).
const LEVEL_HINT={scenarios:'Фраза без сценария — стандартный ответ из пакета озвучки, модель не вызывается. TTS не обязателен.',
  model:'Фраза без сценария — один вызов LLM: промпт и история, без инструментов и памяти.',
  agent:'Фраза без сценария — AI агент: MCP-инструменты, память, задания по расписанию.'};
// Режимы расширений (TASK-034): список — /api/intelligence; сегменты — справа от встроенных
// («Простой · Модель · AI агент»), подвкладки активного режима — в группе «Интеллект»
// (вкладка-заглушка #ext-mode: настройки режима переедут в «Расширения» в TASK-035).
let extModes=[];
const levelValue=()=>settings&&typeof settings.intelligence==='string'?settings.intelligence:'agent';
const extMode=()=>levelValue().startsWith('ext:')?extModes.find(m=>'ext:'+m.id===levelValue()):null;
const GROUPS=[
  {title:'Устройства',tabs:[['devices','Устройства'],['accounts','Пользователи'],['updates','Обновления']]},
  {title:'Эмулятор',tabs:[['scenarios','Сценарии'],['endpoints','HTTP'],['ws','WS'],['quasar-http','Quasar'],['params','Параметры'],['app-catalog','Каталог приложений'],['traffic','Журнал']]},
  {title:'Голос',tabs:[['voice-stack','Голосовой стек'],['replies','Звуковой кеш'],['animations','Анимации']]},
  {title:'Интеллект',tabs:[['intelligence','Режим',null,'hidden'],['llm','Модель','model'],['agent','Агент','agent'],['mcp','MCP','agent'],['memory','Память','agent'],['routines','Задания','agent']]},
  {title:'Дополнительно',tabs:[['extensions','Расширения'],['server','Сервер'],['setup','Настройка'],['studio','Проверка']]}];
const level=()=>LEVELS.includes(settings?.intelligence)?settings.intelligence:'agent';
const allowed=([,,min])=>!min||LEVELS.indexOf(level())>=LEVELS.indexOf(min);
const listed=t=>allowed(t)&&t[3]!=='hidden';  // в подвкладках; скрытая — запасная страница группы
// Подвкладки группы с учётом режима расширения: его tabs — кнопками в конец «Интеллекта».
const groupTabs=g=>g.title==='Интеллект'&&extMode()?g.tabs.concat(extMode().tabs.map(t=>['ext-mode',t.title])):g.tabs;
window.goTab=id=>{if(location.hash==='#'+id)return showTab();history.pushState(null,'','#'+id);window.dispatchEvent(new HashChangeEvent('hashchange'))};
const tabButton=(id,title,active)=>{const b=document.createElement('button');b.type='button';b.textContent=title;b.classList.toggle('active',active);b.onclick=()=>goTab(id);return b};
function showTab(){let tab=location.hash.slice(1)||'devices';if(tab==='users')tab='devices';if(tab==='misses')tab='endpoints';  // старый хеш «Без ответа» — в HTTP (TASK-061)
  let group=GROUPS.find(g=>groupTabs(g).some(t=>t[0]===tab));
  if(group&&!allowed(groupTabs(group).find(t=>t[0]===tab)))tab=(groupTabs(group).find(allowed)||['devices'])[0],group=GROUPS.find(g=>groupTabs(g).some(t=>t[0]===tab));
  if(group&&!listed(groupTabs(group).find(t=>t[0]===tab))&&groupTabs(group).some(listed))tab=groupTabs(group).find(listed)[0];
  if(!group&&!['tools','providers'].includes(tab))tab='devices',group=GROUPS[0];
  document.querySelectorAll('main>.tab').forEach(s=>s.hidden=s.id!==tab);
  $('groups').replaceChildren(...GROUPS.map(g=>tabButton(g===group?tab:(groupTabs(g).find(listed)||g.tabs[0])[0],g.title,g===group)));
  const subtabs=group?groupTabs(group).filter(listed):[];
  $('subnav').replaceChildren(...subtabs.map(([id,title])=>tabButton(id,title,id===tab)));
  $('subnav').hidden=!subtabs.length||(subtabs.length<2&&group.title!=='Интеллект');
  renderLevel()}
function renderLevel(){const value=levelValue(),mode=extMode();
  $('intelligence-hint').textContent=mode?'Фраза без сценария — режим расширения «'+mode.title+'».':LEVEL_HINT[level()];
  if($('ext-mode-title'))$('ext-mode-title').textContent=mode?mode.title:'';
  document.querySelectorAll('.segmented [data-level]').forEach(b=>{b.title=LEVEL_HINT[b.dataset.level]||'Режим расширения «'+b.textContent+'»: фраза без сценария уходит расширению.';b.setAttribute('aria-checked',String(b.dataset.level===value))})}
// Сегменты режимов расширений — строятся из /api/intelligence справа от встроенных трёх.
const renderExtSegments=()=>{const bar=document.querySelector('.segmented');if(!bar)return;bar.querySelectorAll('[data-ext]').forEach(b=>b.remove());
  for(const m of extModes){const b=document.createElement('button');b.type='button';b.setAttribute('role','radio');b.dataset.level='ext:'+m.id;b.dataset.ext=m.id;b.textContent=m.title;b.onclick=()=>setLevel('ext:'+m.id);bar.append(b)}};
const setLevel=value=>work(async()=>{const next=structuredClone(settings);next.intelligence=value;settings=await api('settings','PUT',next);renderSettings();showTab();message('Режим сохранён.')});
document.querySelectorAll('.segmented [data-level]').forEach(b=>b.onclick=()=>setLevel(b.dataset.level));
if($('ext-mode-open'))$('ext-mode-open').onclick=()=>goTab('extensions');
// Вкладка «Расширения» (extensions.js): после установки/включения/набора перечитать настройки и режимы, иначе сохранение из этой страницы затрёт серверное состояние
window.setLevel=setLevel;
window.refreshSettings=async()=>{settings=await api('settings');try{extModes=(await api('intelligence')).modes||[]}catch(e){extModes=[]}renderExtSegments();renderSettings();showTab()};
window.addEventListener('hashchange',showTab);showTab();
if(location.hash)requestAnimationFrame(()=>scrollTo(0,0));  // загрузка по ссылке #tab — без прыжка к секции
document.querySelectorAll('.save-settings').forEach(b=>b.onclick=()=>work(saveSettings));
$('discover').onclick=()=>work(async()=>{await saveSettings();for(const s of settings.mcp_servers){const result=await api('mcp-discover','POST',{server_id:s.id});message('Получено инструментов: '+result.count+(result.next_cursor?' (есть следующая страница; пока импортирована первая)':''))}settings=await api('settings');renderSettings()});
async function chat(){const result=await api('chat','POST',{text:$('chat-input').value});$('answer').value=result.text;$('studio-result').textContent=JSON.stringify({usage:result.usage},null,2);const target=$('tool-calls');target.replaceChildren();for(const call of result.tool_calls||[]){const card=document.createElement('div');card.className='tool-call';const pre=document.createElement('pre');pre.textContent=call.function.name+'\n'+call.function.arguments;const button=document.createElement('button');button.textContent=(call.thread_id?'Подтвердить ':'Выполнить инструмент ')+call.function.name;button.onclick=()=>work(async()=>{$('studio-result').textContent=JSON.stringify(await api('tool-call','POST',{name:call.function.name,arguments:JSON.parse(call.function.arguments),call_id:call.id}),null,2)});card.append(pre,button);target.append(card)}return result}
async function transcribe(){const file=$('audio-file').files[0];if(!file)throw Error('Выберите аудиофайл');const r=await fetch('api/stt',{method:'POST',headers:{'X-Emulator':'1','X-Filename':encodeURIComponent(file.name)},body:file});const result=await r.json();if(!r.ok)throw Error(result.error);$('chat-input').value=result.text||'';message('Распознавание завершено');return result}
async function speak(){const r=await fetch('api/tts',{method:'POST',headers:{'X-Emulator':'1','Content-Type':'application/json'},body:JSON.stringify({text:$('answer').value||$('chat-input').value})});if(!r.ok){const e=await r.json();throw Error(e.error)}if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=URL.createObjectURL(await r.blob());$('audio-output').src=objectUrl;$('audio-output').hidden=false;message('Аудио готово к прослушиванию')}
$('chat').onclick=()=>work(chat);$('transcribe').onclick=()=>work(transcribe);$('speak').onclick=()=>work(speak);$('pipeline').onclick=()=>work(async()=>{await transcribe();await chat();await speak()});
work(async()=>{settings=await api('settings');renderSettings();try{extModes=(await api('intelligence')).modes||[]}catch(e){extModes=[]}renderExtSegments();showTab();const status=await api('status');standalone=status.standalone;for(const src of status.ui_extensions||[]){const s=document.createElement('script');s.src=src;document.body.append(s)}refreshUpdate()});

$('device-status').onclick=()=>work(async()=>{const status=await api('status');$('pipeline-errors').textContent=JSON.stringify(status.errors,null,2);const root=$('device-calls');root.replaceChildren();for(const call of status.pending_calls||[]){const card=document.createElement('div');card.className='tool-call';const pre=document.createElement('pre');pre.textContent=call.function.name+'\n'+call.function.arguments;const button=document.createElement('button');button.textContent=(call.thread_id?'Подтвердить ':'Выполнить ')+call.function.name;button.onclick=()=>work(async()=>{const result=await api('tool-call','POST',{name:call.function.name,arguments:JSON.parse(call.function.arguments),call_id:call.id});$('studio-result').textContent=JSON.stringify(result,null,2);card.remove()});card.append(pre,button);if(call.thread_id){const reject=document.createElement('button');reject.className='secondary';reject.textContent='Отклонить';reject.onclick=()=>work(async()=>{const result=await api('tool-call','POST',{name:call.function.name,arguments:{},call_id:call.id,reject:true});$('studio-result').textContent=JSON.stringify(result,null,2);card.remove()});card.append(reject)}root.append(card)}});

// Сценарии без ИИ: плоские группы-каталоги (_group — путь; builtin — каталог встроенных),
// определение — один JSON-файл на сценарий (state/scenarios), имя/описание ru/en, декларативный executor.
const i18n=v=>!v?'':typeof v==='string'?v:(v.ru||v.en||'');
const scenarioTitle=s=>i18n(s.title)||s.name||s.id||'(без id)';
const scenarioDesc=s=>{const d=i18n(s.description);if(d)return d;const t=(s.triggers||[]).map(x=>x&&x.name).filter(Boolean);return t.length?'«'+t.slice(0,3).join('», «')+'»':''};
// Быстрый сценарий v2 (docs/contracts/scenario-json-v2.md): фраза → ответ текстом, раздел «Пользовательские».
const quickScenario=(id,trigger,reply)=>({"format_version":2,"id":"user."+(id||"scenario"),"version":"1.0.0","category":"custom","title":{"ru":trigger||"Сценарий"},"description":{"ru":""},"triggers":[{"kind":"phrase","phrases":[trigger]}],"flow":[{"id":"reply","op":"compose","reply":{"text":reply}},{"id":"done","op":"finish"}]});
const quickFromScenario=s=>{if(!s||s.format_version!==2||!Array.isArray(s.triggers)||s.triggers.length!==1||s.triggers[0].kind!=='phrase')return null;const ph=s.triggers[0].phrases||[];if(ph.length!==1||!Array.isArray(s.flow)||s.flow.length!==2)return null;const[composeStep,finishStep]=s.flow;if(composeStep.op!=='compose'||!composeStep.reply||typeof composeStep.reply.text!=='string'||Object.keys(composeStep.reply).length!==1)return null;if(finishStep.op!=='finish')return null;return{trigger:ph[0],reply:composeStep.reply.text,id:String(s.id||'').replace(/^user\./,'')}};
const fillQuick=q=>{$('quick-trigger').value=q?q.trigger:'';$('quick-reply').value=q?q.reply:'';$('quick-id').value=q?q.id:''};
const syncQuickToScenario=()=>{$('scenario-json').value=JSON.stringify(quickScenario($('quick-id').value.trim()||'scenario',$('quick-trigger').value,$('quick-reply').value),null,2)};
let scenarioItems=[],scenarioBuiltins=[],scenarioExtensions=[],scenarioSelected=null,scenarioBuiltinEnabled=true,scenarioCategories={};
// Свёрнутые узлы дерева сценариев (пути «раздел/подраздел»): удобство одного зрителя, переживает перезагрузку.
const scenarioCollapsed=new Set((()=>{try{return JSON.parse(localStorage.getItem('scenarioTreeCollapsed')||'[]')}catch(e){return[]}})());
const saveCollapsed=()=>{try{localStorage.setItem('scenarioTreeCollapsed',JSON.stringify([...scenarioCollapsed]))}catch(e){}};
const treePaths=()=>{const paths=new Set();for(const s of [...scenarioItems,...scenarioBuiltins,...scenarioExtensions]){const parts=[s.category||'custom',...String(s._group||'').split('/').map(p=>p.trim()).filter(Boolean)];for(let i=1;i<=parts.length;i++)paths.add(parts.slice(0,i).join('/'))}return paths};
// Режимы редактора «JSON / Скрипт (py)» — как в окне эндпоинта: executor {"kind":"script"},
// код живёт в <id>.py рядом с JSON; сохранение — PUT {scenario, script} (TASK-019).
const SCENARIO_STUB='"""py-сценарий: handle(ctx) вызывается на совпавший триггер\n(docs/contracts/scenario-json-v2.md, раздел «Исполнитель script»).\n\nИзменённый код перечитывается без рестарта; таймаут действует только на async\nhandle — сетевой код пишите async."""\nfrom server_emulator.sdk import say\n\n\nasync def handle(ctx):\n    return say("Привет!")\n';
let scenarioMode='json',scriptEditor=null,scriptLoaded=false,scriptStub=SCENARIO_STUB;
// Состояние окна редактора (TASK-038): scBuiltin — id встроенного, чью копию правим (он выключается только после
// сохранения копии); scScriptFrom — id сценария-источника для кода .py; scBase — снимок содержимого при открытии.
let scBuiltin=null,scScriptFrom=null,scBase='';
const makeScriptEditor=()=>{const box=$('scenario-script-box');
	if(typeof CodeMirror==='undefined'){const ta=document.createElement('textarea');ta.className='code';ta.rows=16;ta.spellcheck=false;box.append(ta);
		return{getValue:()=>ta.value,setValue:v=>{ta.value=v},refresh:()=>{},readOnly:()=>{},mode:()=>{}}}
	const cm=CodeMirror(box,{value:'',mode:'python',theme:'material-darker',lineNumbers:true,foldGutter:true,matchBrackets:true,indentUnit:4,tabSize:4,gutters:['CodeMirror-linenumbers','CodeMirror-foldgutter']});
	cm.setSize('100%','360px');
	return{getValue:()=>cm.getValue(),setValue:v=>{cm.setValue(v);cm.clearHistory()},refresh:()=>cm.refresh(),readOnly:v=>cm.setOption('readOnly',v),mode:m=>cm.setOption('mode',m)}};
// Код .py: у сохранённого сценария — API (GET scenarios?script=<id> → {code, stub});
// несохранённый сервер не знает — локальная копия заготовки (SCRIPT_STUB в ui/api/scenarios.py).
const fetchScenarioScript=async stubOnly=>{let code=scriptStub;
	const from=scScriptFrom||scenarioSelected;
	if(from)try{const r=await api('scenarios?script='+encodeURIComponent(from));
		if(r.stub)scriptStub=r.stub;code=stubOnly?scriptStub:(r.code||scriptStub)}catch(e){}
	return code};
const setScenarioMode=next=>{if(next==='script'&&!scriptEditor)scriptEditor=makeScriptEditor();
	scenarioMode=next;const script=next==='script';
	$('scenario-mode-json').className=script?'secondary':'';
	$('scenario-mode-script').className=script?'':'secondary';
	$('scenario-stub').hidden=!script;$('scenario-form-quick').hidden=script;
	$('scenario-json-label').hidden=script;$('scenario-script-box').hidden=!script;
	if(script){if(!scriptLoaded)(async()=>{scriptEditor.setValue(await fetchScenarioScript());scriptLoaded=true})();
		setTimeout(()=>scriptEditor.refresh(),0)}};
$('scenario-mode-json').onclick=()=>setScenarioMode('json');
$('scenario-mode-script').onclick=()=>setScenarioMode('script');
$('scenario-stub').onclick=()=>work(async()=>{scriptEditor.setValue(await fetchScenarioScript(true));scriptLoaded=true});
// Окно редактора сценария (TASK-038): <dialog> #scenario-dialog, ничего не пишет на диск до «Сохранить».
// Опции: scenario — определение (null — пустое), quick — значения быстрых полей (по умолчанию из определения),
// from — id сценария-источника кода .py (пользовательский, встроенный или расширения), builtin — id встроенного,
// который выключится после сохранения копии, selected — id пользовательского сценария (подсветка, «Удалить»),
// title/hint — заголовок и одно пояснение окна.
const scDialog=()=>$('scenario-dialog');
const scSnapshot=()=>JSON.stringify([scenarioMode,$('scenario-json').value,scenarioMode==='script'&&scriptEditor?scriptEditor.getValue():'']);
const scShowError=text=>{const e=$('scenario-dialog-error');e.textContent=text||'';e.hidden=!text};
async function openScenarioEditor({scenario=null,quick,from=null,builtin=null,selected=null,title='Редактор сценария',hint=''}){
	const script=!!scenario&&(scenario.executor||{}).kind==='script';
	let code=null;
	if(script){ // код грузим строго до открытия: заглушка вместо чужого кода при ошибке сохранила бы копию с пустым обработчиком
		const r=await api('scenarios?script='+encodeURIComponent(from||scenario.id));
		if(r.stub)scriptStub=r.stub;code=r.code!=null?r.code:scriptStub}
	scenarioSelected=selected;scScriptFrom=from;scBuiltin=builtin;
	$('scenario-json').value=scenario?JSON.stringify(scenario,null,2):'';
	fillQuick(quick!==undefined?quick:quickFromScenario(scenario));
	if(script){if(!scriptEditor)scriptEditor=makeScriptEditor();scriptEditor.setValue(code)}
	scriptLoaded=script;
	setScenarioMode(script?'script':'json');
	$('scenario-dialog-title').textContent=title;
	const h=$('scenario-dialog-hint');h.textContent=hint;h.hidden=!hint;
	scShowError('');$('scenario-discard').hidden=true;
	$('delete-scenario').hidden=!(selected&&scenarioItems.some(s=>s.id===selected));
	scBase=scSnapshot();renderScenarios();
	const d=scDialog();if(!d.open)d.showModal();
	if(scriptEditor)scriptEditor.refresh();setTimeout(()=>scriptEditor&&scriptEditor.refresh(),50)}
// Закрытие: «Отмена», ✕, Esc; при несохранённых изменениях — подтверждение внутри окна
function requestCloseScenarioEditor(){
	if(scSnapshot()===scBase){scDialog().close();return}
	$('scenario-discard').hidden=false;$('scenario-discard-no').focus()}
scDialog().addEventListener('cancel',e=>{e.preventDefault();if(!$('scenario-discard').hidden)$('scenario-discard').hidden=true;else requestCloseScenarioEditor()});
scDialog().addEventListener('close',()=>{scBuiltin=null;scScriptFrom=null;$('scenario-discard').hidden=true});
$('scenario-close').onclick=$('scenario-cancel').onclick=requestCloseScenarioEditor;
$('scenario-discard-yes').onclick=()=>scDialog().close();
$('scenario-discard-no').onclick=()=>{$('scenario-discard').hidden=true};
const loadIntoEditor=s=>work(()=>openScenarioEditor({scenario:s,from:s.id,selected:s.id,title:'Сценарий '+(s.id||'')}));
function renderScenarios(){const list=$('scenario-list');list.replaceChildren();if(!scenarioItems.length&&!scenarioBuiltins.length&&!scenarioExtensions.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='Сценариев пока нет. Создайте новый или установите расширение со сценариями.';list.append(empty);return}
	// Дерево: раздел (category) → подразделы (_group пакета, «a/b») → сценарии. Узел сворачивается
	// стрелкой/заголовком (состояние — localStorage), галочка узла включает/выключает всё поддерево.
	const catOrder=Object.keys(scenarioCategories);
	const catTitle=c=>i18n((scenarioCategories[c]||{}).title)||c;
	const root={children:new Map(),items:[]};
	const node=path=>{let n=root,acc=[];for(const part of path){acc.push(part);if(!n.children.has(part))n.children.set(part,{name:part,path:acc.join('/'),children:new Map(),items:[]});n=n.children.get(part)}return n};
	for(const s of scenarioItems)node([s.category||'custom',...String(s._group||'').split('/').map(p=>p.trim()).filter(Boolean)]).items.push({kind:'user',s});
	for(const b of scenarioBuiltins)node([b.category||'custom']).items.push({kind:'builtin',b});
	for(const e of scenarioExtensions)node([e.category||'custom']).items.push({kind:'extension',e});
	const scenarioRow=it=>{const s=it.s||it.b||it.e,isB=it.kind==='builtin',isX=it.kind==='extension';
		const row=document.createElement('div');row.className='scenario-row'+(s.id===scenarioSelected?' selected':'');
		const cl=document.createElement('label');cl.className='check';cl.title='Вкл/выкл сценарий';
		const box=document.createElement('input');box.type='checkbox';box.checked=isB?!!s.enabled:s.enabled!==false;
		box.onchange=()=>work(async()=>{try{await api('scenarios','PUT',{id:s.id,enabled:box.checked})}finally{await loadScenarios()}message('Сценарий '+(box.checked?'включён':'выключен')+': '+(s.id||''))});
		if(isX){box.checked=true;box.disabled=true;cl.title='Включается вместе с расширением'}
		cl.append(box);row.append(cl);
		const meta=document.createElement('div');meta.className='meta';
		const name=document.createElement(isB?'span':'button');
		if(!isB){name.type='button';name.onclick=()=>isX?editExtension(s):loadIntoEditor(s)}
		name.className='scenario-name';name.title='Открыть в редакторе';name.textContent=scenarioTitle(s);
		const nameLine=document.createElement('span');nameLine.className='name-line';nameLine.append(name);
		if((s.executor||{}).kind==='script'){const chip=document.createElement('span');chip.className='chip';chip.textContent='py';nameLine.append(chip)}
		if(isX){const chip=document.createElement('span');chip.className='chip';chip.textContent=s.extension||'расширение';chip.title='Сценарий расширения '+(s.extension||'');nameLine.append(chip)}
		const desc=document.createElement('small');desc.textContent=scenarioDesc(s);
		meta.append(nameLine,desc);row.append(meta);
		const edit=document.createElement('button');edit.type='button';edit.className='secondary';edit.textContent='Редактировать';
		edit.onclick=()=>isB?editBuiltin(s):isX?editExtension(s):loadIntoEditor(s);
		row.append(edit);
		if(!isB&&!isX){const ex=document.createElement('a');ex.className='button secondary';ex.textContent='Экспорт';ex.href='api/scenarios?export='+encodeURIComponent(s.id);ex.setAttribute('download',s.id+'.package.json');row.append(ex)}
		return row};
	const all=n=>[...n.items,...[...n.children.values()].flatMap(all)];
	const enabledOf=it=>it.kind==='user'?it.s.enabled!==false:!!it.b.enabled;
	const renderNode=(n,depth)=>{
		const items=all(n),open=!scenarioCollapsed.has(n.path);
		const box=document.createElement('div');box.className='sc-group sc-depth-'+Math.min(depth,3)+(open?'':' collapsed');
		const head=document.createElement('div');head.className='sc-group-head';
		const caret=document.createElement('button');caret.type='button';caret.className='sc-caret'+(open?' open':'');caret.textContent='▶';
		caret.title=open?'Свернуть':'Развернуть';caret.setAttribute('aria-expanded',String(open));
		const toggle=()=>{scenarioCollapsed.has(n.path)?scenarioCollapsed.delete(n.path):scenarioCollapsed.add(n.path);saveCollapsed();renderScenarios()};
		caret.onclick=toggle;
		const gl=document.createElement('label');gl.className='check';gl.title='Вкл/выкл всё в разделе';
		const gc=document.createElement('input');gc.type='checkbox';
		const toggleable=items.filter(it=>it.kind!=='extension'),states=toggleable.map(enabledOf);
		gc.disabled=!toggleable.length;gc.checked=states.length>0&&states.every(Boolean);gc.indeterminate=!gc.checked&&states.some(Boolean);
		const title=depth===0?catTitle(n.name):n.name;
		gc.onchange=()=>work(async()=>{if(!settings)settings=await api('settings');
			if(gc.checked&&items.some(it=>it.kind==='builtin')&&!scenarioBuiltinEnabled){const next=structuredClone(settings);next.builtin_scenarios=true;settings=await api('settings','PUT',next)}
			for(const it of toggleable)await api('scenarios','PUT',{id:(it.s||it.b).id,enabled:gc.checked});
			await loadScenarios();message('Раздел «'+title+'» '+(gc.checked?'включён':'выключен')+'.')});
		gl.append(gc);
		const label=document.createElement('span');label.className='sc-title';label.textContent=title;label.onclick=toggle;
		const count=document.createElement('span');count.className='count';count.textContent=items.length;
		head.append(caret,gl,label,count);box.append(head);
		if(open){
			const rows=document.createElement('div');rows.className='sc-group-rows';
			for(const child of [...n.children.values()].sort((a,b)=>a.name.localeCompare(b.name,'ru')))rows.append(renderNode(child,depth+1));
			for(const it of n.items)rows.append(scenarioRow(it));
			box.append(rows)}
		return box};
	const rank=name=>{const i=catOrder.indexOf(name);return i<0?99:i};
	for(const cat of [...root.children.values()].sort((a,b)=>rank(a.name)-rank(b.name)||a.name.localeCompare(b.name,'ru')))list.append(renderNode(cat,0));
}
function editBuiltin(item){work(()=>openBuiltinCopy(item))}
// Сценарий расширения — только чтение: в редактор уходит копия без служебных полей списка, сохранение создаёт пользовательский сценарий с тем же id (он перекрывает сценарий расширения)
function editExtension(item){const copy=structuredClone(item);for(const k of['catalog','enabled','extension','_file','_origin','origin'])delete copy[k];
	return work(()=>openScenarioEditor({scenario:copy,from:item.id,title:'Сценарий расширения «'+(item.extension||'')+'»',
		hint:'Сохранение создаст пользовательскую копию с тем же id, она перекроет сценарий расширения.'}))}
// Копия встроенного: черновик user.<имя> открывается в окне, на диск и в настройки ничего не пишется;
// «Сохранить» запишет копию (с кодом .py, если сценарий py), затем выключит встроенный.
async function openBuiltinCopy(item){
	const short=String(item.id||'').replace(/[^a-z0-9_.-]/g,'');
	const taken=new Set(scenarioItems.map(x=>x.id));
	let newId='user.'+(short||'copy'),n=1;while(taken.has(newId)){n+=1;newId='user.'+short+'-'+n}
	const copy=structuredClone(item);
	for(const k of['catalog','enabled','_file','_origin','origin'])delete copy[k];  // origin — метка списка, не поле определения
	copy.id=newId;copy.version='1.0.0';
	copy.title={'ru':'✏ '+(scenarioTitle(item)||short),'en':i18n(item.title)||''};
	return openScenarioEditor({scenario:copy,from:item.id,builtin:item.id,title:'Копия встроенного сценария '+item.id,
		hint:'Встроенный сценарий выключится только после сохранения копии; «Отмена» ничего не меняет.'})}
// Переход из звукового кеша: сценарий в редактор, фраза выделена и прокручена в JSON
let scenarioFocus=null;
// «Эмулятор → Без ответа» (misses.js): новый сценарий с фразой-триггером
window.newScenarioFromPhrase=text=>{goTab('scenarios');return work(()=>openScenarioEditor({scenario:quickScenario('scenario',text,''),quick:{trigger:text,reply:'',id:''},title:'Новый сценарий',hint:'Фраза — в триггере нового сценария: допишите ответ и идентификатор.'}))};
window.newScenarioFromEvent=name=>{goTab('scenarios');return work(()=>openScenarioEditor({quick:null,title:'Новый сценарий на колбэк',hint:'Сценарий на колбэк '+name+': допишите guard и ответ (bare — тихий ответ без реплики).',
  scenario:{...quickScenario(name.replace(/\W+/g,'_').replace(/^_+|_+$/g,'')||'callback',name,''),triggers:[{kind:'protocol_event',name}],flow:[{id:'reply',op:'compose',reply:{text:'',card:false,bare:true}},{id:'done',op:'finish'}]}}))};
window.openScenarioPhrase=(id,text)=>{scenarioFocus={id,text};if(location.hash==='#scenarios')syncScenarios();else goTab('scenarios')};
async function focusPhrase({id,text}){
	const s=scenarioItems.find(x=>x.id===id);
	if(s)await openScenarioEditor({scenario:s,from:s.id,selected:s.id,title:'Сценарий '+(s.id||'')});
	else{const b=scenarioBuiltins.find(x=>x.id===id);if(!b)throw Error('Сценарий не найден: '+id);
		await openBuiltinCopy(b)}  // встроенный — черновик копии, на диск ничего не пишется до «Сохранить»
	if(scenarioMode!=='json')return;  // py-сценарий: фраза не в JSON — переключать вид нельзя, сохранение из JSON-режима отбросит executor script
	const ta=$('scenario-json'),v=ta.value,full=JSON.stringify(text).slice(1,-1);
	let at=v.indexOf(full),len=full.length;
	if(at<0){const head=full.slice(0,24);at=v.indexOf(head);len=head.length}
	if(at<0)return;
	const lh=parseFloat(getComputedStyle(ta).lineHeight)||18;
	ta.focus({preventScroll:true});ta.setSelectionRange(at,at+len);
	ta.scrollTop=Math.max(0,(v.slice(0,at).split('\n').length-4)*lh)}
async function loadScenarios(){const result=await api('scenarios');scenarioItems=result.scenarios||[];scenarioBuiltins=result.builtin||[];scenarioExtensions=(result.extensions||[]).filter(e=>!scenarioItems.some(s=>s.id===e.id));scenarioBuiltinEnabled=!!result.builtin_enabled;scenarioCategories=Object.fromEntries((Array.isArray(result.categories)?result.categories:[]).map(c=>[c.id,c]));if((result.library_errors||[]).length)message('Библиотека сценариев: '+result.library_errors.join('; '));if(!scenarioItems.some(s=>s.id===scenarioSelected))scenarioSelected=null;renderScenarios();if(scenarioFocus){const f=scenarioFocus;scenarioFocus=null;await focusPhrase(f)}
if($('scenario-misses'))window.renderMisses($('scenario-misses'),['phrase']).catch(()=>{})}
$('scenario-expand').onclick=()=>{scenarioCollapsed.clear();saveCollapsed();renderScenarios()};
$('scenario-collapse').onclick=()=>{for(const path of treePaths())scenarioCollapsed.add(path);saveCollapsed();renderScenarios()};
$('new-scenario').onclick=()=>work(()=>openScenarioEditor({scenario:quickScenario('hello','фраза',''),quick:{trigger:'фраза',reply:'',id:'hello'},title:'Новый сценарий',hint:'Шаблон: title/description ru/en, executor — в JSON. Заполните поля — JSON обновится сам.'}));
for(const field of['quick-trigger','quick-reply','quick-id'])$(field).addEventListener('input',syncQuickToScenario);
// Сохранение: сначала запись сценария (копии), только после успеха — выключение встроенного.
// Ошибки показываются в самом окне (страница под модальным окном закрыта).
$('scenario-form').onsubmit=e=>{e.preventDefault();work(async()=>{scShowError('');try{const scenario=JSON.parse($('scenario-json').value);let result;
	delete scenario.origin;  // метка источника из списка (TASK-030) — метаданные, валидатор определение не примет
	if(scenarioMode==='script'){ // executor script: логика в <id>.py — валидатор не принимает flow
		scenario.executor={kind:'script'};delete scenario.flow;
		result=await api('scenarios','PUT',{scenario,script:scriptEditor.getValue()})}
	else{delete scenario.executor; // JSON — декларативный исполнитель; старый .py (если был) не трогаем, решает API
		result=await api('scenarios','PUT',{scenario})}
	const replaced=scBuiltin;
	if(replaced)await api('scenarios','PUT',{id:replaced,enabled:false});
	scenarioSelected=result.saved;scDialog().close();await loadScenarios();
	message('Сценарий сохранён: '+result.saved+(replaced?'; встроенный '+replaced+' выключен.':''))}catch(err){scShowError(err.message);message(err.message,true)}})};
$('delete-scenario').onclick=()=>work(async()=>{scShowError('');try{const scenario=JSON.parse($('scenario-json').value);if(!scenario.id)throw Error('В редакторе нет id сценария');const result=await api('scenarios?id='+encodeURIComponent(scenario.id),'DELETE');scenarioSelected=null;scDialog().close();await loadScenarios();message('Сценарий удалён: '+result.deleted)}catch(err){scShowError(err.message);message(err.message,true)}});
$('scenario-test').onclick=()=>work(async()=>{const report=await api('scenarios','POST',{text:$('scenario-test-text').value});$('scenario-test-result').textContent=report.handled?`${report.scenario} · ${report.status} (${report.reason})`:`не выбран: ${report.verdict||''} ${report.reason||''}`});
$('scenarios-install').onclick=()=>goTab('extensions');
const syncScenarios=()=>{if(location.hash==='#scenarios')work(loadScenarios)};
window.addEventListener('hashchange',syncScenarios);syncScenarios();

// Самообновление (TASK-040): кнопка в навигации видна при новой версии, клик — окно с заметками
// релиза. Обновление перезапускает сервер (updater) — окно опрашивает статус, пока тот не вернётся.
const updateState={shown:false,status:null};
async function refreshUpdate(){try{const s=await api('self_update');updateState.status=s;const btn=$('update-btn');
  btn.hidden=!s.available;if(!s.available)return;
  btn.textContent='Обновление '+s.latest.version;btn.title='Доступна версия '+s.latest.version+' (сейчас '+s.current.version+')';
  if(!updateState.shown){btn.classList.add('pulse');updateState.shown=true;setTimeout(()=>btn.classList.remove('pulse'),1600)}}catch(e){}}
window.refreshUpdate=refreshUpdate;
const ubtn=(label,cls,onclick)=>{const b=document.createElement('button');b.type='button';b.textContent=label;if(cls)b.className=cls;b.onclick=()=>work(onclick);return b};
function renderUpdateDialog(){const s=updateState.status||{},d=$('update-dialog');
  $('update-title').textContent=s.available?'Обновление '+s.latest.version:'Обновление';
  const parts=['Сейчас: '+s.current.version+(s.current.commit?' · '+s.current.commit:'')+(s.current.dirty?' (есть правки)':'')];
  if(s.latest)parts.push('Релиз: '+s.latest.version+(s.latest.published_at?' · '+s.latest.published_at.slice(0,10):''));
  $('update-versions').textContent=parts.join(' · ');
  const notes=$('update-notes');notes.textContent=s.latest&&s.latest.notes?s.latest.notes:'';notes.hidden=!notes.textContent;
  const reasons=$('update-reasons');reasons.replaceChildren();
  if(s.available&&!s.can_update){for(const text of s.reasons){const p=document.createElement('p');p.className='hint';p.textContent=text;reasons.append(p)}
    for(const file of s.blocked_files){const p=document.createElement('p');p.className='hint';p.textContent='· '+file;reasons.append(p)}}
  const last=s.last_update;
  const result=$('update-result');result.hidden=!(last&&last.status);result.className=last&&last.status==='ok'?'hint':'sc-err';
  result.textContent=last&&last.status?('Последнее обновление: '+(last.status==='ok'?'успешно':'не удалось')+' ('+last.from+' → '+last.to+').'+(last.log?' Лог: '+last.log:'')):'';
  const actions=$('update-actions');actions.replaceChildren();
  if(s.available&&s.can_update)actions.append(ubtn('Обновить и перезапустить','',()=>runUpdate('update')));
  if(s.available&&!s.can_update&&s.blocked_files.length)actions.append(ubtn('Отложить правки и обновить','secondary',()=>runUpdate('stash_update')));
  if(s.last_update&&s.last_update.status==='failed'&&s.previous)actions.append(ubtn('Откатить на '+s.previous,'secondary',()=>runUpdate('rollback')));
  actions.append(ubtn('Проверить сейчас','secondary',async()=>{updateState.status=await api('self_update','POST',{action:'check'});renderUpdateDialog();refreshUpdate()}));
  actions.append(ubtn('Позже','secondary',async()=>{await api('self_update','POST',{action:'snooze'});d.close();refreshUpdate()}));
  actions.append(ubtn('Пропустить версию','secondary',async()=>{await api('self_update','POST',{action:'skip'});d.close();refreshUpdate()}));
  if(!d.open)d.showModal()}
async function runUpdate(action){const result=$('update-result');
  const started=await api('self_update','POST',{action});
  result.className='hint';result.hidden=false;result.textContent='Обновление до '+started.target+'… сервер перезапустится сам.';
  $('update-actions').replaceChildren();
  const deadline=Date.now()+300000;let final=null;
  while(Date.now()<deadline){await new Promise(r=>setTimeout(r,2000));
    try{const s=await api('self_update');if(s.last_update&&['ok','failed'].includes(s.last_update.status)){final=s;break}}catch(e){}}
  if(!final){result.className='sc-err';result.textContent='Сервер не вернулся за 5 минут — смотрите state/update/update.log';return}
  updateState.status=final;
  if(final.last_update.status==='ok'){result.className='hint';result.textContent='Обновлено до '+final.current.version+'. Перезагрузка страницы…';setTimeout(()=>location.reload(),2000)}
  else{result.className='sc-err';result.textContent='Обновление не удалось, возвращена версия '+final.current.version+'. Лог: '+final.last_update.log;renderUpdateDialog()}}
$('update-btn').onclick=()=>renderUpdateDialog();
