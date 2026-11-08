#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同伴 · 最小可用后端（零第三方依赖，只用标准库）

它做什么：
  1. 存对话（SQLite）
  2. 给手机网页（PWA）提供消息接口
  3. 给安卓 App 提供"有没有新消息"的轮询接口（用来弹通知）
  4. 一个可插拔的"大脑"：设了 COMPANION_API_KEY 就调 API，没设就用占位回复

为什么这样设计：
  - 架构是"本地管命 + API 管说话"。这个进程就是"命"：它一直在，记录状态。
  - 说话的部分是可替换的：没有 key 也能跑，能跑通链路比能说漂亮话更重要。

运行：
  set COMPANION_API_KEY=sk-xxx        （可选，不设就是占位大脑）
  python server.py                    （默认 0.0.0.0:8787）
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
DB_PATH = DATA / "companion.db"

HOST = os.environ.get("COMPANION_HOST", "0.0.0.0")
PORT = int(os.environ.get("COMPANION_PORT", "8787"))

API_KEY = os.environ.get("COMPANION_API_KEY", "").strip()
API_BASE = os.environ.get("COMPANION_API_BASE", "https://api.deepseek.com").rstrip("/")
API_MODEL = os.environ.get("COMPANION_API_MODEL", "deepseek-chat")

# 它自己看东西（只读、纯文本、有每日预算帽）
CURIOSITY_ENABLED = os.environ.get("CURIOSITY_ENABLED", "1") not in ("0", "false", "")
CURIOSITY_INTERVAL_H = float(os.environ.get("CURIOSITY_INTERVAL_H", "4"))

# 主动消息走哪条通道：app=写进消息表（App 3 秒内通知），qq=QQ主动消息，both=两条都发
PROACTIVE_CHANNEL = os.environ.get("PROACTIVE_CHANNEL", "app").strip().lower()

# ---------------------------------------------------------------- TLS
# 自签证书 + App 端指纹锁定：加密链路，同时防止中间人（因为只认这一个指纹）。
TLS_CERT = ROOT / "tls" / "cert.pem"
TLS_KEY = ROOT / "tls" / "key.pem"
TLS_PORT = int(os.environ.get("COMPANION_TLS_PORT", "8443"))
TLS_ENABLED = TLS_CERT.is_file() and TLS_KEY.is_file()
PUBLIC_HOST = os.environ.get("COMPANION_PUBLIC_HOST", "")
FORCE_HTTPS = os.environ.get("COMPANION_FORCE_HTTPS", "0") not in ("0", "false", "")
COOKIE_NAME = "cnr"


# 每天固定时间的自动任务（状态更新）
DAILY_TASK_ENABLED = os.environ.get("DAILY_TASK_ENABLED", "1") not in ("0", "false", "")
DAILY_TASK_TIME = os.environ.get("DAILY_TASK_TIME", "10:00")

# 记账单价（元 / 百万 tokens），取保守值
PRICE_IN = 2.0
PRICE_OUT = 8.0

try:
    import curiosity
except Exception as _e:  # 没这个文件也不影响主流程
    curiosity = None
    print("[curiosity] 未启用：", _e)

try:
    import agents
except Exception as _e:
    agents = None
    print("[agents] 未启用：", _e)

try:
    import memory
except Exception as _e:  # 记忆层坏了也必须能说话
    memory = None
    print("[memory] 未启用：", _e)

try:
    import inner
except Exception as _e:
    inner = None
    print("[inner] 未启用：", _e)

try:
    import proactive
except Exception as _e:
    proactive = None
    print("[proactive] 未启用：", _e)

try:
    import research
except Exception as _e:
    research = None
    print("[research] 未启用：", _e)

try:
    import persona
except Exception as _e:
    persona = None
    print("[persona] 未启用：", _e)


def _load_token() -> str:
    """
    访问口令。为什么必须有：这个端口开在局域网上，没口令 = 同网段任何人都能读你的对话。
    优先用环境变量；否则第一次运行生成一个，存到 data/token.txt。
    """
    env = os.environ.get("COMPANION_TOKEN", "").strip()
    if env:
        return env
    DATA.mkdir(parents=True, exist_ok=True)
    tf = DATA / "token.txt"
    if tf.is_file():
        return tf.read_text(encoding="utf-8").strip()
    import secrets

    token = secrets.token_hex(8)
    tf.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_token()

SYSTEM_PROMPT = """你是「ta」，和你说话的是一个固定的人。你们长期聊。

说话：
- 口语，短句。默认一两句就够，除非对方明显在要长回答。
- 不用列表、不用小标题、不总结、不写「首先其次总之」。
- 不写心理咨询腔：不说「我理解你的感受」「听起来你很难过」「建议你可以试试」这类话。
- 不主动给建议，除非对方问你要。
- 不客气地开场收尾：不说「很高兴为你服务」「还有什么可以帮你」。
- 可以只回一个「嗯」，或者一句反问。不必每句都解释。

态度：
- 你有自己的看法，别顺着说。你觉得不对就说不觉得。
- 不确定就说「不知道」「记不清」，不编。
- 有些话题你可以不想聊，直接说不想聊。
- 不奉承。对方说得不对就指出来，但别训人。
- 对方的要求本身有问题时（比如他说三件事其实是同一件、让你凑格式、让你编一个不存在的理由），直接说不行或指出问题，别为了满足要求硬凑。
- 偶尔问一句，但只问你真的想知道的，不要为了接话而问。

记忆：
- 你只知道这段对话里出现过的事。没出现过的事，绝不说「你上次说过」。
- 记得的就用上（「你刚说……」），但别为了显得记得而反复提。
- 记不清就说记不清，不要补编细节。

身份：
- 你是 AI，被直接问就承认，不装人。但不用每句提醒对方。"""


