/* Sparring Partner - vanilla JS frontend. No build step, no external requests.
 *
 * Layout of this file:
 *   1. constants + state
 *   2. small helpers (DOM builder, storage, api, formatting)
 *   3. view lifecycle (newView / alive / onLeave) and hash routing
 *   4. turn panel (type/speak, countdown, grace, recorder, submit guard)
 *   5. round flow: start, objection, comeback, judging, calibration, feedback, summary
 *   6. history
 *
 * All server text is inserted with textContent (via h()), never innerHTML.
 */
'use strict';

/* ------------------------------------------------------------------ 1. constants + state */

const DIMENSIONS = [
  ['accuracy', 'Accuracy'],
  ['logic', 'Logic'],
  ['crux', 'Addresses the crux'],
  ['concision', 'Concision'],
  ['concession_discipline', 'Concession discipline'],
];

const FLAG_LABELS = {
  fallacy: 'Fallacy',
  concession: 'Concession',
  missed_point: 'Missed point',
  unsupported_claim: 'Unsupported claim',
  injection_attempt: 'Injection attempt',
};

const REVIEWER_LABELS = { josh: 'Josh', second: 'Second reviewer' };

const MODE_KEY = 'sparring.answerMode';
const SPEAK_MIME = 'audio/webm;codecs=opus';
const STILL_WORKING_MS = 20000;
const NO_AUDIO_TEXT = "Couldn't hear an answer";
const DISCLAIMER = 'Debate practice, not medical or dietary advice.';

const state = {
  config: null,
  round: null, // {id, objections, idx, study, readAloud, calibration, reviewer, active}
  view: 0, // incremented on every screen change; async code checks it with alive()
  cleanups: [],
};

const app = document.getElementById('app');

/* ------------------------------------------------------------------ 2. helpers */

/** Build an element. Props: class, text, on* handlers, anything else becomes an attribute. */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  appendKids(el, kids);
  return el;
}

function appendKids(el, kids) {
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    if (Array.isArray(kid)) appendKids(el, kid);
    else if (kid instanceof Node) el.appendChild(kid);
    else el.appendChild(document.createTextNode(String(kid)));
  }
}

function storeGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function storeSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* storage unavailable: ignore */ }
}

/** fetch wrapper. Never throws: returns {ok, status, data}. status 0 means network failure. */
async function api(path, options) {
  const opts = options || {};
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers['Content-Type'] = 'application/json';
  } else if (opts.form) {
    init.body = opts.form;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    return { ok: false, status: 0, data: { error: 'network' } };
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  return { ok: res.ok, status: res.status, data };
}

function errorText(res, fallback) {
  const d = res && res.data;
  const bits = [];
  if (d && d.error) bits.push(d.error);
  if (d && d.detail) bits.push(d.detail);
  if (res && res.status === 0) bits.push('could not reach the server');
  return bits.length ? `${fallback} (${bits.join(': ')})` : fallback;
}

function fmtNum(n, dp) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
  return Number(n).toFixed(dp === undefined ? 1 : dp);
}

