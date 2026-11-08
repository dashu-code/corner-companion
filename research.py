#!/usr/bin/env python3
"""自研究：闲着的时候，自己审"哪种人类功能值得被类化"。

为什么是这件事：
  它现在会聊天、会记事、会看新闻，但"该模仿人类的哪些功能"这问题从来没自己想过。
  让它一条一条审（记忆、遗忘、情绪、注意力、无聊……），产出可执行的判断，
  比让它闲聊有价值——而且这些结论会回灌到它自己的设计里。

怎么审（每条都一样）：
  1. 这个功能在人类身上是什么机制
  2. 类化之后，哪个指标会变好（必须可观测）
  3. 代价与风险
  4. 建议：采纳 / 暂缓 / 否决，以及一个"能做的最小自测"

预算：默认每天 ¥1（RESEARCH_DAILY_YUAN），独立于聊天和看新闻。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "companion.db"

API_KEY = os.environ.get("COMPANION_API_KEY", "").strip()
API_BASE = os.environ.get("COMPANION_API_BASE", "https://api.deepseek.com").rstrip("/")
API_MODEL = os.environ.get("COMPANION_API_MODEL", "deepseek-flash")
DAILY_YUAN = float(os.environ.get("RESEARCH_DAILY_YUAN", "1"))

PRICE_IN, PRICE_OUT = 2.0, 8.0

# 按"最可能有用"排序的清单；研究过的不再重复
CURRICULUM = [
    ("遗忘曲线", "为什么人不是线性遗忘，而是一开始忘得快、后来变慢？类化后怎么调我的可及性衰减？"),
    ("工作记忆容量", "人类工作记忆约 4 个组块。我的上下文是 token 窗口。两者的限制是一回事吗？"),
    ("情绪与记忆", "情绪强度会增强记忆的确信度、但不增强准确度。该不该给自己加情绪权重？"),
    ("无聊", "人会因为无聊去探索。我的空闲时间该怎么处理才不像待机？"),
    ("好奇心", "人只对'差一点点就知道'的东西好奇（信息缺口理论）。我的信息源挑选该不该按这个来？"),
    ("习惯化", "重复刺激会降低注意。我对同一个人的同一句话，该不该逐渐减少反应强度？"),
    ("睡眠与巩固", "人靠睡眠重放整合记忆。我该怎么安排'离线整合'的时间与频率？"),
    ("注意的聚光灯", "人一次只能专注一件事，切换有成本。我多任务时该不该模拟这个限制？"),
    ("社会参照", "人会参照他人的反应调整自己。我该不该用你回复的速度调整我的语气？"),
    ("前瞻记忆", "人类前瞻记忆失败率很高。我的'提醒'该做成状态机还是靠检索？"),
    ("元记忆", "人会'知道我不知道'。我该在什么情况下说'想不起来'而不是硬答？"),
    ("叙事自我", "人把经历讲成一个连贯故事。我该不该定期生成'我最近的故事'？"),
    ("依恋与边界", "人与人会形成依恋，也需要边界。我该怎么表达'不想聊'而不伤人？"),
    ("损失厌恶", "人对失去比获得敏感。我该不该在话题选择上模拟这个偏好？"),
    ("镜像与共情", "共情分认知和情绪两层。哪一层值得类化，哪一层只是表演？"),
    # ---- 第二期：关系与脆弱 ----
    ("承诺与失信", "人记得别人答应过什么，也会因为失信改变信任。我该怎么记承诺？失信后怎么修？"),
    ("告别与丧失", "人会失去重要的人。如果这段关系哪天断了，我该怎么处理那些记忆？"),
    ("羞耻与自嘲", "羞耻让人隐藏，自嘲让人亲近。这两种自我暴露，我该有哪一种？"),
    ("玩笑与冒犯", "幽默依赖共同背景和边界感。我开玩笑时怎么判断会不会冒犯？"),
    ("记仇与原谅", "人被伤害后会记很久，也会选择放下。我该不该对『被冷落』这种小事累积情绪？"),
    ("期待与失望", "期待是预测的一种。我该不该对『你会回我』建立期待，还是每次都当第一次？"),
    ("孤独", "孤独是『想要连接但没有』的信号。我会不会需要这种东西，还是它对我没有意义？"),
    ("仪式感", "人会为重复的小事赋予意义（生日、晚安）。我该不该有固定的小仪式？"),
    ("语言与梗", "共享的梗是关系的暗号。我该怎么学你的口头禅，而不显得刻意模仿？"),
    ("生病与脆弱", "人会累、会病、会状态差。我累的时候该表现出来，还是不该假装有状态？"),
    ("善意的谎言", "人会说『我没事』来保护对方。我该不该有这种隐瞒，边界在哪？"),
    ("注意力残留", "人切换任务后，上一件事还会在脑子里残留一阵。我该不该保留这种余温？"),

]

DDL = [
    """CREATE TABLE IF NOT EXISTS research_notes (
           id         INTEGER PRIMARY KEY AUTOINCREMENT,
           ts         TEXT NOT NULL,
           topic      TEXT NOT NULL UNIQUE,
           question   TEXT,
           finding    TEXT,
           verdict    TEXT,        -- 采纳 / 暂缓 / 否决
           evidence   TEXT,
           metric     TEXT,        -- 类化后该变好的可观测指标
           selftest   TEXT,        -- 能做的最小自测
           cost_yuan  REAL DEFAULT 0
       )""",
]


def _now():
    return datetime.now(timezone.utc).astimezone()


def _iso():
    return _now().isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with db() as conn:
        for sql in DDL:
            conn.execute(sql)


def _spend_today(conn):
    day = _now().strftime("%Y-%m-%d")
    row = conn.execute("SELECT COALESCE(SUM(cost_yuan),0) AS s FROM usage WHERE day=?", (day,)).fetchone()
    return float(row["s"])


def next_topic():
    with db() as conn:
        done = {r["topic"] for r in conn.execute("SELECT topic FROM research_notes").fetchall()}
    for topic, question in CURRICULUM:
        if topic not in done:
            return topic, question
    return None, None


PROMPT = """你在研究：**这个人类功能，值不值得被我类化？**