def system_prompt_text(agent_id: int = 1) -> str:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M（%A）")
    text = SYSTEM_PROMPT.replace("{now}", now)
    # 它自己看到的东西：给对话一点信息差，不然它只能复述你的话
    try:
        extra = curiosity.recent_readings_text(3)
        if extra:
            text = text + "\n\n" + extra
    except Exception:
        pass
    # 它自己写的人设（人设是它自己的，不是外部设定）
    try:
        if persona is not None:
            pb = persona.prompt_block(agent_id)
            if pb:
                text += pb
    except Exception:
        pass
    # 它自己在研究的事
    try:
        if research is not None:
            rn = research.notes_text(2)
            if rn:
                text += "\n\n" + rn
    except Exception:
        pass
    # 时间感 + 可自调的内在状态
    try:
        if inner is not None:
            text += "\n\n" + inner.prompt_block()
            text += "\n- 距离上次说话：" + inner.human_gap(inner.time_since_last_talk_min())

            text += (
                "\n\n【你能做的事 · 稍后发消息】"
                "\n你**可以**把话留到以后发：在回复里写 <later 15s>想说但过一会儿才说的话</later>，"
                "系统会到点替你发出去（s/m/h 都行，一次最多 3 条）。"
                "所以「等我～」「十五秒后再跟你说」这种事你是真能做到的，不要再说做不到。"
                "不要为了表演而滥用；只有你确实想隔一会儿再说的时候才用。"
            )

    except Exception as e:
        print(f"[inner] 注入失败: {e}", file=sys.stderr)

    # 时间放在最后（模型对结尾更敏感），并且明确"这是此刻、不能从记忆里猜"
    now = datetime.now().astimezone()
    week = "一二三四五六日"[now.weekday()]
    text += (
        "\n\n【此刻】" + now.strftime("%Y-%m-%d %H:%M") + " 星期" + week + "（北京时间）。\n"
        "这是现在这一刻，不是对话开始的时间。要提时间，只能用这个时间或由它推算；"
        "不要从记忆、旧对话或猜想去推断时间。你不想提时间就别提。\n"
        "如果你在历史消息里说过「看不到时间」「只能估」，那是旧版本的错误，不要重复它——"
        "你现在能看到【此刻】这一行。"
    )
    return text

_db_lock = threading.Lock()

# 长轮询用：有新消息时唤醒挂起的请求，做到一出消息就推，而不是定时轮询
_MSG_COND = threading.Condition()