function fmtClock(totalSeconds) {
  const s = Math.max(0, Math.ceil(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function fmtDate(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('en-GB');
}

function fmtMoney(n) {
  return '$' + Number(n || 0).toFixed(4);
}

function capitalise(text) {
  const s = String(text || '');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function numberOr(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function chip(category) {
  return h('span', { class: 'chip', text: category || 'general' });
}

function scoreClass(value) {
  if (value >= 4) return 'high';
  if (value >= 3) return 'mid';
  return 'low';
}

function scoreBar(label, value) {
  const known = value !== null && value !== undefined && !Number.isNaN(Number(value));
  const pct = known ? Math.max(0, Math.min(100, (Number(value) / 5) * 100)) : 0;
  return h(
    'div',
    { class: 'bar-row' },
    h('span', { class: 'bar-label', text: label }),
    h(
      'div',
      { class: 'bar', role: 'meter', 'aria-label': label, 'aria-valuemin': 0, 'aria-valuemax': 5, 'aria-valuenow': known ? Number(value) : null },
      h('div', { class: `bar-fill ${known ? scoreClass(Number(value)) : ''}`, style: `width:${pct}%` })
    ),
    h('span', { class: 'bar-value', text: known ? `${fmtNum(value, Number.isInteger(Number(value)) ? 0 : 1)}/5` : '-' })
  );
}

function table(headers, rows, numericCols) {
  const nums = numericCols || [];
  return h(
    'table',
    {},
    h('thead', {}, h('tr', {}, headers.map((t, i) => h('th', { class: nums.includes(i) ? 'num' : null, text: t })))),
    h('tbody', {}, rows.map((row) => h('tr', {}, row.map((cell, i) => h('td', { class: nums.includes(i) ? 'num' : null }, cell)))))
  );
}

/* ------------------------------------------------------------------ 3. view lifecycle + routing */

function runCleanups() {
  const fns = state.cleanups;
  state.cleanups = [];
  for (const fn of fns) {
    try { fn(); } catch (e) { /* ignore cleanup errors */ }
  }
}

/** Start a fresh screen. Returns an id; async code must check alive(id) after every await. */
function newView() {
  state.view += 1;
  runCleanups();
  app.replaceChildren();
  return state.view;
}

function alive(viewId) {
  return viewId === state.view;
}

function onLeave(fn) {
  state.cleanups.push(fn);
}

/** Card with a focusable heading, appended to #app. */
function shell(title) {
  const heading = h('h2', { text: title, tabindex: -1 });
  const card = h('section', { class: 'card' }, heading);
  app.appendChild(card);
  try { heading.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
  return card;
}

function route() {
  const hash = window.location.hash || '#start';
  if (hash === '#round' && state.round) return; // round screens are driven by round state
  if (hash === '#history') showHistory();
  else showStart();
}

function wireNav() {
  document.querySelectorAll('a[data-nav]').forEach((link) => {
    link.addEventListener('click', (event) => {
      const inRound = state.round && state.round.active;
      if (inRound && !window.confirm('Leave this round? Answers already submitted stay saved in History.')) {
        event.preventDefault();
      }
    });
  });
  window.addEventListener('hashchange', route);
}

/* ------------------------------------------------------------------ 4. voice support + read aloud */

/** Returns a list of reasons Speak is unavailable (empty list means it is available). */
function voiceBlockers() {
  const reasons = [];
  if (!state.config || !state.config.speak_enabled) reasons.push('Voice is off on the free tier - set VOICE_ON_FREE_TIER=1 or GEMINI_TIER=paid in .env (see README)');
  let supported = false;
  try {
    supported =
      typeof window.MediaRecorder !== 'undefined' &&
      window.MediaRecorder.isTypeSupported(SPEAK_MIME) &&
      !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  } catch (e) {
    supported = false;
  }
  if (!supported) reasons.push('Voice needs Chrome or Firefox');
  return reasons;
}

/** Read text aloud. Resolves on end, on error, or when skipped. Resolves at once if unsupported. */
function readAloud(parent, text) {
  if (!('speechSynthesis' in window) || typeof window.SpeechSynthesisUtterance === 'undefined') {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    let done = false;
    const box = h('div', { class: 'reader' });
    const finish = () => {
      if (done) return;
      done = true;
      box.remove();
      resolve();
    };
    const skip = h('button', { type: 'button', text: 'Skip reading', onclick: () => {
      try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
      finish();
    } });
    appendKids(box, [h('span', { text: 'Reading aloud...' }), skip]);
    parent.appendChild(box);
    onLeave(() => {
      try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
      done = true;
    });
    try {
      const utterance = new window.SpeechSynthesisUtterance(text);
      utterance.lang = 'en-GB';
      utterance.onend = finish;
      utterance.onerror = finish; // an error must not leave the countdown stuck
      window.speechSynthesis.speak(utterance);
    } catch (e) {
      finish();
    }
  });
}

/* ------------------------------------------------------------------ 5. turn panel */

/**
 * One answer turn: Type/Speak toggle, countdown + grace, recorder, submit.
 * opts: {v: view id, timerKey: 'opener'|'followup', gate: Promise|null, onSubmit(payload, turn),
 *        onSkip?: () => Promise<true|string>}
 * payload: {mode:'type', text} or {mode:'speak', audio: Blob}
 * The submit guard is the `submitted` flag: the first path to call finish() wins.
 * onSkip (opener turn only) shows a Skip button. It resolves true once the screen has moved on,
 * or to an error message, in which case the turn is reopened. `skipping` blocks double clicks.
 */
function createTurn(parent, opts) {
  const study = state.round.study;
  const timers = (state.config.timers && state.config.timers[opts.timerKey]) || {};
  const graceS = numberOr(state.config.grace_s, 10);
  const blockers = voiceBlockers();
  const speakOk = blockers.length === 0;

  let mode = speakOk && storeGet(MODE_KEY) === 'speak' ? 'speak' : 'type';
  let submitted = false;
  let skipping = false;
  let busy = false;
  let gateOpen = !opts.gate;
  let ticker = null;
  let recorder = null;
  let stream = null;
  let chunks = [];

  const typeBtn = h('button', { type: 'button', class: 'seg', text: 'Type', onclick: () => setMode('type') });
  const speakBtn = h('button', { type: 'button', class: 'seg', text: 'Speak', onclick: () => setMode('speak') });
  const modeNote = h('p', { class: 'muted small', text: speakOk ? '' : `${blockers.join('. ')}.` });
  const modeBar = h('div', { class: 'row' }, h('div', { class: 'seg-group', role: 'group', 'aria-label': 'Answer mode' }, typeBtn, speakBtn));

  const timeEl = h('span', { class: 'clock-time' });
  const clockMsg = h('span', { class: 'clock-msg' });
  const clockEl = h('div', { class: 'clock', role: 'timer' }, timeEl, clockMsg);

  const textarea = h('textarea', { 'aria-label': 'Your answer', placeholder: 'Type your answer...' });
  textarea.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') finish();
  });
  const submitBtn = h('button', { type: 'button', class: 'primary', text: 'Submit', onclick: () => finish() });
  const skipBtns = []; // one per input area (type / speak); both share the same state
  const makeSkipBtn = () => {
    if (!opts.onSkip) return null;
    const btn = h('button', { type: 'button', class: 'skip-btn', text: 'Skip', onclick: () => skip() });
    skipBtns.push(btn);
    return btn;
  };
  const typeArea = h('div', { class: 'type-area stack' }, textarea, h('div', { class: 'row' }, submitBtn, makeSkipBtn(), h('span', { class: 'muted small', text: 'Ctrl+Enter also submits' })));

  const startBtn = h('button', { type: 'button', class: 'primary', text: 'Start recording', onclick: () => startRecording() });
  const stopBtn = h('button', { type: 'button', class: 'primary', text: 'Stop and submit', onclick: () => finish() });
  const recInfo = h('div', { class: 'rec-info' }, h('span', { class: 'rec-dot' }), h('span', { text: 'Recording...' }));
  const speakArea = h('div', { class: 'speak-area row' }, startBtn, recInfo, stopBtn, makeSkipBtn());

  const statusEl = h('p', { class: 'status', role: 'status', 'aria-live': 'polite' });
  const busyText = h('span', { text: '' });
  const busyEl = h('div', { class: 'busy', role: 'status', 'aria-live': 'polite' }, h('span', { class: 'spinner' }), busyText);

  const inputEl = h('div', { class: 'turn-input' }, modeBar, modeNote, clockEl, typeArea, speakArea, statusEl);
  const root = h('div', { class: 'turn' }, inputEl, busyEl);
  parent.appendChild(root);

  const recording = () => !!recorder && recorder.state === 'recording';

  function render() {
    typeBtn.setAttribute('aria-pressed', String(mode === 'type'));
    speakBtn.setAttribute('aria-pressed', String(mode === 'speak'));
    typeBtn.disabled = submitted || recording();
    speakBtn.disabled = !speakOk || submitted || recording();
    if (!speakOk) speakBtn.title = blockers.join('. ');
    typeArea.hidden = mode !== 'type';
    speakArea.hidden = mode !== 'speak';
    const inputsLocked = !gateOpen || submitted || skipping;
    for (const btn of skipBtns) {
      btn.hidden = submitted; // gone once the opener is in (also covers the time's-up auto-submit)
      btn.disabled = skipping || submitted;
    }
    typeBtn.disabled = typeBtn.disabled || skipping;
    speakBtn.disabled = speakBtn.disabled || skipping;
    textarea.disabled = inputsLocked;
    submitBtn.disabled = inputsLocked;
    startBtn.hidden = recording();
    startBtn.disabled = inputsLocked;
    recInfo.hidden = !recording();
    stopBtn.hidden = !recording();
    root.classList.toggle('is-busy', busy);
  }

  function say(message, kind) {
    statusEl.textContent = message || '';
    statusEl.className = kind === 'info' ? 'status info' : 'status';
  }

  /* --- clock --- */

  function stopClock() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  function showIdleClock() {
    clockEl.className = 'clock';
    if (study) {
      timeEl.textContent = 'Untimed';
      clockMsg.textContent = 'Study mode: take as long as you like.';
      return;
    }
    timeEl.textContent = fmtClock(numberOr(timers[mode], 60));
    if (!gateOpen) clockMsg.textContent = 'The clock starts after the objection is read.';
    else if (mode === 'speak') clockMsg.textContent = 'The clock starts when you press Start recording.';
    else clockMsg.textContent = '';
  }

  function paintClock(left, phase) {
    timeEl.textContent = fmtClock(left);
    clockEl.className = 'clock' + (phase === 'grace' ? ' grace' : left <= 10 ? ' warn' : '');
    clockMsg.textContent = phase === 'grace' ? "Time's up - finish your sentence" : '';
  }

  function startClock(seconds) {
    stopClock();
    if (study || submitted) return;
    let phase = 'main';
    let deadline = Date.now() + seconds * 1000;
    const tick = () => {
      let left = Math.ceil((deadline - Date.now()) / 1000);
      if (left <= 0) {
        if (phase === 'main') {
          phase = 'grace';
          deadline = Date.now() + graceS * 1000;
          left = Math.ceil(graceS);
        } else {
          stopClock();
          finish();
          return;
        }
      }
      paintClock(left, phase);
    };
    tick();
    ticker = setInterval(tick, 200);
  }

  function startTypeClock() {
    if (mode === 'type' && gateOpen && !submitted) startClock(numberOr(timers.type, 90));
  }

  /* --- mode switching --- */

  function setMode(next) {
    if (submitted || recording() || next === mode) return;
    if (next === 'speak' && !speakOk) return;
    mode = next;
    storeSet(MODE_KEY, mode);
    say('');
    stopClock();
    showIdleClock();
    render();
    startTypeClock();
  }

  /* --- recording --- */

  function releaseStream() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }

  async function startRecording() {
    if (submitted || skipping || recorder || !gateOpen) return;
    startBtn.disabled = true;
    say('Waiting for microphone permission...', 'info');
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      stream = null;
      if (!alive(opts.v)) return;
      mode = 'type';
      say('Microphone access was blocked, so you are back on Type. Allow the microphone in your browser to use Speak.');
      showIdleClock();
      render();
      startTypeClock();
      return;
    }
    if (!alive(opts.v) || submitted || skipping) {
      releaseStream();
      return;
    }
    say('');
    chunks = [];
    try {
      recorder = new window.MediaRecorder(stream, { mimeType: SPEAK_MIME });
    } catch (e) {
      releaseStream();
      mode = 'type';
      say('Could not start the recorder, so you are back on Type.');
      showIdleClock();
      render();
      startTypeClock();
      return;
    }
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size) chunks.push(event.data);
    };
    // onstop fires after the final dataavailable, so the blob is complete here.
    recorder.onstop = () => {
      const blob = new Blob(chunks, { type: 'audio/webm' });
      recorder = null;
      releaseStream();
      if (!alive(opts.v)) return;
      deliver({ mode: 'speak', audio: blob });
    };
    recorder.start();
    render();
    startClock(numberOr(timers.speak, 45)); // speak clock starts only now, after the permission prompt
  }

  /* --- submit --- */

  /** Opener only: swap this objection. Cancels clock, read-aloud and any recording (discarded, never posted). */
  async function skip() {
    if (!opts.onSkip || submitted || skipping) return; // guard flag against double clicks
    skipping = true;
    stopClock();
    try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
    if (recorder) {
      recorder.onstop = null; // discard: never deliver this audio
      try { recorder.stop(); } catch (e) { /* already stopped */ }
      recorder = null;
    }
    chunks = [];
    releaseStream();
    say('Finding another objection...', 'info');
    render();
    let outcome;
    try {
      outcome = await opts.onSkip();
    } catch (e) {
      outcome = "Couldn't skip this objection. Try again.";
    }
    if (!alive(opts.v) || outcome === true) return;
    skipping = false;
    turn.reopen(outcome);
  }

  function finish() {
    if (submitted || skipping) return; // guard flag: timer, button and shortcut can all race here
    if (mode === 'speak' && !recording()) return;
    submitted = true;
    stopClock();
    render();
    if (mode === 'speak') {
      recorder.stop();
    } else {
      deliver({ mode: 'type', text: textarea.value });
    }
  }

  function deliver(payload) {
    busy = true;
    render();
    Promise.resolve()
      .then(() => opts.onSubmit(payload, turn))
      .catch(() => {
        if (alive(opts.v)) turn.reopen('Something went wrong sending that. Please try again.');
      });
  }

  const turn = {
    setBusy(message) {
      busy = true;
      busyText.textContent = message;
      render();
    },
    /** Re-open the turn after a recoverable failure. forceMode 'type' switches to typing. */
    reopen(message, forceMode) {
      submitted = false;
      busy = false;
      recorder = null;
      chunks = [];
      releaseStream();
      if (forceMode === 'type') mode = 'type';
      showIdleClock();
      render();
      say(message || '');
      startTypeClock();
    },
  };

  onLeave(() => {
    stopClock();
    if (recorder) {
      recorder.onstop = null;
      try { recorder.stop(); } catch (e) { /* already stopped */ }
      recorder = null;
    }
    releaseStream();
  });

  showIdleClock();
  render();
  if (!gateOpen) {
    opts.gate.then(() => {
      if (!alive(opts.v)) return;
      gateOpen = true;
      if (skipping) return; // read-aloud was cancelled by Skip: do not start a clock
      showIdleClock();
      render();
      startTypeClock();
      if (mode === 'type' && !submitted) textarea.focus();
    });
  } else {
    startTypeClock();
    if (mode === 'type') textarea.focus();
  }
  return turn;
}

