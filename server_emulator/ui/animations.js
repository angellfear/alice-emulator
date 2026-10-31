/* Раздел «Анимации»: настоящий редактор — галерея с живыми GIF-превью,
   поиск/фильтры (модель, источник, кадры), карточки-группы, редактор
   (поля + последовательность кадров + JSON-вид), проигрывание по SN. */
'use strict';
const $a = (id) => document.getElementById(id);
let animItems = [], animModels = {}, animFilters = {q: '', model: '', source: ''}, animSelected = null, animMode = 'form';

function animMediaUrl(file) { return file ? `api/animations-media?file=${encodeURIComponent(file)}` : ''; }
function animPreview(item) { return animMediaUrl(item.file) || item.url || ''; }

async function animLoad() {
  const data = await (await fetch('api/animations')).json();
  animItems = data.animations || [];
  animModels = data.models || {};
  const picker = $a('anim-frame-picker');
  if (picker) picker.innerHTML = '<option value="">+ кадр из каталога…</option>' +
    animItems.map((a) => `<option value="${a.id}">${a.title} (${a.id})</option>`).join('');
  animRenderFilterBar();
  animRender();
}

function animModelTitle(model) {
  const entry = animModels[model];
  return entry ? entry.title : model;
}

function animVisible() {
  const q = animFilters.q.toLowerCase();
  return animItems.filter((item) => {
    if (q && !(`${item.title} ${item.id}`.toLowerCase().includes(q))) return false;
    if (animFilters.model === '__none__') {
      if ((item.models || []).length) return false;
    } else if (animFilters.model && !(item.models || []).includes(animFilters.model)) return false;
    if (animFilters.source && (item.source || '') !== animFilters.source) return false;
    if (animFilters.kind === 'frames' && !(item.frames || []).length) return false;
    if (animFilters.kind === 'single' && (item.frames || []).length) return false;
    return true;
  });
}

function animRenderFilterBar() {
  const bar = $a('anim-filter');
  if (!bar || bar.dataset.ready) return;
  bar.dataset.ready = '1';
  bar.innerHTML = `
    <input id="anim-q" placeholder="Поиск: название или id">
    <select id="anim-model"><option value="">Все модели</option><option value="__none__">Универсальные</option>
      ${Object.keys(animModels).map((m) => `<option value="${m}">${animModelTitle(m)}</option>`).join('')}</select>
    <select id="anim-source"><option value="">Любой источник</option><option value="capture">capture</option>
      <option value="download">download</option><option value="manual">manual</option></select>
    <select id="anim-kind"><option value="">Одно- и многокадровые</option><option value="single">Однокадровые</option>
      <option value="frames">Последовательности</option></select>`;
  $a('anim-q').oninput = (event) => { animFilters.q = event.target.value; animRender(); };
  $a('anim-model').onchange = (event) => { animFilters.model = event.target.value; animRender(); };
  $a('anim-source').onchange = (event) => { animFilters.source = event.target.value; animRender(); };
  $a('anim-kind').onchange = (event) => { animFilters.kind = event.target.value; animRender(); };
}

function animRender() {
  const list = $a('animation-list');
  list.replaceChildren();
  const items = animVisible();
  const stats = $a('anim-stats');
  if (stats) stats.textContent = `${animItems.length} в каталоге · показано ${items.length} · ` +
    `${animItems.filter((a) => a.file).length} с локальным GIF`;
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = animFilters.model === 'duo_max'
      ? 'У ДуоМакса анимаций нет — это честное поведение модели.' : 'Ничего не найдено по фильтрам.';
    list.append(empty);
    return;
  }
  for (const item of items) list.append(animCard(item));
}

function animCard(item) {
  const card = document.createElement('div');
  card.className = 'anim-card' + (item.id === animSelected ? ' selected' : '');
  const img = document.createElement('img');
  img.className = 'anim-thumb';
  img.src = animPreview(item);
  img.alt = item.title;
  const body = document.createElement('div');
  body.className = 'anim-card-body';
  const title = document.createElement('strong');
  title.textContent = item.title;
  const meta = document.createElement('small');
  const models = (item.models || []).length ? item.models.map(animModelTitle).join(', ') : 'все устройства с анимациями';
  const frames = (item.frames || []).length ? ` · кадров: ${item.frames.length}` : '';
  meta.textContent = `${item.id} · ${models} · ${item.source || '?'}${item.bytes ? ' · ' + item.bytes + ' Б' : ''}${frames}`;
  const actions = document.createElement('div');
  actions.className = 'anim-actions';
  const play = document.createElement('button');
  play.textContent = '▶ Проиграть';
  play.onclick = () => animPlay(item.id);
  const edit = document.createElement('button');
  edit.className = 'secondary';
  edit.textContent = 'Редактировать';
  edit.onclick = () => animEdit(item);
  actions.append(play, edit);
  body.append(title, meta, actions);
  card.append(img, body);
  return card;
}