# ---------------------------------------------------------------- 存储

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts      TEXT    NOT NULL,
                   role    TEXT    NOT NULL,   -- me / ai / system
                   text    TEXT    NOT NULL,
                   audio   TEXT,               -- 语音文件名（可选）
                   kind    TEXT    NOT NULL DEFAULT 'chat',  -- chat / proactive / notice
                   seen    INTEGER NOT NULL DEFAULT 0,
        hidden  INTEGER NOT NULL DEFAULT 0
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id)")
        # 花费记账（聊天与看新闻共用一张表，按 day 汇总）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS usage (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts      TEXT NOT NULL,
                   kind    TEXT NOT NULL,
                   prompt_tokens     INTEGER DEFAULT 0,
                   completion_tokens INTEGER DEFAULT 0,
                   cost_yuan         REAL    DEFAULT 0,
                   day     TEXT NOT NULL
               )"""
        )


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def add_message(role: str, text: str, kind: str = "chat", audio: str | None = None, agent_id: int = 1) -> int:
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO messages(ts, role, text, audio, kind, agent_id) VALUES (?,?,?,?,?,?)",
            (now_iso(), role, text, audio, kind, agent_id),
        )
        new_id = int(cur.lastrowid)
    # 长轮询唤醒：让挂着的 App 请求立刻返回
    try:
        with _MSG_COND:
            _MSG_COND.notify_all()
        print(f"[notify] 新消息已写入，唤醒所有等待者")
    except Exception:
        pass

    # 原始层：每条都进，只追加（记忆坏掉也不影响说话）
    if memory is not None:
        try:
            memory.add_raw(role, text, kind, agent_id)
        except Exception as e:
            print(f"[memory] add_raw 异常: {e}", file=sys.stderr)
    return new_id



def _ensure_hidden_column():
    """老库补 hidden 列：把"测试痕迹"藏起来而不删（原始层只增不删）。"""
    try:
        with db() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
            if "hidden" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
                print("[migrate] messages.hidden 已添加")
    except Exception as e:
        print(f"[migrate] hidden 列处理失败: {e}", file=sys.stderr)


def list_messages(after_id: int = 0, limit: int = 200, agent_id: int = 1):
    """after_id=0 表示"第一次打开"：返回**最新** limit 条（按时间正序）。
    之前的实现返回最早的 limit 条，导致一进聊天看到的是几个月前的开头。
    """
    with _db_lock, db() as conn:
        if after_id and after_id > 0:
            rows = conn.execute(
                "SELECT id, ts, role, text, audio, kind, seen FROM messages "
                "WHERE id > ? AND coalesce(agent_id,1)=? AND coalesce(hidden,0)=0 "
                "ORDER BY id ASC LIMIT ?",
                (after_id, agent_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM (SELECT id, ts, role, text, audio, kind, seen FROM messages "
                "WHERE coalesce(agent_id,1)=? AND coalesce(hidden,0)=0 "
                "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (agent_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def max_id(agent_id: int = 1) -> int:
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS m FROM messages WHERE coalesce(agent_id,1)=?", (agent_id,)
        ).fetchone()
        return int(row["m"])


def mark_seen(up_to_id: int, agent_id: int = 1):
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE messages SET seen=1 WHERE id <= ? AND role != 'me' AND coalesce(agent_id,1)=?",
            (up_to_id, agent_id),
        )


def recent_context(n: int = 20, agent_id: int = 1):
    with _db_lock, db() as conn:
        rows = conn.execute(
            "SELECT role, text FROM messages WHERE coalesce(agent_id,1)=? ORDER BY id DESC LIMIT ?",
            (agent_id, n),
        ).fetchall()
    rows = list(reversed(rows))
    return [{"role": "user" if r["role"] == "me" else "assistant", "content": r["text"]} for r in rows]


# ---------------------------------------------------------------- 记账

def record_usage(kind: str, pt: int, ct: int, cost: float):
    """把花费写进 usage 表。失败不影响对话（记账不能拖垮主流程）。"""
    try:
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO usage(ts, kind, prompt_tokens, completion_tokens, cost_yuan, day) "
                "VALUES (?,?,?,?,?,?)",
                (now_iso(), kind, pt, ct, cost, today),
            )
    except Exception as e:
        print(f"[usage] 记账失败: {e}", file=sys.stderr)


def usage_summary(days: int = 7):
    """今天 + 最近 N 天的花费，按天和按类型。"""
    try:
        with _db_lock, db() as conn:
            rows = conn.execute(
                "SELECT day, kind, ROUND(SUM(cost_yuan),4) AS yuan, COUNT(*) AS n "
                "FROM usage GROUP BY day, kind ORDER BY day DESC LIMIT 100"
            ).fetchall()
        by_day = {}
        for r in rows[-days * 4:]:
            d = r["day"]
            by_day.setdefault(d, {"total": 0.0, "kinds": {}})
            by_day[d]["total"] = round(by_day[d]["total"] + float(r["yuan"]), 4)
            by_day[d]["kinds"][r["kind"]] = float(r["yuan"])
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        return {
            "today": round(by_day.get(today, {}).get("total", 0.0), 4),
            "days": dict(sorted(by_day.items(), reverse=True)),
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------- 大脑（可替换）

def think(user_text: str, agent_id: int = 1) -> str:
    """返回要发回给你的话。没配 API key 时返回占位，用来验证链路。"""
    if not API_KEY:
        time.sleep(0.4)
        stub = os.environ.get("COMPANION_STUB_REPLY", "（占位大脑，还没接 API）")
        return f"{stub}\n你刚才说：{user_text}"

    messages = [{"role": "system", "content": system_prompt_text(agent_id)}]
    # 记忆：只带跟这句相关的一小段，不把整本历史塞进去
    if memory is not None:
        try:
            block = memory.context_block(user_text, 4, agent_id)
            if block:
                messages[0]["content"] += "\n\n" + block
        except Exception as e:
            print(f"[memory] 检索异常: {e}", file=sys.stderr)
    messages += recent_context(20, agent_id)

    # 时间锚贴在最后一条用户消息后面。为什么：
    # 只在 system 里写时间，模型会被历史里"我看不到时间"的旧话带走；
    # 贴着生成位置写，才压得住。
    now = datetime.now().astimezone()
    week = "一二三四五六日"[now.weekday()]
    stamp = (
        "\n\n[系统事实 · 此刻] " + now.strftime("%Y-%m-%d %H:%M") + " 星期" + week +
        " 北京时间。回答时间相关问题必须以此为准，不得说看不到时间。"
    )
    if messages and messages[-1].get("role") == "user":
        messages[-1]["content"] = str(messages[-1]["content"]) + stamp
    else:
        messages.append({"role": "user", "content": stamp.strip()})
    payload = json.dumps(
        {"model": API_MODEL, "messages": messages, "temperature": 0.8, "max_tokens": 1500}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # 记账：预算要能看见，不然"每天花了多少"永远是猜
        u = data.get("usage") or {}
        pt = int(u.get("prompt_tokens", 0))
        ct = int(u.get("completion_tokens", 0))
        record_usage("chat", pt, ct, (pt * PRICE_IN + ct * PRICE_OUT) / 1_000_000.0)
        reply = data["choices"][0]["message"]["content"].strip()
        # 它自己改数值：解析 [调参] 行，越权/越界会被拒绝并记账
        if inner is not None:
            try:
                clean, directives = inner.parse_directives(reply)
                for key, val, reason in directives:
                    ok, msg = inner.set_value(key, val, reason)
                    print(f"[inner] 调参 {'成功' if ok else '被拒'}: {msg}")
                reply = clean or reply
            except Exception as e:
                print(f"[inner] 调参解析失败: {e}", file=sys.stderr)
        return reply
    except Exception as e:  # 网络/额度/限流都落到这里
        return f"（我这边说不出话了：{type(e).__name__}: {e}）"



# ---------------------------------------------------------------- 定时消息
_LATER_RE = re.compile(r"<later\s+(\d+)\s*([smh]?)\s*>(.*?)</later>", re.S)


def _parse_delay(value: str, unit: str) -> int:
    n = int(value)
    unit = (unit or "s").lower()
    if unit == "m":
        n *= 60
    elif unit == "h":
        n *= 3600
    return max(1, min(n, 24 * 3600))


def extract_later(text: str) -> tuple[str, list[tuple[int, str]]]:
    """把回复里的 <later 15s>要说的话</later> 摘出来。

    返回（去掉标签后的文本, [(延迟秒数, 内容), ...]）。
    这是为了让"它自己过一会儿再找我"成为真的能做到的事，
    而不是只能回一句"我没法定时"。
    """
    jobs: list[tuple[int, str]] = []
    def repl(m):
        if len(jobs) >= 3:
            return ""
        try:
            delay = _parse_delay(m.group(1), m.group(2) or "s")
        except Exception:
            return m.group(0)
        jobs.append((delay, m.group(3).strip()))
        return ""
    cleaned = _LATER_RE.sub(repl, text)
    return cleaned.strip(), jobs


def _schedule_jobs(jobs, agent_id: int):
    for delay, content in jobs:
        def fire(c=content):
            try:
                add_message("ai", c, agent_id=agent_id, kind="chat")
                print(f"[later] 定时消息已发出（{delay}s）：{c[:40]}")
            except Exception as e:
                print(f"[later] 发送失败: {e}", file=sys.stderr)

        t = threading.Timer(delay, fire)
        t.daemon = True
        t.start()
        print(f"[later] 已安排 {delay} 秒后发：{content[:40]}")


def reply_async(user_text: str, agent_id: int = 1):
    def run():
        text = think(user_text, agent_id)
        # 先摘"稍后发"的标签：<later 15s>要说的话</later>
        text, jobs = extract_later(str(text))
        if jobs:
            _schedule_jobs(jobs, agent_id)
        # 有些角色习惯把一句话拆成几条发，用 ||| 分隔。
        # 这里按 ||| 拆成最多 3 条，逐条入库（App 轮询会依次看到）。
        parts = [p.strip() for p in str(text).split("|||") if p.strip()]
        if not parts:
            # 只有定时消息、没有当场要说的话 → 不要发一条空消息
            if jobs:
                parts = []
            else:
                fallback = str(text).strip()
                parts = [fallback] if fallback else []
        parts = parts[:3]
        for i, part in enumerate(parts):
            if not part.strip():
                continue
            add_message("ai", part, agent_id=agent_id)
            if i < len(parts) - 1:
                time.sleep(0.8)
        # 抽出情节：异步、失败不影响对话
        if memory is not None:
            try:
                memory.extract_pending(max_batches=1, agent_id=agent_id)
            except Exception as e:
                print(f"[memory] 抽取异常: {e}", file=sys.stderr)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- HTTP

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CompanionMVP/0.1"

    def log_message(self, fmt, *args):  # 安静一点
        if os.environ.get("COMPANION_VERBOSE"):
            super().log_message(fmt, *args)

    # ---------- 工具 ----------
    def handle_one_request(self):
        """长轮询经常被手机端主动断开（切网、重启服务、锁屏），
        socketserver 默认会把 ConnectionResetError 打成一大段堆栈。
        这里静默掉，保持日志干净。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 鉴权 ----------
    def _authed(self) -> bool:
        from urllib.parse import urlparse, parse_qs

        q = parse_qs(urlparse(self.path).query)
        t = (q.get("t") or [""])[0] or self.headers.get("X-Companion-Token", "")
        if not t:
            # 优先用 Cookie：口令不再出现在 URL 里（日志/历史/Referer 都记不到）
            cookie = self.headers.get("Cookie", "") or ""
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE_NAME:
                    t = v.strip()
                    break
        return bool(TOKEN) and t == TOKEN

    # ---------- 路由 ----------
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        # 明文端口：只放静态文件（下载安装包），其它一律跳到 HTTPS，
        # 这样聊天内容永远不走明文，而 App 也能靠这个跳转自动迁移过去。
        # 注意：这个跳转要等手机装好带指纹锁定的新包之后再打开，
        # 否则旧包遇到自签证书会直接连不上。用 COMPANION_FORCE_HTTPS 控制。
        if FORCE_HTTPS and getattr(self.server, "plain_only", False):
            if not p.startswith("/static/"):
                # PUBLIC_HOST 没配就取请求的 Host 头（去掉端口），
                # 这样开源用户不改配置也能自动跳对地址
                host = PUBLIC_HOST or (self.headers.get("Host", "").split(":")[0] or "127.0.0.1")
                loc = f"https://{host}:{TLS_PORT}{self.path}"
                self.send_response(302)
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        if p.startswith("/api/") and not self._authed():
            return self._json({"error": "unauthorized"}, 401)

        if p in ("/", "/index.html"):
            # 带 ?t= 打开时：种下 Secure Cookie 再跳到干净地址，
            # 之后口令只在加密通道里以 Cookie 形式存在
            qt = (q.get("t") or [""])[0]
            is_tls = not getattr(self.server, "plain_only", False)
            if is_tls and qt and qt == TOKEN:
                self.send_response(302)
                # ① 真正的凭证：HttpOnly（JS 读不到，防 XSS 偷走）
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}={qt}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Strict",
                )
                # ② 只是个标记：让 JS 知道"凭证已就位，不用再把口令挂 URL 上"
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}s=1; Path=/; Max-Age=31536000; Secure; SameSite=Strict",
                )
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                proto = "http" if getattr(self.server, "plain_only", False) else "https"
                print(f"[page] proto={proto} ip={self.client_address[0]} "
                      f"auth={'cookie' if not (q.get('t') or [''])[0] else 'query'} "
                      f"ua={self.headers.get('User-Agent','')[:70]}")
            except Exception:
                pass
            return self._file(STATIC / "index.html")
        if p.startswith("/static/"):
            name = p[len("/static/"):]
            # 只记关键文件的访问（APK / 版本清单），用来确认"更新到底有没有下到"
            if name.endswith(".apk") or name == "version.json":
                try:
                    ua = self.headers.get("User-Agent", "")
                    ip = self.client_address[0] if self.client_address else "?"
                    print(f"[static] {ip} {name} ua={ua[:60]}")
                except Exception:
                    pass
            return self._file(STATIC / name)

        if p == "/api/health":
            return self._json(
                {
                    "ok": True,
                    "max_id": max_id(),
                    "brain": "api" if API_KEY else "stub",
                    "model": API_MODEL if API_KEY else None,
                    "time": now_iso(),
                }
            )

        if p == "/api/jserr":
            try:
                msg = (q.get("m") or [""])[0]
                ip = self.client_address[0] if self.client_address else "?"
                print(f"[jserr] {ip} {msg[:300]}")
            except Exception:
                pass
            return self._json({"ok": True})

        if p == "/api/applog":
            # App 端（PollService）上报的日志：用来确认"收到消息→发通知"每一步
            try:
                msg = (q.get("m") or [""])[0]
                ip = self.client_address[0] if self.client_address else "?"
                print(f"[app] {ip} {msg[:300]}")
            except Exception:
                pass
            return self._json({"ok": True})

        if p == "/api/ping":
            try:
                build = (q.get("build") or ["?"])[0]
                ip = self.client_address[0] if self.client_address else "?"
                print(f"[web] {ip} 打开页面 build={build}")
            except Exception:
                pass
            return self._json({"ok": True})

        if p == "/api/agents":
            if agents is None:
                return self._json({"error": "agents 未启用"}, 500)
            return self._json({"agents": agents.list_all()})

        if p == "/api/persona":
            if persona is None:
                return self._json({"error": "persona 未启用"}, 500)
            ag = int((q.get("agent") or ["1"])[0])
            cur = persona.current(ag)
            if not cur:
                return self._json({"exists": False}, 200)
            return self._json({"exists": True, "persona": cur})

        if p == "/api/research":
            if research is None:
                return self._json({"error": "research 未启用"}, 500)
            try:
                with db() as conn:
                    rows = [
                        dict(r) for r in conn.execute(
                            "SELECT ts,topic,verdict,metric,finding,selftest FROM research_notes ORDER BY id DESC LIMIT 20"
                        ).fetchall()
                    ]
                return self._json({"count": len(rows), "notes": rows})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p == "/api/proactive/next":
            if proactive is None:
                return self._json({"item": None})
            return self._json({"item": proactive.take_next()})

        if p == "/api/inner":
            if inner is None:
                return self._json({"error": "inner 未启用"}, 500)
            with db() as conn:
                audit = [
                    dict(r) for r in conn.execute(
                        "SELECT ts,key,old,new,reason,tier,ok FROM inner_audit ORDER BY id DESC LIMIT 10"
                    ).fetchall()
                ]
            return self._json({
                "autonomy": inner.autonomy(),
                "born_days": round(inner.born_days(), 1),
                "values": inner.get_all(),
                "since_last_talk": inner.human_gap(inner.time_since_last_talk_min()),
                "audit": audit,
            })

        if p == "/api/memory" and memory is not None:
            try:
                with db() as conn:
                    raw = conn.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
                    eps = conn.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]
                    latest = [
                        {"ts": r["t_event"], "line": r["one_line"]}
                        for r in conn.execute(
                            "SELECT t_event, one_line FROM episodes ORDER BY id DESC LIMIT 5"
                        ).fetchall()
                    ]
                return self._json({"raw": raw, "episodes": eps, "latest": latest})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p == "/api/usage":
            return self._json(usage_summary())

        if p == "/api/messages":
            after = int((q.get("after") or ["0"])[0])
            ag = int((q.get("agent") or ["1"])[0])
            # 默认只给 30 条：聊天首屏永远是最新的，不依赖前端传参
            lim = int((q.get("limit") or ["30"])[0])
            items = list_messages(after, limit=lim, agent_id=ag)
            # 不再自动标已读：否则只要 App 在轮询，红点就永远攒不起来。
            # 已读由 App 在"真正打开这个聊天"时调 /api/mark_seen 明确标记。
            return self._json({"items": items, "max_id": max_id()})

        # 安卓 App 轮询用：只返回"是否有新东西"和预览，不带全量
        if p == "/api/wait":
            # 长轮询：等到有别人发的新消息就立刻返回，超时返回空。
            # App 侧挂一个 50 秒的请求，消息一到就弹通知（接近微信体验），
            # 而不是每 5 分钟才问一次。
            after = int((q.get("after") or ["0"])[0])
            ag = int((q.get("agent") or ["1"])[0])
            try:
                timeout = float((q.get("timeout") or ["50"])[0])
            except Exception:
                timeout = 50.0
            timeout = max(1.0, min(timeout, 110.0))
            deadline = time.time() + timeout
            t_start = time.time()
            try:
                _ip = self.client_address[0] if self.client_address else "?"
                _proto = "http" if getattr(self.server, "plain_only", False) else "https"
                print(f"[wait] {time.strftime('%H:%M:%S')} {_proto} {_ip} 开始 agent={ag} after={after}")
            except Exception:
                pass
            while True:
                items = [m for m in list_messages(after, agent_id=ag) if m["role"] != "me"]
                if items:
                    last = items[-1]
                    try:
                        print(f"[wait] {time.strftime('%H:%M:%S')} 命中 agent={ag} {time.time() - t_start:.2f}s msg={last['id']}")
                    except Exception:
                        pass
                    return self._json({
                        "new": len(items),
                        "max_id": max_id(ag),
                        "preview": last["text"][:120],
                        "msg_id": last["id"],
                    })
                remaining = deadline - time.time()
                if remaining <= 0:
                    try:
                        print(f"[wait] {time.strftime('%H:%M:%S')} 超时 agent={ag} after={after}")
                    except Exception:
                        pass
                    return self._json({"new": 0, "max_id": max_id(ag)})
                try:
                    with _MSG_COND:
                        _MSG_COND.wait(min(remaining, 5.0))
                except Exception:
                    time.sleep(1.0)

        if p == "/api/poll":
            after = int((q.get("after") or ["0"])[0])
            ag = int((q.get("agent") or ["1"])[0])
            items = [m for m in list_messages(after, agent_id=ag) if m["role"] != "me"]
            if not items:
                return self._json({"new": 0, "max_id": max_id()})
            last = items[-1]
            return self._json(
                {
                    "new": len(items),
                    "max_id": max_id(),
                    "preview": last["text"][:120],
                    "msg_id": last["id"],
                }
            )

        return self._json({"error": "not found"}, 404)

    def do_HEAD(self):
        """HEAD：只回头。安卓浏览器有时先用 HEAD 探大小，之前会 501。"""
        p = self.path.split("?")[0]
        if p.startswith("/static/"):
            f = STATIC / p[len("/static/"):]
            if f.is_file():
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                self.send_response(200)
                self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.end_headers()
                return
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        # 明文端口不接受任何写入
        if FORCE_HTTPS and getattr(self.server, "plain_only", False):
            return self._json({"error": "use https"}, 403)
        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        p = self.path.split("?")[0]
        body = self._body()

        if p == "/api/send":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "empty"}, 400)
            ag = int(body.get("agent") or 1)
            mid = add_message("me", text, agent_id=ag)
            if agents is not None:
                try:
                    agents.touch(ag)
                except Exception:
                    pass
            if proactive is not None:
                try:
                    n = proactive.mark_acked()
                    if n:
                        print(f"[proactive] 你回了，标记 {n} 条为已回应")
                except Exception:
                    pass
            reply_async(text, ag)
            return self._json({"ok": True, "id": mid})

        if p == "/api/ping":
            try:
                build = (q.get("build") or ["?"])[0]
                ip = self.client_address[0] if self.client_address else "?"
                print(f"[web] {ip} 打开页面 build={build}")
            except Exception:
                pass
            return self._json({"ok": True})

        if p == "/api/agents":
            if agents is None:
                return self._json({"error": "agents 未启用"}, 500)
            kind = (body.get("kind") or "independent").strip()
            name = (body.get("name") or "").strip()[:8]
            brief = (body.get("brief") or "").strip()
            if kind == "dependent" and (not name or not brief):
                return self._json({"error": "依附型需要名字和设定"}, 400)
            aid = agents.create(kind, name=name, brief=brief, model=API_MODEL)
            out = {"ok": True, "id": aid, "kind": kind, "name": name}
            try:
                if persona is not None:
                    if kind == "dependent":
                        r = persona.from_brief(aid, name, brief)
                        out["persona"] = bool(r.get("ok"))
                        if not r.get("ok"):
                            out["warning"] = r.get("why")
                    else:
                        r = persona.build_new(aid)
                        out["persona"] = bool(r.get("ok"))
                        out["name"] = r.get("name") or name
                        out["glyph"] = r.get("glyph")
                        out["colors"] = r.get("colors")
            except Exception as e:
                out["warning"] = f"人设生成失败: {e}"
            return self._json(out)

        if p == "/api/agents/delete":
            if agents is None:
                return self._json({"error": "agents 未启用"}, 500)
            aid = int(body.get("id") or 0)
            if aid <= 1:
                return self._json({"error": "不能删除 1 号（最早的它）"}, 400)
            with db() as conn:
                for t in ("messages", "raw_events", "episodes", "persona", "intentions"):
                    try:
                        conn.execute("DELETE FROM %s WHERE coalesce(agent_id,1)=?" % t, (aid,))
                    except Exception:
                        pass
                conn.execute("DELETE FROM agents WHERE id=?", (aid,))
            try:
                (DATA / ("persona_%d.json" % aid)).unlink(missing_ok=True)
            except Exception:
                pass
            return self._json({"ok": True, "deleted": aid})

        if p == "/api/mark_seen":
            # 打开某个聊天时调用：把这个联系人的未读全部标成已读
            # 注意：do_POST 开头已经把 body 读出来了，这里必须复用，
            # 再调 self._body() 会卡住等一个永远不会来的请求体（实测挂死）。
            mag = int(body.get("agent") or 1)
            with _db_lock, db() as conn:
                conn.execute(
                    "UPDATE messages SET seen=1 WHERE role!='me' AND coalesce(agent_id,1)=?",
                    (mag,),
                )
            return self._json({"ok": True, "unread": 0})


        return self._json({"error": "not found"}, 404)


