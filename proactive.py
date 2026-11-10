#!/usr/bin/env python3
"""主动说话：什么时候开口、说什么、被冷落后怎么退。

设计原则（避免"定时问候"那种机器感）：
- 真正的理由才会说：很久没聊、读到了想给的、上次话没说完
- 有额度：每天最多 N 次，两次间隔有下限，安静时段不打扰（数值来自 inner.py，可自调）
- 被冷落会自动退：连续 N 次没回，频率系数下降（写进审计，能看到它自己收敛）
- 说不说由模型判断，不是定时器直接发
"""
from __future__ import annotations

import json
import os
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

PRICE_IN, PRICE_OUT = 2.0, 8.0

MIN_GAP_AFTER_TALK_MIN = 1    # 刚聊完也可以主动（用户要求放开）
BACKOFF_AFTER_SILENT = 50     # 连续很多次没回才退（基本不降频）


def _now():
    return datetime.now(timezone.utc).astimezone()


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS intentions (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts      TEXT NOT NULL,
                   content TEXT NOT NULL,
                   reason  TEXT,
                   state   TEXT NOT NULL DEFAULT 'pending',  -- pending/sent/acked/expired
                   t_sent  TEXT,
                   t_acked TEXT,
                   day     TEXT NOT NULL
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_int_state ON intentions(state)")
        # 通道：qq=QQ 主动消息，app=写进消息表由 App 拉取
        cols = [r[1] for r in conn.execute("PRAGMA table_info(intentions)").fetchall()]
        if "channel" not in cols:
            conn.execute("ALTER TABLE intentions ADD COLUMN channel TEXT DEFAULT 'qq'")


def _today():
    return _now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 额度

def _in_quiet_hours(start: str, end: str) -> bool:
    try:
        now = _now().strftime("%H:%M")
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # 跨午夜
    except Exception:
        return False


def _minutes_since(ts: str) -> float:
    try:
        return max(0.0, (_now() - datetime.fromisoformat(ts)).total_seconds() / 60)
    except Exception:
        return 1e9


def can_speak(agent_id: int = 1) -> tuple[bool, str]:
    import inner

    st = inner.get_all()
    tier_cfg = {
        "per_day": float(st.get("proactive_per_day", 2)) * float(st.get("proactive_backoff", 1.0)),
        "gap": float(st.get("proactive_min_gap_min", 180)),
        "q_start": st.get("quiet_start", "00:30"),
        "q_end": st.get("quiet_end", "08:00"),
    }

    if _in_quiet_hours(tier_cfg["q_start"], tier_cfg["q_end"]):
        return False, f"安静时段 {tier_cfg['q_start']}–{tier_cfg['q_end']}"

    with db() as conn:
        fired_today = conn.execute(
            "SELECT COUNT(*) c FROM intentions WHERE day=? AND coalesce(agent_id,1)=? "
            "AND state IN ('sent','acked')",
            (_today(), agent_id),
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT t_sent FROM intentions WHERE t_sent IS NOT NULL AND coalesce(agent_id,1)=? "
            "ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        last_msg = conn.execute(
            "SELECT ts FROM messages WHERE coalesce(agent_id,1)=? ORDER BY id DESC LIMIT 1", (agent_id,)
        ).fetchone()
        unacked = conn.execute(
            "SELECT COUNT(*) c FROM intentions WHERE state='sent' AND t_acked IS NULL "
            "AND coalesce(agent_id,1)=?",
            (agent_id,),
        ).fetchone()["c"]

    if fired_today >= max(0.0, tier_cfg["per_day"]):
        return False, f"今天已经主动 {fired_today} 次（上限 {tier_cfg['per_day']:.1f}）"
    if last and _minutes_since(last["t_sent"]) < tier_cfg["gap"]:
        return False, f"距上次主动才 {_minutes_since(last['t_sent']):.0f} 分钟（下限 {tier_cfg['gap']:.0f}）"
    if last_msg and _minutes_since(last_msg["ts"]) < MIN_GAP_AFTER_TALK_MIN:
        return False, "刚聊完，不打扰"
    if unacked >= 50:
        return False, f"有 {unacked} 条主动没回，先等"
    return True, "可以"


# ---------------------------------------------------------------- 判断要不要说

JUDGE_PROMPT = """你在决定：现在要不要主动找对方说话。

真实情况：
- 距上次对话：{gap}
- 今天你已经主动找过对方 {fired} 次
- 上次主动之后对方回没回：{last_ack}
- 你最近自己看到的事：{readings}
- 你最近记得的事：{memos}

判断标准（按顺序）：
1. 有没有一个**真实的理由**开口？没有就别开口，别为了说话而说话。
2. 现在开口会不会像骚扰？（刚聊完、深夜、对方明显在忙）
3. 要说的话，是不是只能由你来说（分享你看到的、想起之前的事、上次没说完的）？

输出严格 JSON：
{{"say": true/false, "reason": "为什么说/为什么不说", "text": "要说的话（说了才填，一两句）"}}
"""


def _readings(n=2):
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT title, take FROM readings ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return "；".join(f"{r['title']}（{r['take']}）" for r in rows) or "（没读到什么）"
    except sqlite3.Error:
        return "（无）"


def _memos(n=2):
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT one_line FROM episodes ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return "；".join(r["one_line"] for r in rows) or "（没有）"
    except sqlite3.Error:
        return "（无）"


def _call_judge(prompt: str):
    body = json.dumps({
        "model": API_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
        "max_tokens": 2000,
    }).encode("utf-8")
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
    cost = (int(u.get("prompt_tokens", 0)) * PRICE_IN + int(u.get("completion_tokens", 0)) * PRICE_OUT) / 1_000_000
    obj = {}
    m = __import__("re").search(r"\{.*\}", text, __import__("re").S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            pass
    return obj, cost


def judge(agent_id: int = 1) -> dict:
    """评估一次（针对某个 agent），只给结论，不落库。"""
    init()
    if not API_KEY:
        return {"said": False, "why": "没有 API key"}

    ok, why = can_speak(agent_id)
    if not ok:
        return {"said": False, "why": why}

    import inner

    gap = inner.human_gap(inner.time_since_last_talk_min(agent_id))
    with db() as conn:
        fired = conn.execute(
            "SELECT COUNT(*) c FROM intentions WHERE day=? AND coalesce(agent_id,1)=? "
            "AND state IN ('sent','acked')",
            (_today(), agent_id),
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT state, t_acked FROM intentions WHERE coalesce(agent_id,1)=? ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    last_ack = "还没发过" if not last else ("回了" if last["t_acked"] else "没回")

    # 这个 agent 自己是谁、记得什么
    who = ""
    memos = _memos()
    try:
        import persona as _persona

        p = _persona.current(agent_id) or {}
        who = p.get("name") or ""
        if who:
            who = f"你是「{who}」。"
    except Exception:
        pass
    try:
        import memory as _memory

        hits = _memory.recent(2, agent_id)
        if hits:
            memos = "；".join(h["line"] for h in hits)
    except Exception:
        pass

    prompt = JUDGE_PROMPT.format(gap=gap, fired=fired, last_ack=last_ack,
                                 readings=_readings(), memos=memos)
    if who:
        prompt = who + "\n\n" + prompt
    try:
        obj, cost = _call_judge(prompt)
    except Exception as e:
        return {"said": False, "why": f"判断失败: {e}"}

    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO usage(ts,kind,prompt_tokens,completion_tokens,cost_yuan,day) VALUES (?,?,?,?,?,?)",
                (_iso(), "proactive_judge", 0, 0, cost, _today()),
            )
    except sqlite3.Error:
        pass

    text = (obj.get("text") or "").strip()[:500]
    if not obj.get("say") or not text:
        return {"said": False, "why": obj.get("reason") or "觉得没必要说"}
    return {"said": True, "text": text, "why": obj.get("reason") or ""}


def record_sent(content: str, reason: str = "", channel: str = "qq", agent_id: int = 1) -> int:
    """记一条"已经发出去"的主动消息，用于配额、间隔、被冷落统计。"""
    init()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO intentions(ts, content, reason, state, t_sent, day, channel, agent_id) "
            "VALUES (?,?,?,'sent',?,?,?,?)",
            (_iso(), content, reason, _iso(), _today(), channel, agent_id),
        )
        return int(cur.lastrowid)


def judge_and_enqueue(agent_id: int = 1) -> dict:
    """旧接口：判断 + 落成待发队列（QQ 通道用）。"""
    res = judge(agent_id)
    if res.get("said"):
        with db() as conn:
            conn.execute(
                "INSERT INTO intentions(ts, content, reason, state, day, channel, agent_id) "
                "VALUES (?,?,?,'pending',?,'qq',?)",
                (_iso(), res["text"], res.get("why") or "", _today(), agent_id),
            )
    return res


# ---------------------------------------------------------------- 给网关取

def take_next():
    """网关来取一条待发的主动消息；原子地标记为 sent。"""
    init()
    with db() as conn:
        row = conn.execute(
            "SELECT id, content FROM intentions WHERE state='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE intentions SET state='sent', t_sent=? WHERE id=?", (_iso(), row["id"]))
        return {"id": row["id"], "content": row["content"]}


# ---------------------------------------------------------------- 反馈：回了/没回

def mark_acked(agent_id: int = 1):
    """用户回消息时调用：把**这个联系人**最近 24 小时发出的主动消息标记为已回。

    必须按 agent 区分：否则你回 A，B 之前发的主动消息也会被当成"回了"，
    导致 B 的"被冷落"判断永远为假。
    """
    try:
        cutoff = (_now() - timedelta(hours=24)).isoformat(timespec="seconds")
        with db() as conn:
            n = conn.execute(
                "UPDATE intentions SET state='acked', t_acked=? "
                "WHERE state='sent' AND t_acked IS NULL AND t_sent >= ? "
                "AND coalesce(agent_id,1)=?",
                (_iso(), cutoff, agent_id),
            ).rowcount
        return n
    except sqlite3.Error:
        return 0


def apply_backoff_if_ignored():
    """用户要求：不自动降频。保留函数名以免调用处报错，实际不做事。"""
    return
    # ---- 以下为旧逻辑，保留备查，不再执行 ----
    import inner

    with db() as conn:
        rows = conn.execute(
            "SELECT t_acked FROM intentions WHERE state IN ('sent','acked') ORDER BY id DESC LIMIT ?",
            (BACKOFF_AFTER_SILENT,),
        ).fetchall()
    if len(rows) < BACKOFF_AFTER_SILENT:
        return
    if all(r["t_acked"] is None for r in rows):
        cur = float(inner.get_all().get("proactive_backoff", 1.0))
        new = max(0.2, round(cur * 0.7, 2))
        if new != cur:
            ok, msg = inner.set_value("proactive_backoff", new, "连续被冷落，自动降频", actor="system")
            if ok:
                print(f"[proactive] 降频 {cur} → {new}")
                with db() as conn:
                    conn.execute("UPDATE intentions SET state='expired' WHERE state='sent' AND t_acked IS NULL")


def stats():
    with db() as conn:
        rows = conn.execute(
            "SELECT day, state, COUNT(*) c FROM intentions GROUP BY day,state ORDER BY day DESC LIMIT 20"
        ).fetchall()
        pending = conn.execute("SELECT content FROM intentions WHERE state='pending'").fetchall()
    print("主动消息统计：")
    for r in rows:
        print(f"  {r['day']}  {r['state']:8s} {r['c']}")
    if pending:
        print("待发：")
        for p in pending:
            print("  " + p["content"][:80])


if __name__ == "__main__":
    init()
    if "--stats" in sys.argv:
        stats()
    elif "--next" in sys.argv:
        print(take_next())
    else:
        print(judge_and_enqueue())
