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

const BUILD = 'v50';

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
let firstLoadDone = false;  // 首屏拉历史时不朗读
let openBaselineId = 0;      // 打开聊天时的最大消息 id；比它新的才朗读
let speechArmed = false;     // 首屏拉取完成前一律不朗读（双保险）
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
  if (a && a.avatar) {
    // 有头像图就用图
    const img = document.createElement('img');
    img.className = 'avatarImg';
    img.alt = a.name || '';
    img.src = a.avatar;
    el.appendChild(img);
    el.classList.add('hasImg');
    return el;
  }
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

    // 最后一句预览（微信式，显示在名字下面）
    const prev = document.createElement('div');
    prev.className = 'contactPreview';
    const lastText = String(a.last_text || '').replace(/\s+/g, ' ').trim();
    prev.textContent = lastText ? lastText.slice(0, 26) : '还没有消息';
    mid.appendChild(prev);

    // 右侧：最后消息时间（没有时间才退回句数）
    const cnt = document.createElement('div');
    cnt.className = 'contactCount';
    cnt.textContent = fmtLastTime(a.last_ts) || (a.msg_count ? `${a.msg_count} 句` : '');
    row.appendChild(cnt);

    row.addEventListener('click', () => { navToChat(a.id); });
    list.appendChild(row);
  }
}

/* ---------------- 聊天 ---------------- */
async function openChat(id) {
  const a = agentsCache.find((x) => Number(x.id) === Number(id));
  if (!a) { location.hash = ''; return; }

  current = a;
  firstLoadDone = false;
  openBaselineId = 0;
  speechArmed = false;
  speakStopAll();
  lastId = 0;
  lastRenderedTs = 0;
  renderedIds = new Set();
  pending = [];
  $('list').innerHTML = '';
  $('text').value = '';

  const nameEl = $('peerName');
  if (nameEl) nameEl.textContent = a.name;
  document.title = a.name;
  try { localStorage.setItem(LAST_KEY, String(a.id)); } catch (e) {}

  $('contacts').hidden = true;
  $('chat').hidden = false;

  await pull();
  scrollToBottom(false);
  renderTtsBtn();
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


/** 微信式时间：刚刚 / N分钟前 / HH:MM / 昨天 / M月D日 */
function fmtLastTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const diffMin = (now - d) / 60000;
    if (diffMin < 1) return '刚刚';
    if (diffMin < 60) return Math.floor(diffMin) + '分钟前';
    const sameDay = d.toDateString() === now.toDateString();
    const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    if (sameDay) return hm;
    const yest = new Date(now);
    yest.setDate(now.getDate() - 1);
    if (d.toDateString() === yest.toDateString()) return '昨天';
    return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  } catch (e) {
    return '';
  }
}


/** 表情包：图在 APK 里，用 Native.stickerData 取 base64；浏览器里退回服务器（如果有） */
let stickerDiagDone = false;

function stickerImg(name) {
  const img = document.createElement('img');
  img.className = 'stickerImg';
  img.alt = name;
  img.loading = 'lazy';
  let uri = '';
  try {
    if (window.Native && typeof Native.stickerData === 'function') {
      uri = Native.stickerData(name) || '';
    }
  } catch (e) { uri = ''; }

  if (uri) {
    img.src = uri;
  } else {
    // 兜底：旧版 APK 没有本地图，就走服务器取（需要联网）
    img.src = '/api/sticker/' + encodeURIComponent(name) + qs();
  }

  if (!stickerDiagDone) {
    stickerDiagDone = true;
    try {
      const hasNative = !!(window.Native && typeof Native.stickerData === 'function');
      ttsReport('sticker native=' + (hasNative ? 1 : 0) + ' uriLen=' + uri.length +
        ' fallback=' + (uri ? 0 : 1));
    } catch (e) { }
  }

  img.onerror = () => { img.classList.add('stickerMissing'); };
  return img;
}