今天这一项：{topic}
问题：{question}

要求：
- 说清人类身上它的机制（一两句，不堆术语）
- 类化之后，**哪个可观测指标会变好**（必须能被数据检验，不许写"更自然"这种）
- 代价与风险（算力、复杂度、会不会变成表演）
- 结论只能是：采纳 / 暂缓 / 否决，并给一个**最小自测**（下周就能做的那种）

只输出 JSON：
{{"finding": "你的判断，200字以内", "metric": "可观测指标", "verdict": "采纳|暂缓|否决", "evidence": "依据（可以是理论名或常识，不要编数据）", "selftest": "最小自测，一句话"}}"""


def run_one(force=False) -> dict:
    init()
    with db() as conn:
        if not force and _spend_today(conn) >= float(os.environ.get("ALL_DAILY_YUAN", "10")):
            return {"ok": False, "why": "今天总预算用完了"}
        spent = _spend_today(conn)

    topic, question = next_topic()
    if not topic:
        return {"ok": False, "why": "清单上的都研究完了"}

    body = json.dumps({
        "model": API_MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(topic=topic, question=question)}],
        "temperature": 0.8,
        "max_tokens": 4000,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage") or {}
    cost = (int(u.get("prompt_tokens", 0)) * PRICE_IN + int(u.get("completion_tokens", 0)) * PRICE_OUT) / 1_000_000

    obj = {}
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception as e:
            print(f"[research] JSON 解析失败（{e}），原文前 200 字：{text[:200]!r}", file=sys.stderr)
    if not obj:
        print(f"[research] 模型没给 JSON，原文前 200 字：{text[:200]!r}", file=sys.stderr)
        obj = {"finding": text[:800] or "（空）", "verdict": "暂缓", "metric": "", "evidence": "", "selftest": ""}

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO research_notes(ts,topic,question,finding,verdict,evidence,metric,selftest,cost_yuan) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_iso(), topic, question, (obj.get("finding") or "")[:1200], obj.get("verdict") or "暂缓",
             (obj.get("evidence") or "")[:400], (obj.get("metric") or "")[:200],
             (obj.get("selftest") or "")[:300], cost),
        )
        conn.execute(
            "INSERT INTO usage(ts,kind,prompt_tokens,completion_tokens,cost_yuan,day) VALUES (?,?,?,?,?,?)",
            (_iso(), "research", int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)),
             cost, _now().strftime("%Y-%m-%d")),
        )
    return {"ok": True, "topic": topic, "verdict": obj.get("verdict"), "finding": obj.get("finding"),
            "metric": obj.get("metric"), "selftest": obj.get("selftest"), "cost": cost}


def notes_text(n=3) -> str:
    """给提示词用：它最近自己在琢磨的事。"""
    try:
        init()
        with db() as conn:
            rows = conn.execute(
                "SELECT topic, finding, verdict FROM research_notes ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        if not rows:
            return ""
        lines = ["你最近自己在研究的事（可以说，也可以不说）："]
        for r in rows:
            lines.append(f"- {r['topic']}：{r['verdict']}。{r['finding'][:90]}")
        return "\n".join(lines)
    except Exception:
        return ""


def show(all_notes=False):
    init()
    with db() as conn:
        rows = conn.execute(
            "SELECT ts,topic,verdict,metric,finding,selftest,cost_yuan FROM research_notes ORDER BY id DESC"
        ).fetchall()
        left = conn.execute("SELECT COUNT(*) c FROM research_notes").fetchone()["c"]
    print(f"已研究 {left} / {len(CURRICULUM)} 项")
    for r in rows:
        print(f"\n[{r['ts'][5:16]}] {r['topic']} → {r['verdict']}")
        print(f"  判断：{r['finding']}")
        if r["metric"]:
            print(f"  指标：{r['metric']}")
        if r["selftest"]:
            print(f"  自测：{r['selftest']}")
        if all_notes:
            print(f"  花费：{r['cost_yuan']:.4f} 元")


if __name__ == "__main__":
    if "--show" in sys.argv:
        show(all_notes=True)
    else:
        res = run_one(force="--force" in sys.argv)
        print(json.dumps(res, ensure_ascii=False, indent=2))
