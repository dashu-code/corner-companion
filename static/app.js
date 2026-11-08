// 前端错误上报：只有出问题时才会往服务器打日志
window.addEventListener('error', function (e) {
  try {
    var msg = (e.message || 'error') + ' @' + (e.filename || '') + ':' + (e.lineno || 0);
    fetch('/api/jserr?m=' + encodeURIComponent(msg), { cache: 'no-store' });
  } catch (_) {}
});
window.addEventListener('unhandledrejection', function (e) {
  try {
    var r = e.reason || {};
    fetch('/api/jserr?m=' + encodeURIComponent('promise: ' + (r.message || r)), { cache: 'no-store' });
  } catch (_) {}
});
/* 同伴 · 多联系人（微信式单列）
 *
 * 设计决定：
 * - 只有一个列表，不分区、不写"自己起名/你塑造的"这种说明文字。
 * - 分类只用一个表情跟在名字后面：🌱 独立 / 🧩 依附。
 * - 新建入口不放在手机上：人设生成要上下文和工具，在手机上调很难，
 *   统一由 DSH 那边创建（App 只负责聊）。
 */
'use strict';

const BUILD = 'v28';

const TOKEN = (() => {
  const q = new URLSearchParams(location.search);
  const fromUrl = q.get('t') || '';
  if (fromUrl) { localStorage.setItem('companion_token', fromUrl); return fromUrl; }
  return localStorage.getItem('companion_token') || '';
})();

// 口令不再挂在 URL 上：首次带 ?t= 打开时服务端会种一个 HttpOnly+Secure 的 Cookie，
// 之后所有请求靠同源 Cookie 鉴权。TOKEN 只在还没 Cookie 时兜底。
// 注意：真正用来鉴权的 cnr 是 HttpOnly（JS 读不到，这是故意的）。
// 服务端另外种了一个秒级标记 cnrs=1，专门给 JS 判断"凭证已就位"。
const HAS_COOKIE = (() => {
  try { return document.cookie.indexOf('cnrs=') >= 0; } catch (e) { return false; }
})();

const qs = (extra) => {
  const p = new URLSearchParams(extra || {});
  if (TOKEN && !HAS_COOKIE) p.set('t', TOKEN);
  const s = p.toString();
  return s ? '?' + s : '';
};

async function postJson(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    cache: 'no-store',
    credentials: 'same-origin',
  });
}

const API = {
  agents: () => `/api/agents${qs()}`,
  messages: (id, after, limit) => `/api/messages${qs({ agent: id, after, limit })}`,
  send: () => `/api/send${qs()}`,
  markSeen: (id) => postJson('/api/mark_seen' + qs(), { agent: id }),
};

const KIND_EMOJI = { independent: '🌱', dependent: '🧩' };

const $ = (id) => document.getElementById(id);

/** 安全绑定：元素不存在时跳过，避免整个脚本因一个 null 中断。 */
function on(id, ev, fn, opts) {
  const el = $(id);
  if (el) el.addEventListener(ev, fn, opts);
  return el;
}


const POLL_MS = 3000;
const TIME_GAP_MS = 5 * 60 * 1000;

let agentsCache = [];
let current = null;
let lastId = 0;
let lastRenderedTs = 0;
let renderedIds = new Set();
let pending = [];
let pollTimer = null;
let sending = false;
const LAST_KEY = 'companion_last_agent';

