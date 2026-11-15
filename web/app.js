const schema = [{"name": "size", "label": "简单随机样本量"}, {"name": "seed", "label": "抽签种子"}];
const defaults = {"operation": "draw", "size": 8, "seed": 17};
const sources = ["frame", "clusters", "dispositions"];
let study = null;
let latestRequest = {...defaults};
let plannedDesigns = [];
let selectedDesign = null;
let currentDraw = null;

async function api(route, payload) {
  const options = payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  };
  const response = await fetch(route, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || String(response.status));
  return result;
}

function display(target, value) {
  document.getElementById(target).textContent = JSON.stringify(value, null, 2);
}

function renderTable(source) {
  const holder = document.getElementById('records');
  holder.replaceChildren();
  const raw = study[source];
  const rows = Array.isArray(raw) ? raw : Object.entries(raw).map(([key, value]) => ({key, value}));
  if (!rows.length) { holder.textContent = '当前集合没有记录'; return; }
  const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
  const table = document.createElement('table');
  const head = table.createTHead().insertRow();
  for (const column of columns) {
    const cell = document.createElement('th'); cell.textContent = column; head.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const rendered = body.insertRow();
    for (const column of columns) {
      const value = row[column];
      rendered.insertCell().textContent = value === null ? '缺失' : typeof value === 'object' ? JSON.stringify(value) : String(value ?? '');
    }
  }
  holder.append(table);
  document.getElementById('record-count').textContent = String(rows.length) + ' 条记录';
}

function renderControls() {
  const form = document.getElementById('action-form');
  for (const control of schema) {
    const label = document.createElement('label'); label.textContent = control.label;
    let input;
    if (control.options) {
      input = document.createElement('select');
      input.multiple = Array.isArray(defaults[control.name]);
      for (const value of control.options) {
        const option = document.createElement('option'); option.value = value; option.textContent = value;
        option.selected = input.multiple ? defaults[control.name].includes(value) : defaults[control.name] === value;
        input.append(option);
      }
    } else {
      input = document.createElement('input'); input.type = 'number'; input.value = defaults[control.name];
    }
    input.name = control.name; label.append(input); form.append(label);
  }
  const button = document.createElement('button'); button.type = 'submit'; button.textContent = "按现行规则抽签";
  form.append(button);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    latestRequest = {operation: defaults.operation};
    for (const control of schema) {
      const field = form.elements.namedItem(control.name);
      latestRequest[control.name] = field.multiple ? [...field.selectedOptions].map(option => option.value)
        : control.options ? field.value : Number(field.value);
    }
    try { display('result', await api('/api/action', latestRequest)); }
    catch (error) { display('result', {error: error.message}); }
  });
}

async function refreshNotes() {
  const result = await api('/api/notes');
  const holder = document.getElementById('notes'); holder.replaceChildren();
  for (const note of result.notes) {
    const item = document.createElement('li'); item.textContent = note.text + ' · ' + note.created_at; holder.append(item);
  }
}

// ---- 多阶段抽样工作台 ----

function renderDesignPick(plan) {
  const holder = document.getElementById('design-pick');
  holder.replaceChildren();
  plannedDesigns = plan.designs || [];
  selectedDesign = null;
  if (!plan.feasible) {
    holder.textContent = '无可行设计：请按上方冲突说明调整约束，工具不会自动退化为便利抽样';
    return;
  }
  for (const design of plannedDesigns) {
    const label = document.createElement('label');
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'design'; radio.value = design.design_id;
    radio.addEventListener('change', () => {
      selectedDesign = plannedDesigns.find(item => item.design_id === radio.value) || null;
    });
    label.append(radio, document.createTextNode(
      design.design_id + '｜' + design.summary +
      '｜最坏工时 ' + design.worst_case_minutes +
      '｜保证稀少专业 ' + design.guaranteed_rare +
      '｜设计方差 ' + design.design_variance_total +
      '｜方差可估计 ' + (design.variance_estimable ? '是' : '否')));
    holder.append(label);
  }
}

async function runPlan() {
  const form = document.getElementById('plan-form');
  const limits = {
    minutes: Number(form.elements.namedItem('minutes').value),
    minimum_rare_major: Number(form.elements.namedItem('minimum_rare_major').value)
  };
  const result = await api('/api/action', {operation: 'plan', limits});
  display('plan-result', result);
  renderDesignPick(result);
}

async function runDraw() {
  if (!selectedDesign) { display('draw-result', {error: '请先在①中选择一项可行设计'}); return; }
  const seed = Number(document.querySelector('#draw-form input[name=seed]').value);
  const result = await api('/api/action', {operation: 'draw_v2', design: selectedDesign.spec, seed});
  currentDraw = result;
  display('draw-result', result);
  await refreshDraws();
}

