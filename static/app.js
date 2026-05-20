/* ReviewLoop — Frontend JS */
/* Polling, approval-gate handlers, dynamic DOM updates */

'use strict';

// ── Utilities ─────────────────────────────────────────────────────────────

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function apiCall(url, method, body) {
  return fetch(url, {
    method: method || 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }).then(r => {
    if (!r.ok) return r.json().then(d => Promise.reject(d.error || r.statusText));
    return r.json();
  });
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.style.cssText = [
    'position:fixed;bottom:20px;right:20px;padding:10px 18px;border-radius:8px;',
    'font-size:13px;font-weight:600;z-index:9999;transition:opacity .4s;',
    type === 'error'
      ? 'background:#2a1010;border:1px solid #6b2020;color:#f87171;'
      : 'background:#0f2a1a;border:1px solid #1e6b3a;color:#4ade80;'
  ].join('');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 400); }, 3000);
}

function setLanguage(lang) {
  apiCall('/api/language', 'POST', { lang }).then(() => {
    window.location.reload();
  }).catch(e => showToast(t('toast.error') + e, 'error'));
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('reviewloop-theme', next);
}

function dismissOnboarding() {
  const panel = document.getElementById('onboarding-panel');
  if (panel) panel.remove();
  localStorage.setItem('reviewloop-onboarding-dismissed', '1');
}

(function initMarketReadyChrome() {
  const theme = localStorage.getItem('reviewloop-theme') || 'dark';
  document.documentElement.dataset.theme = theme;
  const panel = document.getElementById('onboarding-panel');
  if (panel && localStorage.getItem('reviewloop-onboarding-dismissed') === '1') panel.remove();
})();

// ── API Keys Modal ────────────────────────────────────────────────────────

function toggleKeyModal() {
  const m = document.getElementById('key-modal');
  if (!m) return;
  m.style.display = m.style.display === 'none' ? 'flex' : 'none';
  if (m.style.display !== 'none') loadKeyStatus();
}

function loadKeyStatus() {
  fetch('/api/keys/status').then(r => r.json()).then(data => {
    Object.entries(data).forEach(([k, v]) => {
      const el = document.getElementById('ind-' + k);
      if (el) el.textContent = v ? '\u2713' : '';
    });
  }).catch(() => {});
}

function saveKeys() {
  const keys = {
    anthropic: document.getElementById('modal-key-anthropic')?.value.trim() || '',
    google:    document.getElementById('modal-key-google')?.value.trim()    || '',
    openai:    document.getElementById('modal-key-openai')?.value.trim()    || '',
    deepseek:  document.getElementById('modal-key-deepseek')?.value.trim()  || '',
  };
  apiCall('/api/keys', 'POST', keys).then(() => {
    loadKeyStatus();
    const msg = document.getElementById('key-save-msg');
    if (msg) { msg.textContent = '\u2713 ' + t('toast.saved'); setTimeout(() => msg.textContent = '', 2500); }
    showToast(t('toast.keys_saved'), 'success');
  }).catch(e => showToast(t('toast.error') + e, 'error'));
}

// ── Run Page: Polling ─────────────────────────────────────────────────────
// Only active when RUN_ID is defined (set inline in run.html)

let _pollTimer = null;
let _lastStatuses = {};
const POLL_FAST = 2000;   // while a phase is running
const POLL_IDLE = 8000;   // nothing running — reduces extension chatter

function startPolling() {
  if (typeof RUN_ID === 'undefined') return;
  pollStatus();
}

function stopPolling() {
  clearTimeout(_pollTimer);
  _pollTimer = null;
}

function _schedulePoll(ms) {
  clearTimeout(_pollTimer);
  _pollTimer = setTimeout(pollStatus, ms);
}

function pollStatus() {
  if (typeof RUN_ID === 'undefined') return;
  fetch('/api/run/' + RUN_ID + '/status')
    .then(r => r.json())
    .then(data => {
      updateOverallBadge(data.overall_status);
      updateStepper(data.phases);
      data.phases.forEach((phase, idx) => {
        if (_lastStatuses[idx] !== phase.status) {
          _lastStatuses[idx] = phase.status;
          updatePhaseCard(idx, phase, data);
        }
      });
      // Phase 2 live progress
      if (data.live_review) updateLiveReview(data.live_review);
      // Adaptive interval: fast while running, slow at rest
      const anyRunning = data.phases.some(p => p.status === 'running');
      _schedulePoll(anyRunning ? POLL_FAST : POLL_IDLE);
    })
    .catch(() => { _schedulePoll(POLL_IDLE); });
}

