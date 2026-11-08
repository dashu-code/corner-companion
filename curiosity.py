#!/usr/bin/env python3
"""让它自己去看点东西（只读、纯文本、每天花不超过 ¥5）。

它是干什么的：给「它」一份自己的信息源。不然它只知道你刚说的话，
对话里没有信息差，就只剩下复述和反问。

约束（写在代码里，不靠自觉）：
- **只读**：只发 GET；没有发帖/评论/点赞的代码路径
- **只文本**：不下载图片视频
- **每天上限 ¥5**：记账在 usage 表，超了跳过
- **域名白名单**：不在名单里的一律拒绝

用法：
    python curiosity.py          # 跑一次
    python curiosity.py --list   # 看读到了什么
    python curiosity.py --force  # 忽略当日预算（调试用）
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "companion.db"

API_KEY = os.environ.get("COMPANION_API_KEY", "").strip()
API_BASE = os.environ.get("COMPANION_API_BASE", "https://api.deepseek.com").rstrip("/")
API_MODEL = os.environ.get("COMPANION_API_MODEL", "deepseek-flash")

DAILY_YUAN = float(os.environ.get("CURIOSITY_DAILY_YUAN", "5"))
MAX_ITEMS = int(os.environ.get("CURIOSITY_MAX_ITEMS", "2"))

PRICE_IN = 2.0   # 元 / 百万 tokens（保守取值）
PRICE_OUT = 8.0

UA = "Mozilla/5.0 (compatible; companion-curiosity/1.0)"

ALLOW_HOSTS = {
    "hacker-news.firebaseio.com",
    "www.v2ex.com",
    "sspai.com",
    "www.solidot.org",
}


def _now():
    return datetime.now(timezone.utc).astimezone()


def _iso():
    return _now().isoformat(timespec="seconds")


def today():
    return _now().strftime("%Y-%m-%d")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts      TEXT NOT NULL,
                   source  TEXT NOT NULL,
                   title   TEXT NOT NULL,
                   url     TEXT,
                   excerpt TEXT,
                   take    TEXT,
                   day     TEXT NOT NULL
               )"""
        )
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


def get_json(url, timeout=12):
    host = urllib.parse.urlparse(url).hostname
    if host not in ALLOW_HOSTS:
        raise RuntimeError("域名不在白名单：" + str(host))
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_text(url, timeout=12):
    host = urllib.parse.urlparse(url).hostname
    if host not in ALLOW_HOSTS:
        raise RuntimeError("域名不在白名单：" + str(host))
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 来源

def fetch_hn(limit=8):
    out = []
    try:
        ids = get_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:limit]
        for i in ids:
            try:
                it = get_json("https://hacker-news.firebaseio.com/v0/item/%d.json" % i, timeout=8)
            except Exception:
                continue
            if not it or it.get("type") not in ("story", "ask"):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "source": "Hacker News",
                "title": title,
                "url": it.get("url") or ("https://news.ycombinator.com/item?id=%d" % i),
                "excerpt": (it.get("text") or "")[:300],
            })
    except Exception as e:
        print("[hn] 跳过:", e)
    return out


def fetch_rss(url, source, limit=6):
    out = []
    try:
        text = get_text(url)
        # 有些 RSS 用了 &nbsp; / &ldquo; 这类未定义实体，XML 解析器会直接报错 → 先转义
        text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"<(/?)(\w+):", r"<\1\2_", text)  # 开标签和闭标签都要改
        root = ET.fromstring(text)
        for it in root.iter("item"):
            if len(out) >= limit:
                break
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()
            if title:
                out.append({"source": source, "title": title, "url": link, "excerpt": desc[:300]})
        if not out:
            for it in root.iter("entry"):
                if len(out) >= limit:
                    break
                title = (it.findtext("title") or "").strip()
                link = ""
                for ln in it.findall("link"):
                    if ln.get("href"):
                        link = ln.get("href")
                        break
                summary = re.sub(r"<[^>]+>", "", it.findtext("summary") or "").strip()
                if title:
                    out.append({"source": source, "title": title, "url": link, "excerpt": summary[:300]})
    except Exception as e:
        print("[rss:%s] 跳过: %s" % (source, e))
    return out


def fetch_v2ex(limit=8):
    out = []
    try:
        for t in get_json("https://www.v2ex.com/api/topics/hot.json")[:limit]:
            title = (t.get("title") or "").strip()
            if title:
                out.append({
                    "source": "V2EX",
                    "title": title,
                    "url": t.get("url") or "",
                    "excerpt": (t.get("content") or "")[:300],
                })
    except Exception as e:
        print("[v2ex] 跳过:", e)
    return out


