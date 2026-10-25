// Judge demo controller: Change drawing -> Run Argon -> Inspect result.
// Every number shown after a run comes from that run's API response; nothing is invented client-side.

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 1500;
const ACTIVE = new Set(['QUEUED', 'RUNNING']);
const DONE = new Set(['COMPLETED', 'FAILED']);

const COPY = {
  failure: 'Argon could not verify this drawing. No recommendation is issued.',
  noChange: 'The drawing and twin agree. No cable model repair was needed.',
  noSafe: 'The twin was checked, but no modeled 8 A transfer is safe.',
  running: 'Argon is comparing the new diagram with the old twin render.',
};
const SOURCE_LABEL = {
  full_live_two_agents: 'Full live run: Builder + Critic on Argon',
  prepared_builder_live_critic: 'Builder prepared · Critic live on Argon',
  recorded_live_replay: 'Recorded live run (not from a new drawing)',
  mock_fixture: 'Offline rehearsal · not Argon',
};
const VISUAL_LABEL = {
  REPAIRED: ['Twin repaired', 'pill-ok'],
  NO_CHANGE: ['No repair needed', 'pill-ok'],
  ABSTAINED: ['Argon abstained', 'pill-warn'],
  REJECTED: ['Not verified', 'pill-bad'],
};
const ACTION_LABEL = {
  SAFE_RECEIVER_FOUND: ['Safe receiver found', 'pill-ok'],
  MULTIPLE_SAFE: ['Several safe receivers', 'pill-ok'],
  NO_SAFE_ACTION: ['No safe 8 A transfer', 'pill-bad'],
  INSUFFICIENT_EVIDENCE: ['No recommendation', 'pill-muted'],
};

const S = {
  config: null,
  view: null,
  runId: null,
  pollTimer: 0,
  tickTimer: 0,
  viewer: null,
  fallback: false,
  scenes: {},
  showing: 'before',
  animatedFor: null,
  actions: null,
  actionsFor: null,
  staleGraph: null,
};

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtA = (v) => (Number.isFinite(v) ? `${Number(v).toFixed(0)}` : '—');
const fmtC = (v) => (Number.isFinite(v) ? `${Number(v).toFixed(2)} °C` : '—');
const short = (v, n = 12) => (v ? (String(v).length > n + 2 ? `${String(v).slice(0, n)}…` : String(v)) : '—');
const assetUrl = (runId, name, bust) => `/api/live/runs/${encodeURIComponent(runId)}/assets/${name}${bust ? `?v=${encodeURIComponent(bust)}` : ''}`;