function updateOverallBadge(status) {
  const el = document.getElementById('overall-badge');
  if (!el) return;
  const labels = {
    pending: t('status.pending'), running: t('status.running'),
    review: t('status.review'), completed: t('status.completed'), rejected: t('status.rejected')
  };
  el.textContent = labels[status] || status;
  el.className = 'status-badge badge-' + status;
}

function updateStepper(phases) {
  phases.forEach((phase, idx) => {
    const step = document.getElementById('step-' + idx);
    if (!step) return;
    step.className = 'stepper-step step-' + phase.status;
  });
}

function updatePhaseCard(idx, phase, fullData) {
  const card = document.getElementById('phase-card-' + idx);
  if (!card) return;

  // Update card border class
  card.className = 'phase-card status-' + phase.status;

  // Update status badge
  const badge = document.getElementById('badge-' + idx);
  if (badge) {
    const labels = {
      pending: t('status.pending'), running: t('status.running_dots'), review: t('status.review'),
      approved: t('status.approved'), rejected: t('status.rejected')
    };
    badge.textContent = labels[phase.status] || phase.status;
    badge.className = 'status-badge badge-' + phase.status;
  }

  // Update body based on new status
  if (phase.status === 'running') {
    renderPhaseRunning(idx);
  } else if (phase.status === 'review') {
    renderPhaseReview(idx, phase);
  }

  // Update approval gates
  renderApprovalGates(idx, phase);
}

function renderPhaseRunning(idx) {
  const body = document.getElementById('body-' + idx);
  if (!body) return;
  const msgs = [
    t('phase.running_review'),
    t('phase.running_reviewers'),
    t('phase.running_consolidation'),
    t('phase.running_task'),
    t('phase.running_file')
  ];
  body.innerHTML = '<div class="status-bar status-running"><div class="spinner"></div><span>' + (msgs[idx] || 'L\u00e4uft...') + '</span></div>';
  if (idx === 1) {
    body.innerHTML += '<div id="phase2-live-results"></div>';
  }
}