/* ---------------- 工具 ---------------- */
function fmtTime(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return sameDay ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

function agentColors(a) {
  return [a.avatar_c1 || '#3a3f3a', a.avatar_c2 || '#beb6a3'];
}

function avatarNode(a, cls) {
  const el = document.createElement('div');
  el.className = 'avatar ai ' + (cls || '');
  const [c1, c2] = agentColors(a);
  el.style.background = `linear-gradient(150deg, ${c1}, ${c1})`;
  el.style.color = c2;
  el.textContent = a.avatar_glyph || (a.name || '?').slice(0, 1);
  return el;
}

/* ---------------- 联系人 ---------------- */
async function loadAgents() {
  const r = await fetch(API.agents(), { cache: 'no-store', credentials: 'same-origin' });
  const d = await r.json();
  agentsCache = d.agents || [];
  const list = $('agentList');
  list.innerHTML = '';
  for (const a of agentsCache) {
    const row = document.createElement('button');
    row.className = 'contact';
    row.appendChild(avatarNode(a));

    const mid = document.createElement('div');
    mid.className = 'contactMid';
    const nm = document.createElement('div');
    nm.className = 'contactName';
    nm.textContent = a.name;
    const emo = document.createElement('span');
    emo.className = 'kindEmoji';
    emo.textContent = KIND_EMOJI[a.kind] || '';
    nm.appendChild(emo);
    mid.appendChild(nm);
    row.appendChild(mid);

    if (Number(a.unread) > 0) {
      const bd = document.createElement('span');
      bd.className = 'badge';
      bd.textContent = Number(a.unread) > 99 ? '99+' : String(a.unread);
      row.appendChild(bd);
    }

    const cnt = document.createElement('div');
    cnt.className = 'contactCount';
    cnt.textContent = a.msg_count ? `${a.msg_count} 句` : '';
    row.appendChild(cnt);

    row.addEventListener('click', () => { location.hash = `#/a/${a.id}`; });
    list.appendChild(row);
  }
}

/* ---------------- 聊天 ---------------- */
async function openChat(id) {
  const a = agentsCache.find((x) => Number(x.id) === Number(id));
  if (!a) { location.hash = ''; return; }

  current = a;
  lastId = 0;
  lastRenderedTs = 0;
  renderedIds = new Set();
  pending = [];
  $('list').innerHTML = '';
  $('text').value = '';

  const nameEl = $('peerName');
  if (nameEl) nameEl.textContent = a.name;
  const av = $('peerAvatar');
  if (av) {
    const [c1, c2] = agentColors(a);
    av.textContent = a.avatar_glyph || a.name.slice(0, 1);
    av.style.background = `linear-gradient(150deg, ${c1}, ${c1})`;
    av.style.color = c2;
  }
  document.title = a.name;
  try { localStorage.setItem(LAST_KEY, String(a.id)); } catch (e) {}

  $('contacts').hidden = true;
  $('chat').hidden = false;

  await pull();
  scrollToBottom(false);
  updateComposerIcons();
  // 打开即已读，并清掉列表红点
  try { await API.markSeen(a.id); } catch (e) { }
  loadAgents().catch(() => { });
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => { if (!document.hidden && current) pull(); }, POLL_MS);
}

function closeChat() {
  current = null;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  $('chat').hidden = true;
  $('contacts').hidden = false;
  document.title = '联系人';
  loadAgents().catch(() => {});
}

/* ---------------- 消息 ---------------- */
function nearBottom() {
  const el = $('list');
  return el.scrollHeight - el.scrollTop - el.clientHeight < 140;
}
function scrollToBottom(smooth) {
  const el = $('list');
  if (!el) return;
  if (smooth) {
    try { el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }); } catch (e) { }
    return;
  }
  const hit = () => {
    // 先 scrollIntoView（它对齐时会忽略底部 padding），再把 scrollTop 顶到底，
    // 否则会永远差一个 padding 的高度（实测差 19px）。
    const last = el.lastElementChild;
    if (last && last.scrollIntoView) {
      try { last.scrollIntoView({ block: 'end' }); } catch (e) { }
    }
    el.scrollTop = el.scrollHeight;
  };
  hit();
  requestAnimationFrame(hit);
  requestAnimationFrame(() => requestAnimationFrame(hit));
  [30, 80, 200, 400, 800, 1200].forEach((ms) => setTimeout(hit, ms));

  // 首屏：字体/布局可能持续撑高，2 秒内一直校正到底；
  // 一旦用户自己滚动了，立刻停手，不跟人抢。
  let stopped = false;
  const stop = () => { stopped = true; };
  try {
    el.addEventListener('touchstart', stop, { once: true, passive: true });
    el.addEventListener('wheel', stop, { once: true, passive: true });
    el.addEventListener('mousedown', stop, { once: true });
  } catch (e) { }
  let n = 0;
  const timer = setInterval(() => {
    n += 1;
    if (stopped || n > 20) { clearInterval(timer); return; }
    if (el.scrollHeight - el.scrollTop - el.clientHeight > 8) hit();
  }, 100);
}

function buildRow(m, optimistic) {
  const row = document.createElement('div');
  row.className = 'row ' + (m.role === 'me' ? 'me' : 'ai');
  if (optimistic) row.classList.add('pending');

  const av = document.createElement('div');
  av.className = 'avatar ' + (m.role === 'me' ? 'me' : 'ai');
  if (m.role === 'me') {
    av.textContent = '我';
  } else {
    const [c1, c2] = agentColors(current || {});
    av.textContent = (current && (current.avatar_glyph || current.name.slice(0, 1))) || '它';
    av.style.background = `linear-gradient(150deg, ${c1}, ${c1})`;
    av.style.color = c2;
  }

  const wrap = document.createElement('div');
  wrap.className = 'wrap';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = m.text;
  wrap.appendChild(bubble);

  if (m.role === 'me') { row.appendChild(wrap); row.appendChild(av); }
  else { row.appendChild(av); row.appendChild(wrap); }
  return row;
}

