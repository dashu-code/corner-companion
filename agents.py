#!/usr/bin/env python3
"""联系人（agents）：一个 App 里可以存在多个 AI，各自独立。

两种标签：
- independent（独立）：它自己起名字、自己写人设（像「隅」）。
- dependent（依附）：由使用者塑造——给一个名字和一段设定，它按那个设定活。

隔离原则：**记忆不共享**。每个 agent 有自己的消息、原始层、情节层、人设。
（全局的"它自己的生活"——看新闻、自研究——目前只属于 1 号 agent。）
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "companion.db"

DEFAULT_ID = 1


def _now():
    return datetime.now(timezone.utc).astimezone()


def _iso():
    return _now().isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn, table, col):
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
        return col in cols
    except sqlite3.Error:
        return False


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agents (
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   name       TEXT NOT NULL,
                   kind       TEXT NOT NULL DEFAULT 'independent',  -- independent / dependent
                   model      TEXT,
                   brief      TEXT,        -- 依附型：使用者给的设定
                   avatar_glyph  TEXT,
                   avatar_c1  TEXT,
                   avatar_c2  TEXT,
                   created    TEXT NOT NULL,
                   last_active TEXT
               )"""
        )
        # 每个 agent 是否允许主动找你（默认允许）
        if not _has_column(conn, "agents", "proactive"):
            try:
                conn.execute("ALTER TABLE agents ADD COLUMN proactive INTEGER NOT NULL DEFAULT 1")
            except sqlite3.Error:
                pass

        # 给已有表补 agent_id（默认 1 = 最早那个）
        for table in ("messages", "raw_events", "episodes", "persona", "intentions"):
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if exists and not _has_column(conn, table, "agent_id"):
                    conn.execute("ALTER TABLE %s ADD COLUMN agent_id INTEGER DEFAULT 1" % table)
            except sqlite3.Error:
                pass

        # 1 号 agent 必须存在
        row = conn.execute("SELECT id FROM agents WHERE id=?", (DEFAULT_ID,)).fetchone()
        if not row:
            p = conn.execute(
                "SELECT data FROM persona ORDER BY id DESC LIMIT 1"
            ).fetchone() if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona'"
            ).fetchone() else None
            import json as _json

            name = "它"
            glyph, c1, c2 = "它", "#3A3F3A", "#BEB6A3"
            if p:
                try:
                    d = _json.loads(p["data"])
                    name = d.get("name") or name
                    glyph = d.get("avatar_glyph") or name[:1]
                    cols = d.get("avatar_colors") or []
                    if isinstance(cols, list) and len(cols) == 2:
                        c1, c2 = cols
                except Exception:
                    pass
            conn.execute(
                "INSERT INTO agents(id, name, kind, produced, created) VALUES (?,?,?,?,?)"
                if False else
                "INSERT INTO agents(id, name, kind, avatar_glyph, avatar_c1, avatar_c2, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (DEFAULT_ID, name, "independent", glyph, c1, c2, _iso()),
            )


def list_all():
    init()
    with db() as conn:
        rows = conn.execute(
            "SELECT a.*, "
            "(SELECT COUNT(*) FROM messages m WHERE m.agent_id=a.id AND coalesce(m.hidden,0)=0) AS msg_count, "
            "(SELECT COUNT(*) FROM messages m WHERE coalesce(m.agent_id,1)=a.id "
            " AND m.role='ai' AND m.seen=0 AND coalesce(m.hidden,0)=0) AS unread, "
            "(SELECT MAX(m.ts) FROM messages m WHERE coalesce(m.agent_id,1)=a.id "
            " AND coalesce(m.hidden,0)=0) AS last_ts, "
            "(SELECT m.text FROM messages m WHERE coalesce(m.agent_id,1)=a.id "
            " AND coalesce(m.hidden,0)=0 ORDER BY m.id DESC LIMIT 1) AS last_text "
            "FROM agents a "
            "ORDER BY (last_ts IS NULL), last_ts DESC, a.id"
        ).fetchall()
    return [dict(r) for r in rows]


def get(agent_id):
    init()
    with db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return dict(row) if row else None


def create(kind: str, name: str = "", brief: str = "", model: str | None = None) -> int:
    init()
    kind = "dependent" if kind == "dependent" else "independent"
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO agents(name, kind, model, brief, created) VALUES (?,?,?,?,?)",
            (name or "（还没起名）", kind, model, brief, _iso()),
        )
        return int(cur.lastrowid)


def update_identity(agent_id: int, name: str = None, glyph: str = None, c1: str = None, c2: str = None):
    init()
    sets, vals = [], []
    for key, val in (("name", name), ("avatar_glyph", glyph), ("avatar_c1", c1), ("avatar_c2", c2)):
        if val:
            sets.append("%s=?" % key)
            vals.append(val)
    if not sets:
        return
    vals.append(agent_id)
    with db() as conn:
        conn.execute("UPDATE agents SET %s WHERE id=?" % ", ".join(sets), vals)


def touch(agent_id: int):
    try:
        with db() as conn:
            conn.execute("UPDATE agents SET last_active=? WHERE id=?", (_iso(), agent_id))
    except sqlite3.Error:
        pass


def rename_agent(agent_id: int) -> str:
    """给 agent 换个显示名，并同步头像字形。返回新名字。"""
    a = get(agent_id)
    if not a:
        return ""
    glyph = a.get("avatar_glyph") or (a.get("name") or "")[:1]
    return a.get("name") or ""
