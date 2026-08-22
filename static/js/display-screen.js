/**
 * display-screen.js
 * Standalone JS for the public display screen (/public/display/{id}).
 * Handles WebSocket updates, DOM patching, clock, and reconnection.
 */

'use strict';

class DisplayScreen {
  constructor(orgId) {
    this.orgId = orgId;
    this._previousTokens = {};
  }

  init() {
    this._startClock();
    this._connectWS();
    // Fallback HTTP poll every 30 s
    setInterval(() => this._pollHTTP(), 30_000);
  }

  // ── Clock ──────────────────────────────────────────────────────────────────

  _startClock() {
    const tick = () => {
      const now = new Date();
      const timeEl = document.getElementById('liveTime');
      const dateEl = document.getElementById('liveDate');
      if (timeEl) timeEl.textContent = now.toLocaleTimeString('en-IN');
      if (dateEl) dateEl.textContent = now.toLocaleDateString('en-IN', {
        weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
      });
    };
    tick();
    setInterval(tick, 1_000);
  }

  // ── WebSocket ──────────────────────────────────────────────────────────────

  _connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url   = `${proto}://${location.host}/ws/org/${this.orgId}`;
    let ws, reconnectTimer, pingInterval;

    const connect = () => {
      ws = new WebSocket(url);

      ws.onopen = () => {
        this._setWsStatus(true);
        clearTimeout(reconnectTimer);
        pingInterval = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send('ping');
        }, 25_000);
      };

      ws.onmessage = (ev) => {
        if (ev.data === 'pong') return;
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'display_update') this.render(msg.data);
          if (msg.type === 'announcement' && msg.data?.announcement) {
            this._updateAnnouncement(msg.data.announcement);
          }
        } catch (_) {}
      };

      ws.onclose = () => {
        clearInterval(pingInterval);
        this._setWsStatus(false);
        reconnectTimer = setTimeout(connect, 3_000);
      };

      ws.onerror = () => ws.close();
    };

    connect();
  }

  _setWsStatus(online) {
    const dot   = document.getElementById('wsDot');
    const label = document.getElementById('wsLabel');
    if (dot)   dot.classList.toggle('offline', !online);
    if (label) label.textContent = online ? 'Live' : 'Reconnecting…';
  }

  // ── HTTP fallback ──────────────────────────────────────────────────────────

  async _pollHTTP() {
    try {
      const resp = await fetch(`/public/display/${this.orgId}`);
      const data = await resp.json();
      if (data?.data) this.render(data.data);
    } catch (_) {
      this._setWsStatus(false);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  render(data) {
    if (!data) return;

    if (data.announcement) this._updateAnnouncement(data.announcement);

    (data.counters || []).forEach(c => {
      const card       = document.getElementById(`counter-${c.counter_id}`);
      const tokenEl    = document.getElementById(`token-${c.counter_id}`);
      const upcomingEl = document.getElementById(`upcoming-${c.counter_id}`);
      if (!card) return;

      const prev = this._previousTokens[c.counter_id];
      const curr = c.current_token || '—';

      if (tokenEl && prev !== undefined && prev !== curr) {
        tokenEl.classList.remove('blink');
        void tokenEl.offsetWidth; // reflow to restart animation
        tokenEl.classList.add('blink');
        // Optional: browser notification
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification(`${c.counter_name}: ${curr}`);
        }
      }
      this._previousTokens[c.counter_id] = curr;

      if (tokenEl)    tokenEl.textContent = curr;
      card.classList.toggle('is-active', !!c.current_token);

      if (upcomingEl) {
        upcomingEl.innerHTML = (c.upcoming_tokens?.length)
          ? c.upcoming_tokens.slice(0, 5).map(t =>
              `<span class="upcoming-chip">${t}</span>`).join('')
          : '<span style="color:#475569;font-size:.8rem">Queue empty</span>';
      }
    });
  }

  _updateAnnouncement(text) {
    const el = document.getElementById('announcementText');
    if (el) el.textContent = text;
  }
}

window.DisplayScreen = DisplayScreen;