function insertRow(m, optimistic) {
  const ts = new Date(m.ts).getTime();
  if (lastRenderedTs && ts - lastRenderedTs > TIME_GAP_MS) {
    const sep = document.createElement('div');
    sep.className = 'time-sep';
    sep.textContent = fmtTime(m.ts);
    $('list').appendChild(sep);
  }
  lastRenderedTs = ts;
  const row = buildRow(m, optimistic);
  if (m.id != null) row.dataset.id = m.id;
  $('list').appendChild(row);
  return row;
}

function acceptMessage(m) {
  if (m.id != null && renderedIds.has(m.id)) return;
  if (m.id != null) renderedIds.add(m.id);
  if (m.role === 'me') {
    const i = pending.findIndex((p) => p.text === m.text);
    if (i >= 0) {
      const p = pending.splice(i, 1)[0];
      const real = buildRow(m, false);
      real.dataset.id = m.id;
      p.el.replaceWith(real);
      return;
    }
  }
  insertRow(m, false);
}

async function pull() {
  if (!current) return;
  try {
    // 首次只拉最近 30 条：即使滚动没生效，顶上也是最近的消息，而不是几个月前的开头
    const limit = lastId === 0 ? 30 : 500;
    const r = await fetch(API.messages(current.id, lastId, limit), { cache: 'no-store', credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const items = data.items || [];
    if (items.length) {
      // 人正看着这个聊天 → 收到即已读，不攒红点
      try { if (current && API.markSeen) API.markSeen(current.id).catch(() => { }); } catch (e) { }
      const stick = lastId === 0 ? true : nearBottom();
      for (const m of items) acceptMessage(m);
      lastId = Math.max(lastId, items[items.length - 1].id);
      if (stick) scrollToBottom();
    }
    setStatus(true);
  } catch (e) {
    setStatus(false);
  }
}

async function send() {
  if (!current) return;
  const inputEl = $('text');
  const text = (inputEl.value || '').trim();
  if (!text || sending) return;
  sending = true;
  const sb = $('sendIcon'); if (sb) sb.disabled = true;
  inputEl.value = '';

  const optimistic = { id: null, ts: new Date().toISOString(), role: 'me', text, kind: 'chat' };
  const el = insertRow(optimistic, true);
  pending.push({ text, el });
  scrollToBottom(true);

  try {
    const r = await fetch(API.send(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, agent: current.id }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await pull();
  } catch (e) {
    el.classList.add('failed');
  } finally {
    sending = false;
    const sb2 = $('sendIcon'); if (sb2) sb2.disabled = false;
    inputEl.focus();
  }
}

function setStatus(ok) {
  const dot = (current ? $('chatDot') : $('dot')) || $('dot');
  const txt = (current ? $('chatStatusText') : $('statusText')) || $('statusText');
  if (dot) {
    dot.classList.toggle('on', ok);
    dot.classList.toggle('off', !ok);
  }
  if (txt) txt.textContent = ok ? '在线' : '连不上';
}


function updateComposerIcons() {
  const has = ($('text').value || '').trim().length > 0;
  const send = $('sendIcon');
  const plus = $('plusIcon');
  if (send) send.hidden = !has;
  if (plus) plus.hidden = has;
}

/* ---------------- 事件 ---------------- */

on('settingsBtn', 'click', () => { location.hash = '#/settings'; route(); });
on('settingsBack', 'click', () => { leaveSettings(); });

on('notifyFix', 'click', () => {
  try { if (window.Native && Native.openNotificationSettings) Native.openNotificationSettings(); } catch (e) { }
});
on('notifyTest', 'click', () => {
  try {
    if (!window.Native) return;
    const st = (Native.notifDiag ? JSON.parse(Native.notifDiag()) : {});
    const txt = $('notifyText');
    const bar = $('notifyBar');
    if (bar) bar.hidden = false;
    if (txt) {
      txt.textContent = '权限:' + (st.enabled ? '开' : '关') +
        ' 渠道:' + (st.channel || '无') +
        ' 级别:' + (st.importance === undefined ? '?' : st.importance) +
        ' 发送:' + (st.posted ? '成功' : ('失败 ' + (st.error || ''))) +
        ' 系统通知数:' + (st.active === undefined ? '?' : st.active);
    }
  } catch (e) {
    const txt = $('notifyText');
    if (txt) txt.textContent = '诊断出错: ' + e.message;
  }
});

on('backBtn', 'click', () => {
  // 主动返回列表 = 下次打开显示列表；只有"在聊天里被关掉"才恢复到那个聊天
  try { localStorage.removeItem(LAST_KEY); } catch (e) {}
  location.hash = '';
});
on('sendIcon', 'click', send);
on('text', 'keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
on('text', 'input', updateComposerIcons);
on('list', 'scroll', () => {
  const jp = $('jump');
  if (jp) jp.classList.toggle('show', !nearBottom());
});
on('jump', 'click', () => scrollToBottom(true));

/* ---------------- 路由 ---------------- */
function leaveSettings() {
  hideSettings();
  location.hash = '';
  route();
}

function detectAppVersion() {
  // 有 appVersion() 就直接读安装包版本；老包没有这个接口，用能力探测推断个大概
  try {
    if (!window.Native) return '浏览器模式';
    if (typeof Native.appVersion === 'function') return String(Native.appVersion());
    if (typeof Native.notifDiag === 'function') return '1.10–1.13 之间（旧包）';
    if (typeof Native.notifStatus === 'function') return '1.9（旧包）';
    if (typeof Native.canNotify === 'function') return '1.8（旧包）';
    return '1.7 或更早（旧包）';
  } catch (e) {
    return '读不到';
  }
}

function showSettings() {
  current = null;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const c = $('contacts'); if (c) c.hidden = true;
  const ch = $('chat'); if (ch) ch.hidden = true;
  const st = $('settings'); if (st) st.hidden = false;
  const bt = $('buildText'); if (bt) bt.textContent = BUILD;
  const sv = $('serverText'); if (sv) sv.textContent = location.host || '—';
  const av = $('appVersionText'); if (av) av.textContent = detectAppVersion();
  checkNotifyPermission();
  document.title = '设置';
}

function hideSettings() {
  const st = $('settings'); if (st) st.hidden = true;
  document.title = '角落';
}

function route() {
  if (location.hash === '#/settings') {
    showSettings();
    return;
  }
  hideSettings();
  const m = location.hash.match(/^#\/a\/(\d+)/);
  if (m) openChat(Number(m[1])).catch(() => { location.hash = ''; });
  else closeChat();
}
window.addEventListener('hashchange', route);


// 布局探针：排查横向溢出用（写在 buildTag / statusText 的 title 上）
function layoutProbe() {
  try {
    const list = $('list');
    const gap = list ? (list.scrollHeight - list.scrollTop - list.clientHeight) : -1;
    const m = 'vw=' + window.innerWidth + ' sw=' + document.documentElement.scrollWidth +
      ' gap=' + Math.round(gap) + ' sh=' + (list ? list.scrollHeight : 0) +
      ' st=' + (list ? Math.round(list.scrollTop) : 0) +
      ' ch=' + (list ? list.clientHeight : 0);
    const a = $('buildTag');
    if (a) a.title = m;
    const b = $('statusText');
    if (b) b.title = m;
  } catch (e) { }
}


/** 通知权限检查（只在这个 App 里有效：网页通过 Native 桥问 Android） */
function checkNotifyPermission() {
  const txtEl = $('notifyStatusText');
  const dotEl = $('navDot');
  try {
    if (!window.Native || typeof Native.notifStatus !== 'function') {
      if (txtEl) txtEl.textContent = '浏览器模式（这个页面没有系统通知）';
      return;
    }
    const st = JSON.parse(Native.notifStatus());
    const okEnabled = !!st.enabled;
    const okChannel = (st.importance === undefined || st.importance === null || st.importance >= 3);
    const ok = okEnabled && okChannel;
    if (txtEl) {
      txtEl.textContent = !okEnabled ? '系统里被关掉了'
        : (!okChannel ? '通知渠道被静音了'
          : '正常');
    }
    // 只有出问题才在主页加号上点一个小点
    if (dotEl) dotEl.hidden = ok;
  } catch (e) {
    if (txtEl) txtEl.textContent = '读不到状态';
  }
}


/* ---------------- 启动 ---------------- */
(async function boot() {
  // 埋点：让服务端知道手机加载到了哪个版本的网页（排查缓存用）
  try { fetch('/api/ping' + qs({ build: BUILD }), { cache: 'no-store', credentials: 'same-origin' }); } catch (e) {}
  try {
    await loadAgents();
    setStatus(true);
  } catch { setStatus(false); }
  // 关键：服务端把我们 302 到 https 之后，把新地址写回 App，
  // 后台轮询才会跟着换到加密通道（这段必须在启动时跑，不能只在设置页跑）。
  try {
    if (location.protocol === 'https:' && window.Native && Native.updateServer) {
      Native.updateServer(location.origin);
    }
  } catch (e) { }

  // 上次在某个聊天里被关掉 → 直接回那个聊天，而不是回到列表
  try {
    const saved = localStorage.getItem(LAST_KEY);
    if (!location.hash && saved) location.hash = '#/a/' + saved;
  } catch (e) {}
  route();
  layoutProbe();
  setTimeout(layoutProbe, 1600);
  checkNotifyPermission();
  setTimeout(checkNotifyPermission, 600);
  setInterval(() => { if (!current) loadAgents().catch(() => {}); }, 10000);
})();
