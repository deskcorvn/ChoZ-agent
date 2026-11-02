// Chợ Z listing agent — demo UI. Plain JS, no build step.
const USD_TO_VND = 25855;   // 26/10/2026, as in the hackathon plan
const $ = (id) => document.getElementById(id);
const screens = ['capture', 'working', 'question', 'preview', 'done'];
const state = { photos: [], audio: null, answerAudio: null, run: null, polling: null };

function show(name) {
  for (const s of screens) $(`screen-${s}`).hidden = s !== name;
}

const money = (vnd) => vnd == null ? 'chưa có' : new Intl.NumberFormat('vi-VN').format(vnd) + 'đ';

// ---- photos ---------------------------------------------------------------------------------

function renderSlots() {
  const slots = $('slots');
  slots.replaceChildren();
  for (let i = 0; i < 3; i++) {
    const slot = document.createElement('button');
    slot.type = 'button';
    slot.className = 'slot';
    const photo = state.photos[i];
    if (photo) {
      slot.classList.add('filled');
      slot.style.backgroundImage = `url(${photo.url})`;
      slot.setAttribute('aria-label', `Bỏ ảnh ${i + 1}`);
      slot.onclick = () => { URL.revokeObjectURL(photo.url); state.photos.splice(i, 1); renderSlots(); };
    } else {
      slot.textContent = i === state.photos.length ? '+ Ảnh' : '';
      slot.onclick = () => $('photo-input').click();
    }
    slots.append(slot);
  }
  updateCreate();
}

$('photo-input').addEventListener('change', (e) => {
  for (const file of e.target.files) {
    if (state.photos.length >= 3) break;
    state.photos.push({ file, url: URL.createObjectURL(file) });
  }
  e.target.value = '';
  renderSlots();
});

function updateCreate() {
  $('create').disabled = !(state.photos.length && (state.audio || $('note').value.trim()));
}
$('note').addEventListener('input', updateCreate);

// ---- hold to talk → 16 kHz mono WAV (what Muse Voice Transcribe's file endpoint takes) --------

async function toWav(blob) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  ctx.close();
  const rate = 16000;
  const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start();
  const pcm = (await offline.startRendering()).getChannelData(0);
  const view = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const text = (offset, s) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const v = Math.max(-1, Math.min(1, pcm[i]));
    view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return { blob: new Blob([view], { type: 'audio/wav' }), seconds: decoded.duration };
}

function holdToTalk(button, onDone, onError) {
  let recorder = null, chunks = [];
  const start = async (e) => {
    e.preventDefault();
    if (recorder) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (ev) => chunks.push(ev.data);
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        button.classList.remove('recording');
        const rec = recorder; recorder = null;
        try { onDone(await toWav(new Blob(chunks, { type: rec.mimeType }))); } catch (err) { onError(err); }
      };
      recorder.start();
      button.classList.add('recording');
    } catch (err) { recorder = null; onError(err); }
  };
  const stop = () => { if (recorder && recorder.state === 'recording') recorder.stop(); };
  button.addEventListener('pointerdown', start);
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) button.addEventListener(ev, stop);
}

holdToTalk($('mic'), ({ blob, seconds }) => {
  state.audio = blob;
  $('heard').textContent = `Đã ghi ${seconds.toFixed(1)} giây — giữ lại để nói lại`;
  updateCreate();
}, (err) => { $('capture-error').textContent = 'Không ghi âm được: ' + err.message; });

holdToTalk($('mic-answer'), ({ blob }) => { state.answerAudio = blob; sendAnswer(); },
  (err) => { $('answer-error').textContent = 'Không ghi âm được: ' + err.message; });

// ---- run the agent ------------------------------------------------------------------------

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

$('create').addEventListener('click', async () => {
  $('capture-error').textContent = '';
  const form = new FormData();
  state.photos.forEach((p) => form.append('photos', p.file, p.file.name));
  if (state.audio) form.append('audio', state.audio, 'speech.wav');
  form.append('note', $('note').value);
  try {
    const run = await api('/api/runs', { method: 'POST', body: form });
    show('working');
    follow(run);
  } catch (err) { $('capture-error').textContent = err.message; }
});

