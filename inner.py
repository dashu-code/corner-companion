#!/usr/bin/env python3
"""内在状态：一组它自己的数值 + 放权机制 + 改动审计。

为什么要有这个：
  没有"自己的数值"，它就没有时间感——不知道上次说话多久前、今天能不能主动找你。
  但数值不能一上来就全给它改（那是让它自己给自己放假）。
  所以分三档放权，像小孩的作息：先家长定，长大一点能微调，再大自己定。

档位（autonomy）：
  1 = 家长控制：所有数值锁定，它只能读
  2 = 有限自调：可以改自己范围内的（有上下限），每次必须写理由
  3 = 自主：可以改到更宽的范围，仍受硬边界约束

解锁：默认按天数自动晋级（7 天 → 2 档，30 天 → 3 档），可用 INNER_AUTONOMY 强制。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "companion.db"

# 每个参数的：默认值、硬边界、第 2 档可动范围、第 3 档可动范围
SPECS = {
    "proactive_per_day": dict(value=6, hard=(0, 20), t2=(0, 12), t3=(0, 20),
                              desc="每天最多主动找你几次"),
    "proactive_min_gap_min": dict(value=45, hard=(5, 720), t2=(15, 240), t3=(5, 720),
                                  desc="两次主动之间至少隔多少分钟"),
    "quiet_start": dict(value="00:00", hard=None, t2=None, t3=None,
                        desc="安静时段开始（这之后不主动打扰）"),
    "quiet_end": dict(value="00:00", hard=None, t2=None, t3=None,
                      desc="安静时段结束"),
    "judge_interval_min": dict(value=15, hard=(5, 120), t2=(10, 60), t3=(5, 120),
                               desc="多久评估一次要不要找你"),
    "proactive_backoff": dict(value=1.0, hard=(0.2, 2.0), t2=(0.5, 1.5), t3=(0.2, 2.0),
                              desc="主动频率系数（被冷落后自动降低）"),
}

LOCKED_T1 = set()  # 1 档全锁，无需单列


def _now():
    return datetime.now(timezone.utc).astimezone()


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS inner_state (
                   key     TEXT PRIMARY KEY,
                   value   TEXT NOT NULL,
                   updated TEXT NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS inner_audit (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts      TEXT NOT NULL,
                   key     TEXT NOT NULL,
                   old     TEXT,
                   new     TEXT,
                   reason  TEXT,
                   tier    INTEGER,
                   ok      INTEGER
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS meta (
                   key TEXT PRIMARY KEY,
                   value TEXT NOT NULL
               )"""
        )
        # 初始化默认值（只补缺失的）
        for k, spec in SPECS.items():
            row = conn.execute("SELECT value FROM inner_state WHERE key=?", (k,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO inner_state(key, value, updated) VALUES (?,?,?)",
                    (k, json.dumps(spec["value"], ensure_ascii=False), _iso()),
                )
        first = conn.execute("SELECT value FROM meta WHERE key='born_at'").fetchone()
        if not first:
            conn.execute("INSERT INTO meta(key, value) VALUES ('born_at', ?)", (_iso(),))


def _get(conn, key, default=None):
    row = conn.execute("SELECT value FROM inner_state WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def get_all():
    with db() as conn:
        return {k: _get(conn, k, SPECS[k]["value"]) for k in SPECS}


def born_days() -> float:
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='born_at'").fetchone()
    if not row:
        return 0.0
    try:
        born = datetime.fromisoformat(row["value"])
        return max(0.0, (_now() - born).total_seconds() / 86400)
    except Exception:
        return 0.0


def autonomy() -> int:
    """1=家长控制 2=有限自调 3=自主。"""
    forced = os.environ.get("INNER_AUTONOMY", "").strip()
    if forced.isdigit():
        return max(1, min(3, int(forced)))
    days = born_days()
    if days >= 30:
        return 3
    if days >= 7:
        return 2
    return 1


def allowed_range(key: str, tier: int | None = None):
    spec = SPECS.get(key)
    if not spec:
        return None
    tier = tier if tier is not None else autonomy()
    if tier == 1:
        return None
    return spec["t2"] if tier == 2 else spec["t3"]


def is_time_like(key: str) -> bool:
    return key in ("quiet_start", "quiet_end")


def _valid_time(v) -> bool:
    return isinstance(v, str) and bool(re.fullmatch(r"[0-2]\d:[0-5]\d", v))


def set_value(key: str, value, reason: str = "", tier: int | None = None, actor: str = "agent"):
    """改一个数值。返回 (ok, message)。越权/越界一律拒绝并记账。"""
    tier = tier if tier is not None else autonomy()
    spec = SPECS.get(key)
    if not spec:
        return False, f"没有这个参数：{key}"

    if actor == "agent" and tier == 1:
        _audit(key, None, value, reason, tier, 0)
        return False, f"当前 1 档（家长控制），{key} 不能改"

    if is_time_like(key):
        if not _valid_time(value):
            _audit(key, None, value, reason, tier, 0)
            return False, f"{key} 需要 HH:MM 格式"
    else:
        try:
            value = float(value)
        except Exception:
            return False, f"{key} 需要数字"
        rng = allowed_range(key, tier)
        if rng is None:
            return False, f"{key} 在当前档位不可改"
        lo, hi = rng
        if not (lo <= value <= hi):
            _audit(key, None, value, reason, tier, 0)
            return False, f"{key} 必须在 {lo}~{hi} 之间（当前档位 {tier}）"
        if float(value).is_integer():
            value = int(value)

    with db() as conn:
        old = _get(conn, key)
        conn.execute(
            "UPDATE inner_state SET value=?, updated=? WHERE key=?",
            (json.dumps(value, ensure_ascii=False), _iso(), key),
        )
    _audit(key, old, value, reason, tier, 1)
    return True, f"{key}: {old} → {value}"


def _audit(key, old, new, reason, tier, ok):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO inner_audit(ts,key,old,new,reason,tier,ok) VALUES (?,?,?,?,?,?,?)",
                (_iso(), key, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False), reason, tier, ok),
            )
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- 给提示词用