function buildAnswerForm(fields, payload) {
  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) form.append(key, String(value));
  form.append('mode', payload.mode);
  if (payload.mode === 'type') form.append('text', payload.text);
  else if (payload.audio) form.append('audio', payload.audio, 'answer.webm');
  return form;
}

/** Shows "Judging..." then "Still working..." after 20 s. Returns a stop function. */
function judgingStatus(turnOrSetter) {
  turnOrSetter('Judging...');
  const timer = setTimeout(() => turnOrSetter('Judging... Still working, this can take a little longer.'), STILL_WORKING_MS);
  const stop = () => clearTimeout(timer);
  onLeave(stop);
  return stop;
}

/* ------------------------------------------------------------------ 5a. start screen */

function showStart() {
  state.round = null;
  if (window.location.hash === '#round') window.history.replaceState(null, '', window.location.pathname + '#start');
  newView();
  const cfg = state.config;
  const card = shell('Practise answering objections');
  card.appendChild(h('p', { class: 'muted', text: `A round is ${cfg.round_size} objections. Answer each one, handle one pushback, then get scored feedback.` }));

  const category = h(
    'select',
    { id: 'category' },
    h('option', { value: '', text: 'All categories' }),
    (cfg.categories || []).map((c) => h('option', { value: c, text: capitalise(c) }))
  );
  const study = h('input', { type: 'checkbox', id: 'study' });
  const shuffle = h('input', { type: 'checkbox', id: 'shuffle' });
  const read = h('input', { type: 'checkbox', id: 'read' });
  const calib = h('input', { type: 'checkbox', id: 'calib' });
  const reviewer = h('select', { id: 'reviewer', 'aria-label': 'Reviewer' }, h('option', { value: 'josh', text: 'Josh' }), h('option', { value: 'second', text: 'Second reviewer' }));
  reviewer.disabled = true;
  calib.addEventListener('change', () => { reviewer.disabled = !calib.checked; });

  const options = h(
    'div',
    { class: 'options' },
    h('div', { class: 'field' }, h('label', { for: 'category', text: 'Category' }), category),
    h('label', { class: 'check' }, study, h('span', {}, 'Study mode (untimed)', h('span', { class: 'hint', text: 'No countdown. Take as long as you need.' }))),
    h('label', { class: 'check' }, shuffle, h('span', {}, 'Shuffle', h('span', { class: 'hint', text: 'Random order instead of least-practised first.' }))),
    h('label', { class: 'check' }, read, h('span', {}, 'Read objections aloud', h('span', { class: 'hint', text: 'Uses your browser voice. The clock starts when reading ends.' }))),
    h(
      'div',
      {},
      h('label', { class: 'check' }, calib, h('span', {}, 'Calibration mode', h('span', { class: 'hint', text: 'Score each answer yourself before the AI feedback is shown.' }))),
      h('div', { class: 'sub-option row' }, h('label', { for: 'reviewer', text: 'Reviewer' }), reviewer)
    )
  );

  const message = h('p', { class: 'status', role: 'status' });
  const startBtn = h('button', { type: 'button', class: 'primary large', text: 'Start round' });
  startBtn.addEventListener('click', () => {
    startBtn.disabled = true;
    startRound(
      { category: category.value || null, study: study.checked, shuffle: shuffle.checked, readAloud: read.checked, calibration: calib.checked, reviewer: reviewer.value },
      startBtn,
      message
    );
  });

  card.appendChild(h('div', { class: 'stack' }, options, h('div', { class: 'row' }, startBtn, h('a', { href: '#history', text: 'History' })), message));
}

