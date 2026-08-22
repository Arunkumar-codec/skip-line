/**
 * queue-operator.js
 * Standalone JS module for the organization queue management page.
 * Handles WebSocket connection, queue rendering and operator actions.
 * Loaded only on /org/queue.
 */

'use strict';

class QueueOperator {
  constructor(orgId) {
    this.orgId = orgId;
    this.currentCounterId = null;
    this.calledTokenId = null;
    this._actionInFlight = false;
    this._ws = null;
    this._pollTimer = null;
  }

  // ── Bootstrap ──────────────────────────────────────────────────────────────

  init(firstCounterId) {
    this.currentCounterId = firstCounterId;
    this._connectWS();
    this.loadQueue();
    // Fallback polling every 15 s in case WS drops
    this._pollTimer = setInterval(() => this.loadQueue(), 15_000);
  }

  // ── WebSocket ──────────────────────────────────────────────────────────────

  _connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url   = `${proto}://${location.host}/ws/org/${this.orgId}`;
    let reconnectTimer;

    const connect = () => {
      this._ws = new WebSocket(url);

      this._ws.onopen = () => {
        document.getElementById('wsStatus').innerHTML =
          '<i class="bi bi-circle-fill me-1" style="font-size:.5rem"></i>Live';
        document.getElementById('wsStatus').className =
          'badge rounded-pill bg-success-subtle text-success px-3 py-2';
        clearTimeout(reconnectTimer);
      };

      this._ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'display_update') this.loadQueue();
        } catch (_) {}
      };

      this._ws.onclose = () => {
        document.getElementById('wsStatus').innerHTML =
          '<i class="bi bi-circle-fill me-1" style="font-size:.5rem"></i>Reconnecting…';
        document.getElementById('wsStatus').className =
          'badge rounded-pill bg-warning-subtle text-warning px-3 py-2';
        reconnectTimer = setTimeout(connect, 3_000);
      };

      this._ws.onerror = () => this._ws.close();
    };

    connect();
  }

  // ── Data loading ───────────────────────────────────────────────────────────

  async loadQueue() {
    if (!this.currentCounterId) return;
    try {
      const data = await apiRequest(`/org/queue-data/${this.currentCounterId}`);
      this.renderWaiting(data.waiting || []);
      this.renderCurrent(data.current);
      this.renderCompleted(data.completed || []);
      this._updateBadges(data);
    } catch (e) {
      console.warn('loadQueue error:', e);
    }
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  renderWaiting(tokens) {
    const el = document.getElementById('waitingList');
    if (!tokens.length) {
      el.innerHTML = `
        <div class="text-center text-muted py-5">
          <i class="bi bi-inbox fs-2 d-block mb-2"></i>
          <p class="mb-0">Queue is empty</p>
        </div>`;
      return;
    }

    el.innerHTML = tokens.map((t, i) => `
      <div class="token-queue-item ${t.status === 'called' ? 'is-called' : ''}" data-id="${t.id}">
        <div class="d-flex align-items-center gap-3">
          <span class="token-num">${t.token_number}</span>
          <div>
            <div class="text-sm fw-600">${t.guest_name || (t.user_id ? 'User #' + t.user_id : 'Guest')}</div>
            <div class="text-xs text-muted">#${i + 1} in queue · ${
              t.booked_at ? new Date(t.booked_at).toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit'}) : ''
            }</div>
          </div>
        </div>
        <div class="d-flex flex-column align-items-end gap-1">
          <span class="badge-status badge-${t.status}">${t.status.replace(/_/g, ' ')}</span>
        </div>
      </div>`).join('');
  }

  renderCurrent(token) {
    const el = document.getElementById('currentTokenPanel');
    if (!token) {
      this.calledTokenId = null;
      el.innerHTML = `
        <div class="text-center text-muted py-3">
          <i class="bi bi-bell-slash fs-2 d-block mb-2"></i>No token called yet
        </div>`;
      this._setButtons(false);
      return;
    }

    this.calledTokenId = token.id;
    const called = new Date(token.called_at || Date.now());
    const elapsed = Math.round((Date.now() - called.getTime()) / 60_000);

    el.innerHTML = `
      <div class="text-center py-2">
        <div class="token-display-number mb-1 ${token.status === 'called' ? 'pulse' : ''}">${token.token_number}</div>
        <span class="badge-status badge-${token.status} mb-3">${token.status.replace(/_/g, ' ')}</span>
        <div class="row text-sm g-0 mt-3 text-start">
          <div class="col-5 text-muted py-1 border-bottom">Name</div>
          <div class="col-7 fw-600 py-1 border-bottom">${token.guest_name || 'App User'}</div>
          <div class="col-5 text-muted py-1 border-bottom">Mobile</div>
          <div class="col-7 fw-600 py-1 border-bottom">${token.guest_mobile || '—'}</div>
          <div class="col-5 text-muted py-1 border-bottom">Called at</div>
          <div class="col-7 fw-600 py-1 border-bottom">${token.called_at ? new Date(token.called_at).toLocaleTimeString('en-IN') : '—'}</div>
          <div class="col-5 text-muted py-1">Elapsed</div>
          <div class="col-7 fw-600 py-1">${elapsed}m</div>
        </div>
      </div>`;

    this._setButtons(true, token.status);
  }

  renderCompleted(tokens) {
    const el = document.getElementById('completedList');
    if (!tokens.length) {
      el.innerHTML = '<p class="text-center text-muted small py-2 mb-0">No completed tokens today</p>';
      return;
    }
    el.innerHTML = tokens.slice(0, 25).map(t => `
      <div class="d-flex justify-content-between align-items-center py-2 px-1 border-bottom">
        <span class="fw-600 text-sm text-primary">${t.token_number}</span>
        <span class="badge-status badge-completed" style="font-size:.7rem">done</span>
        <span class="text-xs text-muted">${
          t.completed_at ? new Date(t.completed_at).toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit'}) : ''
        }</span>
      </div>`).join('');
  }

  _updateBadges(data) {
    const wc = document.getElementById('waitingCount');
    const cc = document.getElementById('completedCount');
    if (wc) wc.textContent = (data.waiting || []).length;
    if (cc) cc.textContent = (data.completed || []).length;

    // Update per-counter tab badge
    const badge = document.querySelector(`.waiting-badge-${this.currentCounterId}`);
    if (badge) badge.textContent = (data.waiting || []).length;
  }

  _setButtons(active, status = '') {
    const btns = {
      btnComplete: active,
      btnHold:     active,
      btnSkip:     active,
      btnCancel:   active,
      btnRecall:   active && status === 'called',
      btnCall:     !(active && status === 'called'),
    };
    for (const [id, enabled] of Object.entries(btns)) {
      const el = document.getElementById(id);
      if (el) el.disabled = !enabled;
    }
  }

  // ── Actions ────────────────────────────────────────────────────────────────

  async action(actionName) {
    if (this._actionInFlight) return;
    this._actionInFlight = true;

    try {
      let tokenId = this.calledTokenId;

      if (actionName === 'call') {
        const data = await apiRequest(`/org/queue-data/${this.currentCounterId}`);
        const next = (data.waiting || []).find(t => t.status === 'waiting');
        if (!next) { showToast('No waiting tokens', 'warning'); return; }
        tokenId = next.id;
      }

      if (!tokenId) { showToast('No active token', 'warning'); return; }

      await apiRequest(`/org/tokens/${tokenId}/action`, 'POST', { action: actionName });
      showToast(`Token ${actionName.replace(/_/g, ' ')}d`, actionName === 'cancel' ? 'warning' : 'success');
      await this.loadQueue();

    } catch (e) {
      showToast(e.message || 'Action failed', 'danger');
    } finally {
      this._actionInFlight = false;
    }
  }

  selectCounter(id, btn) {
    this.currentCounterId = id;
    document.querySelectorAll('#counterTabs .nav-link').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    this.calledTokenId = null;
    this._setButtons(false);
    this.loadQueue();
  }
}

// Instantiated by the queue.html template
window.QueueOperator = QueueOperator;