function follow(run) {
  state.run = run;
  renderLog(run);
  clearTimeout(state.polling);
  if (run.status === 'running') {
    state.polling = setTimeout(async () => follow(await api(`/api/runs/${run.id}`)), 400);
    return;
  }
  route(run);
}

function route(run) {
  if (run.status === 'needs_answer') {
    $('question').textContent = run.question;
    $('answer-text').value = '';
    show('question');
  } else if (run.status === 'ready' || run.status === 'blocked') {
    fillPreview(run);
    show('preview');
  } else if (run.status === 'failed') {
    show('capture');
    $('capture-error').textContent = 'Agent lỗi: ' + run.error;
  }
}

async function sendAnswer() {
  $('answer-error').textContent = '';
  const form = new FormData();
  if (state.answerAudio) form.append('audio', state.answerAudio, 'answer.wav');
  form.append('text', $('answer-text').value);
  state.answerAudio = null;
  show('working');
  try { follow(await api(`/api/runs/${state.run.id}/answer`, { method: 'POST', body: form })); }
  catch (err) { show('question'); $('answer-error').textContent = err.message; }
}
$('send-answer').addEventListener('click', sendAnswer);

function fillPreview(run) {
  const l = run.listing;
  $('studio').src = `/files/runs/${run.id}/${run.studio_image || run.photos[0]}`;
  $('f-title').value = l.title;
  $('f-price').value = l.price_vnd ?? '';
  $('f-stock').value = l.stock ?? '';
  // Provenance: the seller's own words behind each number, checked in code to appear verbatim in what they said.
  for (const [field, id] of [['price_vnd', 'src-price'], ['stock', 'src-stock']]) {
    const p = (l.provenance || {})[field];
    $(id).textContent = p && p.quote ? `${p.verified ? '🎙' : '?'} “${p.quote}”` : '';
    $(id).className = 'src' + (p && !p.verified ? ' unverified' : '');
  }
  $('f-category').value = l.category;
  $('f-description').value = l.description;
  const chips = $('chips');
  chips.replaceChildren(...[...l.attributes.map((a) => `${a.name}: ${a.value}`), ...l.variants].map((t) => {
    const chip = document.createElement('span'); chip.className = 'chip'; chip.textContent = t; return chip;
  }));
  const v = run.verification || { verdict: 'warn', summary_vi: 'Chưa kiểm tra.' };
  $('verdict').className = `verdict ${v.verdict}`;
  const summary = v.summary_vi.charAt(0).toUpperCase() + v.summary_vi.slice(1);
  // The written rule behind a warning or a block (policy/listing-policy.md), e.g. "Quy định P1 · Prescription medicine".
  const RULES_VI = { P1: 'thuốc kê đơn', P2: 'vũ khí', P3: 'chất cấm, thuốc lá', P4: 'động vật hoang dã', P5: 'nội dung người lớn',
    P6: 'hàng giả', P7: 'ảnh khác sản phẩm', W1: 'theo nhãn in trên bao bì', W2: 'thông tin ảnh không khớp',
    W3: 'chữ lạ ra lệnh trong ảnh/lời nói', W4: 'sai danh mục',
    W5: 'ảnh không phải của bạn', W6: 'ảnh do AI tạo', R1: 'đã có người xem lại', C4: 'tên hàng cấm' };
  // Sellers see the rule by its plain name; the rule codes stay in the log panel and the audit log.
  const rules = [...new Set((v.rules || []).filter((r) => r.id[0] !== 'C').map((r) => RULES_VI[r.id] || r.title))].join(', ');
  $('verdict').textContent = ({ pass: '✓ ', warn: '! ', block: '✕ ' }[v.verdict] || '') + summary
    + (rules && v.verdict !== 'pass' ? ` (theo quy định: ${rules})` : '');
  $('restyle').hidden = !run.studio_image || run.status !== 'ready';
  // A warning must be read before publishing (human in the loop); the server enforces the same rule.
  const needsAck = v.verdict === 'warn' && run.status !== 'blocked';
  $('ack-row').hidden = !needsAck;
  $('ack').checked = false;
  $('publish').disabled = run.status === 'blocked' || needsAck;
  const appeal = run.appeal || {};
  $('appeal-row').hidden = run.status !== 'blocked' && !appeal.status;
  $('appeal').hidden = run.status !== 'blocked' || appeal.status === 'pending' || appeal.status === 'upheld';
  $('appeal-note').hidden = $('appeal').hidden;
  $('appeal-status').textContent = { pending: 'Đã gửi, đang chờ người duyệt xem lại.',
    upheld: `Người duyệt giữ quyết định chặn: ${appeal.reason || ''}`,
    overturned: `Người duyệt cho phép đăng: ${appeal.reason || ''}` }[appeal.status] || '';
  $('ack').onchange = () => { $('publish').disabled = !$('ack').checked; };
  $('preview-error').textContent = run.status === 'blocked'
    ? 'Sản phẩm này không được đăng. Bạn có thể xoá tin, hoặc nhờ người duyệt xem lại nếu bạn thấy chặn nhầm.' : '';
}