async function startRound(opts, btn, message) {
  const view = state.view;
  message.textContent = '';
  const res = await api('/rounds', { method: 'POST', json: { category: opts.category, study_mode: opts.study, shuffle: !!opts.shuffle } });
  if (!alive(view)) return;
  const data = res.data;
  if (!res.ok || !data || !Array.isArray(data.objections) || data.objections.length === 0) {
    message.textContent = errorText(res, "Couldn't start a round.");
    btn.disabled = false;
    return;
  }
  state.round = {
    id: data.round_id,
    objections: data.objections,
    idx: 0,
    study: opts.study,
    readAloud: opts.readAloud,
    calibration: opts.calibration,
    reviewer: opts.reviewer,
    active: true,
  };
  window.location.hash = '#round';
  showObjection();
}

/* ------------------------------------------------------------------ 5b. objection + comeback */

function roundHeader(card, objection) {
  const r = state.round;
  card.appendChild(
    h('div', { class: 'row' }, h('span', { class: 'progress', text: `Objection ${r.idx + 1} of ${r.objections.length}` }), chip(objection.category))
  );
}

function showObjection() {
  const v = newView();
  const round = state.round;
  const objection = round.objections[round.idx];
  const card = shell('Your turn');
  roundHeader(card, objection);
  card.appendChild(h('p', { class: 'objection-text', text: objection.text }));

  const gate = round.readAloud ? readAloud(card, objection.text) : null;
  createTurn(card, {
    v,
    timerKey: 'opener',
    gate,
    onSubmit: (payload, turn) => submitOpener(v, objection, payload, turn),
    onSkip: () => skipObjection(v, objection),
  });
}

/**
 * Swap an unanswered objection via POST /rounds/{id}/skip. Resolves true when the screen has moved on
 * (new objection, next objection, or summary), or to an error message so the turn can reopen.
 */