/** 头像：有图就用图，没图退回字形（老数据不受影响） */
function avatarEl(agent, role) {
  const d = document.createElement('div');
  d.className = 'avatar ' + (role === 'me' ? 'me' : 'ai');
  if (role === 'me') { d.textContent = '我'; return d; }
  const av = agent && agent.avatar;
  if (av) {
    const img = document.createElement('img');
    img.className = 'avatarImg';
    img.alt = (agent && agent.name) || '';
    img.src = av;
    d.appendChild(img);
    d.classList.add('hasImg');
  } else {
    const [c1, c2] = agentColors(agent || {});
    d.textContent = (agent && (agent.avatar_glyph || String(agent.name || '').slice(0, 1))) || '它';
    d.style.background = `linear-gradient(150deg, ${c1}, ${c1})`;
    d.style.color = c2;
  }
  return d;
}

function buildRow(m, optimistic) {
  const row = document.createElement('div');
  row.className = 'row ' + (m.role === 'me' ? 'me' : 'ai');
  if (optimistic) row.classList.add('pending');

  const av = avatarEl(current || {}, m.role);

  const wrap = document.createElement('div');
  wrap.className = 'wrap';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (m.sticker) {
    // 纯表情气泡：只放图。文字由 insertRow 拆成单独一条，不混在一起。
    bubble.classList.add('stickerBubble');
    bubble.appendChild(stickerImg(m.sticker));
  } else {
    bubble.textContent = m.text;
  }
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

  // 一条消息同时带表情和文字 → 拆成两行：先图，后普通文字气泡
  let row;
  if (m.sticker && m.text && String(m.text).trim()) {
    const imgRow = buildRow({ ...m, text: '' }, optimistic);
    if (m.id != null) { imgRow.dataset.id = m.id; imgRow.dataset.part = 'sticker'; }
    $('list').appendChild(imgRow);

    row = buildRow({ ...m, sticker: null }, optimistic);
    if (m.id != null) { row.dataset.id = m.id; row.dataset.part = 'text'; }
    $('list').appendChild(row);
    return row;
  }

  row = buildRow(m, optimistic);
  if (m.id != null) row.dataset.id = m.id;
  $('list').appendChild(row);
  return row;
}


/** 依附型才朗读；独立型保持安静。只在"看到新消息"时念，重进聊天不重念历史。 */

/* ---------------- 朗读开关（每个联系人各自记住） ---------------- */
function ttsKey(id) { return 'tts_' + id; }

function ttsIsOn(id) {
  // 默认值：依附型开、独立型关。
  // 注意：优先用 current.kind（打开聊天时就存好了）；
  // 以前查 agentsCache，而它每次回列表都会重建，正好在重建时 find() 会返回空，
  // 默认值算成"不念" —— 表现为"有时念有时不念"。
  let dflt = false;
  if (current && Number(current.id) === Number(id)) {
    dflt = (current.kind === 'dependent');
  } else {
    const a = (agentsCache || []).find((x) => Number(x.id) === Number(id));
    dflt = !!(a && a.kind === 'dependent');
  }
  try {
    const v = localStorage.getItem(ttsKey(id));
    return v === null ? dflt : v === '1';
  } catch (e) {
    return dflt;
  }
}

function ttsSetOn(id, on) {
  try { localStorage.setItem(ttsKey(id), on ? '1' : '0'); } catch (e) { }
}

/** 刷新顶部喇叭图标的开/关外观 */
function renderTtsBtn() {
  const btn = $('ttsBtn');
  const on = $('ttsOnIcon');
  const off = $('ttsOffIcon');
  if (!btn) return;
  const enabled = current ? ttsIsOn(current.id) : false;
  if (on) on.style.display = enabled ? '' : 'none';
  if (off) off.style.display = enabled ? 'none' : '';
  btn.style.opacity = enabled ? '1' : '0.5';
}