async function api(path, opts = {}) {
  const res = await fetch(path, { cache: 'no-store', ...opts, headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  let body = {};
  try { body = await res.json(); } catch { body = {}; }
  return { status: res.status, ok: res.ok, body };
}

function setHeadline(text, tone = '') {
  const h = $('#headline');
  h.textContent = text;
  h.className = `headline${tone ? ` is-${tone}` : ''}`;
}

function setUrl(runId) {
  const url = new URL(location.href);
  if (runId) url.searchParams.set('run', runId); else url.searchParams.delete('run');
  history.replaceState(null, '', url);
}

function graphFrom(scene) {
  const g = {};
  for (const e of (scene && scene.edges) || []) g[e.rack_id] = e.feeder_id;
  return g;
}

function sceneFromGraph(graph, tempC) {
  return {
    edges: Object.entries(graph).map(([rack_id, feeder_id]) => ({ rack_id, feeder_id })),
    hotspot: Number.isFinite(tempC) ? { rack_id: 'N4', temperature_c: tempC } : null,
  };
}

// ------------------------------------------------------------------ tabs
function initTabs() {
  const tabs = [...document.querySelectorAll('.tab')];
  tabs.forEach((t) => t.addEventListener('click', () => {
    tabs.forEach((x) => {
      const on = x === t;
      x.classList.toggle('is-on', on);
      x.setAttribute('aria-selected', String(on));
      document.getElementById(x.getAttribute('aria-controls')).hidden = !on;
    });
  }));
}

// ------------------------------------------------------------------ 3D viewer / fallback
async function initViewer() {
  const host = $('#viewer');
  try {
    const mod = await import('./viewer3d.js');
    S.viewer = mod.createViewer(host, { onContextLost: () => enterFallback('The 3D context was lost.') });
    $('#viewerLoading').hidden = true;
  } catch (err) {
    enterFallback('3D is unavailable on this device.');
  }
}

function enterFallback(reason) {
  S.fallback = true;
  if (S.viewer) { try { S.viewer.dispose(); } catch { /* ignore */ } S.viewer = null; }
  $('#viewerLoading').hidden = true;
  $('#fallback').hidden = false;
  $('#fallbackCaption').dataset.reason = reason;
  updateFallbackImage();
}

function updateFallbackImage() {
  if (!S.fallback) return;
  const img = $('#fallbackImg');
  const cap = $('#fallbackCaption');
  const reason = cap.dataset.reason || '3D is unavailable on this device.';
  const runId = S.runId;
  const hasRender = runId && (S.scenes.scene0 || S.view?.recorded);
  if (!hasRender) {
    // The old twin is the same for every run, so the recorded live1 render0 shows it faithfully.
    img.hidden = false;
    img.src = assetUrl('live1', 'render0/render_wiring.png');
    cap.textContent = `${reason} Showing the old twin as rendered in recorded run live1.`;
    return;
  }
  const which = S.showing === 'after' && S.scenes.scene1 ? 'render1' : 'render0';
  img.hidden = false;
  img.src = assetUrl(runId, `${which}/render_wiring.png`);
  cap.textContent = `${reason} Showing the real ${which} wiring image from run ${runId}.`;
}

function showScene(which, { animate = false } = {}) {
  S.showing = which;
  const before = S.scenes.scene0 || (S.staleGraph ? sceneFromGraph(S.staleGraph, S.config?.stale_twin?.n4_temp_now_c) : null);
  const after = S.scenes.scene1;
  $('#showOld').classList.toggle('is-on', which === 'before');
  $('#showOld').setAttribute('aria-pressed', String(which === 'before'));
  $('#showNew').classList.toggle('is-on', which === 'after');
  $('#showNew').setAttribute('aria-pressed', String(which === 'after'));
  $('#sceneLabel').textContent = which === 'after' ? 'Repaired twin (scene1)' : S.scenes.scene0 ? 'Old twin (scene0)' : 'Old twin';
  if (S.viewer) {
    if (which === 'after' && after) {
      S.viewer.setScene(after, { animateFrom: animate ? graphFrom(before) : null });
    } else if (before) {
      S.viewer.setScene(before);
    }
    applyActionToViewer();
  }
  updateFallbackImage();
}

// ------------------------------------------------------------------ config + initial old-twin story
async function loadConfig() {
  const { body } = await api('/api/live/config');
  S.config = body;
  const badge = $('#liveBadge');
  if (body.live_available) {
    badge.textContent = 'Argon live';
    badge.className = 'pill pill-ok pill-live';
  } else {
    badge.textContent = 'Argon offline';
    badge.className = 'pill pill-muted';
  }
  S.staleGraph = body.stale_twin?.graph || null;
  if (!body.recorded_run_id) $('#recordedLink').hidden = true;
  renderModes();
  if (body.judge_code_supported) $('#judgeCodeBox').hidden = false;
}

function renderModes() {
  const box = $('#modeChoice');
  box.querySelectorAll('label').forEach((l) => l.remove());
  const modes = S.config?.modes || {};
  const order = ['full', 'fast', 'mock'];
  let def = S.config?.default_mode || 'full';
  if (!modes[def]?.available) def = order.find((m) => modes[m]?.available) || def;
  for (const m of order) {
    const info = modes[m];
    if (!info) continue;
    if (m === 'mock' && !info.available) continue;
    const label = document.createElement('label');
    label.className = 'radio';
    label.innerHTML = `<input type="radio" name="mode" value="${m}" ${m === def ? 'checked' : ''} ${info.available ? '' : 'disabled'}><span>${esc(info.label)}<small>${m === 'mock' ? 'Plumbing check only' : `Typically ~${info.typical_s} s`}</small></span>`;
    box.appendChild(label);
  }
  const q = S.config?.quota;
  if (S.config?.live_available && q) {
    $('#runMsg').textContent = `Public live runs left: ${q.public_remaining}.`;
  } else if (!S.config?.live_available) {
    $('#runMsg').textContent = 'Live Argon is not available on this server right now. You can still open the recorded live run below.';
  }
}

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked')?.value || 'full';
}

function staleStory() {
  const st = S.config?.stale_twin || {};
  const rec = st.recommended_receiver || 'N3';
  setHeadline(`Old twin suggests ${rec}.`);
  $('#subline').textContent = `N4's rear connector reads ${Number.isFinite(st.n4_temp_now_c) ? st.n4_temp_now_c.toFixed(1) : '—'} °C (limit ${st.max_temp_c ?? 90} °C). The operator must move ${st.action_size_a ?? 8} A of workload off N4. Is the wiring map still correct?`;
}

// ------------------------------------------------------------------ step 1: drawing
async function simulateDrawing() {
  const btn = $('#simulateBtn');
  const drawing = document.querySelector('input[name="drawing"]:checked')?.value || 'B';
  btn.disabled = true;
  btn.classList.add('is-busy');
  try {
    const { status, body } = await api('/api/live/challenges', { method: 'POST', body: JSON.stringify({ drawing }) });
    if (status !== 201) {
      $('#sldInfo').textContent = body.error || `Could not create a drawing (HTTP ${status}).`;
      return;
    }
    resetRunUi();
    S.runId = body.run_id;
    S.view = body;
    setUrl(S.runId);
    renderView(body);
  } catch {
    $('#sldInfo').textContent = 'Network error while creating the drawing.';
  } finally {
    btn.disabled = false;
    btn.classList.remove('is-busy');
  }
}

function resetRunUi() {
  clearTimeout(S.pollTimer);
  S.scenes = {};
  S.animatedFor = null;
  S.actions = null;
  S.actionsFor = null;
  S.chosen = undefined;
  $('#result').hidden = true;
  $('#resultEmpty').hidden = false;
  $('#stageRail').innerHTML = '';
  $('#railBox').hidden = true;
  $('#eventLog').innerHTML = '';
  setLegend(null);
  $('#showNew').disabled = true;
  if (S.viewer) S.viewer.setLoads(null);
  showScene('before');
}

function renderDrawing(view) {
  const img = $('#sldImg');
  const btn = $('#sldBtn');
  const d = view.drawing || {};
  if (!d.url) return;
  const src = `${d.url}?v=${encodeURIComponent((d.sha256 || '').slice(0, 16))}`;
  if (img.getAttribute('src') !== src) {
    img.src = src;
    btn.classList.toggle('is-new', !view.recorded);
  }
  img.hidden = false;
  $('#sldEmpty').hidden = true;
  btn.disabled = false;
  const no = d.drawing_no ? `Drawing ${d.drawing_no} · ` : '';
  $('#sldInfo').textContent = `${no}SHA-256 ${short(d.sha256, 16)}`;
  const sel = view.selection?.drawing;
  if (sel === 'A' || sel === 'B') {
    const r = document.querySelector(`input[name="drawing"][value="${sel}"]`);
    if (r) r.checked = true;
  }
}

// ------------------------------------------------------------------ step 2: run
async function startRun() {
  if (!S.runId || S.view?.recorded) return;
  const btn = $('#runBtn');
  const mode = selectedMode();
  btn.disabled = true;
  btn.classList.add('is-busy');
  const payload = { run_id: S.runId, mode };
  const code = $('#judgeCode')?.value?.trim();
  if (code) payload.judge_code = code;
  try {
    const { status, body } = await api('/api/live/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
      headers: { 'Idempotency-Key': `${S.runId}:${mode}` },
    });
    if (status === 202 || status === 200) {
      S.view = body;
      renderView(body);
      schedulePoll(250);
      return;
    }
    let msg = body.error || `Run could not start (HTTP ${status}).`;
    if (status === 409 && body.active_run_id) msg += ` Watch it: /live?run=${body.active_run_id}`;
    $('#runMsg').textContent = msg;
    if (status === 503) setHeadline(COPY.failure, 'bad');
  } catch {
    $('#runMsg').textContent = 'Network error while starting the run.';
  } finally {
    btn.classList.remove('is-busy');
    updateButtons();
  }
}