async function skipObjection(v, objection) {
  const round = state.round;
  const res = await api(`/rounds/${encodeURIComponent(round.id)}/skip`, { method: 'POST', json: { objection_id: objection.id } });
  if (!alive(v) || state.round !== round) return true; // user left the screen meanwhile
  const data = res.data || {};
  if (!res.ok) {
    if (res.status === 409 && data.error === 'already_answered') {
      showSkipLink();
      return 'That objection was already answered, so it cannot be skipped. Use Next to move on.';
    }
    return errorText(res, "Couldn't skip this objection. Try again.");
  }
  if (data.objection) {
    round.objections[round.idx] = data.objection;
    showObjection();
    return true;
  }
  // Bank exhausted: the server just removed it, so the round is one shorter.
  round.objections.splice(round.idx, 1);
  if (round.idx >= round.objections.length) showSummary();
  else showObjection();
  return true;
}

async function submitOpener(v, objection, payload, turn) {
  const round = state.round;
  turn.setBusy('Thinking of a comeback...');
  const res = await api('/answers', {
    method: 'POST',
    form: buildAnswerForm({ round_id: round.id, objection_id: objection.id }, payload),
  });
  if (!alive(v)) return;
  const data = res.data || {};
  if (res.ok) {
    showFollowup(objection, data.answer_id, data.followup_text, describeAnswer(payload.mode, payload.text, data.opener_transcript));
    return;
  }
  if (res.status === 409) return recoverDuplicate(v, objection, turn);
  if (res.status === 413) return turn.reopen('Recording too large - type your answer instead', 'type');
  if (res.status === 400 && data.error === 'no_audio') return turn.reopen('No audio came through. Try recording again, or switch to Type.');
  if (res.status === 502 && data.error === 'followup_failed' && data.answer_id) {
    showComebackRetry(objection, data.answer_id, describeAnswer(payload.mode, payload.text, undefined));
    return;
  }
  turn.reopen(errorText(res, "Couldn't send your answer. Try again."));
}

/** What to show as "your answer": typed text, a transcript, or a note. */
function describeAnswer(mode, text, transcript) {
  if (mode !== 'speak') return { mode: 'type', text: text || '', empty: !text || !text.trim() };
  if (transcript === undefined || transcript === null) {
    return { mode: 'speak', text: 'Your recording was saved. The transcript appears with your feedback.', empty: false, note: true };
  }
  return { mode: 'speak', text: transcript, empty: !String(transcript).trim() };
}

/** Opener answer shown on the comeback screen. Voice with empty transcript shows the "couldn't hear" line. */
function openerBubble(info) {
  let body;
  if (info.empty && info.mode === 'speak') body = h('p', { class: 'empty', text: NO_AUDIO_TEXT });
  else if (info.empty) body = h('p', { class: 'muted', text: '(no answer given)' });
  else body = h('p', { text: info.text });
  return h('div', { class: 'bubble' }, h('div', { class: 'who', text: 'Your answer' }), body);
}

/** The server said "duplicate". Work out where that answer got to and carry on from there. */
async function recoverDuplicate(v, objection, turn) {
  turn.setBusy('That answer was already submitted. Picking up where it left off...');
  const sum = await api(`/rounds/${encodeURIComponent(state.round.id)}/summary`);
  if (!alive(v)) return;
  const row = sum.ok && sum.data && (sum.data.objections || []).find((o) => String(o.objection_id) === String(objection.id));
  const found = row ? await api(`/answers/${encodeURIComponent(row.answer_id)}`) : null;
  if (!alive(v)) return;
  if (!found || !found.ok) {
    turn.reopen('That answer was already submitted. Use Next to move on.');
    showSkipLink();
    return;
  }
  resumeFromAnswer(objection, found.data);
}

function showSkipLink() {
  const card = app.querySelector('.card');
  if (!card || card.querySelector('.skip-next')) return;
  card.appendChild(h('div', { class: 'row skip-next' }, h('button', { type: 'button', text: 'Next objection', onclick: () => nextObjection() })));
}

function resumeFromAnswer(objection, answer) {
  if (answer.reply_mode || answer.reply_text) return showResult(answer);
  const opener = describeAnswer(answer.opener_mode || 'type', answer.opener_text, answer.transcripts ? answer.transcripts.opener : undefined);
  if (answer.followup_text) return showFollowup(objection, answer.id, answer.followup_text, opener);
  showComebackRetry(objection, answer.id, opener);
}

function showComebackRetry(objection, answerId, opener) {
  const v = newView();
  const card = shell('Your answer is saved');
  roundHeader(card, objection);
  card.appendChild(h('p', { class: 'objection-text compact', text: objection.text }));
  card.appendChild(openerBubble(opener));
  const message = h('p', { class: 'status', role: 'status', text: "Couldn't get a comeback this time. Your answer is saved." });
  const retry = h('button', { type: 'button', class: 'primary', text: 'Retry comeback' });
  retry.addEventListener('click', async () => {
    retry.disabled = true;
    message.className = 'status info';
    message.textContent = 'Thinking of a comeback...';
    const res = await api(`/answers/${encodeURIComponent(answerId)}/followup`, { method: 'POST' });
    if (!alive(v)) return;
    if (res.ok && res.data && res.data.followup_text) {
      let info = opener;
      if (opener.note) {
        const a = await api(`/answers/${encodeURIComponent(answerId)}`);
        if (!alive(v)) return;
        if (a.ok && a.data && a.data.transcripts) info = describeAnswer('speak', '', a.data.transcripts.opener);
      }
      showFollowup(objection, answerId, res.data.followup_text, info);
      return;
    }
    message.className = 'status';
    message.textContent = errorText(res, "Still couldn't get a comeback.");
    retry.disabled = false;
  });
  card.appendChild(h('div', { class: 'row' }, retry, h('button', { type: 'button', text: 'Skip this objection', onclick: () => nextObjection() })));
  card.appendChild(message);
}

