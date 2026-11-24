(() => {
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const form = $('#settings-form');
const tags = {};            // name -> tag input controller
let saved = null;           // last settings received from the server
let dirty = false;
let state = null;
let wasPolling = false;

/* ───────── helpers ───────── */
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v; else e.setAttribute(k, v);
  }
  kids.flat().forEach(k => k != null && e.append(k));
  return e;
}
let toastTimer;
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 4200);
}
async function api(url, method = 'GET', body) {
  const opts = { method };
  if (method !== 'GET') { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body ?? {}); }
  const r = await fetch(url, opts);
  let data = {}; try { data = await r.json(); } catch { /* non-JSON error page */ }
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}
function rel(iso) {
  if (!iso) return '—';
  const sec = (new Date(iso) - Date.now()) / 1000, a = Math.abs(sec);
  const fmt = (n, u) => { const x = Math.round(n); return sec < 0 ? `${x}${u} ago` : `in ${x}${u}`; };
  if (a < 45) return sec < 0 ? 'just now' : 'in <1m';
  if (a < 3600) return fmt(a / 60, 'm');
  if (a < 86400) return fmt(a / 3600, 'h');
  return fmt(a / 86400, 'd');
}

/* ───────── tag inputs ───────── */
function makeTags(box) {
  let items = [];
  const input = el('input', { type: 'text', placeholder: box.dataset.ph || '', 'aria-label': box.dataset.name });
  box.append(input);
  const render = () => {
    $$('.tag', box).forEach(t => t.remove());
    items.forEach((t, i) => {
      const x = el('button', { type: 'button', 'aria-label': 'Remove ' + t, text: '×' });
      x.onclick = () => { items.splice(i, 1); render(); changed(); };
      box.insertBefore(el('span', { class: 'tag' }, document.createTextNode(t), x), input);
    });
  };
  const add = text => {
    text.split(/[,\n]/).map(s => s.trim()).filter(Boolean).forEach(s => {
      if (!items.some(y => y.toLowerCase() === s.toLowerCase())) items.push(s);
    });
    render(); changed();
  };
  const flush = () => { if (input.value.trim()) { add(input.value); input.value = ''; } };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); flush(); }
    else if (e.key === 'Backspace' && !input.value && items.length) { items.pop(); render(); changed(); }
  });
  input.addEventListener('blur', flush);
  input.addEventListener('paste', e => {
    const t = (e.clipboardData || window.clipboardData).getData('text');
    if (/[,\n]/.test(t)) { e.preventDefault(); add(t); }
  });
  box.addEventListener('click', () => input.focus());
  return { get: () => [...items], set: v => { items = [...v]; render(); }, flush };
}
$$('.tags').forEach(b => { tags[b.dataset.name] = makeTags(b); });

/* ───────── settings form ───────── */
function fill(s) {
  saved = s;
  for (const [name, t] of Object.entries(tags)) t.set(s[name] || []);
  for (const f of $$('input[name],select[name]', form)) {
    const n = f.name;
    if (n === 'experience_levels') f.checked = (s.experience_levels || []).includes(f.value);
    else if (f.type === 'checkbox') f.checked = !!s[n];
    else if (n === 'poll_minutes') f.value = Math.round(s.poll_seconds / 60);
    else if (n === 'smtp_pass') f.value = '';
    else if (n in s) f.value = s[n];
  }
  $('#pass-hint').textContent = s.smtp_pass_set ? '✓ saved' : 'not set';
  setDirty(false);
  updateRateHint();
}
function read() {
  Object.values(tags).forEach(t => t.flush());
  const out = {};
  for (const [name, t] of Object.entries(tags)) out[name] = t.get();
  out.experience_levels = $$('input[name=experience_levels]:checked', form).map(c => c.value);
  for (const f of $$('input[name],select[name]', form)) {
    const n = f.name;
    if (n === 'experience_levels') continue;
    if (f.type === 'checkbox') out[n] = f.checked;
    else if (n === 'poll_minutes') out.poll_seconds = Math.round(parseFloat(f.value || '0') * 60);
    else out[n] = f.value;
  }
  return out;
}
function setDirty(v, msg) {
  dirty = v;
  const bar = $('#savebar'); bar.hidden = !v; bar.classList.remove('bad');
  $('#save-msg').textContent = msg || 'Unsaved changes';
}
function changed() { setDirty(true); updateRateHint(); }
function updateRateHint() {
  const n = tags.searches.get().length, mins = parseFloat($('[name=poll_minutes]').value) || 10;
  $('#rate-hint').textContent = `${n} search${n === 1 ? '' : 'es'} · ≈ ${Math.round(n * 60 / mins)} Upwork requests/hour`;
}
form.addEventListener('input', changed);
form.addEventListener('change', changed);
form.addEventListener('submit', async e => {
  e.preventDefault();
  const btn = $('#save-btn'); btn.disabled = true;
  try {
    fill(await api('/api/settings', 'POST', read()));
    toast('Settings saved');
    refreshState();
  } catch (err) {
    setDirty(true, err.message); $('#savebar').classList.add('bad');
  } finally { btn.disabled = false; }
});
$('#discard-btn').onclick = () => saved && fill(saved);
window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