def prompt_block() -> str:
    st = get_all()
    tier = autonomy()
    tier_name = {1: "家长控制（你不能改数值）", 2: "有限自调（可小范围改，必须写理由）", 3: "自主（可较大范围改）"}[tier]
    lines = [
        "你的内在状态（这些是真实的数值，不是设定）：",
        f"- 每天最多主动找对方 {st.get('proactive_per_day')} 次（频率系数 {st.get('proactive_backoff')}）",
        f"- 两次主动至少隔 {st.get('proactive_min_gap_min')} 分钟",
        f"- 安静时段 {st.get('quiet_start')}–{st.get('quiet_end')}（这期间不主动打扰）",
        f"- 放权档位：{tier} 档 · {tier_name}",
        f"- 你已经存在了 {born_days():.0f} 天",
    ]
    if tier >= 2:
        lines.append(
            "想调整时，在回复最后单独一行写：[调参] 参数=数值 理由=一句话"
            "（每次最多一条，越权或越界会被拒绝并记账）"
        )
    return "\n".join(lines)


def parse_directives(text: str):
    """从回复里抠出 [调参] 指令：返回 (剩余文本, [(key,value,reason)])。"""
    out = []
    keep = []
    for line in text.splitlines():
        m = re.match(r"\s*\[调参\]\s*([a-z_]+)\s*=\s*([^ ]+)\s*(?:理由[=:]\s*(.*))?$", line)
        if m:
            out.append((m.group(1), m.group(2), (m.group(3) or "").strip()))
        else:
            keep.append(line)
    return "\n".join(keep).strip(), out[:1]  # 一次最多一条


# ---------------------------------------------------------------- 时间感

def time_since_last_talk_min(agent_id: int = 1) -> float:
    """距离上次对话多少分钟（按 agent 分开算）。"""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT ts FROM messages WHERE coalesce(agent_id,1)=? ORDER BY id DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
    except sqlite3.Error:
        return 0.0
    if not row:
        return 0.0
    try:
        return max(0.0, (_now() - datetime.fromisoformat(row["ts"])).total_seconds() / 60)
    except Exception:
        return 0.0


def human_gap(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)} 分钟"
    if minutes < 1440:
        return f"{minutes / 60:.1f} 小时"
    return f"{minutes / 1440:.1f} 天"


def audit_tail(n=8):
    with db() as conn:
        rows = conn.execute(
            "SELECT ts,key,old,new,reason,tier,ok FROM inner_audit ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    for r in rows:
        flag = "✅" if r["ok"] else "❌"
        print(f"{flag} {r['ts'][5:16]} {r['key']}: {r['old']} → {r['new']}  理由={r['reason']}  档={r['tier']}")


if __name__ == "__main__":
    import sys

    init()
    if "--audit" in sys.argv:
        audit_tail(20)
    else:
        print(f"档位 {autonomy()}（存在 {born_days():.1f} 天）")
        for k, v in get_all().items():
            print(f"  {k:24s} = {v!r:>8}   {SPECS[k]['desc']}")
        print(f"  距上次说话：{human_gap(time_since_last_talk_min())}")