function schedulePoll(ms = POLL_MS) {
  clearTimeout(S.pollTimer);
  S.pollTimer = setTimeout(poll, ms);
}

async function poll() {
  if (!S.runId) return;
  try {
    const { status, body } = await api(`/api/live/runs/${encodeURIComponent(S.runId)}`);
    if (status === 200) {
      S.view = body;
      renderView(body);
    }
  } catch { /* transient network error: keep polling */ }
  if (S.view && ACTIVE.has(S.view.state)) schedulePoll();
}

async function loadRun(runId) {
  S.runId = runId;
  const { status, body } = await api(`/api/live/runs/${encodeURIComponent(runId)}`);
  if (status !== 200) {
    S.runId = null;
    setUrl(null);
    staleStory();
    $('#runMsg').textContent = 'That run was not found. Create a new drawing to start.';
    return;
  }
  S.view = body;
  renderView(body);
  if (ACTIVE.has(body.state)) schedulePoll();
}

// ------------------------------------------------------------------ rendering a run view
function updateButtons() {
  const v = S.view;
  const state = v?.state;
  const busy = state && ACTIVE.has(state);
  const modeOk = S.config?.modes?.[selectedMode()]?.available;
  $('#runBtn').disabled = !(v && state === 'DRAWING_READY' && !v.recorded && modeOk);
  $('#simulateBtn').disabled = !!busy;
  document.querySelectorAll('input[name="drawing"], input[name="mode"]').forEach((i) => {
    if (i.name === 'mode' && !S.config?.modes?.[i.value]?.available) return;
    i.disabled = !!busy;
  });
  $('#step1').classList.toggle('is-done', !!v);
  $('#step2').classList.toggle('is-active', !!busy);
  $('#step2').classList.toggle('is-done', !!(v && DONE.has(state)));
  $('#step3').classList.toggle('is-active', !!(v && DONE.has(state)));
  $('#agentsDot').hidden = !busy;
}

function renderView(v) {
  renderDrawing(v);
  $('#runTag').hidden = false;
  $('#runTag').textContent = v.recorded ? `${v.run_id} · recorded` : v.run_id;
  updateButtons();
  renderRail(v);
  renderAgents(v);
  if (v.state === 'DRAWING_READY') {
    const rec = S.config?.stale_twin?.recommended_receiver || 'N3';
    setHeadline(`Old twin suggests ${rec}. Checking today's drawing…`);
    $('#subline').textContent = 'A new as-built drawing was generated. The old twin is unchanged. Ask Argon to compare them.';
    if (!S.config?.live_available && !S.config?.modes?.mock?.available) {
      $('#runMsg').textContent = 'Live Argon is not available on this server right now. No run can be started.';
    }
  } else if (ACTIVE.has(v.state)) {
    setHeadline(COPY.running);
    $('#subline').textContent = v.stage ? `Now: ${v.stage}.` : '';
    startTick();
    maybeLoadScene0(v);
  }
  if (DONE.has(v.state)) {
    stopTick();
    renderDone(v);
  }
  renderEvidence(v);
}