function animFormState() {
  return {
    id: $a('animation-id').value.trim(),
    title: $a('animation-title').value.trim(),
    models: $a('animation-models').value.split(',').map((x) => x.trim()).filter(Boolean),
    url: $a('animation-url').value.trim(),
    frames: $a('animation-frames').value.split(',').map((x) => x.trim()).filter(Boolean),
  };
}

function animFillForm(item) {
  $a('animation-id').value = item ? item.id : '';
  $a('animation-title').value = item ? item.title : '';
  $a('animation-models').value = item ? (item.models || []).join(', ') : '';
  $a('animation-url').value = item ? (item.url || '') : '';
  $a('animation-frames').value = item ? (item.frames || []).join(', ') : '';
  $a('animation-file').value = '';
  animRefreshPreview();
}

function animRefreshPreview() {
  const state = animFormState();
  const preview = $a('animation-preview-live');
  const existing = animItems.find((a) => a.id === state.id);
  const source = state.frames.length
    ? animPreview(animItems.find((a) => a.id === state.frames[0]) || {})
    : (existing && existing.file ? animMediaUrl(existing.file) : state.url);
  preview.src = source || '';
  preview.hidden = !source;
  const framesBox = $a('anim-frames-preview');
  framesBox.replaceChildren();
  for (const frame of state.frames) {
    const child = animItems.find((a) => a.id === frame);
    const img = document.createElement('img');
    img.src = child ? animPreview(child) : '';
    img.title = frame;
    img.className = 'anim-thumb small';
    framesBox.append(img);
  }
}

function animPickFrame() {
  const select = $a('anim-frame-picker');
  if (!select.value) return;
  const frames = animFormState().frames;
  if (!frames.includes(select.value)) {
    frames.push(select.value);
    $a('animation-frames').value = frames.join(', ');
    animRefreshPreview();
  }
}

function animEdit(item) {
  animSelected = item ? item.id : null;
  animMode = 'form';
  animFillForm(item);
  animToggleEditor();
  animRender();
}

function animToggleEditor() {
  $a('anim-editor-json').hidden = animMode !== 'json';
  $a('anim-editor-form').hidden = animMode !== 'form';
  if (animMode === 'json') {
    const state = animFormState();
    const existing = animItems.find((a) => a.id === state.id);
    $a('animation-json').value = JSON.stringify(existing && !animSelected ? existing : state, null, 2);
  }
}

async function animSave(event) {
  event.preventDefault();
  const file = $a('animation-file').files[0];
  let media = null;
  if (file) media = await new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.readAsDataURL(file);
  });
  const animation = animMode === 'json' ? JSON.parse($a('animation-json').value) : animFormState();
  const response = await fetch('api/animations', {method: 'PUT', headers: {'X-Emulator': '1', 'Content-Type': 'application/json'},
                                                  body: JSON.stringify({animation, media_base64: media})});
  const data = await response.json();
  message(data.saved ? `Анимация «${data.saved}» сохранена.` : `Ошибка: ${data.error || '?'}`);
  if (data.saved) { await animLoad(); const saved = animItems.find((a) => a.id === data.saved); animFillForm(saved); }
}

async function animDelete() {
  const id = $a('animation-id').value.trim() || animSelected;
  if (!id) return;
  const data = await (await fetch(`api/animations?id=${encodeURIComponent(id)}`,
    {method: 'DELETE', headers: {'X-Emulator': '1'}})).json();
  message(data.deleted ? `Анимация «${data.deleted}» удалена.` : `Ошибка: ${data.error || '?'}`);
  if (data.deleted) { animSelected = null; await animLoad(); animFillForm(null); }
}

async function animPlay(id) {
  const sn = $a('animation-sn').value.trim();
  if (!sn) { message('Укажите SN устройства над списком.'); return; }
  const data = await (await fetch('api/animations', {method: 'POST',
    headers: {'X-Emulator': '1', 'Content-Type': 'application/json'}, body: JSON.stringify({id, sn})})).json();
  message(data.error ? `Не проиграно: ${data.error}` : `Отправлено на ${sn}: ${data.delivery}. Смотрите на станцию.`);
}

document.addEventListener('DOMContentLoaded', () => {
  if (!$a('animations')) return;
  $a('animation-form').addEventListener('submit', animSave);
  $a('animation-delete').onclick = animDelete;
  $a('animation-new').onclick = () => animEdit(null);
  $a('anim-mode-form').onclick = () => { animMode = 'form'; animToggleEditor(); };
  $a('anim-mode-json').onclick = () => { animMode = 'json'; animToggleEditor(); };
  for (const id of ('animation-title animation-models animation-url animation-frames').split(' '))
    $a(id).oninput = animRefreshPreview;
  $a('anim-add-frame').onclick = animPickFrame;
  animLoad();
});