/* ---------------- 朗读队列：一次只念一条，不叠着念 ---------------- */
let speakQueue = [];      // 待念的文本
let speakBusy = false;    // 正在念
let speakGen = 0;         // 世代号：切走/停止后旧回调失效
let speakAudio = null;    // 当前 audio 对象


/** 与服务器 tts.chunks 相同的分段规则：按句切，打包到 ≤110 字。 */
function splitSpeech(text, limit) {
  limit = limit || 110;
  let t = String(text || '').replace(/[\r\n]+/g, '，').trim();
  if (!t) return [];
  const parts = t.split(/(?<=[。！？!?；;…])/);
  const out = [];
  let cur = '';
  for (let p of parts) {
    p = p.trim();
    if (!p) continue;
    while (p.length > limit) {
      if (cur) { out.push(cur); cur = ''; }
      out.push(p.slice(0, limit));
      p = p.slice(limit);
    }
    if ((cur + p).length <= limit) cur += p;
    else { if (cur) out.push(cur); cur = p; }
  }
  if (cur) out.push(cur);
  return out.length ? out : [t.slice(0, limit)];
}

function speakEnqueue(text) {
  if (!text) return;
  const parts = splitSpeech(text);
  for (const p of parts) {
    if (speakQueue.length > 8) speakQueue = speakQueue.slice(-8);  // 防积压
    speakQueue.push(p);
  }
  speakPump();
}


/** 把朗读结果报到服务器日志（只在出问题时才有用，正常也留一行）。 */
function ttsReport(msg) {
  try {
    fetch('/api/applog' + qs({ m: '[voice] ' + String(msg).slice(0, 120) }),
          { credentials: 'same-origin', cache: 'no-store' }).catch(() => { });
  } catch (e) { }
}

function speakPump() {
  if (speakBusy || !speakQueue.length) return;
  const text = speakQueue.shift();
  const gen = speakGen;
  speakBusy = true;
  const url = '/api/tts' + qs({ text: text.slice(0, 2000) });
  const a = new Audio(url);
  speakAudio = a;

  let done = false;
  let watchdog = null;

  const finish = (why) => {
    if (done) return;
    if (gen !== speakGen) { done = true; return; }
    done = true;
    if (watchdog) { clearTimeout(watchdog); watchdog = null; }
    speakBusy = false;
    speakAudio = null;
    if (why && why !== 'ended') ttsReport('finish:' + why + ' len=' + text.length);
    speakPump();
  };

  // 看门狗：按字数估时长，超时强制继续，避免卡死队列
  const budgetMs = Math.max(4000, text.length * 400);
  watchdog = setTimeout(() => finish('timeout'), budgetMs);

  a.onended = () => finish('ended');
  a.onerror = () => {
    ttsReport('audio-error len=' + text.length);
    // 退回系统 TTS，念完再继续
    try {
      if (window.Native && Native.speak) {
        Native.speak(text);
        setTimeout(() => finish('native'), Math.max(1500, text.length * 180));
        return;
      }
    } catch (e) { }
    finish('error');
  };

  a.play().then(() => {
    ttsReport('play-ok len=' + text.length);
  }).catch((e) => {
    const msg = (e && e.message) ? e.message : String(e);
    ttsReport('play-rejected ' + msg);
    try {
      if (window.Native && Native.speak) {
        Native.speak(text);
        setTimeout(() => finish('native'), Math.max(1500, text.length * 180));
        return;
      }
    } catch (err) { }
    finish('rejected');
  });
}

function speakStopAll() {
  speakGen++;
  speakQueue = [];
  try { if (speakAudio) { speakAudio.pause(); speakAudio = null; } } catch (e) { }
  try { if (window.Native && Native.stopSpeak) Native.stopSpeak(); } catch (e) { }
  speakBusy = false;
}