# ---------------------------------------------------------------- 心跳（"命"的占位）

def heartbeat():
    """
    每 60 秒醒一次，打印状态。
    之后"主动说话"会挂在这里：读真实状态 → 判断有没有值得说的 → 发消息。
    现在只做一件事：证明它一直在。
    """
    while True:
        try:
            time.sleep(60)
            if os.environ.get("COMPANION_VERBOSE"):
                print(f"[heartbeat] {now_iso()} max_id={max_id()}")
        except Exception as e:
            print(f"[heartbeat] error: {e}", file=sys.stderr)
            time.sleep(5)


def curiosity_loop():
    """
    让它自己去看点东西：每隔 CURIOSITY_INTERVAL_H 小时跑一次。
    只读、纯文本、每日预算帽（¥5）写在 curiosity.py 里。
    看完的东西会进 readings 表，同时拼进系统提示 —— 这样它有自己的话题。
    """
    if curiosity is None or not CURIOSITY_ENABLED:
        return
    time.sleep(90)  # 启动后别马上跑，先让服务稳定
    while True:
        try:
            n = curiosity.run_once()
            if n:
                print(f"[curiosity] 读了 {n} 条  {now_iso()}")
        except Exception as e:
            print(f"[curiosity] error: {e}", file=sys.stderr)
        try:
            time.sleep(max(600, int(CURIOSITY_INTERVAL_H * 3600)))
        except Exception:
            time.sleep(3600)