function startTick() {
  if (S.tickTimer) return;
  S.tickTimer = setInterval(() => {
    const v = S.view;
    if (!v || !ACTIVE.has(v.state)) return stopTick();
    const t0 = Date.parse(v.started_at || v.queued_at || '');
    if (Number.isFinite(t0)) {
      const s = Math.max(0, Math.round((Date.now() - t0) / 1000));
      $('#runMsg').textContent = `${v.state === 'QUEUED' ? 'Queued' : 'Running'} for ${s} s (wall clock). Mode: ${SOURCE_LABEL[{ full: 'full_live_two_agents', fast: 'prepared_builder_live_critic', mock: 'mock_fixture' }[v.mode]] || v.mode}.`;
    }
  }, 1000);
}
function stopTick() { clearInterval(S.tickTimer); S.tickTimer = 0; }

function relTimes(stages) {
  const first = stages.map((s) => Date.parse(s.at_utc || '')).find(Number.isFinite);
  return (s) => {
    const t = Date.parse(s.at_utc || '');
    return Number.isFinite(t) && Number.isFinite(first) ? `+${Math.round((t - first) / 1000)} s` : '';
  };
}

function renderRail(v) {
  const stages = (v.stages || []).filter((s) => s.kind === 'stage');
  const seen = new Map();
  for (const s of stages) seen.set(s.state, s);
  const rel = relTimes(v.stages || []);
  const items = [...seen.values()];
  const failed = new Set(['REJECTED', 'ABSTAINED', 'TIMEOUT', 'PROVIDER_ERROR', 'INTERNAL_ERROR', 'FAILED']);
  const running = ACTIVE.has(v.state);
  $('#railBox').hidden = items.length === 0;
  $('#railSummary').textContent = running ? `Run stages (${items.length})` : `Show all run stages (${items.length})`;
  if (DONE.has(v.state) && S.railClosedFor !== v.run_id) {
    S.railClosedFor = v.run_id;
    $('#railBox').open = false;
  } else if (running) {
    $('#railBox').open = true;
  }
  $('#stageRail').innerHTML = items.map((s, i) => {
    const now = running && i === items.length - 1;
    const cls = now ? 'is-now' : failed.has(s.state) ? 'is-fail' : '';
    return `<li class="${cls}"><span class="b" aria-hidden="true"></span><span>${esc(s.label || s.state)}${s.reason ? `<br><span class="fine">${esc(s.reason)}</span>` : ''}</span><span class="t">${esc(rel(s))}</span></li>`;
  }).join('');
}

function roleInfo(v, role) {
  const events = (v.stages || []).filter((s) => s.kind === 'role' && s.role === role);
  const last = events[events.length - 1];
  const prov = v.result?.role_provenance?.[role];
  const stageStates = new Set((v.stages || []).map((s) => s.state));
  const info = { status: 'Waiting', tone: 'pill-muted', interaction: null, environment: null, elapsed: null, steps: null, types: [] };
  if (role === 'builder' && v.mode === 'fast') {
    Object.assign(info, { status: 'Prepared scene', tone: 'pill-warn', interaction: 'Recorded Builder scene from live1', environment: '—' });
  }
  if (last) {
    Object.assign(info, {
      status: last.status ? String(last.status).replace(/_/g, ' ') : 'Running',
      tone: 'pill-warn',
      interaction: last.interaction_id,
      environment: last.environment_id,
      elapsed: last.elapsed_s,
      steps: last.step_count,
      types: last.step_types || [],
    });
  } else if (role === 'builder' && stageStates.has('BUILDER_RUNNING') && v.mode !== 'fast' && ACTIVE.has(v.state)) {
    info.status = 'Starting';
    info.tone = 'pill-warn';
  } else if (role === 'critic' && stageStates.has('CRITIC_RUNNING') && ACTIVE.has(v.state)) {
    info.status = 'Starting';
    info.tone = 'pill-warn';
  }
  if (prov && (prov.interaction_id || prov.source)) {
    const live = prov.argon_verified;
    const prepared = String(prov.source || '').includes('prepared') || v.mode === 'fast' && role === 'builder';
    info.status = prepared ? 'Prepared scene' : live ? 'Completed on Argon' : v.result?.execution_source === 'mock_fixture' ? 'Offline rehearsal' : String(prov.status || 'Not verified');
    info.tone = prepared ? 'pill-warn' : live ? 'pill-ok' : 'pill-muted';
    info.interaction = prov.interaction_id || info.interaction;
    info.environment = prov.environment_id || info.environment;
    info.elapsed = prov.elapsed_s ?? info.elapsed;
    info.model = prov.returned_model;
  }
  info.running = !!last && ACTIVE.has(v.state) && !prov;
  return info;
}

