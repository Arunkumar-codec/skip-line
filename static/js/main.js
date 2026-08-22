/* ══════════════════════════════════════════════════════════════════════════════
   Skip Line — main.js
   Global utilities: sidebar, AJAX, toast, notifications, WebSocket helpers
   ══════════════════════════════════════════════════════════════════════════════ */

'use strict';

// ── Sidebar toggle ──────────────────────────────────────────────────────────
const sidebar  = document.getElementById('sidebar');
const overlay  = document.getElementById('sidebarOverlay');
const toggler  = document.getElementById('sidebarToggle');

function openSidebar() {
  sidebar?.classList.add('open');
  overlay?.classList.add('open');
}
function closeSidebar() {
  sidebar?.classList.remove('open');
  overlay?.classList.remove('open');
}

toggler?.addEventListener('click', openSidebar);
overlay?.addEventListener('click', closeSidebar);

// ── Toast helper ────────────────────────────────────────────────────────────
function showToast(message, type = 'success') {
  const container = document.getElementById('toastContainer') || (() => {
    const el = document.createElement('div');
    el.id = 'toastContainer';
    el.className = 'toast-container position-fixed bottom-0 end-0 p-3';
    el.style.zIndex = 9999;
    document.body.appendChild(el);
    return el;
  })();

  const iconMap = {
    success: 'check-circle-fill text-success',
    danger:  'exclamation-triangle-fill text-danger',
    warning: 'exclamation-circle-fill text-warning',
    info:    'info-circle-fill text-info',
  };

  const id = 'toast-' + Date.now();
  container.insertAdjacentHTML('beforeend', `
    <div id="${id}" class="toast align-items-center border-0 shadow" role="alert">
      <div class="d-flex">
        <div class="toast-body d-flex align-items-center gap-2">
          <i class="bi bi-${iconMap[type] || iconMap.info}"></i>
          <span>${message}</span>
        </div>
        <button type="button" class="btn-close me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>
    </div>
  `);

  const toastEl = new bootstrap.Toast(document.getElementById(id), { delay: 4000 });
  toastEl.show();
  document.getElementById(id).addEventListener('hidden.bs.toast', () =>
    document.getElementById(id)?.remove()
  );
}

// ── AJAX helper ─────────────────────────────────────────────────────────────
async function apiRequest(url, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  };
  if (body) opts.body = JSON.stringify(body);

  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));

  if (!resp.ok) {
    throw new Error(data.detail || `HTTP ${resp.status}`);
  }
  return data;
}

// ── Confirm modal helper ────────────────────────────────────────────────────
function confirmAction(message, onConfirm) {
  let modal = document.getElementById('globalConfirmModal');
  if (!modal) {
    document.body.insertAdjacentHTML('beforeend', `
      <div class="modal fade" id="globalConfirmModal" tabindex="-1">
        <div class="modal-dialog modal-sm modal-dialog-centered">
          <div class="modal-content">
            <div class="modal-body text-center p-4">
              <i class="bi bi-exclamation-triangle-fill text-warning fs-2 mb-3 d-block"></i>
              <p id="confirmMessage" class="mb-4"></p>
              <div class="d-flex gap-2 justify-content-center">
                <button class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancel</button>
                <button class="btn btn-danger btn-sm" id="confirmOkBtn">Confirm</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    `);
    modal = document.getElementById('globalConfirmModal');
  }
  document.getElementById('confirmMessage').textContent = message;
  const okBtn = document.getElementById('confirmOkBtn');
  const clone = okBtn.cloneNode(true);
  okBtn.parentNode.replaceChild(clone, okBtn);
  clone.addEventListener('click', () => {
    bootstrap.Modal.getInstance(modal)?.hide();
    onConfirm();
  });
  new bootstrap.Modal(modal).show();
}

// ── Notifications ────────────────────────────────────────────────────────────
async function loadNotifications() {
  try {
    const data = await apiRequest('/notifications/?unread_only=true');
    const items = data.data || [];
    const badge  = document.getElementById('notifCount');
    const list   = document.getElementById('notifList');
    if (!badge || !list) return;

    if (items.length > 0) {
      badge.textContent = items.length > 9 ? '9+' : items.length;
      badge.classList.remove('d-none');
    } else {
      badge.classList.add('d-none');
    }

    list.innerHTML = items.length === 0
      ? '<p class="text-center text-muted py-4 mb-0 small">No new notifications</p>'
      : items.map(n => `
          <div class="notif-item unread">
            <p class="fw-600">${n.subject || n.type.replace(/_/g, ' ')}</p>
            <p>${n.message}</p>
            <span class="notif-time">${new Date(n.sent_at).toLocaleString()}</span>
          </div>`).join('');
  } catch (_) {}
}

async function markAllRead() {
  try {
    await apiRequest('/notifications/mark-read', 'POST');
    loadNotifications();
    showToast('All notifications marked as read');
  } catch (e) {
    showToast(e.message, 'danger');
  }
}

if (document.getElementById('notifList')) {
  loadNotifications();
  setInterval(loadNotifications, 30_000);
}

// ── WebSocket client factory ─────────────────────────────────────────────────
function createWebSocket(path, onMessage, onOpen) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url   = `${proto}://${location.host}${path}`;
  let ws, pingInterval, reconnectTimeout;

  function connect() {
    ws = new WebSocket(url);

    ws.onopen = () => {
      console.log('[WS] Connected:', path);
      onOpen?.();
      pingInterval = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send('ping'), 25_000);
    };

    ws.onmessage = ev => {
      try { onMessage(JSON.parse(ev.data)); }
      catch (_) { if (ev.data !== 'pong') console.warn('[WS] Non-JSON:', ev.data); }
    };

    ws.onclose = () => {
      clearInterval(pingInterval);
      reconnectTimeout = setTimeout(connect, 3_000);
    };

    ws.onerror = () => ws.close();
  }

  connect();
  return { close: () => { clearTimeout(reconnectTimeout); ws?.close(); } };
}

// ── Chart.js helpers ─────────────────────────────────────────────────────────
function createLineChart(ctx, labels, datasets, options = {}) {
  return new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: 'top' } },
      scales: {
        y: { beginAtZero: true, grid: { color: 'rgba(0,0,0,.05)' } },
        x: { grid: { display: false } },
      },
      ...options,
    },
  });
}

function createBarChart(ctx, labels, datasets, options = {}) {
  return new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, grid: { color: 'rgba(0,0,0,.05)' } },
        x: { grid: { display: false } },
      },
      ...options,
    },
  });
}

function createDoughnutChart(ctx, labels, data, colors) {
  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data, backgroundColor: colors, borderWidth: 0 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } },
      cutout: '70%',
    },
  });
}

// ── Date formatting ──────────────────────────────────────────────────────────
function timeAgo(dateStr) {
  const diff = Date.now() - new Date(dateStr);
  const m = Math.floor(diff / 60_000);
  if (m < 1)  return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// Expose to window for inline HTML use
Object.assign(window, { showToast, apiRequest, confirmAction, createWebSocket,
  createLineChart, createBarChart, createDoughnutChart, timeAgo, markAllRead });