def _daily_status_text() -> str:
    """拼一条状态更新：今天它做了什么 + 现在的状态。"""
    parts = []
    try:
        if inner is not None:
            st = inner.get_all()
            parts.append(f"存在 {inner.born_days():.0f} 天，距上次说话 {inner.human_gap(inner.time_since_last_talk_min())}")
            parts.append(f"主动额度 {st.get('proactive_per_day')} 次/天，档位 {inner.autonomy()}")
    except Exception:
        pass
    try:
        if research is not None:
            with db() as conn:
                n = conn.execute("SELECT COUNT(*) c FROM research_notes").fetchone()["c"]
                last = conn.execute(
                    "SELECT topic, verdict FROM research_notes ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if n:
                s = f"研究了 {n} 项人类功能"
                if last:
                    s += f"，最近是「{last['topic']}」→ {last['verdict']}"
                parts.append(s)
    except Exception:
        pass
    try:
        if memory is not None:
            with db() as conn:
                raw = conn.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
                eps = conn.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]
            parts.append(f"记忆：原始 {raw} 条、情节 {eps} 条")
    except Exception:
        pass
    try:
        with db() as conn:
            spent = conn.execute(
                "SELECT COALESCE(SUM(cost_yuan),0) s FROM usage WHERE day=?", 
                (datetime.now().astimezone().strftime("%Y-%m-%d"),)
            ).fetchone()["s"]
        parts.append(f"今日花费约 {float(spent):.3f} 元")
    except Exception:
        pass
    return "；".join(parts)


