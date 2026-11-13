/* Раздел «Анимации»: подвкладки всех платформ реестра (доступны без единой колонки —
   отправлять просто будет некуда, редактор это не блокирует), у каждой — каталог проектов,
   редактор кадров в родном виде дисплея и плеер предпросмотра. Кадры хранятся как у колонки:
   ring/panel/oknix — цвета LED (.led), clock — яркости 32 сегментов (.scled),
   frontal — пиксели 25×16, монохромные (GIF). API: animations-platforms/-projects/-import/-export/-play. */
(() => {
  const root = $('animations');
  if (!root) return;
  const NS = 'http://www.w3.org/2000/svg';
  const ID_RE = /^[a-z0-9_.-]{1,64}$/;
  const PLAYABLE = ['clock', 'frontal'];
  const SWATCHES = ['#ff0000', '#ff8000', '#ffd000', '#00c040', '#00c0ff', '#2040ff', '#a030ff', '#ffffff'];
  const KIND_TITLES = {ring: 'Кольцо', panel: 'Панель', oknix: 'Окно', clock: 'Часы', frontal: 'Матрица'};

  const state = {
    platforms: [], platform: null, kind: null, projects: [], work: null, sel: 0,
    isNew: false, dirty: false, rgb: [255, 0, 0], level: 255, timer: null, loaded: false,
  };

  const el = (tag, props = {}, ...kids) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node[key] = value;
      else if (value !== false && value != null) node.setAttribute(key, value === true ? '' : value);
    }
    node.append(...kids.flat().filter((k) => k != null && k !== false));
    return node;
  };
  const svgEl = (tag, attrs = {}) => {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  };
  const button = (text, onclick, extra = {}) => el('button', {type: 'button', text, onclick, ...extra});
  const secondary = (text, onclick, extra = {}) => button(text, onclick, {class: 'secondary', ...extra});

  const display = () => state.platform.displays.find((d) => d.kind === state.kind);
  const cellCount = (d) => d.size.reduce((a, b) => a * b, 1);
  const hex2rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) || 0);
  const rgb2hex = (c) => '#' + c.slice(0, 3).map((v) => v.toString(16).padStart(2, '0')).join('');

  /* ---------- поверхность: SVG под тип дисплея ---------- */
  function buildSurface(kind, size) {
    const svg = svgEl('svg', {class: 'anim-surface', role: 'img'});
    const cells = [];
    const add = (shape) => {
      shape.setAttribute('class', 'anim-cell');
      shape.dataset.i = String(cells.length);
      svg.append(shape);
      cells.push(shape);
    };
    if (kind === 'clock') {
      svg.setAttribute('viewBox', '0 0 222 70');
      [4, 46, 124, 166].forEach((x0) => {
        // A B C D E F G
        [[6, 0, 24, 6], [30, 7, 6, 21], [30, 36, 6, 21], [6, 58, 24, 6], [0, 36, 6, 21], [0, 7, 6, 21], [6, 29, 24, 6]]
          .forEach(([x, y, w, h]) => add(svgEl('rect', {x: x0 + x, y, width: w, height: h, rx: 2})));
      });
      add(svgEl('rect', {x: 102, y: 10, width: 6, height: 20, rx: 2}));    // PLUS 1
      add(svgEl('rect', {x: 102, y: 38, width: 6, height: 20, rx: 2}));    // PLUS 2
      add(svgEl('rect', {x: 92, y: 31, width: 26, height: 6, rx: 2}));     // MINUS
      add(svgEl('circle', {cx: 211, cy: 10, r: 6}));                        // DEGREE
    } else if (kind === 'ring') {
      const n = size[0], radius = 100, dot = Math.min(14, (Math.PI * radius / n) * 0.8);
      svg.setAttribute('viewBox', '0 0 240 240');
      for (let i = 0; i < n; i++) {
        const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
        add(svgEl('circle', {cx: 120 + radius * Math.cos(angle), cy: 120 + radius * Math.sin(angle), r: dot}));
      }
    } else {
      const cols = kind === 'frontal' ? size[0] : kind === 'panel' ? 12 : size[0];
      const rows = Math.ceil((kind === 'frontal' ? size[0] * size[1] : size[0]) / cols);
      const step = kind === 'oknix' ? 40 : 14, cell = step - 2;
      svg.setAttribute('viewBox', `0 0 ${cols * step} ${rows * step}`);
      for (let i = 0; i < cols * rows; i++) {
        add(svgEl('rect', {x: (i % cols) * step, y: Math.floor(i / cols) * step, width: cell, height: cell, rx: 2}));
      }
    }
    return {svg, cells, kind};
  }

  function paint(surface, frame) {
    surface.cells.forEach((cell, i) => {
      if (surface.kind === 'clock') {
        const v = frame.brightnesses[i] | 0;
        cell.style.fill = v > 0 ? 'var(--live)' : '';
        cell.style.fillOpacity = v > 0 ? String(0.25 + (0.75 * v) / 255) : '';
      } else {
        const [r, g, b, w = 0] = frame.colors[i];
        cell.style.fill = r | g | b | w ? `rgb(${Math.min(255, r + w)},${Math.min(255, g + w)},${Math.min(255, b + w)})` : '';
        cell.style.fillOpacity = '';
      }
    });
  }

  function previewOf(project) {
    const surface = buildSurface(project.kind, project.size);
    paint(surface, project.frames[0]);
    return surface.svg;
  }

  /* ---------- загрузка ---------- */
  async function load() {
    try {
      state.platforms = (await api('animations-platforms')).platforms;
    } catch (e) {
      state.platforms = [];
      message('Платформы не загружены: ' + e.message, true);
    }
    state.loaded = true;
    if (!state.platform || !state.platforms.some((p) => p.platform === state.platform.platform)) {
      state.platform = state.platforms[0] || null;
      state.kind = state.platform ? state.platform.displays[0].kind : null;
      state.work = null;
    } else {
      state.platform = state.platforms.find((p) => p.platform === state.platform.platform);
    }
    await loadProjects();
    renderTabs();
  }

  async function loadProjects() {
    state.projects = [];
    if (state.platform) {
      try {
        state.projects = (await api('animations-projects?platform=' + encodeURIComponent(state.platform.platform))).projects;
      } catch (e) {
        message('Проекты не загружены: ' + e.message, true);
      }
    }
    renderView();
  }

  const projectsOfKind = () => state.projects.filter((p) => p.kind === state.kind);

  /* ---------- подтверждения и несохранённое ---------- */
  function ask(text, action = 'Да') {
    return new Promise((resolve) => {
      const dlg = el('dialog', {class: 'catalog-dialog'});
      const done = (value) => { dlg.close(); dlg.remove(); resolve(value); };
      dlg.append(el('form', {onsubmit: (ev) => { ev.preventDefault(); done(true); }},
        el('p', {text}),
        el('div', {class: 'row'}, secondary('Отмена', () => done(false)), el('button', {type: 'submit', text: action}))));
      dlg.addEventListener('cancel', () => done(false));
      document.body.append(dlg);
      dlg.showModal();
    });
  }
  const discardOk = async () => !state.dirty || ask('Несохранённые изменения будут потеряны. Продолжить?', 'Продолжить');

  /* ---------- подвкладки платформ и дисплеев ---------- */
  function renderTabs() {
    const bar = $('anim-platforms');
    bar.hidden = state.platforms.length === 0;
    bar.replaceChildren(...state.platforms.map((p) => {
      const b = button(p.title, async () => {
        if (p === state.platform || !(await discardOk())) return;
        stopPlayer();
        state.platform = p;
        state.kind = p.displays[0].kind;
        state.work = null;
        state.dirty = false;
        renderTabs();
        loadProjects();
      });
      b.classList.toggle('active', p === state.platform);
      return b;
    }));
  }

  /* ---------- вид платформы ---------- */
  function renderView() {
    const view = $('anim-view');
    stopPlayer(false);
    state.player = null;
    view.hidden = !state.platform;
    if (!state.platform) return;
    const parts = [];
    if (state.platform.displays.length > 1) {
      parts.push(el('div', {class: 'segmented anim-kinds', role: 'radiogroup', 'aria-label': 'Дисплей'},
        state.platform.displays.map((d) => el('button', {
          type: 'button', role: 'radio', 'aria-checked': String(d.kind === state.kind), text: d.title,
          class: d.kind === state.kind ? 'active' : '',
          onclick: async () => {
            if (d.kind === state.kind || !(await discardOk())) return;
            state.kind = d.kind; state.work = null; state.dirty = false; renderView();
          },
        }))));
    } else {
      parts.push(el('h3', {text: display().title}));
    }
    if (!PLAYABLE.includes(state.kind)) {
      parts.push(el('p', {class: 'hint', text: 'Этот дисплей колонка не принимает по сети: анимацию можно нарисовать, '
        + 'импортировать и экспортировать в файл .led для самой колонки.'}));
    }
    parts.push(catalogPanel());
    if (state.work) parts.push(editorPanel());
    view.replaceChildren(...parts);
    if (state.work) { refreshCanvas(); refreshStrip(); }
  }

  /* ---------- каталог ---------- */
  function catalogPanel() {
    const list = projectsOfKind();
    const grid = el('div', {class: 'anim-grid'}, list.map((p) => {
      const card = el('div', {class: 'anim-card' + (state.work && !state.isNew && state.work.id === p.id ? ' selected' : ''),
        tabindex: '0', role: 'button', onclick: () => openProject(p),
        onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openProject(p); } }},
      el('div', {class: 'anim-card-preview'}, previewOf(p)),
      el('strong', {text: p.title}),
      el('small', {text: `${p.frames.length} кадр.`}));
      return card;
    }));
    return el('div', {class: 'panel'},
      el('div', {class: 'anim-head'}, el('h3', {text: 'Проекты'}),
        el('div', {class: 'anim-head-actions'}, button('Новый', newProject), secondary('Импорт', openImport))),
      list.length ? grid : el('div', {class: 'empty', text: 'Проектов пока нет: нарисуйте новый или импортируйте файл.'}));
  }

  async function openProject(project) {
    if (!(await discardOk())) return;
    stopPlayer();
    state.work = structuredClone(project);
    state.isNew = false; state.dirty = false; state.sel = 0;
    renderView();
  }

  const blankFrame = (kind, size) => kind === 'clock'
    ? {brightnesses: Array(32).fill(0), delayMs: 100}
    : {colors: Array.from({length: size.reduce((a, b) => a * b, 1)}, () => (kind === 'frontal' ? [0, 0, 0] : [0, 0, 0, 0])), delayMs: 100};

  async function newProject() {
    if (!(await discardOk())) return;
    stopPlayer();
    const d = display();
    let n = 1;
    while (state.projects.some((p) => p.id === `my.${d.kind}.${n}`)) n++;
    state.work = {platform: state.platform.platform, id: `my.${d.kind}.${n}`, title: `${KIND_TITLES[d.kind]} ${n}`,
      kind: d.kind, size: d.size, loop_from: -1, frames: [blankFrame(d.kind, d.size)]};
    state.isNew = true; state.dirty = true; state.sel = 0;
    renderView();
  }

  /* ---------- редактор ---------- */
  let canvas = null;      // {surface}
  let strip = null;
  const frame = () => state.work.frames[state.sel];
  const touch = () => { state.dirty = true; const mark = $('anim-dirty'); if (mark) mark.hidden = false; };

  function editorPanel() {
    const w = state.work;
    const isClock = w.kind === 'clock';
    const isMono = isClock || w.kind === 'frontal';   // клетки без цвета — только яркость белого
    const idInput = el('input', {id: 'anim-id', value: w.id, readonly: !state.isNew, pattern: ID_RE.source, maxlength: '64',
      oninput: (ev) => { w.id = ev.target.value.trim(); touch(); }});
    const titleInput = el('input', {id: 'anim-title', value: w.title, oninput: (ev) => { w.title = ev.target.value; touch(); }});

    canvas = {box: el('div', {class: 'anim-canvas'})};
    strip = el('div', {class: 'anim-strip'});

    const tools = isMono
      ? el('div', {class: 'anim-tools'},
        el('label', {text: 'Яркость сегмента'},
          el('input', {id: 'anim-level', type: 'range', min: '0', max: '255', value: String(state.level),
            oninput: (ev) => { state.level = +ev.target.value; $('anim-level-out').textContent = String(state.level); }}),
          el('output', {id: 'anim-level-out', text: String(state.level)})),
        el('div', {class: 'anim-tool-row'}, secondary('Погасить', () => setLevel(0)), secondary('Полная', () => setLevel(255)),
          secondary('Залить кадр', () => fillFrame())))
      : el('div', {class: 'anim-tools'},
        el('div', {class: 'anim-tool-row'},
          el('input', {id: 'anim-color', type: 'color', value: rgb2hex(state.rgb), 'aria-label': 'Цвет',
            oninput: (ev) => { state.rgb = hex2rgb(ev.target.value); }}),
          SWATCHES.map((hex) => el('button', {type: 'button', class: 'anim-swatch', style: `background:${hex}`,
            'aria-label': hex, onclick: () => setColor(hex2rgb(hex))})),
          el('button', {type: 'button', class: 'anim-swatch anim-eraser', 'aria-label': 'Ластик', title: 'Ластик',
            onclick: () => setColor([0, 0, 0]), text: '∅'})),
        el('div', {class: 'anim-tool-row'}, secondary('Залить кадр', () => fillFrame())));

    const loopSelect = el('select', {id: 'anim-loop', 'aria-label': 'Цикл',
      onchange: (ev) => { w.loop_from = +ev.target.value; touch(); }},
    el('option', {value: '-1', text: 'Без цикла'}),
    w.frames.map((_, i) => el('option', {value: String(i), text: `Цикл с кадра ${i + 1}`, selected: i === w.loop_from})));
    loopSelect.value = String(w.loop_from);

    const delayInput = el('input', {id: 'anim-delay', type: 'number', min: w.kind === 'frontal' ? '20' : '0', max: '65535', step: '10',
      value: String(frame().delayMs), 'aria-label': 'Задержка кадра, мс',
      onchange: (ev) => {
        const min = w.kind === 'frontal' ? 20 : 0;
        frame().delayMs = Math.max(min, Math.min(65535, Math.round(+ev.target.value) || 0));
        ev.target.value = String(frame().delayMs);
        touch();
      }});

    const player = {box: el('div', {class: 'anim-player'}), surface: buildSurface(w.kind, w.size)};
    state.player = player;
    paint(player.surface, frame());
    player.button = button('▶ Играть', togglePlayer);
    player.box.append(player.surface.svg);

    const devices = state.platform.devices;
    const playable = PLAYABLE.includes(w.kind);
    const hasDevices = devices.length > 0;
    const deviceSelect = el('select', {id: 'anim-device', 'aria-label': 'Колонка для проигрывания', disabled: !playable || !hasDevices},
      devices.map((d) => el('option', {value: d.sn, text: [...new Set([d.name, d.model || state.platform.title, d.sn].filter(Boolean))].join(' — ')})));

    return el('div', {class: 'panel anim-editor'},
      el('div', {class: 'anim-head'}, el('h3', {text: 'Редактор'}), el('span', {id: 'anim-dirty', class: 'chip warn', text: 'не сохранён', hidden: !state.dirty})),
      el('label', {text: 'Название'}, titleInput),
      el('label', {text: 'Идентификатор'}, idInput),
      canvas.box, tools,
      el('div', {class: 'anim-frames-head'},
        el('strong', {text: `Кадр ${state.sel + 1} из ${w.frames.length}`, id: 'anim-frame-title'}),
        el('div', {class: 'anim-tool-row'},
          secondary('+ Кадр', () => addFrame(blankFrame(w.kind, w.size))),
          secondary('Дубль', () => addFrame(structuredClone(frame()))),
          secondary('←', () => moveFrame(-1), {'aria-label': 'Кадр влево'}),
          secondary('→', () => moveFrame(1), {'aria-label': 'Кадр вправо'}),
          secondary('Удалить кадр', removeFrame))),
      strip,
      el('div', {class: 'anim-tool-row anim-timing'}, el('label', {text: 'Задержка, мс'}, delayInput), loopSelect),
      el('h3', {text: 'Предпросмотр'}),
      player.box, el('div', {class: 'anim-tool-row'}, player.button),
      el('div', {class: 'anim-actions'},
        button('Сохранить', saveProject),
        playable ? [deviceSelect, button('Проиграть на колонке', playOnDevice, {disabled: !hasDevices}),
          !hasDevices ? el('span', {class: 'hint', text: 'Нет подключённых колонок этой платформы.'}) : null] : null,
        secondary('Экспорт', exportProject),
        state.isNew ? null : secondary('Удалить проект', deleteProject)));
  }

  function setColor(rgb) {
    state.rgb = rgb;
    const input = $('anim-color');
    if (input) input.value = rgb2hex(rgb);
  }
  function setLevel(value) {
    state.level = value;
    $('anim-level').value = String(value);
    $('anim-level-out').textContent = String(value);
  }

  function refreshCanvas() {
    if (!canvas) return;
    const w = state.work;
    if (!canvas.surface) {
      canvas.surface = buildSurface(w.kind, w.size);
      canvas.box.replaceChildren(canvas.surface.svg);
      attachPainting(canvas.surface);
    }
    paint(canvas.surface, frame());
    const title = $('anim-frame-title');
    if (title) title.textContent = `Кадр ${state.sel + 1} из ${w.frames.length}`;
    const delay = $('anim-delay');
    if (delay) delay.value = String(frame().delayMs);
  }

  function attachPainting(surface) {
    const svg = surface.svg;
    let down = false, last = -1;
    const apply = (ev) => {
      const target = document.elementFromPoint(ev.clientX, ev.clientY);
      const cell = target && target.closest ? target.closest('.anim-cell') : null;
      if (!cell || !svg.contains(cell) || +cell.dataset.i === last) return;
      last = +cell.dataset.i;
      setCell(last);
    };
    svg.addEventListener('pointerdown', (ev) => { down = true; last = -1; svg.setPointerCapture(ev.pointerId); apply(ev); ev.preventDefault(); });
    svg.addEventListener('pointermove', (ev) => { if (down) apply(ev); });
    for (const type of ['pointerup', 'pointercancel']) svg.addEventListener(type, () => { down = false; });
  }

  function setCell(i) {
    const f = frame();
    const kind = state.work.kind;
    if (kind === 'clock') f.brightnesses[i] = state.level;
    else if (kind === 'frontal') f.colors[i] = [state.level, state.level, state.level];
    else f.colors[i] = [...state.rgb, 0];
    touch();
    paint(canvas.surface, f);
    repaintThumb();
  }

  function fillFrame() {
    const f = frame();
    const kind = state.work.kind;
    if (kind === 'clock') f.brightnesses = Array(32).fill(state.level);
    else if (kind === 'frontal') f.colors = f.colors.map(() => [state.level, state.level, state.level]);
    else f.colors = f.colors.map(() => [...state.rgb, 0]);
    touch();
    refreshCanvas();
    repaintThumb();
  }

  /* ---------- кадры ---------- */
  function refreshStrip() {
    if (!strip) return;
    const w = state.work;
    strip.replaceChildren(...w.frames.map((f, i) => {
      const thumb = buildSurface(w.kind, w.size);
      paint(thumb, f);
      const item = el('div', {class: 'anim-thumb' + (i === state.sel ? ' selected' : ''), tabindex: '0', role: 'button',
        'aria-label': `Кадр ${i + 1}`, onclick: () => selectFrame(i),
        onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectFrame(i); } }},
      thumb.svg, el('small', {text: String(i + 1)}));
      item.thumb = thumb;
      return item;
    }));
    const loop = $('anim-loop');
    if (loop) {
      loop.replaceChildren(el('option', {value: '-1', text: 'Без цикла'}),
        ...w.frames.map((_, i) => el('option', {value: String(i), text: `Цикл с кадра ${i + 1}`})));
      loop.value = String(w.loop_from);
    }
  }
  function repaintThumb() {
    const item = strip && strip.children[state.sel];
    if (item && item.thumb) paint(item.thumb, frame());
    if (state.player && state.player.surface && !state.timer) paint(state.player.surface, frame());
  }
  function selectFrame(i) {
    stopPlayer();
    state.sel = i;
    refreshStrip();
    refreshCanvas();
  }
  function afterStructure() {
    touch();
    stopPlayer();
    refreshStrip();
    refreshCanvas();
  }
  function addFrame(f) {
    const w = state.work;
    if (w.frames.length >= 500) { message('Не больше 500 кадров.', true); return; }
    w.frames.splice(state.sel + 1, 0, f);
    if (w.loop_from > state.sel) w.loop_from++;
    state.sel++;
    afterStructure();
  }
  function removeFrame() {
    const w = state.work;
    if (w.frames.length < 2) { message('Нужен хотя бы один кадр.', true); return; }
    w.frames.splice(state.sel, 1);
    if (w.loop_from >= w.frames.length) w.loop_from = -1;
    else if (w.loop_from > state.sel) w.loop_from--;
    state.sel = Math.min(state.sel, w.frames.length - 1);
    afterStructure();
  }
  function moveFrame(delta) {
    const w = state.work, to = state.sel + delta;
    if (to < 0 || to >= w.frames.length) return;
    [w.frames[state.sel], w.frames[to]] = [w.frames[to], w.frames[state.sel]];
    state.sel = to;
    afterStructure();
  }

  /* ---------- плеер ---------- */
  function stopPlayer(repaint = true) {
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
    if (state.player && state.player.button) state.player.button.textContent = '▶ Играть';
    if (repaint && state.player && state.player.surface && state.work) paint(state.player.surface, frame());
  }
  function togglePlayer() {
    if (state.timer) { stopPlayer(); return; }
    const w = state.work;
    let i = 0;
    state.player.button.textContent = '■ Стоп';
    const step = () => {
      paint(state.player.surface, w.frames[i]);
      const delay = Math.max(20, w.frames[i].delayMs);
      i++;
      if (i >= w.frames.length) {
        if (w.loop_from < 0) { state.timer = setTimeout(stopPlayer, delay); return; }
        i = w.loop_from;
      }
      state.timer = setTimeout(step, delay);
    };
    step();
  }

  /* ---------- сохранение, экспорт, удаление, проигрывание ---------- */
  async function saveProject() {
    const w = state.work;
    if (!ID_RE.test(w.id)) { message('Идентификатор: строчные буквы, цифры, . _ - до 64 символов.', true); return false; }
    if (!w.title.trim()) { message('Укажите название.', true); return false; }
    if (state.isNew && state.projects.some((p) => p.id === w.id)) { message('Проект с таким идентификатором уже есть.', true); return false; }
    try {
      await api('animations-projects', 'PUT', w);
    } catch (e) {
      message('Не сохранено: ' + e.message, true);
      return false;
    }
    state.isNew = false; state.dirty = false;
    state.projects = (await api('animations-projects?platform=' + encodeURIComponent(w.platform))).projects;
    const keep = state.sel;
    renderView();
    state.sel = keep;
    message('Проект «' + w.title + '» сохранён.');
    return true;
  }

  async function exportProject() {
    if ((state.dirty || state.isNew) && !(await saveProject())) return;
    const w = state.work;
    const link = el('a', {href: `api/animations-export?platform=${encodeURIComponent(w.platform)}&id=${encodeURIComponent(w.id)}`, download: ''});
    document.body.append(link);
    link.click();
    link.remove();
  }

  async function deleteProject() {
    const w = state.work;
    if (!(await ask(`Удалить проект «${w.title}»? Это нельзя отменить.`, 'Удалить'))) return;
    try {
      await api(`animations-projects?platform=${encodeURIComponent(w.platform)}&id=${encodeURIComponent(w.id)}`, 'DELETE');
    } catch (e) {
      message('Не удалено: ' + e.message, true);
      return;
    }
    state.work = null; state.dirty = false;
    await loadProjects();
    message('Проект удалён.');
  }

  async function playOnDevice() {
    const sn = $('anim-device').value;
    if (!sn) { message('Выберите колонку.', true); return; }
    if ((state.dirty || state.isNew) && !(await saveProject())) return;
    try {
      const result = await api('animations-play', 'POST', {platform: state.work.platform, id: state.work.id, sn});
      if (result.delivery === 'no_session') {
        message(result.reason === 'transparent' ? 'В режиме «Прозрачно» колонка не на связи.' : 'Колонка не на связи.', true);
      } else if (result.delivery === 'failed') {
        message('Колонка не приняла кадр.', true);
      } else {
        message('Отправлено на колонку ' + sn + '. Экран колонки не подтверждается.');
      }
    } catch (e) {
      message('Не проиграно: ' + e.message, true);
    }
  }

  /* ---------- импорт ---------- */
  const EXTENSIONS = {ring: '.led', panel: '.led', oknix: '.led', clock: '.scled', frontal: '.gif'};

  function slug(name) {
    const stem = name.replace(/\.[^.]*$/, '').toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^[-.]+|[-.]+$/g, '').slice(0, 56);
    let id = stem || 'import', n = 1;
    while (state.projects.some((p) => p.id === id)) id = `${stem || 'import'}-${++n}`;
    return id;
  }

  function openImport() {
    const kind = state.kind, ext = EXTENSIONS[kind];
    const text = kind !== 'frontal';
    const dlg = el('dialog', {class: 'catalog-dialog'});
    const close = () => { dlg.close(); dlg.remove(); };
    const file = el('input', {type: 'file', accept: ext, id: 'anim-import-file'});
    const paste = text ? el('textarea', {id: 'anim-import-text', rows: '5', spellcheck: 'false', placeholder: 'или вставьте текст ' + ext}) : null;
    const idInput = el('input', {id: 'anim-import-id', required: true, pattern: ID_RE.source, value: slug('import')});
    const titleInput = el('input', {id: 'anim-import-title', required: true});
    file.onchange = () => {
      const f = file.files[0];
      if (!f) return;
      idInput.value = slug(f.name);
      if (!titleInput.value) titleInput.value = f.name.replace(/\.[^.]*$/, '');
    };
    dlg.append(el('form', {onsubmit: async (ev) => {
      ev.preventDefault();
      let bytes, filename;
      if (file.files[0]) {
        bytes = new Uint8Array(await file.files[0].arrayBuffer());
        filename = file.files[0].name;
      } else if (paste && paste.value.trim()) {
        bytes = new TextEncoder().encode(paste.value);
        filename = 'paste' + ext;
      } else {
        message('Выберите файл' + (text ? ' или вставьте текст.' : '.'), true);
        return;
      }
      let binary = '';
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      try {
        const done = await api('animations-import', 'POST', {platform: state.platform.platform, id: idInput.value.trim(),
          title: titleInput.value.trim(), kind, filename, data_base64: btoa(binary)});
        close();
        await loadProjects();
        const imported = state.projects.find((p) => p.id === done.saved);
        if (imported) { state.work = structuredClone(imported); state.isNew = false; state.dirty = false; state.sel = 0; renderView(); }
        message(`Импортировано: ${done.frames} кадр.`);
      } catch (e) {
        message('Импорт не удался: ' + e.message, true);
      }
    }},
    el('h3', {text: 'Импорт анимации'}),
    el('label', {text: 'Файл ' + ext}, file),
    paste,
    el('label', {text: 'Идентификатор'}, idInput),
    el('label', {text: 'Название'}, titleInput),
    el('div', {class: 'row'}, secondary('Отмена', close), el('button', {type: 'submit', text: 'Импортировать'}))));
    dlg.addEventListener('cancel', close);
    document.body.append(dlg);
    dlg.showModal();
  }

  /* ---------- вход во вкладку ---------- */
  const sync = () => {
    if (location.hash === '#animations') work(load);
    else stopPlayer();
  };
  window.addEventListener('hashchange', sync);
  sync();
})();
