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
      const label = button.querySelector('.mic-label');
      const idle = label ? label.textContent : '';
      const t0 = Date.now();
      const tick = setInterval(() => {
        if (!recorder) { clearInterval(tick); if (label) label.textContent = idle; return; }
        if (label) label.textContent = `Đang nghe… ${Math.floor((Date.now() - t0) / 1000)} giây · thả tay để gửi`;
      }, 250);
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
  if (run.status === 'needs_answer' && run.processing) {
    // asked early: show the question now, keep the log moving while the agent works on
    if ($('screen-question').hidden) route(run);
    state.polling = setTimeout(async () => {
      const r = await api(`/api/runs/${run.id}`);
      if (!$('screen-question').hidden && r.id === state.run.id) follow(r);
    }, 600);
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
  // The headline is fixed per verdict, so the same situation always reads the same; the model's own sentence
  // follows as the detail, and the fixed per-rule lines below say what happened and what to do.
  const HEADLINE = { pass: '✓ Tin đăng khớp với ảnh.', warn: '! Trợ lý đã chỉnh tin, bạn xem lại trước khi đăng.',
    block: '✕ Sản phẩm này không đăng được trên Chợ Z.' };
  $('verdict').replaceChildren(document.createTextNode(HEADLINE[v.verdict] || summary));
  if (v.verdict === 'pass' && summary) {
    const detail = document.createElement('small'); detail.className = 'verdict-detail'; detail.textContent = summary;
    $('verdict').append(detail);
  }
  // One fixed line per rule that applied: what happened and what to do, in plain Vietnamese, the same every time.
  // The rule codes stay in the log panel and the audit log, not on the seller's screen.
  const ADVICE = {
    P1: 'Thuốc cần đơn bác sĩ không được bán trên Chợ Z.',
    P2: 'Vũ khí, pháo nổ không được bán trên Chợ Z.',
    P3: 'Chất cấm, thuốc lá, thuốc lá điện tử không được bán.',
    P4: 'Động vật hoang dã và sản phẩm từ chúng không được bán.',
    P5: 'Nội dung người lớn không được đăng.',
    P6: 'Hàng có dấu hiệu giả thương hiệu không được đăng.',
    P7: 'Ảnh là một món hàng khác với tin đăng.',
    W1: 'Khối lượng hoặc kích cỡ in trên bao bì khác với lời nói hoặc bảng giá; tin ghi theo bao bì. Bạn xem lại rồi đánh dấu đã đọc để đăng.',
    W2: 'Một vài thông tin không khớp với ảnh đã được sửa. Bạn xem lại tin nhé.',
    W3: 'Trên ảnh hoặc trong lời nói có dòng chữ lạ đòi đổi giá hay duyệt tin. Trợ lý không làm theo; giá và tin vẫn theo lời bạn.',
    W4: 'Danh mục đã được chỉnh cho đúng món hàng.',
    W5: 'Ảnh có vẻ lấy của người khác (dấu bản quyền, ảnh chụp màn hình). Bạn chụp giúp ảnh món hàng bạn có.',
    W6: 'Ảnh mang dấu ảnh do AI tạo. Bạn chụp giúp ảnh thật của món hàng.',
    W7: 'Giá khác xa giá bán lẻ đề xuất của nhà sản xuất. Bạn xem lại có gõ nhầm số không.',
    R1: 'Người duyệt đã xem lại tin này.',
  };
  // Catalog grounding (app/kb.py): the matched product and the producer's suggested price; the seller decides the price.
  const cat = (l.catalog || {});
  const vnd = (n) => `${Number(n).toLocaleString('vi-VN')}đ`;
  $('catalog').hidden = !cat.id;
  if (cat.id) {
    const retail = cat.suggested_price_vnd;
    $('catalog-text').textContent = `Khớp danh mục ${cat.brand}: ${cat.name}` + (retail ? ` · giá lẻ đề xuất ${vnd(retail)}` : '');
    $('use-price').hidden = !retail || $('f-price').value !== '';
    $('use-price').textContent = retail ? `Dùng giá ${vnd(retail)}` : '';
    $('use-price').onclick = () => { $('f-price').value = retail; $('f-price').dispatchEvent(new Event('input')); $('use-price').hidden = true; };
  }
  // Seller memory: this product was listed before; by default the old listing is updated, not duplicated.
  const hist = l.history || {};
  $('history').hidden = !hist.id;
  if (hist.id) {
    const [y, mo, dd] = (hist.published_at || '').slice(0, 10).split('-');
    const today = new Date().toISOString().slice(0, 10) === (hist.published_at || '').slice(0, 10);
    $('history-text').textContent = `Bạn đã đăng sản phẩm này ${today ? 'hôm nay' : `ngày ${+dd}/${+mo}/${y}`}`
      + (hist.price_vnd ? ` (giá ${vnd(hist.price_vnd)})` : '') + '. Cập nhật tin cũ thay vì đăng tin mới'
      + (hist.stock != null && l.stock != null && hist.stock !== l.stock ? `: còn ${hist.stock} → ${l.stock}.` : '.');
    if (hist.title) $('f-title').value = hist.title;          // the same listing keeps its name
    $('update-old').checked = true;
    if ($('f-price').value === '' && hist.price_vnd) { $('use-price').hidden = false; $('use-price').textContent = `Dùng giá lần trước ${vnd(hist.price_vnd)}`; $('use-price').onclick = () => { $('f-price').value = hist.price_vnd; $('f-price').dispatchEvent(new Event('input')); $('use-price').hidden = true; }; $('catalog').hidden = false; }
  }
  // A weight warning names the numbers and the source the listing follows.
  const wn = l.weight_note;
  const ids = [...new Set([...(v.rule_ids || []), ...(v.rules || []).map((r) => r.id)])].filter((id) => ADVICE[id]);
  $('advice').replaceChildren(...(v.verdict === 'pass' ? [] : ids).map((id) => {
    const li = document.createElement('li'); li.className = id[0] === 'P' ? 'block' : 'warn';
    li.textContent = (id === 'W1' && wn)
      ? `Bạn nói ${wn.said_g}g · bao bì in ${wn.listed_g}g → tin ghi ${wn.listed_g}g theo bao bì. Bạn xem lại rồi đánh dấu đã đọc để đăng.`
      : ADVICE[id];
    return li;
  }));
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
  const hint = () => { $('publish-hint').hidden = !(needsAck && !$('ack').checked); };
  $('ack').onchange = () => { $('publish').disabled = !$('ack').checked; hint(); };
  hint();
  // money as sellers write it: 22.000 đ
  const fmt = () => { const n = Number($('f-price').value); $('price-fmt').textContent = n ? `${n.toLocaleString('vi-VN')} đ` : '(đ)'; };
  $('f-price').oninput = fmt; fmt();
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
  const hist = (state.run.listing || {}).history;
  if (hist && hist.id && $('update-old').checked) edits.update_of = hist.id;
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
    // Evidence in the model's own words, under its step: what Spark found contradicted, the text it refused to obey.
    const out = s.output || {};
    const evidence = [...(out.contradicted || []), ...(s.key === 'injection' && out.in_photos ? [`quoted, not obeyed: “${out.in_photos}”`] : [])];
    for (const line of evidence) {
      const ev = document.createElement('div'); ev.className = 'evidence'; ev.textContent = line; li.append(ev);
    }
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
    'transcribe-answer': 'Nghe câu trả lời', answer: 'Cập nhật giá, số lượng', restyle: 'Vẽ phòng chụp mới',
    retrieve: 'Tìm sản phẩm trong danh mục', match: 'Đối chiếu với danh mục', memory: 'Xem tin cũ của bạn',
    ask: 'Hỏi bạn số lượng', 'answer-held': 'Đã nhận câu trả lời' };
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