function renderPhaseReview(idx, phase) {
  const body = document.getElementById('body-' + idx);
  if (!body) return;

  if (idx === 0) {
    const text = (phase.result && phase.result.yaml_text) || '';
    const dur = (phase.result && phase.result.duration) ? '<div class="result-meta">\u23F1 ' + phase.result.duration + 's</div>' : '';
    const errBox = phase.error ? '<div class="error-box">' + esc(phase.error) + '</div>' : '';
    body.innerHTML = '<div class="result-label">' + t('review.generated_label') + '</div>'
      + '<textarea class="result-textarea" id="result-text-' + idx + '">' + esc(text) + '</textarea>'
      + dur + errBox;
  } else if (idx === 1) {
    const results = (phase.result && phase.result.results) || {};
    const errors  = (phase.result && phase.result.errors)  || {};
    let html = '<div id="phase2-results-display">';
    Object.entries(results).forEach(([kid, r]) => {
      html += '<details class="reviewer-result" open>'
        + '<summary><span class="reviewer-result-name">' + esc(r.ki_name) + '</span>'
        + '<span class="reviewer-result-meta">\u23F1 ' + r.duration + 's &nbsp; ' + esc(r.role || '') + '</span></summary>'
        + '<pre class="result-pre">' + esc(r.text) + '</pre></details>';
    });
    Object.entries(errors).forEach(([kid, e]) => {
      html += '<div class="reviewer-error">\u2717 ' + esc(e.ki_name || kid) + ': ' + esc(e.error) + '</div>';
    });
    html += '<div class="phase2-save-row">'
      + '<a class="btn btn-secondary btn-sm" href="/api/run/' + encodeURIComponent(RUN_ID) + '/phase/1/export" download>'
      + '&#8681; Download (.md)</a>'
      + '<button class="btn btn-secondary btn-sm" onclick="savePhase2()">&#128190; In Ordner speichern</button>'
      + '<span id="phase2-save-msg" style="font-size:11px;color:var(--text-dim)"></span>'
      + '</div>';
    if (phase.result && phase.result.auto_saved_path) {
      html += '<div class="result-meta" style="margin-top:8px">&#9989; Auto-save: ' + esc(phase.result.auto_saved_path) + '</div>';
    } else if (phase.result && phase.result.auto_save_error) {
      html += '<div class="error-box">Auto-save Fehler: ' + esc(phase.result.auto_save_error) + '</div>';
    }
    html += '</div>';
    if (phase.error) html += '<div class="error-box">' + esc(phase.error) + '</div>';
    body.innerHTML = html;
  } else if (idx === 2) {
    const text = (phase.result && phase.result.consolidation_text) || '';
    const dur  = (phase.result && phase.result.duration) ? '<div class="result-meta">\u23F1 ' + phase.result.duration + 's</div>' : '';
    const errBox = phase.error ? '<div class="error-box">' + esc(phase.error) + '</div>' : '';
    body.innerHTML = '<div class="result-label">' + t('review.consolidated_label') + '</div>'
      + '<textarea class="result-textarea result-textarea-tall" id="result-text-' + idx + '">' + esc(text) + '</textarea>'
      + dur
      + '<div class="phase2-save-row">'
      + '<button class="btn btn-secondary btn-sm" onclick="savePhase3()">&#128190; Konsolidierung speichern</button>'
      + '<span id="phase3-save-msg" style="font-size:11px;color:var(--text-dim)"></span>'
      + '</div>'
      + ((phase.result && phase.result.auto_saved_path)
        ? ('<div class="result-meta" style="margin-top:8px">&#9989; Auto-save: ' + esc(phase.result.auto_saved_path) + '</div>')
        : ((phase.result && phase.result.auto_save_error)
          ? ('<div class="error-box">Auto-save Fehler: ' + esc(phase.result.auto_save_error) + '</div>')
          : ''))
      + errBox;
  } else if (idx === 3) {
    const text = (phase.result && (phase.result.task_yaml || phase.result.task_markdown)) || '';
    const dur  = (phase.result && phase.result.duration) ? '<div class="result-meta">\u23F1 ' + phase.result.duration + 's</div>' : '';
    const thinkBudget = phase.result && phase.result.thinking_budget;
    const thinkBadge  = thinkBudget ? ' <span class="thinking-badge">\uD83E\uDD14 ' + thinkBudget.toLocaleString('de') + ' Thinking-Token</span>' : '';
    const cacheRead   = phase.result && phase.result.cache_read_tokens;
    const cacheBadge  = cacheRead ? ' <span class="cache-badge">\u26A1 ' + cacheRead.toLocaleString('de') + ' cached</span>' : '';
    const km   = (phase.result && phase.result.km_model) ? '<div class="result-meta" style="margin-bottom:6px">\uD83C\uDFB8 ' + esc(phase.result.km_model) + thinkBadge + cacheBadge + '</div>' : '';
    const errBox = phase.error ? '<div class="error-box">' + esc(phase.error) + '</div>' : '';
    body.innerHTML = km + '<div class="result-label">' + t('review.cursor_task_label') + '</div>'
      + '<textarea class="result-textarea result-textarea-tall" id="result-text-' + idx + '">' + esc(text) + '</textarea>'
      + dur + errBox;
  } else if (idx === 4) {
    const fp = phase.result && phase.result.file_path;
    const fn = phase.result && phase.result.filename;
    const errBox = phase.error ? '<div class="error-box">' + esc(phase.error) + '</div>' : '';
    if (fp) {
      body.innerHTML = '<div class="file-written-box">'
        + '<div class="file-written-icon">\uD83D\uDCE4</div>'
        + '<div>'
        + '<div class="file-written-title">' + t('file.written') + '</div>'
        + '<div class="file-written-path">' + esc(fp) + '</div>'
        + '<div class="file-written-hint">' + t('file.open_hint') + ' <code>@' + esc(fn || '') + '</code>.</div>'
        + '</div></div>'
        + '<div class="phase2-save-row">'
        + '<button class="btn btn-secondary btn-sm" onclick="generateTokenReport()">&#128202; Token-Report (.yaml)</button>'
        + '<span id="token-report-msg" style="font-size:11px;color:var(--text-dim)"></span>'
        + '</div>'
        + errBox;
    } else {
      body.innerHTML = errBox || '<div class="phase-hint">Phase abgeschlossen.</div>';
    }
  }
}

