#!/usr/bin/env python3
"""记忆层 P0：原始层（只追加）+ 情节层（异步抽取）+ 检索 + 上下文拼装。

设计依据：原始层只追加（触发器拒绝改删）+ 情节层由模型异步抽取，两条线互不阻塞。
- 原始层只增不删（数据库触发器拒绝 UPDATE/DELETE）
- 情节层由 LLM 异步抽取，每条带来源 raw id（证据链）
- 检索打分 = 字面重合 + 时间衰减 + 可及性（P0 不上向量，够用再换）
- 有每日预算帽，超了暂停抽取（不阻塞说话）

用法：
    python memory.py --stats        看层数和最近记忆
    python memory.py --dump 10      看最近 10 条情节
    python memory.py --extract      立刻抽取一批（调试用）
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "companion.db"

API_KEY = os.environ.get("COMPANION_API_KEY", "").strip()
API_BASE = os.environ.get("COMPANION_API_BASE", "https://api.deepseek.com").rstrip("/")
API_MODEL = os.environ.get("COMPANION_API_MODEL", "deepseek-flash")

# 抽取用便宜/稳定的设置；预算帽（元），与 curiosity 共用 usage 表
DAILY_YUAN = float(os.environ.get("MEMORY_DAILY_YUAN", "5"))
MIN_CHARS = int(os.environ.get("MEMORY_MIN_CHARS", "2"))

PRICE_IN = 2.0
PRICE_OUT = 8.0

DDL = [
    # 原始层：只追加
    """CREATE TABLE IF NOT EXISTS raw_events (
           id        INTEGER PRIMARY KEY AUTOINCREMENT,
           ts        TEXT NOT NULL,
           role      TEXT NOT NULL,
           text      TEXT NOT NULL,
           kind      TEXT NOT NULL DEFAULT 'chat',
           processed INTEGER NOT NULL DEFAULT 0
       )""",
    # 情节层：一句话摘要 + 证据链
    """CREATE TABLE IF NOT EXISTS episodes (
           id         INTEGER PRIMARY KEY AUTOINCREMENT,
           ts         TEXT NOT NULL,
           t_event    TEXT NOT NULL,
           one_line   TEXT NOT NULL,
           topics     TEXT,
           emotion    REAL DEFAULT 0.3,
           raw_from   INTEGER NOT NULL,
           raw_to     INTEGER NOT NULL,
           access     REAL NOT NULL DEFAULT 1.0,
           ref_count  INTEGER NOT NULL DEFAULT 0,
           state      TEXT NOT NULL DEFAULT 'active'
       )""",
    # 语义层：先建表，P1 才填
    """CREATE TABLE IF NOT EXISTS patterns (
           id         INTEGER PRIMARY KEY AUTOINCREMENT,
           ts         TEXT NOT NULL,
           pattern    TEXT NOT NULL,
           confidence REAL NOT NULL DEFAULT 0.5,
           evidence   TEXT,
           counter    TEXT,
           state      TEXT NOT NULL DEFAULT 'active'
       )""",
    # 标错五型
    """CREATE TABLE IF NOT EXISTS flags (
           id         INTEGER PRIMARY KEY AUTOINCREMENT,
           ts         TEXT NOT NULL,
           target_table TEXT NOT NULL,
           target_id    INTEGER NOT NULL,
           kind         TEXT NOT NULL,
           reason       TEXT,
           body_policy  TEXT NOT NULL DEFAULT 'metadata_only'
       )""",
    """CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts)""",
    """CREATE INDEX IF NOT EXISTS idx_raw_processed ON raw_events(processed)""",
]


def _now():
    return datetime.now(timezone.utc).astimezone()


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        for sql in DDL:
            conn.execute(sql)
        # 原始层：内容只增不改不删。
        # 注意：processed 是"处理状态"不是内容，必须允许更新，否则抽取永远跑不完。
        try:
            conn.execute("DROP TRIGGER IF EXISTS raw_content_locked")
            conn.execute("DROP TRIGGER IF EXISTS raw_no_update")
            conn.execute("DROP TRIGGER IF EXISTS raw_no_delete")
            conn.execute("DROP TRIGGER IF EXISTS raw_no_delete_row")
            conn.execute(
                """CREATE TRIGGER raw_content_locked BEFORE UPDATE ON raw_events
                   WHEN OLD.ts IS NOT NEW.ts OR OLD.role IS NOT NEW.role
                     OR OLD.text IS NOT NEW.text OR OLD.kind IS NOT NEW.kind
                   BEGIN SELECT RAISE(ABORT, 'raw_events 内容只允许追加'); END"""
            )
            conn.execute(
                """CREATE TRIGGER raw_no_delete BEFORE DELETE ON raw_events
                   BEGIN SELECT RAISE(ABORT, 'raw_events 不允许删除'); END"""
            )
        except sqlite3.Error as e:
            print(f"[memory] 建触发器失败: {e}", file=sys.stderr)


# ---------------------------------------------------------------- 写入

def add_raw(role: str, text: str, kind: str = "chat", agent_id: int = 1) -> int:
    """每条对话都进原始层。失败不抛出（不能因为记忆坏了就不说话）。"""
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO raw_events(ts, role, text, kind, agent_id) VALUES (?,?,?,?,?)",
                (_iso(), role, text, kind, agent_id),
            )
            return int(cur.lastrowid)
    except Exception as e:
        print(f"[memory] add_raw 失败: {e}", file=sys.stderr)
        return 0


def _spend_today(conn):
    day = _now().strftime("%Y-%m-%d")
    try:
        row = conn.execute("SELECT COALESCE(SUM(cost_yuan),0) AS s FROM usage WHERE day=?", (day,)).fetchone()
        return float(row["s"])
    except sqlite3.Error:
        return 0.0


def _add_cost(conn, kind, pt, ct):
    cost = (pt * PRICE_IN + ct * PRICE_OUT) / 1_000_000.0
    try:
        conn.execute(
            "INSERT INTO usage(ts, kind, prompt_tokens, completion_tokens, cost_yuan, day) VALUES (?,?,?,?,?,?)",
            (_iso(), kind, pt, ct, cost, _now().strftime("%Y-%m-%d")),
        )
    except sqlite3.Error:
        pass
    return cost


# ---------------------------------------------------------------- 抽取

EXTRACT_PROMPT = """把下面这段对话压缩成"一条记忆"。