$('restyle').addEventListener('click', async () => {
  const button = $('restyle');
  button.disabled = true;
  button.textContent = 'Đang vẽ nền mới…';
  $('preview-error').textContent = '';
  try {
    const run = await api(`/api/runs/${state.run.id}/restyle`, { method: 'POST' });
    state.run = run;
    renderLog(run);
    $('studio').src = `/files/runs/${run.id}/${run.studio_image}`;
    if (run.error) $('preview-error').textContent = 'Chưa đổi được nền: ' + run.error;
  } catch (err) { $('preview-error').textContent = err.message; }
  button.disabled = false;
  button.textContent = 'Đổi nền hợp với sản phẩm';
});

$('publish').addEventListener('click', async () => {
  $('preview-error').textContent = '';
  const num = (id) => $(id).value === '' ? null : Number($(id).value);
  const edits = { title: $('f-title').value, description: $('f-description').value,
                  price_vnd: num('f-price'), stock: num('f-stock'), acknowledge: $('ack').checked };
  try {
    const item = await api(`/api/runs/${state.run.id}/publish`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(edits) });
    $('done-summary').textContent = `${item.title} · ${money(item.price_vnd)} · còn ${item.stock}`;
    $('export-run').href = `/api/runs/${state.run.id}/export`;
    await renderShop();
    show('done');
    renderLog({ ...state.run, status: 'published' });
  } catch (err) { $('preview-error').textContent = err.message; }
});

async function renderShop() {
  const items = (await api('/api/catalog')).slice(-4).reverse();
  $('shop').replaceChildren(...items.map((item) => {
    const fig = document.createElement('figure');
    const img = document.createElement('img'); img.src = `/files/catalog/${item.image}`; img.alt = item.title;
    const cap = document.createElement('figcaption');
    const name = document.createElement('span'); name.textContent = item.title;
    const price = document.createElement('strong'); price.textContent = money(item.price_vnd);
    cap.append(name, price); fig.append(img, cap); return fig;
  }));
}

function reset() {
  state.photos.forEach((p) => URL.revokeObjectURL(p.url));
  Object.assign(state, { photos: [], audio: null, answerAudio: null, run: null });
  $('note').value = ''; $('heard').textContent = ''; $('capture-error').textContent = '';
  renderSlots();
  show('capture');
}
$('restart').addEventListener('click', reset);

async function deleteRun() {
  if (!state.run || !confirm('Xoá ảnh, ảnh studio, nhật ký và tin đã đăng (nếu có)? Không hoàn tác được.')) return;
  try {
    await api(`/api/runs/${state.run.id}`, { method: 'DELETE' });
    reset();
  } catch (err) { $('preview-error').textContent = err.message; }
}
$('delete-run').addEventListener('click', deleteRun);
$('appeal').addEventListener('click', async () => {
  try {
    state.run = await api(`/api/runs/${state.run.id}/appeal`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: $('appeal-note').value }) });
    fillPreview(state.run);
  } catch (err) { $('preview-error').textContent = err.message; }
});
$('delete-done').addEventListener('click', deleteRun);
$('another').addEventListener('click', reset);