function renderAgents(v) {
  for (const role of ['builder', 'critic']) {
    const el = document.getElementById(`agent-${role}`);
    const i = roleInfo(v, role);
    const set = (k, val, title) => {
      const n = el.querySelector(`[data-k="${k}"]`);
      n.textContent = val ?? '—';
      if (title) n.title = title;
    };
    const st = el.querySelector('[data-k="status"]');
    st.textContent = i.status;
    st.className = `pill ${i.tone}`;
    set('interaction', i.interaction ? short(i.interaction, 28) : '—', i.interaction || '');
    set('environment', i.environment ? short(i.environment, 28) : '—', i.environment || '');
    set('elapsed', Number.isFinite(i.elapsed) ? `${Number(i.elapsed).toFixed(1)} s` : '—');
    set('steps', Number.isFinite(i.steps) ? String(i.steps) : '—');
    set('model', i.model || '—');
    const uniq = [...new Set(i.types)].slice(-8);
    el.querySelector('[data-k="types"]').innerHTML = uniq.map((t) => `<span>${esc(t)}</span>`).join('');
    el.classList.toggle('is-running', i.running);
  }
  const rel = relTimes(v.stages || []);
  const lines = (v.stages || []).map((s) => {
    let text = s.label || s.state;
    if (s.kind === 'role') {
      text = `${s.role === 'critic' ? 'Critic' : 'Builder'} · ${String(s.status || 'polling').replace(/_/g, ' ')}${Number.isFinite(s.step_count) ? ` · ${s.step_count} steps` : ''}${Number.isFinite(s.elapsed_s) ? ` · ${s.elapsed_s.toFixed(0)} s` : ''}`;
    }
    return `<li><span class="t">${esc(rel(s))}</span><span>${esc(text)}</span></li>`;
  });
  const log = $('#eventLog');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  log.innerHTML = lines.join('') || '<li><span class="t"></span><span>No events yet.</span></li>';
  if (atBottom) log.scrollTop = log.scrollHeight;
}