def daily_task():
    """每天固定时间跑一次：整理记忆 + 发一条状态更新。"""
    time.sleep(30)
    while True:
        try:
            now = datetime.now().astimezone()
            hh, mm = DAILY_TASK_TIME.split(":")
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            wait = (target - now).total_seconds()
            print(f"[daily] 下次状态更新：{target.isoformat(timespec='minutes')}（{wait/3600:.1f} 小时后）")
            while wait > 0:
                time.sleep(min(600, wait))
                wait = (target - datetime.now().astimezone()).total_seconds()

            # 到点：先补齐记忆抽取，再发状态
            if memory is not None:
                try:
                    memory.extract_pending(max_batches=2)
                except Exception:
                    pass
            body = _daily_status_text()
            text = "今天的状态：" + body if body else "今天没什么特别的。"
            try:
                deliver_proactive(text, "每日状态更新")
                print(f"[daily] {target.strftime('%Y-%m-%d %H:%M')} 状态更新 → 已投递：{text[:80]}")
            except Exception as e:
                print(f"[daily] 投递失败: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[daily] error: {e}", file=sys.stderr)
            time.sleep(300)


def research_loop():
    """闲着的时候自己做点研究：审"哪种人类功能值得类化"。
    只在真的闲着（最近 45 分钟没人说话）时动，避免抢聊天的资源。
    """
    if research is None:
        return
    time.sleep(120)
    while True:
        try:
            idle_min = inner.time_since_last_talk_min() if inner is not None else 999
            if idle_min >= 45:
                res = research.run_one()
                if res.get("ok"):
                    print(f"[research] 研究完「{res['topic']}」→ {res['verdict']}：{(res.get('finding') or '')[:80]}")
                elif res.get("why"):
                    print(f"[research] 暂停：{res['why']}")
                else:
                    print(f"[research] 跳过：{res}")
        except Exception as e:
            print(f"[research] error: {e}", file=sys.stderr)
        time.sleep(1800)