// ---- agent log panel ------------------------------------------------------------------------

function renderLog(run) {
  const steps = $('steps');
  steps.replaceChildren(...run.steps.map((s) => {
    const li = document.createElement('li');
    li.className = `step ${s.status}`;
    const top = document.createElement('div'); top.className = 'step-top';
    const icon = document.createElement('span'); icon.className = 'step-status';
    icon.textContent = { done: '✓', failed: '✕', running: '…', skipped: '–', warn: '!' }[s.status] || '?';
    const title = document.createElement('span'); title.className = 'step-title'; title.textContent = s.title;
    top.append(icon, title);
    if (s.model) {
      const model = document.createElement('span'); model.className = 'model' + (s.mock ? ' mock' : '');
      model.textContent = s.model + (s.mock ? ' (mock)' : ''); top.append(model);
    }
    const meta = document.createElement('span'); meta.className = 'step-meta';
    meta.textContent = s.status === 'running' ? 'running' : `${s.ms} ms · $${s.cost_usd.toFixed(4)}`;
    top.append(meta);
    li.append(top);
    if (s.error) { const err = document.createElement('div'); err.className = 'err'; err.textContent = s.error; li.append(err); }
    if (s.endpoint || s.input || s.output) {
      const details = document.createElement('details');
      const summary = document.createElement('summary'); summary.textContent = s.endpoint || 'details';
      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify({ input: s.input, output: s.output }, null, 2);
      details.append(summary, pre); li.append(details);
    }
    // Progress list on the phone mirrors the log.
    return li;
  }));
  // The phone shows the seller plain Vietnamese steps; the log panel keeps the technical titles, models and costs.
  const SELLER_STEP = { prepare: 'Chuẩn bị ảnh', transcribe: 'Nghe lời bạn nói', background: 'Chuẩn bị phòng chụp',
    listing: 'Soạn tin từ ảnh và lời nói', verify: 'Kiểm tra tin với ảnh', segment: 'Tách sản phẩm khỏi nền',
    compose: 'Ghép ảnh đẹp', privacy: 'Ẩn số điện thoại, liên kết', injection: 'Bỏ qua câu ra lệnh lạ',
    origin: 'Kiểm tra nguồn ảnh', backstop: 'Kiểm tra hàng cấm', fallback: 'Không nghe rõ, dùng chữ bạn gõ',
    'transcribe-answer': 'Nghe câu trả lời', answer: 'Cập nhật giá, số lượng', restyle: 'Vẽ phòng chụp mới' };
  $('progress').replaceChildren(...run.steps.map((s) => {
    const li = document.createElement('li'); li.className = s.status;
    li.textContent = SELLER_STEP[s.key] || s.title; return li;
  }));
  if (run.status === 'running') {
    $('eta').textContent = `Thường mất khoảng 20 giây · đã ${Math.round(run.totals.seconds)} giây`;
  }
  const t = run.totals;
  $('totals').innerHTML = '';
  for (const [label, value] of [['time', `${t.seconds} s`], ['model calls', t.model_calls],
    ['cost', `$${t.cost_usd.toFixed(4)} ≈ ${Math.round(t.cost_usd * USD_TO_VND).toLocaleString('vi-VN')}đ`],
    ['status', run.status]]) {
    const span = document.createElement('span'); span.append(`${label} `);
    const b = document.createElement('b'); b.textContent = value; span.append(b); $('totals').append(span);
  }
}

(async () => {
  renderSlots();
  try {
    const health = await api('/api/health');
    $('mode').textContent = health.mode === 'live' ? 'LIVE · Meta Model API' : 'MOCK · no MODEL_API_KEY';
    $('mode').className = `mode ${health.mode}`;
  } catch { $('mode').textContent = 'server offline'; }
})();