function renderApprovalGates(idx, phase) {
  const gates = document.getElementById('gates-' + idx);
  if (!gates) return;

  if (phase.status === 'running') {
    gates.innerHTML = '<button class="btn btn-reject" onclick="cancelPhase(' + idx + ')">\u25A0 ' + t('phase.cancel') + '</button>'
      + '<span style="font-size:12px;color:var(--text-dim);margin-left:4px">Running until the next API response.</span>';
  } else if (phase.status === 'review') {
    let html = '';
    if (idx === 4) {
      html = '<button class="btn btn-approve" onclick="approvePhase(' + idx + ')">\u2713 ' + t('phase.done') + '</button>'
           + '<button class="btn btn-reject" onclick="rejectPhase(' + idx + ')">\u21BB ' + t('phase.retry') + '</button>';
    } else if (idx === 1) {
      html = '<button class="btn btn-approve" onclick="approvePhase(' + idx + ')">\u2713 ' + t('phase.approve_reviews') + '</button>'
           + '<button class="btn btn-reject" onclick="rejectPhase(' + idx + ')">\u2717 ' + t('phase.reject') + '</button>';
    } else {
      html = '<button class="btn btn-approve" onclick="approvePhase(' + idx + ')">\u2713 ' + t('phase.approve') + '</button>'
           + '<button class="btn btn-reject" onclick="rejectPhase(' + idx + ')">\u2717 ' + t('phase.reject') + '</button>'
           + '<button class="btn btn-ghost btn-sm" onclick="saveEdit(' + idx + ')">\uD83D\uDCBE ' + t('phase.save_changes') + '</button>';
    }
    gates.innerHTML = html;
  } else if (phase.status === 'rejected') {
    gates.innerHTML = '<button class="btn btn-secondary" onclick="retryPhase(' + idx + ')">\u21BB ' + t('phase.retry') + '</button>';
  } else if (phase.status === 'approved') {
    gates.innerHTML = '<span class="approved-stamp">\u2713 ' + t('phase.approved') + '</span>';
  } else {
    gates.innerHTML = '';
  }
}

// ── Phase 2: Live Review Progress ─────────────────────────────────────────

function updateLiveReview(state) {
  const prog = document.getElementById('phase2-progress');
  const liveResults = document.getElementById('phase2-live-results');
  if (!prog && !liveResults) return;

  const done = state.done_count || 0;
  const total = state.total_count || 0;
  const names = state.done_names || [];

  if (prog) {
    prog.innerHTML = '<div class="spinner"></div><span id="phase2-progress-text">'
      + (state.running ? '\u23F3' : '\u2713') + ' ' + done + '/' + total + ' fertig'
      + (names.length ? ' \u2014 ' + names.join(', ') : '') + '</span>';
  }

  if (liveResults && state.results) {
    let html = '';
    Object.entries(state.results).forEach(([kid, r]) => {
      html += '<details class="reviewer-result" open>'
        + '<summary><span class="reviewer-result-name">' + esc(r.ki_name) + '</span>'
        + '<span class="reviewer-result-meta">\u23F1 ' + r.duration + 's</span></summary>'
        + '<pre class="result-pre">' + esc(r.text) + '</pre></details>';
    });
    if (state.errors) {
      Object.entries(state.errors).forEach(([kid, e]) => {
        html += '<div class="reviewer-error">\u2717 ' + esc(e.ki_name || kid) + ': ' + esc(e.error) + '</div>';
      });
    }
    liveResults.innerHTML = html;
  }
}

// ── Orchestrator Model Selection (km_* technical names remain) ───────────