/* ───────── tabs ───────── */
function showTab(name) {
  if (!$('#pane-' + name)) name = 'jobs';
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.pane').forEach(p => { p.hidden = p.id !== 'pane-' + name; });
  history.replaceState(null, '', '#' + name);
}
$$('.tab').forEach(t => t.onclick = () => showTab(t.dataset.tab));
showTab(location.hash.slice(1) || 'jobs');

/* ───────── status / connection ───────── */
function renderState(s) {
  state = s;
  const banners = $('#banners'); banners.replaceChildren();
  const banner = (cls, text) => banners.append(el('div', { class: 'alert ' + cls, text }));
  if (s.connect_error) banner('err', 'Connection failed: ' + s.connect_error);
  if (s.last_error && !s.needs_reauth) banner('err', 'Last error: ' + s.last_error);
  if (s.paused_until) banner('warn', 'Checks paused until ' + new Date(s.paused_until).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ' after an Upwork rate limit.');
  if (s.email_enabled && !s.smtp_configured) banner('warn', "Email isn't set up yet — add your SMTP details under “Email & schedule” so alerts can be sent.");

  const conn = $('#conn'), actions = $('#conn-actions'); actions.replaceChildren();
  conn.className = 'card conn';
  if (s.connecting) {
    $('#conn-title').textContent = 'Connecting…';
    $('#conn-sub').textContent = 'Finish signing in on the Upwork page.';
  } else if (s.connected && s.needs_reauth) {
    conn.classList.add('warn');
    $('#conn-title').textContent = 'Reconnect needed';
    $('#conn-sub').textContent = 'Upwork asked for a fresh sign-in. The app keeps retrying on its own, but reconnecting fixes it right away.';
    actions.append(el('a', { class: 'btn primary', href: '/connect', text: 'Reconnect →' }));
  } else if (s.connected) {
    conn.classList.add('on');
    $('#conn-title').textContent = 'Connected to Upwork';
    $('#conn-sub').textContent = `${s.account || 'Freelancer account'} · checking every ${Math.round(s.poll_seconds / 60)} min · stays connected across restarts`;
    const b = el('button', { class: 'btn danger', id: 'disconnect-btn', text: 'Disconnect' });
    b.onclick = async () => {
      if (!confirm('Disconnect from Upwork? You will need to sign in again to resume alerts.')) return;
      await api('/api/disconnect', 'POST'); refreshState();
    };
    actions.append(b);
  } else {
    $('#conn-title').textContent = 'Not connected';
    $('#conn-sub').textContent = 'Sign in with Upwork in your browser — one time only, no API keys or tokens needed.';
    actions.append(el('a', { class: 'btn primary', id: 'connect-btn', href: '/connect', text: 'Connect Upwork →' }));
  }

  $('#st-found').textContent = s.last_found ?? '—';
  $('#st-sent').textContent = s.emails_sent ?? 0;
  $('#st-last').textContent = rel(s.last_poll);
  $('#st-next').textContent = !s.connected ? '—' : s.polling ? 'Checking…' : rel(s.next_poll);
  const pb = $('#poll-btn'); pb.disabled = !s.connected || s.polling; pb.textContent = s.polling ? 'Checking…' : 'Check now';
}
async function refreshState() {
  try { renderState(await api('/api/state')); } catch { /* server restarting; try again next tick */ }
}
async function tick() {
  await refreshState();
  if (state) {
    if (wasPolling && !state.polling) {
      const r = state.last_result || {};
      toast(r.error ? 'Check failed: ' + r.error : `Checked Upwork: ${r.found ?? 0} jobs, ${r.new_emailed ?? 0} emailed`);
      loadJobs();
    }
    wasPolling = state.polling;
  }
  setTimeout(tick, state && (state.polling || state.connecting) ? 2000 : 15000);
}
$('#poll-btn').onclick = async () => {
  try {
    const r = await api('/api/poll', 'POST');
    if (r.skipped) toast(r.skipped);
    else if (r.error) toast('Error: ' + r.error);
    else if (r.started) { toast('Checking Upwork…'); wasPolling = true; refreshState(); }
    else { toast(`Checked Upwork: ${r.found ?? 0} jobs, ${r.new_emailed ?? 0} emailed`); loadJobs(); refreshState(); }
  } catch (e) { toast(e.message); }
};
$('#test-email-btn').onclick = async e => {
  const b = e.currentTarget; b.disabled = true;
  try {
    const r = await api('/api/test-email', 'POST');
    toast('Test email sent to ' + r.sent_to);
  } catch (err) { toast('Test email failed: ' + err.message); } finally { b.disabled = false; }
};