function showFollowup(objection, answerId, followupText, opener) {
  const v = newView();
  const round = state.round;
  const card = shell('Their comeback');
  roundHeader(card, objection);
  card.appendChild(h('p', { class: 'objection-text compact', text: objection.text }));
  card.appendChild(openerBubble(opener));
  card.appendChild(h('div', { class: 'bubble comeback' }, h('div', { class: 'who', text: 'Comeback' }), h('p', { text: followupText || '' })));

  const gate = round.readAloud && followupText ? readAloud(card, followupText) : null;
  createTurn(card, {
    v,
    timerKey: 'followup',
    gate,
    onSubmit: (payload, turn) => submitReply(v, objection, answerId, payload, turn),
  });
}

async function submitReply(v, objection, answerId, payload, turn) {
  const stop = judgingStatus((msg) => turn.setBusy(msg));
  const res = await api(`/answers/${encodeURIComponent(answerId)}/reply`, { method: 'POST', form: buildAnswerForm({}, payload) });
  stop();
  if (!alive(v)) return;
  const data = res.data || {};
  if (res.ok && data.answer) {
    showResult(Object.assign({}, data.answer, { status: data.status || data.answer.status, feedback: data.feedback || data.answer.feedback }));
    return;
  }
  if (res.status === 409) {
    const found = await api(`/answers/${encodeURIComponent(answerId)}`);
    if (!alive(v)) return;
    if (found.ok) return showResult(found.data);
  }
  if (res.status === 413) return turn.reopen('Recording too large - type your answer instead', 'type');
  if (res.status === 400 && data.error === 'no_audio') return turn.reopen('No audio came through. Try recording again, or switch to Type.');
  turn.reopen(errorText(res, "Couldn't send your reply. Try again."));
}

function nextObjection() {
  const round = state.round;
  round.idx += 1;
  if (round.idx >= round.objections.length) showSummary();
  else showObjection();
}

/* ------------------------------------------------------------------ 5c. result routing, failed, calibration */

function showResult(answer) {
  const round = state.round;
  if (answer.status !== 'judged' || !answer.feedback) showFailed(answer);
  else if (round && round.calibration && !answer.calibrated) showCalibration(answer);
  else showFeedback(answer);
}

function showFailed(answer) {
  const v = newView();
  const card = shell("Couldn't judge this answer - it's saved");
  if (state.round) roundHeader(card, state.round.objections[state.round.idx]);
  card.appendChild(h('p', { class: 'muted', text: 'Your answers are stored. Re-judge will try again on the saved text.' }));
  const status = h('p', { class: 'status', role: 'status' });
  const rejudgeBtn = h('button', { type: 'button', class: 'primary', text: 'Re-judge' });
  rejudgeBtn.addEventListener('click', async () => {
    rejudgeBtn.disabled = true;
    status.className = 'status info';
    const stop = judgingStatus((msg) => { status.textContent = msg; });
    const res = await api(`/answers/${encodeURIComponent(answer.id)}/rejudge`, { method: 'POST' });
    stop();
    if (!alive(v)) return;
    const data = res.data || {};
    if (res.ok && data.answer) {
      showResult(Object.assign({}, data.answer, { status: data.status || data.answer.status, feedback: data.feedback || data.answer.feedback }));
      return;
    }
    status.className = 'status';
    status.textContent = errorText(res, "Couldn't judge it again.");
    rejudgeBtn.disabled = false;
  });
  const buttons = h('div', { class: 'row' }, rejudgeBtn);
  if (state.round) buttons.appendChild(h('button', { type: 'button', text: nextLabel(), onclick: () => nextObjection() }));
  card.appendChild(buttons);
  card.appendChild(status);
}

function nextLabel() {
  const r = state.round;
  return r && r.idx + 1 >= r.objections.length ? 'See round summary' : 'Next objection';
}

/** Collect a reviewer's own scores before the AI feedback is revealed. */
function showCalibration(answer) {
  const v = newView();
  const round = state.round;
  const card = shell('Score it yourself first');
  roundHeader(card, round.objections[round.idx]);
  card.appendChild(h('p', { class: 'muted', text: `Reviewer: ${REVIEWER_LABELS[round.reviewer] || round.reviewer}. The AI feedback is hidden until you submit.` }));
  card.appendChild(h('p', { class: 'objection-text compact', text: answer.objection_text || '' }));
  card.appendChild(answersSection(answer, false));

  const inputs = {};
  const grid = h('div', { class: 'cal-grid' });
  for (const [key, label] of DIMENSIONS) {
    const input = h('input', { type: 'number', id: `cal-${key}`, min: 1, max: 5, step: 1, required: true, inputmode: 'numeric' });
    inputs[key] = input;
    grid.appendChild(h('label', { for: `cal-${key}`, text: `${label} (1-5)` }));
    grid.appendChild(input);
  }
  const seeded = h('input', { type: 'checkbox', id: 'cal-seeded' });
  const msg = h('p', { class: 'status', role: 'status' });
  const submit = h('button', { type: 'button', class: 'primary', text: 'Save my scores and reveal feedback' });
  let sent = false;
  submit.addEventListener('click', async () => {
    if (sent) return;
    const scores = {};
    for (const [key, label] of DIMENSIONS) {
      const n = Number(inputs[key].value);
      if (!Number.isInteger(n) || n < 1 || n > 5) {
        msg.textContent = `${label} needs a whole number from 1 to 5.`;
        inputs[key].focus();
        return;
      }
      scores[key] = n;
    }
    sent = true;
    submit.disabled = true;
    msg.textContent = '';
    const res = await api(`/answers/${encodeURIComponent(answer.id)}/calibration`, {
      method: 'POST',
      json: { reviewer: round.reviewer, scores, seeded_error: seeded.checked },
    });
    if (!alive(v)) return;
    if (!res.ok) {
      sent = false;
      submit.disabled = false;
      msg.textContent = errorText(res, "Couldn't save your scores.");
      return;
    }
    showFeedback(Object.assign({}, answer, { calibrated: true }));
  });

  card.appendChild(
    h(
      'div',
      { class: 'calibration-form' },
      h('h3', { text: 'Your scores' }),
      grid,
      h('label', { class: 'check' }, seeded, h('span', { text: 'This answer contains a deliberate error' })),
      h('div', { class: 'row', style: 'margin-top:14px' }, submit),
      msg
    )
  );
}