function selectKM(btn, idx) {
  const container = document.getElementById('km-choices-' + idx);
  if (container) container.querySelectorAll('.km-choice-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  // Show effort row for providers that support thinking (Anthropic, Google)
  const effortRow = document.getElementById('km-effort-row-' + idx);
  const supportsThinking = btn.dataset.provider === 'anthropic' || btn.dataset.provider === 'google';
  if (effortRow) effortRow.style.display = supportsThinking ? 'flex' : 'none';
}

function selectEffort(btn, idx) {
  const container = document.getElementById('km-effort-' + idx);
  if (container) container.querySelectorAll('.km-effort-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// ── Temperature Controls ─────────────────────────────────────────────────

function updateTempDisplay(slider, idx) {
  const val = parseFloat(slider.value).toFixed(1);
  const display = document.getElementById('km-temp-val-' + idx);
  if (display) display.textContent = val;
  _highlightTempPreset(idx, val);
}

function setTemp(val, idx) {
  const slider = document.getElementById('km-temp-slider-' + idx);
  if (slider) { slider.value = val; updateTempDisplay(slider, idx); }
}

function _highlightTempPreset(idx, val) {
  const row = document.getElementById('km-temp-row-' + idx);
  if (!row) return;
  row.querySelectorAll('.temp-preset-btn').forEach(b => {
    b.classList.toggle('active', parseFloat(b.textContent).toFixed(1) === parseFloat(val).toFixed(1));
  });
}

function updateReviewerTempDisplay(slider) {
  const val = parseFloat(slider.value).toFixed(1);
  const display = document.getElementById('reviewer-temp-val');
  if (display) display.textContent = val;
  const row = document.getElementById('reviewer-temp-row');
  if (row) row.querySelectorAll('.temp-preset-btn').forEach(b => {
    b.classList.toggle('active', parseFloat(b.textContent).toFixed(1) === parseFloat(val).toFixed(1));
  });
}

function setReviewerTemp(val) {
  const slider = document.getElementById('reviewer-temp-slider');
  if (slider) { slider.value = val; updateReviewerTempDisplay(slider); }
}

function _getKMTemperature(idx) {
  const slider = document.getElementById('km-temp-slider-' + idx);
  return slider ? parseFloat(slider.value) : 0.2;
}

function _getReviewerTemperature() {
  const slider = document.getElementById('reviewer-temp-slider');
  return slider ? parseFloat(slider.value) : 0.2;
}

function _getSelectedKM(idx) {
  const container = document.getElementById('km-choices-' + idx);
  if (!container) return {};
  const active = container.querySelector('.km-choice-btn.active');
  if (!active) return {};
  const effortContainer = document.getElementById('km-effort-' + idx);
  const effortActive = effortContainer && effortContainer.querySelector('.km-effort-btn.active');
  const supportsThinking = active.dataset.provider === 'anthropic' || active.dataset.provider === 'google';
  const effort = (supportsThinking && effortActive)
    ? (effortActive.dataset.effort || 'none')
    : 'none';
  return {
    km_provider:     active.dataset.provider || null,
    km_model:        active.dataset.model    || null,
    thinking_effort: effort,
    temperature:     _getKMTemperature(idx),
  };
}

// Init: hide effort row for providers that don't support thinking
(function() {
  document.querySelectorAll('[id^="km-choices-"]').forEach(container => {
    const idx = container.id.replace('km-choices-', '');
    const active = container.querySelector('.km-choice-btn.active');
    const effortRow = document.getElementById('km-effort-row-' + idx);
    const supportsThinking = active && (active.dataset.provider === 'anthropic' || active.dataset.provider === 'google');
    if (effortRow && !supportsThinking) {
      effortRow.style.display = 'none';
    }
  });
})();

// ── Approval Gate Actions ─────────────────────────────────────────────────

function startPhase(idx) {
  if (typeof RUN_ID === 'undefined') return;
  for (let i = 0; i < idx; i++) {
    if (_lastStatuses[i] !== 'approved') {
      showToast('Phase ' + (i + 1) + ' muss zuerst freigegeben werden (aktuell: ' + (_lastStatuses[i] || '?') + ').', 'error');
      return;
    }
  }
  const kmData = _getSelectedKM(idx);
  apiCall('/api/run/' + RUN_ID + '/phase/' + idx + '/start', 'POST', kmData)
    .then(() => {
      renderPhaseRunning(idx);
      renderApprovalGates(idx, { status: 'running' });
      const card = document.getElementById('phase-card-' + idx);
      if (card) card.className = 'phase-card status-running';
      const badge = document.getElementById('badge-' + idx);
      if (badge) { badge.textContent = 'L\u00e4uft...'; badge.className = 'status-badge badge-running'; }
    })
    .catch(e => showToast('Fehler beim Starten: ' + e, 'error'));
}

function cancelPhase(idx) {
  if (typeof RUN_ID === 'undefined') return;
  if (!confirm('Phase abbrechen? Der laufende API-Call wird noch abgewartet, das Ergebnis wird dann verworfen.')) return;
  apiCall('/api/run/' + RUN_ID + '/phase/' + idx + '/cancel', 'POST', {})
    .then(() => {
      showToast('Phase ' + (idx + 1) + ' abgebrochen', 'error');
      _lastStatuses[idx] = null;
    })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

function selectReviewerEffort(btn) {
  const container = document.getElementById('reviewer-effort');
  if (container) container.querySelectorAll('.km-effort-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function _getReviewerEffort() {
  const container = document.getElementById('reviewer-effort');
  if (!container) return 'none';
  const active = container.querySelector('.km-effort-btn.active');
  return active ? (active.dataset.effort || 'none') : 'none';
}

function startPhase2() {
  if (typeof RUN_ID === 'undefined') return;
  if (_lastStatuses[0] !== 'approved') {
    showToast('Phase 1 muss zuerst freigegeben werden (aktuell: ' + (_lastStatuses[0] || '?') + ').', 'error');
    return;
  }
  const checked = [...document.querySelectorAll('.reviewer-checkbox:checked')].map(c => c.value);
  if (checked.length === 0) { showToast('Bitte mindestens einen Reviewer ausw\u00e4hlen.', 'error'); return; }
  const effort = _getReviewerEffort();
  const temp = _getReviewerTemperature();
  apiCall('/api/run/' + RUN_ID + '/phase/1/start', 'POST', { selected_reviewers: checked, thinking_effort: effort, temperature: temp })
    .then(() => {
      renderPhaseRunning(1);
      renderApprovalGates(1, { status: 'running' });
      const card = document.getElementById('phase-card-1');
      if (card) card.className = 'phase-card status-running';
      const badge = document.getElementById('badge-1');
      if (badge) { badge.textContent = 'L\u00e4uft...'; badge.className = 'status-badge badge-running'; }
    })
    .catch(e => showToast('Fehler beim Starten: ' + e, 'error'));
}

function approvePhase(idx) {
  if (typeof RUN_ID === 'undefined') return;
  // If there's an editable textarea, send the edited value
  const ta = document.getElementById('result-text-' + idx);
  let body = {};
  if (ta) {
    const key = idx === 0 ? 'yaml_text' : idx === 2 ? 'consolidation_text' : idx === 3 ? 'task_yaml' : null;
    if (key) body = { result: { [key]: ta.value } };
  }
  apiCall('/api/run/' + RUN_ID + '/phase/' + idx + '/approve', 'POST', body)
    .then(() => {
      showToast('Phase ' + (idx + 1) + ' freigegeben', 'success');
      _lastStatuses[idx] = null; // force re-render
    })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

function rejectPhase(idx) {
  if (typeof RUN_ID === 'undefined') return;
  apiCall('/api/run/' + RUN_ID + '/phase/' + idx + '/reject', 'POST', {})
    .then(() => {
      showToast('Phase ' + (idx + 1) + ' abgelehnt', 'error');
      _lastStatuses[idx] = null;
    })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

function retryPhase(idx) {
  if (typeof RUN_ID === 'undefined') return;
  apiCall('/api/run/' + RUN_ID + '/phase/' + idx + '/retry', 'POST', {})
    .then(() => {
      showToast('Phase ' + (idx + 1) + ' zur\u00fcckgesetzt', 'success');
      setTimeout(() => location.reload(), 600);
    })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

function saveEdit(idx) {
  if (typeof RUN_ID === 'undefined') return;
  const ta = document.getElementById('result-text-' + idx);
  if (!ta) return;
  const key = idx === 0 ? 'yaml_text' : idx === 2 ? 'consolidation_text' : idx === 3 ? 'task_yaml' : null;
  if (!key) return;
  apiCall('/api/run/' + RUN_ID + '/phase/' + idx + '/update', 'POST', { result: { [key]: ta.value } })
    .then(() => showToast('\u00c4nderungen gespeichert', 'success'))
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

function savePhase2() {
  if (typeof RUN_ID === 'undefined') return;
  const msg = document.getElementById('phase2-save-msg');
  if (msg) msg.textContent = 'Speichert...';
  apiCall('/api/run/' + RUN_ID + '/phase/1/save', 'POST', {})
    .then(data => {
      if (msg) {
        msg.style.color = '#81c784';
        msg.textContent = '\u2713 Gespeichert: ' + (data.filename || '');
      }
      showToast('Phase 2 gespeichert', 'success');
    })
    .catch(e => {
      if (msg) {
        msg.style.color = '#ef5350';
        msg.textContent = 'Fehler: ' + e;
      }
      showToast('Speichern fehlgeschlagen: ' + e, 'error');
    });
}

function savePhase3() {
  if (typeof RUN_ID === 'undefined') return;
  const msg = document.getElementById('phase3-save-msg');
  if (msg) msg.textContent = 'Speichert...';
  apiCall('/api/run/' + RUN_ID + '/phase/2/save', 'POST', {})
    .then(data => {
      if (msg) {
        msg.style.color = '#81c784';
        msg.textContent = '\u2713 Gespeichert: ' + (data.filename || '');
      }
      showToast('Phase 3 gespeichert', 'success');
    })
    .catch(e => {
      if (msg) {
        msg.style.color = '#ef5350';
        msg.textContent = 'Fehler: ' + e;
      }
      showToast('Speichern fehlgeschlagen: ' + e, 'error');
    });
}

function generateTokenReport() {
  if (typeof RUN_ID === 'undefined') return;
  const msg = document.getElementById('token-report-msg');
  if (msg) msg.textContent = 'Erstellt...';
  apiCall('/api/run/' + RUN_ID + '/token_report', 'POST', {})
    .then(data => {
      if (msg) {
        msg.style.color = '#81c784';
        msg.textContent = '\u2713 Erstellt: ' + (data.filename || '');
      }
      showToast('Token-Report erstellt', 'success');
    })
    .catch(e => {
      if (msg) {
        msg.style.color = '#ef5350';
        msg.textContent = 'Fehler: ' + e;
      }
      showToast('Token-Report fehlgeschlagen: ' + e, 'error');
    });
}

// ── Run: Delete + Abort ───────────────────────────────────────────────────

function deleteRun(runId, event) {
  if (event) event.preventDefault();
  if (!confirm('Run wirklich l\u00f6schen? Das kann nicht r\u00fcckg\u00e4ngig gemacht werden.')) return;
  apiCall('/api/run/' + runId + '/delete', 'POST', {})
    .then(() => {
      const wrapper = document.getElementById('wrapper-' + runId);
      if (wrapper) {
        wrapper.style.transition = 'opacity 0.3s';
        wrapper.style.opacity = '0';
        setTimeout(() => wrapper.remove(), 300);
      }
      showToast('Run gel\u00f6scht', 'success');
    })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

function abortRun(runId, event) {
  if (event) event.preventDefault();
  apiCall('/api/run/' + runId + '/abort', 'POST', {})
    .then(() => {
      showToast('Run gestoppt', 'success');
      setTimeout(() => location.reload(), 800);
    })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

// On the run detail page
function deleteRunPage() {
  if (typeof RUN_ID === 'undefined') return;
  if (!confirm('Run wirklich l\u00f6schen? Das kann nicht r\u00fcckg\u00e4ngig gemacht werden.')) return;
  apiCall('/api/run/' + RUN_ID + '/delete', 'POST', {})
    .then(() => { window.location.href = '/'; })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

function abortRunPage() {
  if (typeof RUN_ID === 'undefined') return;
  apiCall('/api/run/' + RUN_ID + '/abort', 'POST', {})
    .then(() => {
      showToast('Run gestoppt', 'success');
      _lastStatuses = {};
    })
    .catch(e => showToast('Fehler: ' + e, 'error'));
}

// ── Statistics ─────────────────────────────────────────────────────────────

const PHASE_NAMES = ['Review-Auftrag', 'Parallel Review', 'Konsolidierung', 'Cursor-Auftrag', 'Build'];

function fmtNum(n) {
  if (n === null || n === undefined) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'k';
  return String(n);
}

function fmtDur(s) {
  if (s === null || s === undefined) return '—';
  if (s >= 60) return Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
  return s.toFixed(1) + 's';
}

function fmtSavings(readTok) {
  // Sonnet 4.6: input $3/MTok, cache-read $0.30/MTok → saving $2.70/MTok
  const saved = readTok * 2.70 / 1_000_000;
  if (saved < 0.01) return '<$0.01';
  return '$' + saved.toFixed(2);
}

function renderBarList(containerId, usage) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const entries = Object.entries(usage).sort((a, b) => b[1] - a[1]);
  if (!entries.length) { el.innerHTML = '<span style="font-size:12px;color:var(--text-dim)">Keine Daten</span>'; return; }
  const max = entries[0][1];
  el.innerHTML = entries.map(([name, count]) =>
    `<div class="stats-bar-item">
      <span class="stats-bar-item-name" title="${esc(name)}">${esc(name)}</span>
      <div class="stats-bar-item-track"><div class="stats-bar-item-fill" style="width:${Math.round(count/max*100)}%"></div></div>
      <span class="stats-bar-item-count">${count}</span>
    </div>`
  ).join('');
}

function renderStats(d) {
  // Kompakt-Leiste
  const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setText('st-total',      d.runs.total);
  setText('st-completed',  d.runs.completed);
  setText('st-cache-read', fmtNum(d.cache.total_read));
  setText('st-thinking',   fmtNum(d.tokens.thinking_total));
  setText('st-avg-dur',    fmtDur(d.avg_run_duration_s));

  // Cache-Tabelle
  const cacheBody = document.getElementById('cache-table-body');
  if (cacheBody) {
    const entries = Object.entries(d.cache.by_ki).sort((a, b) => b[1].read - a[1].read);
    if (!entries.length) {
      cacheBody.innerHTML = '<tr><td colspan="5" class="stats-empty">Noch keine Cache-Daten</td></tr>';
    } else {
      cacheBody.innerHTML = entries.map(([name, v]) =>
        `<tr>
          <td>${esc(name)}</td>
          <td class="num">${fmtNum(v.read)}</td>
          <td class="num">${fmtNum(v.write)}</td>
          <td class="num">${v.calls}</td>
          <td class="num savings">${v.read ? fmtSavings(v.read) : '—'}</td>
        </tr>`
      ).join('');
    }
  }

  // Token-KVs
  setText('tk-cache-read',  fmtNum(d.cache.total_read));
  setText('tk-cache-write', fmtNum(d.cache.total_write));
  setText('tk-thinking',    fmtNum(d.tokens.thinking_total));

  // Modell- und Reviewer-Nutzung
  renderBarList('model-usage-list',    d.model_usage);
  renderBarList('reviewer-usage-list', d.reviewer_usage);

  // Phasen-Tabelle
  const phaseBody = document.getElementById('phase-table-body');
  if (phaseBody) {
    phaseBody.innerHTML = PHASE_NAMES.map((name, i) => {
      const rate = d.phases.approve_rates[i];
      const rateClass = rate === null ? '' : rate >= 75 ? 'rate-high' : rate >= 40 ? 'rate-mid' : 'rate-low';
      const rateStr = rate !== null ? rate + '%' : '—';
      return `<tr>
        <td>${name}</td>
        <td class="num">${d.phases.approvals[i].approved}</td>
        <td class="num">${d.phases.approvals[i].rejected}</td>
        <td class="num approve-rate ${rateClass}">${rateStr}</td>
        <td class="num">${fmtDur(d.phases.avg_duration_s[i])}</td>
      </tr>`;
    }).join('');
  }

  // Run-Übersicht KVs
  setText('ro-total',     d.runs.total);
  setText('ro-completed', d.runs.completed);
  setText('ro-running',   d.runs.running);
  setText('ro-review',    d.runs.review);
  setText('ro-aborted',   d.runs.aborted + (d.runs.rejected || 0));
  setText('ro-avg-dur',   fmtDur(d.avg_run_duration_s));
}

function loadStats() {
  fetch('/api/stats')
    .then(r => r.json())
    .then(renderStats)
    .catch(() => {});
}

function toggleStatsDetail() {
  const detail = document.getElementById('stats-detail');
  const btn    = document.getElementById('stats-toggle-btn');
  if (!detail) return;
  const open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : 'grid';
  if (btn) btn.textContent = open ? '\u25bc Details' : '\u25b2 Schlie\u00dfen';
}

// ── Init ──────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  if (typeof RUN_ID !== 'undefined') {
    startPolling();
  }
  // Initialize key indicators in modal
  loadKeyStatus();
  // Load statistics on dashboard page
  if (document.getElementById('stats-panel')) {
    loadStats();
    setInterval(loadStats, 30_000);
  }
});