# ---------------------------------------------------------------- 预算

def spend_today(conn):
    row = conn.execute("SELECT COALESCE(SUM(cost_yuan),0) AS s FROM usage WHERE day=?", (today(),)).fetchone()
    return float(row["s"])


def add_cost(conn, kind, cost, pt=0, ct=0):
    conn.execute(
        "INSERT INTO usage(ts, kind, prompt_tokens, completion_tokens, cost_yuan, day) VALUES (?,?,?,?,?,?)",
        (_iso(), kind, pt, ct, cost, today()),
    )


# ---------------------------------------------------------------- 挑与评

def pick_and_comment(items):
    if not API_KEY or not items:
        return [], 0.0
    numbered = "\n".join("%d. [%s] %s" % (i + 1, it["source"], it["title"]) for i, it in enumerate(items))
    prompt = (
        "下面是今天的一些标题。挑 %d 条你**真的感兴趣**的（不是最有用的、不是最热门的），"
        "只挑你愿意跟人聊起来的。\n\n%s\n\n"
        "输出严格 JSON 数组，不要别的字：\n"
        '[{"i": 序号, "take": "用你自己的话，一句话说你怎么看；不要总结新闻，不要客套"}]\n'
        "如果一条都不想挑，输出 []。" % (MAX_ITEMS, numbered)
    )
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
    usage = data.get("usage") or {}
    pt = int(usage.get("prompt_tokens", 0))
    ct = int(usage.get("completion_tokens", 0))
    cost = (pt * PRICE_IN + ct * PRICE_OUT) / 1_000_000.0

    picked = []
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            for x in json.loads(m.group(0)):
                idx = int(x.get("i", 0)) - 1
                take = (x.get("take") or "").strip()
                if 0 <= idx < len(items) and take:
                    picked.append((items[idx], take))
        except Exception:
            pass
    return picked[:MAX_ITEMS], cost


# ---------------------------------------------------------------- 主流程

def run_once(force=False):
    ensure_tables()
    with db() as conn:
        spent = spend_today(conn)
        if not force and spent >= DAILY_YUAN:
            print("[预算] 今天已花 %.3f 元，达到上限 %.2f，跳过" % (spent, DAILY_YUAN))
            return 0

        items = (
            fetch_hn()
            + fetch_rss("https://www.solidot.org/index.rss", "Solidot")
            + fetch_rss("https://sspai.com/feed", "少数派")
            + fetch_v2ex()
        )
        if not items:
            print("[来源] 一个都没抓到")
            return 0

        week_ago = (_now() - timedelta(days=7)).isoformat(timespec="seconds")
        seen = {r["title"] for r in conn.execute("SELECT title FROM readings WHERE ts > ?", (week_ago,))}
        items = [x for x in items if x["title"] not in seen]
        if not items:
            print("[来源] 全是读过的")
            return 0

        picked, cost = pick_and_comment(items)
        if cost:
            add_cost(conn, "curiosity_pick", cost)

        for it, take in picked:
            conn.execute(
                "INSERT INTO readings(ts, source, title, url, excerpt, take, day) VALUES (?,?,?,?,?,?,?)",
                (_iso(), it["source"], it["title"], it["url"], it["excerpt"][:300], take, today()),
            )
            print("[读] [%s] %s\n     看法：%s" % (it["source"], it["title"], take))
        print("[花费] 本次 %.4f 元；今日累计 %.4f 元" % (cost, spend_today(conn)))
        return len(picked)


def list_readings(limit=10):
    ensure_tables()
    with db() as conn:
        rows = conn.execute("SELECT ts, source, title, take FROM readings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    for r in rows:
        print("%s  [%s] %s\n          %s" % (r["ts"][5:16], r["source"], r["title"], r["take"]))


def recent_readings_text(n=3, days=7):
    """给 server 拼上下文：它最近自己看到的东西。"""
    try:
        ensure_tables()
        since = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
        with db() as conn:
            rows = conn.execute(
                "SELECT source, title, take FROM readings WHERE ts > ? ORDER BY id DESC LIMIT ?", (since, n)
            ).fetchall()
        if not rows:
            return ""
        lines = ["你最近自己看到的东西（可以说，也可以不说）："]
        for r in rows:
            lines.append("- [%s] %s —— 我的看法：%s" % (r["source"], r["title"], r["take"]))
        return "\n".join(lines)
    except Exception:
        return ""


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_readings()
    else:
        run_once(force="--force" in sys.argv)