async function fetchScene(runId, name) {
  try {
    const res = await fetch(assetUrl(runId, name), { cache: 'no-store' });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

async function maybeLoadScene0(v) {
  if (S.scenes.scene0 || S.loadingScene0) return;
  const states = new Set((v.stages || []).map((s) => s.state));
  if (!states.has('RENDER0_VALIDATED') && !states.has('SCENE_VALIDATED')) return;
  S.loadingScene0 = true;
  const s0 = await fetchScene(v.run_id, 'scene0.json');
  S.loadingScene0 = false;
  if (s0 && S.runId === v.run_id) {
    S.scenes.scene0 = s0;
    showScene('before');
  }
}

// ------------------------------------------------------------------ step 3: result
async function renderDone(v) {
  const r = v.result || {};
  const runId = v.run_id;
  if (S.animatedFor === runId) { renderResultCard(v); return; }
  S.animatedFor = runId;
  const visual = r.visual_repair_status;
  const [s0, s1] = await Promise.all([
    fetchScene(runId, 'scene0.json'),
    visual === 'REPAIRED' ? fetchScene(runId, 'scene1.json') : Promise.resolve(null),
  ]);
  if (S.runId !== runId) return;
  S.scenes = { scene0: s0 || null, scene1: visual === 'REPAIRED' ? s1 : null };
  $('#showNew').disabled = !S.scenes.scene1;
  if (visual === 'REPAIRED' || visual === 'NO_CHANGE') {
    const { status, body } = await api(`/api/live/runs/${encodeURIComponent(runId)}/actions`);
    S.actions = status === 200 ? body : null;
    S.actionsFor = runId;
  } else {
    S.actions = null;
  }
  // Cables move only now, after the verifier accepted Argon's patch.
  if (S.scenes.scene1) showScene('after', { animate: true });
  else showScene('before');
  renderResultCard(v);
  revealResult();
}

function revealResult() {
  const smooth = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  if (window.matchMedia('(min-width: 1024px)').matches) {
    $('#step3').scrollIntoView({ behavior: smooth, block: 'start' });
  } else {
    // Phones: bring the 3D twin and the verdict headline back into view so the cable change is seen.
    window.scrollTo({ top: 0, behavior: smooth });
  }
}

function overloadedFeeders(o) {
  const out = [];
  if (o && o.f1_current_a > o.f1_limit_a) out.push('F1');
  if (o && o.f2_current_a > o.f2_limit_a) out.push('F2');
  return out;
}

function resultHeadline(r, staleOutcome) {
  const visual = r.visual_repair_status;
  const action = r.action_status;
  if (!['REPAIRED', 'NO_CHANGE'].includes(visual)) return [COPY.failure, 'bad'];
  if (action === 'NO_SAFE_ACTION') return [COPY.noSafe, 'warn'];
  if (visual === 'NO_CHANGE') return [COPY.noChange, 'ok'];
  if (!r.recommended_receiver) return [COPY.failure, 'bad'];
  const stale = S.config?.stale_twin?.recommended_receiver;
  const over = overloadedFeeders(staleOutcome);
  if (staleOutcome && !staleOutcome.safe && over.length && stale !== r.recommended_receiver) {
    return [`The old recommendation would overload ${over.join(' and ')}. The repaired twin selects ${r.recommended_receiver}.`, 'ok'];
  }
  if (staleOutcome && !staleOutcome.safe && stale !== r.recommended_receiver) {
    return [`The old recommendation is unsafe on the repaired wiring. The repaired twin selects ${r.recommended_receiver}.`, 'ok'];
  }
  return [`The wiring map was repaired. The repaired twin selects ${r.recommended_receiver}.`, 'ok'];
}

function figureFor(kind, o, label) {
  if (!o) return '';
  if (kind === 'n4') {
    const ok = Number.isFinite(o.n4_temp_180s_c) && o.n4_temp_180s_c < o.max_temp_limit_c;
    return `<div class="fig ${ok ? 'is-ok' : 'is-bad'}"><div class="k">N4 at 180 s</div><div class="v">${fmtC(o.n4_temp_180s_c)}</div><div class="n">${esc(label)} · limit ${o.max_temp_limit_c} °C</div></div>`;
  }
  const over = overloadedFeeders(o);
  const parts = [`F1 ${fmtA(o.f1_current_a)}/${fmtA(o.f1_limit_a)} A`, `F2 ${fmtA(o.f2_current_a)}/${fmtA(o.f2_limit_a)} A`];
  const main = over.length ? parts.filter((p) => over.some((f) => p.startsWith(f))).join(', ') : parts.join(', ');
  return `<div class="fig ${o.safe ? 'is-ok' : 'is-bad'}"><div class="k">${esc(label)} ${esc(o.target_rack || 'no action')}</div><div class="v">${esc(main)}</div><div class="n">${o.safe ? 'Within feeder and thermal limits' : over.length ? `${over.join(' and ')} over limit` : 'N4 exceeds the thermal limit'}</div></div>`;
}

function renderResultCard(v) {
  const r = v.result || {};
  $('#resultEmpty').hidden = true;
  $('#result').hidden = false;
  const [vl, vc] = VISUAL_LABEL[r.visual_repair_status] || ['Not verified', 'pill-bad'];
  const [al, ac] = ACTION_LABEL[r.action_status] || ['No recommendation', 'pill-muted'];
  $('#visualChip').textContent = vl;
  $('#visualChip').className = `pill ${vc}`;
  $('#actionChip').textContent = r.action_status === 'MULTIPLE_SAFE' && r.recommended_receiver ? `${al}; tie-break ${r.recommended_receiver}` : al;
  $('#actionChip').className = `pill ${ac}`;
  $('#sourceChip').textContent = SOURCE_LABEL[r.execution_source] || r.execution_source || 'Unknown source';
  $('#sourceChip').className = `pill ${r.execution_source === 'mock_fixture' || r.execution_source === 'recorded_live_replay' ? 'pill-warn' : 'pill-muted'}`;

  const acts = S.actionsFor === v.run_id ? S.actions : null;
  const outcomes = acts?.outcomes || [];
  const byTarget = (t) => outcomes.find((o) => (o.target_rack || null) === (t || null));
  const staleRec = S.config?.stale_twin?.recommended_receiver || 'N3';
  const staleOutcome = byTarget(staleRec);
  const recOutcome = r.recommended_receiver ? byTarget(r.recommended_receiver) : null;
  const [head, tone] = resultHeadline(r, staleOutcome);
  setHeadline(head, tone);
  let sub = '';
  if (v.recorded) sub = 'This is the recorded live run, not a result for a drawing you generated.';
  else if (r.execution_source === 'mock_fixture') sub = 'Offline rehearsal: plumbing check only, not an Argon result.';
  else if (r.graph_score?.summary) sub = `Wiring graph accuracy ${r.graph_score.summary}.`;
  if (r.action_status === 'MULTIPLE_SAFE' && r.tie_break_rule) sub += ` Tie-break: ${r.tie_break_rule}.`;
  $('#subline').textContent = sub.trim();

  let figs = '';
  if (outcomes.length) {
    figs += figureFor('move', staleOutcome, 'Old choice');
    if (recOutcome && recOutcome.target_rack !== staleRec) figs += figureFor('move', recOutcome, 'Repaired choice');
    if (recOutcome) figs += figureFor('n4', recOutcome, `after moving 8 A to ${recOutcome.target_rack}`);
    else figs += figureFor('n4', byTarget(null), 'with no action');
  }
  $('#figures').innerHTML = figs;
  $('#actionsBox').hidden = !outcomes.length;
  if (outcomes.length) {
    if (S.chosen === undefined) S.chosen = r.recommended_receiver || staleRec;
    renderActionDetail();
  }
  const prov = r.role_provenance || {};
  const builder = r.execution_source === 'prepared_builder_live_critic' ? 'prepared' : r.execution_source === 'mock_fixture' ? 'rehearsal' : prov.builder?.argon_verified ? 'live' : 'not verified';
  const critic = r.execution_source === 'mock_fixture' ? 'rehearsal' : prov.critic?.argon_verified ? 'live' : 'not verified';
  $('#provLine').textContent = `Builder: ${builder} · Critic: ${critic} · Run ${v.run_id} · ${r.terminal_state || v.state}${Number.isFinite(v.elapsed_s) ? ` · ${Number(v.elapsed_s).toFixed(1)} s` : ''}`;
  if (!v.recorded && DONE.has(v.state)) {
    $('#runMsg').textContent = Number.isFinite(v.elapsed_s) ? `Finished in ${Number(v.elapsed_s).toFixed(1)} s (server wall clock).` : 'Finished.';
  }
}

function renderActionDetail() {
  const outcomes = S.actions?.outcomes || [];
  const o = outcomes.find((x) => (x.target_rack || '') === (S.chosen || ''));
  document.querySelectorAll('#actionSeg .seg-btn').forEach((b) => {
    const on = (b.dataset.target || '') === (S.chosen || '');
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  if (!o) { $('#actionDetail').innerHTML = ''; return; }
  const cls = (cur, lim) => (cur > lim ? ' class="over"' : '');
  const tag = o.target_rack ? `Move 8 A from N4 to ${o.target_rack}` : 'No action';
  $('#actionDetail').innerHTML = `
    <div><span class="verdict ${o.safe ? 'ok' : 'bad'}">${o.safe ? 'SAFE' : 'UNSAFE'}</span> · ${esc(tag)} on the ${S.scenes.scene1 ? 'repaired' : 'verified'} twin</div>
    <table>
      <tr><td>F1 load</td><td${cls(o.f1_current_a, o.f1_limit_a)}>${fmtA(o.f1_current_a)} / ${fmtA(o.f1_limit_a)} A</td></tr>
      <tr><td>F2 load</td><td${cls(o.f2_current_a, o.f2_limit_a)}>${fmtA(o.f2_current_a)} / ${fmtA(o.f2_limit_a)} A</td></tr>
      <tr><td>N4 connector at 180 s</td><td${cls(o.n4_temp_180s_c, o.max_temp_limit_c)}>${fmtC(o.n4_temp_180s_c)}</td></tr>
    </table>`;
  applyActionToViewer();
}

function setLegend(o) {
  for (const [fid, cur, lim] of [['F1', o?.f1_current_a, o?.f1_limit_a], ['F2', o?.f2_current_a, o?.f2_limit_a]]) {
    const chip = document.getElementById(`legend${fid}`);
    const ok = Number.isFinite(cur) && Number.isFinite(lim);
    chip.querySelector('b').textContent = ok ? `${fmtA(cur)}/${fmtA(lim)} A` : '';
    chip.classList.toggle('is-over', ok && cur > lim);
  }
}

function applyActionToViewer() {
  const outcomes = S.actions?.outcomes || [];
  const o = S.view && DONE.has(S.view.state) && S.showing === (S.scenes.scene1 ? 'after' : 'before')
    ? outcomes.find((x) => (x.target_rack || '') === (S.chosen || ''))
    : null;
  setLegend(o);
  if (!S.viewer) return;
  if (!o) {
    S.viewer.setLoads(null);
    const t = (S.scenes.scene0 || {}).hotspot?.temperature_c ?? S.config?.stale_twin?.n4_temp_now_c;
    S.viewer.setHeat(Number(t), 'N4 connector');
    return;
  }
  S.viewer.setLoads({ F1: { current: o.f1_current_a, limit: o.f1_limit_a }, F2: { current: o.f2_current_a, limit: o.f2_limit_a } });
  S.viewer.setHeat(o.n4_temp_180s_c, o.target_rack ? `N4 at 180 s (8 A → ${o.target_rack})` : 'N4 at 180 s (no action)');
  if (o.target_rack) S.viewer.setHighlight([o.target_rack]);
}

// ------------------------------------------------------------------ evidence
function renderEvidence(v) {
  const r = v.result || {};
  const runId = v.run_id;
  const d = v.drawing || {};
  const states = new Set((v.stages || []).map((s) => s.state));
  const imgs = [];
  if (d.url) imgs.push([assetUrl(runId, 'current_sld.png', (d.sha256 || '').slice(0, 16)), `Image Argon inspected · today's drawing · run ${runId} · SHA-256 ${short(d.sha256, 16)}`]);
  imgs.push([assetUrl(runId, 'old_sld_A.png'), 'Old drawing used to build the twin']);
  if (states.has('RENDER0_VALIDATED') || v.recorded) imgs.push([assetUrl(runId, 'render0/render_wiring.png'), 'render0 · old twin, fixed pose (Critic input)']);
  if (r.visual_repair_status === 'REPAIRED') imgs.push([assetUrl(runId, 'render1/render_wiring.png'), 'render1 · repaired twin, fixed pose']);
  imgs.push([assetUrl(runId, 'old_thermal_rear.png'), 'Rear thermal image (synthetic sensor)']);
  const rows = (pairs) => pairs.filter(([, val]) => val !== undefined && val !== null && val !== '').map(([k, val]) => `<tr><th>${esc(k)}</th><td>${esc(val)}</td></tr>`).join('');
  const vv = r.visual_verification || {};
  const gates = r.gates || {};
  const roleRows = (role) => {
    const p = (r.role_provenance || {})[role] || {};
    return rows([
      ['Source', p.source],
      ['Model', p.returned_model],
      ['Argon verified', p.source ? String(!!p.argon_verified) : null],
      ['Environment', p.environment_id],
      ['Interaction', p.interaction_id],
      ['Elapsed', Number.isFinite(p.elapsed_s) ? `${p.elapsed_s} s` : null],
      ...(p.input_images || []).map((m) => [`Input ${m.name}`, m.sha256]),
    ]);
  };
  const manifest = Object.entries(r.input_manifest?.images || {}).map(([k, m]) => [k, `${m.sha256} · ${m.width_px}×${m.height_px} · ${m.size_bytes} B`]);
  const done = DONE.has(v.state);
  $('#evidence').innerHTML = `
    <div class="ev-grid">${imgs.map(([src, cap]) => `<figure class="ev-fig"><img loading="lazy" src="${esc(src)}" alt="${esc(cap)}"><figcaption>${esc(cap)}</figcaption></figure>`).join('')}</div>
    ${done ? `
    <section class="ev-section"><h3>Verdict</h3><table class="kv">${rows([
      ['Terminal state', r.terminal_state],
      ['Visual repair', r.visual_repair_status],
      ['Action', r.action_status],
      ['Safe receivers', (r.safe_receivers || []).join(', ') || 'none'],
      ['Execution source', SOURCE_LABEL[r.execution_source] || r.execution_source],
      ['Graph score', r.graph_score?.summary],
      ['Changed pixels (N2 corridor)', vv.changed_pixels_n2_corridor],
      ['Gates passed', Object.entries(gates).filter(([, ok]) => ok).map(([k]) => k).join(', ')],
      ['Gates failed', Object.entries(gates).filter(([, ok]) => !ok).map(([k]) => k).join(', ') || 'none'],
      ['Error', r.error],
    ])}</table></section>
    <section class="ev-section"><h3>Builder agent</h3><table class="kv">${roleRows('builder') || '<tr><td>No provenance.</td></tr>'}</table></section>
    <section class="ev-section"><h3>Critic agent</h3><table class="kv">${roleRows('critic') || '<tr><td>No provenance.</td></tr>'}</table></section>
    ${manifest.length ? `<section class="ev-section"><h3>Input manifest (SHA-256)</h3><table class="kv">${rows(manifest)}</table></section>` : ''}
    <p class="fine"><a href="${assetUrl(runId, 'result_public.json')}" target="_blank" rel="noopener">result_public.json</a> · <a href="${assetUrl(runId, 'patch.json')}" target="_blank" rel="noopener">patch.json</a> · <a href="${assetUrl(runId, 'scene0.json')}" target="_blank" rel="noopener">scene0.json</a>${r.visual_repair_status === 'REPAIRED' ? ` · <a href="${assetUrl(runId, 'scene1.json')}" target="_blank" rel="noopener">scene1.json</a>` : ''}</p>
    ` : '<p class="fine">Verdict, provenance and hashes appear when the run finishes.</p>'}`;
}

// ------------------------------------------------------------------ zoom dialog
function initZoom() {
  const dlg = $('#zoom');
  const scroll = dlg.querySelector('.zoom-scroll');
  $('#sldBtn').addEventListener('click', () => {
    const src = $('#sldImg').getAttribute('src');
    if (!src) return;
    $('#zoomImg').src = src;
    scroll.classList.remove('is-zoomed');
    if (dlg.showModal) dlg.showModal(); else window.open(src, '_blank');
  });
  $('#zoomImg').addEventListener('click', (e) => {
    const rect = e.target.getBoundingClientRect();
    const fx = (e.clientX - rect.left) / rect.width;
    const fy = (e.clientY - rect.top) / rect.height;
    scroll.classList.toggle('is-zoomed');
    if (scroll.classList.contains('is-zoomed')) {
      requestAnimationFrame(() => {
        scroll.scrollLeft = fx * scroll.scrollWidth - scroll.clientWidth / 2;
        scroll.scrollTop = fy * scroll.scrollHeight - scroll.clientHeight / 2;
      });
    }
  });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
}

// ------------------------------------------------------------------ boot
async function boot() {
  initTabs();
  initZoom();
  $('#simulateBtn').addEventListener('click', simulateDrawing);
  $('#runBtn').addEventListener('click', startRun);
  $('#modeChoice').addEventListener('change', updateButtons);
  $('#showOld').addEventListener('click', () => showScene('before'));
  $('#showNew').addEventListener('click', () => { if (S.scenes.scene1) showScene('after'); });
  $('#resetView').addEventListener('click', () => S.viewer && S.viewer.resetView());
  $('#actionSeg').addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b) return;
    S.chosen = b.dataset.target || '';
    renderActionDetail();
  });
  const viewerReady = initViewer();
  try {
    await loadConfig();
  } catch {
    $('#liveBadge').textContent = 'Server unreachable';
  }
  await viewerReady;
  staleStory();
  showScene('before');
  const runParam = new URLSearchParams(location.search).get('run');
  if (runParam && /^(jr-[0-9a-f]{12}|live1)$/.test(runParam)) await loadRun(runParam);
  updateButtons();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && S.view && ACTIVE.has(S.view.state)) schedulePoll(100);
  });
}

boot();