function speakIfDependent(m) {
  try {
    if (!current) return;
    if (!m || m.role !== 'ai') return;
    if (!ttsIsOn(current.id)) return;          // 开关关着就不念
    const text = (m.text || '').trim();
    if (!text) return;
    speakEnqueue(text);
  } catch (e) { }
}



/** 点一下 AI 的消息，就单独念这一条（手动朗读，不受自动开关影响）。 */
function bindBubbleTap(row, m) {
  try {
    if (!row || !m || m.role !== 'ai') return;
    const bubble = row.querySelector('.bubble');
    if (!bubble) return;
    bubble.addEventListener('click', () => {
      try {
        if (window.Native && Native.stopSpeak) Native.stopSpeak();
        speakStopAll();
        const text = (m.text || '').trim();
        if (!text) return;
        // 手动朗读：直接入队，不考虑自动朗读开关
        const parts = splitSpeech(text);
        speakStopAll();
        for (const p of parts) speakQueue.push(p);
        const a = new Audio('/api/tts' + qs({ text: parts[0].slice(0, 2000) }));
        speakBusy = true;
        speakAudio = a;
        const rest = parts.slice(1);
        const finish = () => {
          speakBusy = false;
          speakAudio = null;
          if (rest.length) {
            for (let i = rest.length - 1; i >= 0; i--) speakQueue.unshift(rest[i]);
            speakPump();
          }
        };
        a.onended = finish;
        a.onerror = finish;
        a.play().catch(finish);
      } catch (e) { }
    });
  } catch (e) { }
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
  // 只朗读"打开聊天之后新到达"的消息（按 id 比较，比布尔标志可靠）
  const isNew = (m.id != null && openBaselineId > 0 && Number(m.id) > Number(openBaselineId));
  if (m.role === 'ai') {
    ttsReport('ai-msg id=' + m.id + ' baseline=' + openBaselineId +
      ' new=' + (isNew ? 1 : 0) + ' on=' + (current ? (ttsIsOn(current.id) ? 1 : 0) : -1) +
      ' kind=' + (current ? current.kind : '?'));
  }
  if (isNew && speechArmed) { speakIfDependent(m); }
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
    // 首次拉取后无条件初始化基线（即使一条都没有），
    // 否则基线一直是 0，后续新消息会被误判成"历史"而不朗读
    if (openBaselineId === 0) openBaselineId = Number(data.max_id || 0);

    if (items.length) {
      // 人正看着这个聊天 → 收到即已读，不攒红点
      try { if (current && API.markSeen) API.markSeen(current.id).catch(() => { }); } catch (e) { }
      const stick = lastId === 0 ? true : nearBottom();
      for (const m of items) acceptMessage(m);
      lastId = Math.max(lastId, items[items.length - 1].id);
      if (stick) scrollToBottom();
      firstLoadDone = true;
      if (openBaselineId === 0) openBaselineId = Number(data.max_id || lastId || 0);
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
  const dot = $('dot');
  const txt = $('statusText');
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

on('settingsBtn', 'click', () => { navToSettings(); });
on('settingsBack', 'click', () => { leaveSettings(); });

on('notifyFix', 'click', () => {
  try { if (window.Native && Native.openNotificationSettings) Native.openNotificationSettings(); } catch (e) { }
});
on('notifyTest', 'click', () => {
  // 诊断结果显示在设置页的「通知」状态那一行
  const txt = $('notifyStatusText');
  try {
    if (!window.Native) {
      if (txt) txt.textContent = '浏览器模式，没有通知';
      return;
    }
    const st = (Native.notifDiag ? JSON.parse(Native.notifDiag()) : {});
    if (txt) {
      txt.textContent = '权限:' + (st.enabled ? '开' : '关') +
        ' 渠道:' + (st.channel || '无') +
        ' 级别:' + (st.importance === undefined ? '?' : st.importance) +
        ' 发送:' + (st.posted ? '成功' : ('失败 ' + (st.error || ''))) +
        ' 系统通知数:' + (st.active === undefined ? '?' : st.active);
    }
  } catch (e) {
    if (txt) txt.textContent = '诊断出错: ' + e.message;
  }
});

on('voiceSelect', 'change', saveVoiceSettings);
on('rateSelect', 'change', saveVoiceSettings);
on('pitchSelect', 'change', saveVoiceSettings);
on('voiceTest', 'click', playVoicePreview);
on('voiceStop', 'click', () => {
  try { if (voicePreview) { voicePreview.pause(); voicePreview = null; } } catch (e) { }
  speakStopAll();
});

on('ttsBtn', 'click', () => {
  if (!current) return;
  const next = !ttsIsOn(current.id);
  ttsSetOn(current.id, next);
  renderTtsBtn();
  if (!next) {
    speakStopAll();
  }
});

on('backBtn', 'click', () => {
  // 主动返回列表 = 下次打开显示列表；只有"在聊天里被关掉"才恢复到那个聊天
  try { localStorage.removeItem(LAST_KEY); } catch (e) { }
  navBackOrContacts();
});
on('sendIcon', 'click', send);
on('text', 'keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
on('text', 'input', updateComposerIcons);

/* ---------------- 路由 ---------------- */
function leaveSettings() {
  hideSettings();
  navBackOrContacts();
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


/* ---------------- 设置页：音色选择 ---------------- */
let voiceCache = null;
let voicePreview = null;

async function setupVoiceSettings() {
  const sel = $('voiceSelect');
  const rsel = $('rateSelect');
  const psel = $('pitchSelect');
  const st = $('voiceStatus');
  if (!sel) return;
  try {
    const r = await fetch('/api/tts/voices' + qs(), { credentials: 'same-origin', cache: 'no-store' });
    const d = await r.json();
    voiceCache = d;
    const cur = d.current || {};

    sel.innerHTML = '';
    for (const v of (d.voices || [])) {
      const o = document.createElement('option');
      o.value = v.id;
      o.textContent = v.name;
      if (v.id === cur.voice) o.selected = true;
      sel.appendChild(o);
    }
    const fill = (node, arr, val) => {
      node.innerHTML = '';
      for (const x of (arr || [])) {
        const o = document.createElement('option');
        o.value = x; o.textContent = x;
        if (x === val) o.selected = true;
        node.appendChild(o);
      }
    };
    fill(rsel, d.rate_presets, cur.rate);
    fill(psel, d.pitch_presets, cur.pitch);

    if (st) {
      st.textContent = cur.available
        ? ('可用 · ' + (cur.voice || '').replace('Neural', '') + ' ' + (cur.rate || '') + ' ' + (cur.pitch || ''))
        : '引擎不可用（服务器没装 edge-tts）';
    }
  } catch (e) {
    if (st) st.textContent = '读取失败';
  }
}

async function saveVoiceSettings() {
  const sel = $('voiceSelect'), rsel = $('rateSelect'), psel = $('pitchSelect');
  const st = $('voiceStatus');
  if (!sel) return;
  try {
    const r = await fetch('/api/tts/settings' + qs(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        voice: sel.value,
        rate: rsel ? rsel.value : '',
        pitch: psel ? psel.value : '',
      }),
    });
    const d = await r.json();
    if (st) st.textContent = d.ok ? '已保存' : ('失败：' + (d.error || ''));
  } catch (e) {
    if (st) st.textContent = '保存失败';
  }
}

function playVoicePreview() {
  const sel = $('voiceSelect'), rsel = $('rateSelect'), psel = $('pitchSelect');
  const st = $('voiceStatus');
  const text = '嗯，我在呢。今天过得怎么样？';
  try {
    if (voicePreview) { voicePreview.pause(); voicePreview = null; }
    const url = '/api/tts' + qs({
      text,
      voice: sel ? sel.value : '',
      rate: rsel ? rsel.value : '',
      pitch: psel ? psel.value : '',
    });
    const a = new Audio(url);
    voicePreview = a;
    if (st) st.textContent = '正在合成…';
    a.onended = () => { if (st) st.textContent = '播放完毕'; };
    a.onerror = () => { if (st) st.textContent = '播放失败（服务器合成出错）'; };
    a.play().then(() => { if (st) st.textContent = '播放中…'; })
      .catch((e) => { if (st) st.textContent = '播放被拦截：' + e.message; });
  } catch (e) {
    if (st) st.textContent = '出错：' + e.message;
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
  setupVoiceSettings();
  checkNotifyPermission();
  document.title = '设置';
}

function hideSettings() {
  const st = $('settings'); if (st) st.hidden = true;
  document.title = '角落';
}


/* ---------------- 导航栈（聊天/设置各占一层，返回一定回列表） ---------------- */
const BASE_STATE = { v: 'contacts' };

function navToChat(id) {
  try {
    history.pushState({ v: 'chat', id: Number(id) }, '', '#/a/' + Number(id));
  } catch (e) {
    location.hash = '#/a/' + Number(id);
  }
  route();
}

function navToSettings() {
  try {
    history.pushState({ v: 'settings' }, '', '#/settings');
  } catch (e) {
    location.hash = '#/settings';
  }
  route();
}

function navBackOrContacts() {
  // 有上一层层级就回退（系统返回会走同一条路），否则直接回列表
  let deeper = false;
  try {
    const st = history.state || {};
    deeper = (st.v === 'chat' || st.v === 'settings');
  } catch (e) { deeper = false; }
  if (deeper) {
    try { history.back(); return; } catch (e) { }
  }
  leaveToContacts();
}

function leaveToContacts() {
  try {
    history.replaceState(BASE_STATE, '', location.pathname + location.search);
  } catch (e) {
    try { location.hash = ''; } catch (e2) { }
  }
  route();
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

// 系统返回键 / 手势：WebView 回退时触发 popstate，走同一个 route
window.addEventListener('popstate', () => {
  try { route(); } catch (e) { }
});
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



// 切到后台/回到前台时复位朗读状态，避免 onended 没触发导致队列卡死
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    speakStopAll();
    ttsReport('hidden-reset');
  }
});


/* ---------------- 系统返回键：交给页面决定 ----------------
   之前依赖 WebView 的历史栈，卡在"首次进入直接退出"：
   页面刚加载时栈里可能只有一层，goBack() 无处可去就退 App。
   现在改成页面自己处理 —— 在聊天里回列表、在设置里回列表，
   不在任何子页面时才返回 false 交给原生决定（双击退出）。
*/
window.__handleBack = function () {
  try {
    const st = $('settings');
    if (st && !st.hidden) {
      hideSettings();
      try { history.replaceState(BASE_STATE, '', location.pathname + location.search); } catch (e) { }
      return true;
    }
    if (current) {
      closeChat();
      try { history.replaceState(BASE_STATE, '', location.pathname + location.search); } catch (e) { }
      return true;
    }
  } catch (e) { }
  return false;
};

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
    // 直接进聊天（恢复上次会话 / 从通知点进来）时，先补一层"列表"，
  // 否则历史栈里只有聊天这一层，系统返回无路可走会直接退出 App。
  const _bootHash = location.hash;
  const _bootM = _bootHash.match(/^#\/a\/(\d+)/);
  if (_bootM) {
    try { history.replaceState(BASE_STATE, '', location.pathname + location.search); } catch (e) { }
    navToChat(Number(_bootM[1]));      // 回到原来那个聊天，但下面垫了列表层
  } else if (!location.hash && saved) {
    navToChat(saved);
  }
  } catch (e) {}
  route();
  layoutProbe();
  setTimeout(layoutProbe, 1600);
  checkNotifyPermission();
  setTimeout(checkNotifyPermission, 600);
  setInterval(() => { if (!current) loadAgents().catch(() => {}); }, 10000);
})();