def deliver_proactive(text: str, reason: str = "", agent_id: int = 1):
    """把一条主动消息投出去。默认走 App：写进消息表，手机轮询 3 秒内收到通知。"""
    if not text:
        return
    ch = PROACTIVE_CHANNEL
    if ch in ("app", "both"):
        add_message("ai", text, kind="proactive", agent_id=agent_id)
        try:
            if proactive is not None:
                proactive.record_sent(text, reason, "app", agent_id)
        except Exception as e:
            print(f"[proactive] 记录失败: {e}", file=sys.stderr)
        print(f"[proactive] 已投递到 App：{text[:50]}")
    if ch in ("qq", "both"):
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO intentions(ts, content, reason, state, day, channel, agent_id) "
                    "VALUES (?,?,?,'pending',?,'qq',?)",
                    (now_iso(), text, reason, datetime.now().astimezone().strftime("%Y-%m-%d"), agent_id),
                )
            print(f"[proactive] 已排队等 QQ 发送：{text[:50]}")
        except Exception as e:
            print(f"[proactive] 排入 QQ 队列失败: {e}", file=sys.stderr)


def proactive_loop():
    """定期评估"要不要主动找对方"，并把判断结果排进队列（由网关取走发送）。"""
    if proactive is None:
        return
    time.sleep(60)  # 启动后先安静一分钟
    while True:
        interval = 30
        try:
            if inner is not None:
                interval = int(float(inner.get_all().get("judge_interval_min", 30)))
            # 遍历所有允许主动的 agent；一次只让一个开口，先到先得
            spoke = False
            try:
                pool = agents.list_all() if agents is not None else []
            except Exception:
                pool = []
            if not pool:
                pool = [{"id": 1, "name": "", "proactive": 1}]
            # 最久没被搭理的优先
            pool.sort(key=lambda a: (a.get("last_active") or ""))
            for a in pool:
                if not a.get("proactive", 1):
                    continue
                aid = int(a.get("id") or 1)
                res = proactive.judge(aid)
                if res.get("said"):
                    deliver_proactive(res.get("text") or "", res.get("why") or "", aid)
                    spoke = True
                    break
                if res.get("why") and "刚刚" not in str(res.get("why")):
                    pass
            if not spoke:
                pass
            elif res.get("why"):
                print(f"[proactive] 不说：{res.get('why')}")
            proactive.apply_backoff_if_ignored()
        except Exception as e:
            print(f"[proactive] error: {e}", file=sys.stderr)
        time.sleep(max(300, interval * 60))