/* ------------------------------------------------------------------ 5d. feedback */

/** Your answers (typed text or transcript) with audio players when recordings exist. */
function answersSection(answer, withAudio) {
  const wrap = h('div', { class: 'answers' });
  const transcripts = answer.transcripts || null;
  const turns = [
    ['opener', 'Your answer', answer.opener_mode, answer.opener_text, answer.has_opener_audio],
    ['reply', 'Your reply', answer.reply_mode, answer.reply_text, answer.has_reply_audio],
  ];
  for (const [turnName, label, mode, typed, hasAudio] of turns) {
    const block = h('div', { class: 'answer-block', 'data-turn': turnName }, h('div', { class: 'who', text: label }));
    let text = typed;
    if (mode === 'speak' || (transcripts && typeof transcripts[turnName] === 'string' && typed === null)) {
      text = transcripts ? transcripts[turnName] : null;
    }
    if (text === '' || text === null || text === undefined || !String(text).trim()) {
      block.appendChild(h('p', { class: 'empty', text: mode === 'speak' ? NO_AUDIO_TEXT : '(no answer given)' }));
    } else {
      block.appendChild(h('p', { text }));
    }
    if (withAudio && hasAudio) {
      block.appendChild(h('audio', { controls: true, preload: 'none', src: `/audio/${encodeURIComponent(answer.id)}/${turnName}`, 'aria-label': `${label} recording` }));
    }
    wrap.appendChild(block);
  }
  return wrap;
}

function flagsSection(flags) {
  const section = h('div', {});
  if (!flags || flags.length === 0) {
    section.appendChild(h('p', { class: 'muted', text: 'No flags raised.' }));
    return section;
  }
  for (const [turnName, label] of [['opener', 'Your answer'], ['reply', 'Your reply']]) {
    const group = flags.filter((f) => f.turn === turnName);
    if (!group.length) continue;
    section.appendChild(
      h(
        'div',
        { class: 'flag-group' },
        h('h4', { text: label }),
        h('ul', { class: 'flag-list' }, group.map((f) => h('li', { class: f.type }, h('span', { class: 'flag-type', text: FLAG_LABELS[f.type] || f.type }), f.detail || '')))
      )
    );
  }
  return section;
}

function showFeedback(answer) {
  const v = newView();
  const round = state.round;
  const fb = answer.feedback;
  const card = shell('Feedback');
  if (round) roundHeader(card, round.objections[round.idx]);
  card.appendChild(h('p', { class: 'objection-text compact', text: answer.objection_text || '' }));

  const overall = answer.overall !== null && answer.overall !== undefined ? answer.overall : fb.overall;
  card.appendChild(h('div', { class: 'overall' }, h('span', { class: 'big', text: fmtNum(overall, 0) }), h('span', { class: 'of', text: '/ 5 overall' })));
  for (const [key, label] of DIMENSIONS) card.appendChild(scoreBar(label, fb.scores ? fb.scores[key] : null));

  card.appendChild(h('h3', { text: 'Flags' }));
  card.appendChild(flagsSection(fb.flags));

  card.appendChild(h('h3', { text: 'Tips' }));
  card.appendChild(h('ul', { class: 'tips' }, (fb.tips || []).map((t) => h('li', { text: t }))));

  card.appendChild(h('details', { class: 'model' }, h('summary', { text: 'Model answer' }), h('p', { text: fb.model_answer || '' })));

  const notes = fb.notes || {};
  const noteItems = [['Tone', notes.tone], ['Persuasiveness', notes.persuasiveness], ['Delivery', notes.delivery]].filter(([, t]) => t);
  if (noteItems.length) {
    card.appendChild(h('h3', { text: 'Notes' }));
    card.appendChild(h('dl', { class: 'notes' }, noteItems.map(([label, text]) => [h('dt', { text: label }), h('dd', { text })])));
  }

  card.appendChild(h('h3', { text: 'What you said' }));
  const answers = answersSection(answer, true);
  card.appendChild(answers);

  const delMsg = h('p', { class: 'status info', role: 'status' });
  if (answer.has_opener_audio || answer.has_reply_audio) {
    const delBtn = h('button', { type: 'button', class: 'danger', text: 'Delete recordings' });
    delBtn.addEventListener('click', async () => {
      if (!window.confirm("Delete the recordings for this answer? Transcripts and scores are kept. This doesn't delete Google's copy.")) return;
      delBtn.disabled = true;
      const res = await api(`/answers/${encodeURIComponent(answer.id)}/audio`, { method: 'DELETE' });
      if (!alive(v)) return;
      if (res.ok) {
        answers.querySelectorAll('audio').forEach((a) => a.remove());
        delBtn.remove();
        delMsg.className = 'status info';
        delMsg.textContent = 'Recordings deleted. Transcripts and scores are kept.';
      } else {
        delBtn.disabled = false;
        delMsg.className = 'status';
        delMsg.textContent = errorText(res, "Couldn't delete the recordings.");
      }
    });
    card.appendChild(h('div', { class: 'row' }, delBtn));
  }
  card.appendChild(delMsg);

  if (answer.cost_usd !== null && answer.cost_usd !== undefined) {
    card.appendChild(h('p', { class: 'muted small', text: `Cost of this answer: ${fmtMoney(answer.cost_usd)}` }));
  }
  card.appendChild(h('p', { class: 'disclaimer', text: DISCLAIMER }));
  if (round) card.appendChild(h('div', { class: 'row' }, h('button', { type: 'button', class: 'primary', id: 'next', text: nextLabel(), onclick: () => nextObjection() })));
}

/* ------------------------------------------------------------------ 5e. round summary */