async function refreshStudents() {
  const drawId = document.querySelector('#receipt-form select[name=draw_id]').value;
  const select = document.querySelector('#receipt-form select[name=student]');
  const previous = select.value;
  select.replaceChildren();
  if (!drawId) return;
  const detail = await api('/api/action', {operation: 'draw_detail', draw_id: drawId});
  for (const entry of detail.main) {
    const option = document.createElement('option');
    option.value = entry.student;
    option.textContent = entry.student + '｜' + entry.campus + '/' + entry.class_id +
      '｜' + (entry.cell === 'rare' ? '稀少专业' : '非稀少专业') + '｜π=' + entry.pi;
    select.append(option);
  }
  if (previous && detail.main.some(entry => entry.student === previous)) select.value = previous;
}

async function refreshDraws() {
  const result = await api('/api/action', {operation: 'draws'});
  const select = document.querySelector('#receipt-form select[name=draw_id]');
  const previous = select.value;
  select.replaceChildren();
  for (const draw of result.draws) {
    const option = document.createElement('option');
    option.value = draw.draw_id;
    option.textContent = draw.draw_id + '｜' + draw.created_at + '｜正式 ' + draw.main_size + ' 人';
    select.append(option);
  }
  if (previous && result.draws.some(draw => draw.draw_id === previous)) select.value = previous;
  else if (currentDraw && result.draws.some(draw => draw.draw_id === currentDraw.draw_id)) {
    select.value = currentDraw.draw_id;
  }
  await refreshStudents();
  const list = document.getElementById('draws-list');
  list.replaceChildren();
  if (!result.draws.length) { list.textContent = '暂无抽签批次'; return; }
  const table = document.createElement('table');
  const columns = ['draw_id', 'created_at', 'seed', 'design_id', 'main_size', 'receipts', 'substitutions'];
  const head = table.createTHead().insertRow();
  for (const column of columns) { const cell = document.createElement('th'); cell.textContent = column; head.append(cell); }
  const body = table.createTBody();
  for (const draw of result.draws) {
    const row = body.insertRow();
    for (const column of columns) row.insertCell().textContent = String(draw[column]);
  }
  list.append(table);
}

async function submitReceipt() {
  const form = document.getElementById('receipt-form');
  const result = await api('/api/action', {
    operation: 'receipt',
    draw_id: form.elements.namedItem('draw_id').value,
    student: form.elements.namedItem('student').value,
    status: form.elements.namedItem('status').value
  });
  display('field-result', result);
  await refreshDraws();
}

async function runSubstitute() {
  const form = document.getElementById('receipt-form');
  const result = await api('/api/action', {
    operation: 'substitute',
    draw_id: form.elements.namedItem('draw_id').value,
    original: form.elements.namedItem('student').value
  });
  display('field-result', result);
  await refreshDraws();
}

async function runEstimate() {
  const drawId = document.querySelector('#receipt-form select[name=draw_id]').value;
  if (!drawId) { display('estimate-result', {error: '暂无抽签批次'}); return; }
  display('estimate-result', await api('/api/action', {operation: 'estimate', draw_id: drawId}));
}

function renderWorkflow() {
  const planForm = document.getElementById('plan-form');
  planForm.elements.namedItem('minutes').value = study.limits.minutes;
  planForm.elements.namedItem('minimum_rare_major').value = study.limits.minimum_rare_major;
  planForm.addEventListener('submit', async event => {
    event.preventDefault();
    try { await runPlan(); } catch (error) { display('plan-result', {error: error.message}); }
  });
  document.getElementById('draw-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { await runDraw(); } catch (error) { display('draw-result', {error: error.message}); }
  });
  document.getElementById('receipt-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { await submitReceipt(); } catch (error) { display('field-result', {error: error.message}); }
  });
  document.querySelector('#receipt-form select[name=draw_id]').addEventListener('change', refreshStudents);
  document.getElementById('substitute-button').addEventListener('click', async () => {
    try { await runSubstitute(); } catch (error) { display('field-result', {error: error.message}); }
  });
  document.getElementById('estimate-button').addEventListener('click', async () => {
    try { await runEstimate(); } catch (error) { display('estimate-result', {error: error.message}); }
  });
}

async function start() {
  study = await api('/api/study');
  display('summary', await api('/api/summary'));
  const selector = document.getElementById('source');
  for (const source of sources) {
    const option = document.createElement('option'); option.value = source; option.textContent = source; selector.append(option);
  }
  selector.addEventListener('change', () => renderTable(selector.value));
  renderTable(sources[0]); renderControls(); renderWorkflow();
  document.getElementById('note-form').addEventListener('submit', async event => {
    event.preventDefault();
    const field = document.getElementById('note');
    try { await api('/api/notes', {text: field.value, selection: latestRequest}); field.value = ''; await refreshNotes(); }
    catch (error) { display('result', {error: error.message}); }
  });
  await refreshNotes();
  await refreshDraws();
}

start().catch(error => display('summary', {error: error.message}));