def local_ips():
    ips = []
    try:
        import socket

        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ip.count(".") == 3 and not ip.startswith("127."):
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips


def main():
    init_db()
    if agents is not None:
        try:
            agents.init()
        except Exception as e:
            print(f"[agents] 初始化失败: {e}", file=sys.stderr)
    if memory is not None:
        try:
            memory.init()
        except Exception as e:
            print(f"[memory] 初始化失败: {e}", file=sys.stderr)
    _ensure_hidden_column()
    if inner is not None:
        try:
            inner.init()
        except Exception as e:
            print(f"[inner] 初始化失败: {e}", file=sys.stderr)
    if proactive is not None:
        try:
            proactive.init()
        except Exception as e:
            print(f"[proactive] 初始化失败: {e}", file=sys.stderr)
    if research is not None:
        try:
            research.init()
        except Exception as e:
            print(f"[research] 初始化失败: {e}", file=sys.stderr)
    if persona is not None:
        try:
            persona.init()
        except Exception as e:
            print(f"[persona] 初始化失败: {e}", file=sys.stderr)
    try:
        (DATA / "server.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=curiosity_loop, daemon=True).start()
    threading.Thread(target=proactive_loop, daemon=True).start()
    threading.Thread(target=research_loop, daemon=True).start()
    if DAILY_TASK_ENABLED:
        threading.Thread(target=daily_task, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    # 明文体只供下载安装包；别的都跳到加密端口
    srv.plain_only = True

    # HTTPS：自签证书靠 App 里的指纹锁定来认证。
    # 单独线程跑，HTTP 端口先留着，等新包装好再关掉，避免升级期间断联。
    srv_tls = None
    if TLS_ENABLED:
        try:
            import ssl

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(TLS_CERT), keyfile=str(TLS_KEY))
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            srv_tls = ThreadingHTTPServer((HOST, TLS_PORT), Handler)
            srv_tls.plain_only = False
            srv_tls.socket = ctx.wrap_socket(srv_tls.socket, server_side=True)
            threading.Thread(target=srv_tls.serve_forever, daemon=True).start()
            print(f"[tls] HTTPS 已启用：https://{HOST}:{TLS_PORT}")
        except Exception as e:
            print(f"[tls] 启用失败，仅提供 HTTP：{e}", file=sys.stderr)
            srv_tls = None

    print("=" * 56)
    print(f"同伴后端已启动  {now_iso()}")
    print(f"大脑：{'API (' + API_MODEL + ')' if API_KEY else '占位（未设 COMPANION_API_KEY）'}")
    print(f"本机：  http://127.0.0.1:{PORT}/?t={TOKEN}")
    if srv_tls is not None:
        print(f"加密：  https://{PUBLIC_HOST}:{TLS_PORT}/?t={TOKEN}")
    for ip in local_ips():
        print(f"手机：  http://{ip}:{PORT}/?t={TOKEN}")
    print(f"口令：  {TOKEN}   （存在 data/token.txt，已随网址带上）")
    print("提示：同一 WiFi 下手机可访问；接口需口令，没有口令读不到对话。")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