async function showSummary() {
  const v = newView();
  const round = state.round;
  round.active = false;
  const card = shell('Round complete');
  card.appendChild(h('p', { class: 'muted', text: 'Loading your summary...' }));
  const res = await api(`/rounds/${encodeURIComponent(round.id)}/summary`);
  if (!alive(v)) return;
  card.replaceChildren(h('h2', { text: 'Round complete', tabindex: -1 }));
  const data = res.data;
  if (!res.ok || !data) {
    card.appendChild(h('p', { class: 'status', text: errorText(res, "Couldn't load the summary.") }));
  } else {
    card.appendChild(h('h3', { text: 'Average per dimension' }));
    for (const [key, label] of DIMENSIONS) card.appendChild(scoreBar(label, data.dimension_means ? data.dimension_means[key] : null));
    card.appendChild(h('h3', { text: 'Weakest category' }));
    card.appendChild(h('p', { text: data.weakest_category ? capitalise(data.weakest_category) : 'Not enough judged answers to say.' }));
    card.appendChild(h('h3', { text: 'Your objections' }));
    card.appendChild(
      h(
        'ul',
        { class: 'summary-list' },
        (data.objections || []).map((o) =>
          h(
            'li',
            {},
            h('span', {}, chip(o.category), ' ', o.text),
            h('span', { class: 'score', text: o.overall !== null && o.overall !== undefined ? `${fmtNum(o.overall, 0)}/5` : o.status === 'judged' ? '-' : `Not judged (${o.status})` })
          )
        )
      )
    );
  }
  card.appendChild(h('p', { class: 'disclaimer', text: DISCLAIMER }));
  card.appendChild(
    h('div', { class: 'row' }, h('button', { type: 'button', class: 'primary large', id: 'new-round', text: 'New round', onclick: () => showStart() }), h('a', { href: '#history', text: 'History' }))
  );
}

/* ------------------------------------------------------------------ 6. history */

async function showHistory() {
  state.round = null;
  const v = newView();
  const card = shell('History');
  card.appendChild(h('p', { class: 'muted', text: 'Loading...' }));
  const res = await api('/history');
  if (!alive(v)) return;
  card.replaceChildren(h('h2', { text: 'History', tabindex: -1 }));
  if (!res.ok || !res.data) {
    card.appendChild(h('p', { class: 'status', text: errorText(res, "Couldn't load history.") }));
    return;
  }
  const d = res.data;

  card.appendChild(h('p', { class: 'row' }, h('strong', { text: 'Total spend: ' }), h('span', { id: 'spend', text: fmtMoney(d.spend_usd) })));

  card.appendChild(historySection('Rounds', roundsTable(d.rounds || [])));
  card.appendChild(historySection('Category trend (last 10 rounds)', trendTable(d.category_trend || [])));
  card.appendChild(historySection('Calibration', calibrationBlock(d.calibration || [])));
  card.appendChild(historySection('Pending or failed answers', pendingBlock(d.pending || [])));
  card.appendChild(h('div', { class: 'row', style: 'margin-top:24px' }, h('a', { href: '#start', text: 'Back to practice' })));
}

function historySection(title, body) {
  return h('div', { class: 'history-section' }, h('h3', { text: title }), body);
}

function roundsTable(rounds) {
  if (!rounds.length) return h('p', { class: 'muted', text: 'No rounds yet.' });
  return table(
    ['Started', 'Category', 'Mode', 'Answered', 'Mean overall'],
    rounds.map((r) => [fmtDate(r.started_at), r.category_filter ? capitalise(r.category_filter) : 'All', r.study_mode ? 'Study' : 'Timed', String(r.answered), fmtNum(r.mean_overall)]),
    [3, 4]
  );
}

function trendTable(trend) {
  if (!trend.length) return h('p', { class: 'muted', text: 'No judged answers yet.' });
  return table(['Category', 'Mean overall', 'Answers'], trend.map((t) => [capitalise(t.category), fmtNum(t.mean_overall), String(t.n)]), [1, 2]);
}

function calibrationBlock(rows) {
  if (!rows.length) return h('p', { class: 'muted', text: 'No calibration scores yet.' });
  return h(
    'div',
    {},
    rows.map((c) => {
      const pct = c.total > 0 ? Math.round((100 * c.within) / c.total) : null;
      return h(
        'div',
        { class: 'bubble calibration-row' },
        h('div', { class: 'who', text: `${REVIEWER_LABELS[c.reviewer] || c.reviewer} - prompt ${c.prompt_version}` }),
        h('p', { text: `Within ±1 on ${c.within} of ${c.total} dimension-scores (${pct === null ? '-' : pct + '%'})` }),
        h('p', { text: `Seeded errors flagged: ${c.seeded_flagged} of ${c.seeded_total}` })
      );
    })
  );
}

function pendingBlock(pending) {
  if (!pending.length) return h('p', { class: 'muted', text: 'Nothing waiting. Every saved answer has been judged.' });
  return table(
    ['Objection', 'Status', ''],
    pending.map((p) => {
      const statusCell = h('span', { class: 'pill', text: p.status });
      const msg = h('span', { class: 'muted small' });
      const btn = h('button', { type: 'button', text: 'Re-judge' });
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        msg.textContent = ' Judging...';
        const res = await api(`/answers/${encodeURIComponent(p.answer_id)}/rejudge`, { method: 'POST' });
        const status = res.data && res.data.status;
        if (res.ok && status === 'judged') {
          statusCell.textContent = 'judged';
          msg.textContent = ' Done.';
          btn.remove();
        } else {
          statusCell.textContent = status || p.status;
          msg.textContent = ` ${errorText(res, 'Still failed.')}`;
          btn.disabled = false;
        }
      });
      return [p.objection_text || '', statusCell, h('span', {}, btn, msg)];
    })
  );
}

/* ------------------------------------------------------------------ boot */

async function init() {
  wireNav();
  const res = await api('/config');
  if (!res.ok || !res.data) {
    app.replaceChildren(
      h(
        'section',
        { class: 'card' },
        h('h2', { text: "Can't reach the server" }),
        h('p', { class: 'muted', text: errorText(res, 'The app could not load its settings.') }),
        h('button', { type: 'button', class: 'primary', text: 'Try again', onclick: () => window.location.reload() })
      )
    );
    return;
  }
  state.config = res.data;
  route();
}

init();