规则：
- 一句话，不超过 60 字，只写发生了什么，或对方说的值得记住的事。
- 不评价、不总结感受、不写客套话。
- 如果这段对话没有任何值得记的内容（打招呼、嗯、好的、纯功能确认），输出 {{"skip": true}}。
- 只输出 JSON，不要别的字：

{{"line": "一句话记忆", "topics": ["关键词1","关键词2"], "emotion": 0.0到1.0, "about": "me"或"it"}}

对话：
{convo}"""


def _call_extract(convo: str):
    body = json.dumps(
        {
            "model": API_MODEL,
            "messages": [{"role": "user", "content": EXTRACT_PROMPT.format(convo=convo)}],
            "temperature": 0.3,
            "max_tokens": 1200,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage") or {}
    m = re.search(r"\{.*\}", text, re.S)
    obj = {}
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = {}
    return obj, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def extract_pending(max_batches: int = 3, batch_size: int = 8, agent_id: int = 1) -> int:
    """把未处理的原始事件按批抽成情节。返回新增情节数。"""
    if not API_KEY:
        return 0
    made = 0
    with db() as conn:
        if _spend_today(conn) >= DAILY_YUAN:
            return 0

    for _ in range(max_batches):
        with db() as conn:
            rows = conn.execute(
                "SELECT id, ts, role, text FROM raw_events WHERE processed=0 AND coalesce(agent_id,1)=? "
                "ORDER BY id ASC LIMIT ?",
                (agent_id, batch_size),
            ).fetchall()
        if not rows:
            break

        convo = "\n".join(
            ("对方: " if r["role"] == "me" else "我: ") + r["text"].replace("\n", " ")[:300] for r in rows
        )
        if sum(len(r["text"]) for r in rows) < MIN_CHARS * len(rows):
            with db() as conn:
                conn.executemany("UPDATE raw_events SET processed=1 WHERE id=?", [(r["id"],) for r in rows])
            continue

        try:
            obj, pt, ct = _call_extract(convo)
        except Exception as e:
            print(f"[memory] 抽取失败: {e}", file=sys.stderr)
            break

        with db() as conn:
            _add_cost(conn, "memory_extract", pt, ct)
            if obj.get("skip") or not (obj.get("line") or "").strip():
                conn.executemany("UPDATE raw_events SET processed=1 WHERE id=?", [(r["id"],) for r in rows])
                continue
            conn.execute(
                "INSERT INTO episodes(ts, t_event, one_line, topics, emotion, raw_from, raw_to, agent_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    _iso(),
                    rows[-1]["ts"],
                    obj["line"].strip()[:120],
                    json.dumps(obj.get("topics") or [], ensure_ascii=False),
                    float(obj.get("emotion") or 0.3),
                    rows[0]["id"],
                    rows[-1]["id"],
                    agent_id,
                ),
            )
            conn.executemany("UPDATE raw_events SET processed=1 WHERE id=?", [(r["id"],) for r in rows])
        made += 1
    return made


# ---------------------------------------------------------------- 检索

def _bigrams(text: str):
    text = re.sub(r"\s+", "", text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def relevant(query: str, n: int = 4, agent_id: int = 1):
    """按 字面重合 + 时间衰减 + 可及性 打分。
    关键：**必须真有点字面重合才返回**——否则"时间新"这一项会让所有记忆都合格，
    检索就等于把整本历史倒出来，那和没有检索一样。
    """
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, t_event, one_line, topics, access, ref_count FROM episodes "
                "WHERE state='active' AND coalesce(agent_id,1)=? ORDER BY id DESC LIMIT 500",
                (agent_id,),
            ).fetchall()
    except sqlite3.Error:
        return []
    if not rows:
        return []

    q = _bigrams(query)
    now = _now()
    scored = []
    for r in rows:
        try:
            age_days = max(0.0, (now - datetime.fromisoformat(r["t_event"])).total_seconds() / 86400)
        except Exception:
            age_days = 0.0
        memo = _bigrams((r["one_line"] or "") + " " + (r["topics"] or ""))
        overlap = len(q & memo) / max(1, len(q))
        if overlap <= 0:
            continue  # 没有共同字面，不算相关
        recency = math.exp(-age_days / 30.0)
        access = min(2.0, float(r["access"] or 1.0)) / 2.0
        score = 0.45 * min(1.0, overlap * 4) + 0.35 * recency + 0.20 * access
        scored.append((score, r))

    scored.sort(key=lambda x: -x[0])
    return [
        {"id": r["id"], "ts": r["t_event"], "line": r["one_line"], "score": round(s, 3)}
        for s, r in scored[:n]
    ]


def recent(n: int = 2, agent_id: int = 1):
    """最近发生的事：不靠检索，保证连续性（"上次我们说到…"）。"""
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, t_event, one_line FROM episodes WHERE state='active' "
                "AND coalesce(agent_id,1)=? ORDER BY id DESC LIMIT ?", (agent_id, n)
            ).fetchall()
    except sqlite3.Error:
        return []
    return [{"id": r["id"], "ts": r["t_event"], "line": r["one_line"]} for r in reversed(rows)]


def touch(ids):
    """被用到的记忆：可及性 +，引用次数 +（这就是再巩固的雏形）。"""
    if not ids:
        return
    try:
        with db() as conn:
            for i in ids:
                conn.execute(
                    "UPDATE episodes SET access = MIN(2.0, access + 0.2), ref_count = ref_count + 1 WHERE id=?",
                    (i,),
                )
    except sqlite3.Error:
        pass


def context_block(query: str, n: int = 4, agent_id: int = 1) -> str:
    """拼进系统提示的记忆片段：最近发生的 + 与这句话相关的。

    为什么分两组：
    - "最近"保证连续性（上次聊到哪），不依赖检索命中
    - "相关"才让长期记忆起作用；两者重复的不算两次
    没有任何内容时返回空串（不要塞一句"你没什么记忆"进提示）。
    """
    recent_hits = recent(2, agent_id)
    rel_hits = relevant(query, n, agent_id)
    seen = {h["id"] for h in recent_hits}
    rel_hits = [h for h in rel_hits if h["id"] not in seen]

    if not recent_hits and not rel_hits:
        return ""

    lines = []
    if recent_hits:
        lines.append("你最近经历的事：")
        for h in recent_hits:
            lines.append(f"- [{h['ts'][5:10]}] {h['line']}")
    if rel_hits:
        lines.append("和这个话题可能有关的旧记忆（不相关就别硬提）：")
        for h in rel_hits:
            lines.append(f"- [{h['ts'][5:10]}] {h['line']}")

    touch([h["id"] for h in rel_hits])
    return "\n".join(lines)


# ---------------------------------------------------------------- 查看

def backfill_from_messages() -> int:
    """一次性把已有的 messages 灌进原始层（只做一次，幂等）。"""
    try:
        with db() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
            if n:
                return 0
            rows = conn.execute("SELECT ts, role, text, kind FROM messages ORDER BY id ASC").fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO raw_events(ts, role, text, kind) VALUES (?,?,?,?)",
                    (r["ts"], r["role"], r["text"], r["kind"] or "chat"),
                )
            return len(rows)
    except sqlite3.Error as e:
        print(f"[memory] backfill 失败: {e}", file=sys.stderr)
        return 0


def stats():
    with db() as conn:
        raw = conn.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
        unprocessed = conn.execute("SELECT COUNT(*) c FROM raw_events WHERE processed=0").fetchone()["c"]
        eps = conn.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]
        pats = conn.execute("SELECT COUNT(*) c FROM patterns").fetchone()["c"]
        spend = _spend_today(conn)
        latest = conn.execute("SELECT ts, one_line FROM episodes ORDER BY id DESC LIMIT 5").fetchall()
    print(f"原始层   : {raw} 条（未抽取 {unprocessed}）")
    print(f"情节层   : {eps} 条")
    print(f"语义层   : {pats} 条（P0 不填）")
    print(f"今日花费 : {spend:.4f} 元")
    if latest:
        print("最近情节：")
        for r in latest:
            print(f"  {r['ts'][5:16]}  {r['one_line']}")


def dump(n=10):
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, t_event, one_line, topics, raw_from, raw_to FROM episodes ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    for r in rows:
        print(f"{r['ts'][5:16]}  {r['one_line']}")
        print(f"           topics={r['topics']}  raw={r['raw_from']}..{r['raw_to']}")


if __name__ == "__main__":
    init()
    if "--stats" in sys.argv:
        stats()
    elif "--dump" in sys.argv:
        i = sys.argv.index("--dump")
        n = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 10
        dump(n)
    elif "--backfill" in sys.argv:
        print(f"回填 {backfill_from_messages()} 条到原始层")
        stats()
    elif "--query" in sys.argv:
        i = sys.argv.index("--query")
        q = sys.argv[i + 1] if len(sys.argv) > i + 1 else "在吗"
        print("查询：", q)
        for h in relevant(q, 8):
            print(f"  {h['score']:.2f}  [{h['ts'][5:10]}] {h['line']}")
    else:
        made = extract_pending(max_batches=10)
        print(f"新增情节 {made} 条")
        stats()
