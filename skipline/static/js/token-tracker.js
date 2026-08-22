/**
 * token-tracker.js
 * WebSocket + polling client for the live token tracking page.
 * Auto-reloads when token reaches a terminal state.
 */

'use strict';

class TokenTracker {
  constructor(tokenId) {
    this.tokenId = tokenId;
    this.TERMINAL = new Set(['completed', 'skipped', 'cancelled']);
  }

  init() {
    this._connectWS();
    // Polling fallback every 20 s
    setInterval(() => this._poll(), 20_000);
  }

  _connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url   = `${proto}://${location.host}/ws/token/${this.tokenId}`;
    let ws, pingInterval, reconnectTimer;

    const connect = () => {
      ws = new WebSocket(url);

      ws.onopen = () => {
        this._setStatus(true);
        clearTimeout(reconnectTimer);
        pingInterval = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send('ping');
        }, 25_000);
      };

      ws.onmessage = (ev) => {
        if (ev.data === 'pong') return;
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'tracking_update') this._apply(msg.data);
        } catch (_) {}
      };

      ws.onclose = () => {
        clearInterval(pingInterval);
        this._setStatus(false);
        reconnectTimer = setTimeout(connect, 3_000);
      };

      ws.onerror = () => ws.close();
    };

    connect();
  }

  async _poll() {
    try {
      const resp = await fetch(`/user/api/tokens/${this.tokenId}/track`);
      const data = await resp.json();
      if (data?.data) this._apply(data.data);
    } catch (_) {}
  }

  _apply(d) {
    const el = id => document.getElementById(id);

    // Status label
    if (el('tokenStatus')) el('tokenStatus').textContent = d.status.replace(/_/g, ' ').toUpperCase();

    // Queue numbers
    if (el('peopleAhead')) el('peopleAhead').textContent = d.people_ahead;
    if (el('etaMin'))      el('etaMin').textContent      = d.estimated_wait_minutes;
    if (el('queuePos'))    el('queuePos').textContent    = d.queue_position;
    if (el('nowServing'))  el('nowServing').textContent  = d.current_token_at_counter || '—';

    // Alert banners
    const calledAlert = el('calledAlert');
    const servingAlert = el('servingAlert');
    if (calledAlert)  calledAlert.classList.toggle('d-none',  d.status !== 'called');
    if (servingAlert) servingAlert.classList.toggle('d-none', d.status !== 'in_progress');

    // Pulse animation on card
    if (d.status === 'called') {
      el('trackCard')?.classList.add('pulse');
      showToast('Your token has been called! Please proceed.', 'info');
      this._playBeep();
    }

    // Terminal — reload to show completed screen
    if (this.TERMINAL.has(d.status)) {
      showToast(`Token ${d.status}.`, d.status === 'completed' ? 'success' : 'warning');
      setTimeout(() => location.reload(), 2_000);
    }
  }

  _setStatus(online) {
    const dot   = document.getElementById('wsDot');
    const label = document.getElementById('wsLabel');
    if (dot)   dot.style.background = online ? '#22c55e' : '#ef4444';
    if (label) label.textContent    = online ? 'Live' : 'Reconnecting…';
  }

  _playBeep() {
    try {
      const ctx  = new (window.AudioContext || window.webkitAudioContext)();
      const osc  = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type      = 'sine';
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.8);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.8);
    } catch (_) {}
  }
}

window.TokenTracker = TokenTracker;