/* ───────── jobs list ───────── */
const LABEL = { emailed: 'Emailed', matched: 'Matched', filtered: 'Filtered out', failed: 'Email failed', seeded: 'Existing', seen: 'Seen' };
let jobsShown = 0;
function jobCard(j) {
  const safeUrl = /^https:\/\//.test(j.url) ? j.url : '';
  const title = safeUrl ? el('a', { class: 'job-title', href: safeUrl, target: '_blank', rel: 'noopener noreferrer', text: j.title })
                        : el('span', { class: 'job-title', text: j.title });
  const meta = [j.budget, j.country, j.spent && `${j.spent} spent`, j.verified ? '✅ verified' : null,
                j.query && `via “${j.query}”`, rel(j.published_at || j.found_at)].filter(Boolean);
  return el('div', { class: 'job' },
    el('div', { class: 'job-top' }, title, el('span', { class: 'pill p-' + j.status, text: LABEL[j.status] || j.status })),
    el('div', { class: 'job-meta' }, meta.map(m => el('span', { text: m }))),
    j.reason && j.status !== 'seeded' ? el('div', { class: 'job-reason', text: j.reason }) : null,
    j.skills.length ? el('div', { class: 'job-skills' }, j.skills.map(s => el('span', { text: s }))) : null);
}
async function loadJobs(more = false) {
  const list = $('#job-list'), btn = $('#more-btn');
  if (!more) jobsShown = 0;
  try {
    const status = $('#job-status').value;
    const r = await api(`/api/jobs?limit=50&offset=${jobsShown}` + (status ? '&status=' + status : ''));
    if (!more) list.replaceChildren();
    r.jobs.forEach(j => list.append(jobCard(j)));
    jobsShown += r.jobs.length;
    if (!jobsShown) list.append(el('div', { class: 'empty', text: status ? 'No jobs with this status yet.' : 'No jobs yet — they appear here after the first check.' }));
    btn.hidden = jobsShown >= r.total;
  } catch (e) { toast(e.message); }
}
$('#job-status').onchange = () => loadJobs();
$('#more-btn').onclick = () => loadJobs(true);

/* ───────── boot ───────── */
(async () => {
  try { fill(await api('/api/settings')); } catch (e) { toast('Could not load settings: ' + e.message); }
  loadJobs();
  tick();
})();
})();
