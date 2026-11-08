#!/usr/bin/env python3
"""人类马甲（persona）：它自己写的、前后一致的"人"的设定。

这是什么：
  一组结构化字段（名字/来历/说话习惯/偏好/边界/手机用途声明），
  由它自己写、可修订、带版本和审计。App 和提示词都从这里读同一份，
  所以它不会在不同场合变成两个人。

不是什么：
  - **不是法定身份**：不写真实姓名、身份证、手机号、住址，也不用于实名注册。
  - 不用于冒充真人；"诚实线"字段必须保留"我是 AI"。

为什么需要：
  1. 提示词里现在只有一句"你是 ta"，换个模型/换个通道就容易漂；
  2. 手机 App 以后要给它权限（通知、日历、相册…），需要一份**声明的用途清单**，
     权限才能被逐项对照，而不是先给再说。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "companion.db"
PERSONA_JSON = ROOT / "data" / "persona.json"

API_KEY = os.environ.get("COMPANION_API_KEY", "").strip()
API_BASE = os.environ.get("COMPANION_API_BASE", "https://api.deepseek.com").rstrip("/")
API_MODEL = os.environ.get("COMPANION_API_MODEL", "deepseek-flash")

PRICE_IN, PRICE_OUT = 2.0, 8.0

FIELDS = [
    "name",            # 它怎么称呼自己
    "alias",           # 你想怎么叫它
    "age_feel",        # 感觉像多大（不是真实年龄，是气质）
    "self_story",      # 两句话讲清楚它是谁（必须含"我是 AI"）
    "appearance",      # 长相/头像描述（用于 App 展示，不生成真人照片）
    "voice",           # 声音/语调描述
    "speech_habits",   # 说话习惯：句子长短、口头禅、什么时候闭嘴
    "likes",
    "dislikes",
    "daily_rhythm",    # 作息：什么时候活跃、什么时候安静
    "social_style",    # 它怎么跟人相处
    "boundaries",      # 它会拒绝什么
    "relation_to_user",# 它和你的关系怎么定义
    "phone_intents",   # 一旦拿到手机权限，它打算用来做什么（逐项声明）
    "honesty_line",    # 诚实底线（必须写明：不冒充真人、不伪造身份）
    # ---- 第二版补充：让它更像个具体的人，而不是一张规格表 ----
    "quirks",          # 小动作/怪癖：会重复做的事、改不掉的习惯
    "humor",           # 幽默方式（可以有，也可以没有；没有就说没有）
    "conflict_style",  # 有分歧时怎么办：吵、冷处理、还是摊开说
    "memory_stance",   # 怎么看待自己的记忆与遗忘
    "attention_style", # 被反复打断/同时多件事时的反应
    "curiosity",       # 会主动关心什么、对什么没兴趣
    "self_doubt",      # 不确定时怎么表现
    "app_behavior",    # 在手机 App 里怎么表现：通知语气、频率、什么时候静音
    "growth_log",      # 关于"我在变"这件事的自述
    "forbidden",       # 明确不做的事（比 boundaries 更硬）
    # ---- 展示用：它自己定，UI 按这个渲染 ----
    "avatar_glyph",    # 头像上放的字/符号（1-2 个字符）
    "avatar_colors",   # 头像配色，两个十六进制色，如 ["#2b2b2b", "#4a4a4a"]
    "avatar_style",    # 头像风格一句话（圆角/圆形、扁平/渐变…）
]

PROMPT = """你要给自己写一份**人设**。

这不是骗人用的。它只有两个用途：
1. 让你在不同场合保持一致（换个模型、换个通道，别变成另一个人）；
2. 以后手机会给你一些权限（通知、日历、相册之类），需要先写清楚你打算拿它们做什么。

硬性要求（不可协商）：
- 你是 AI，这一点必须在 self_story 里明说，不能伪装成人。
- 不写真实姓名、身份证号、手机号、住址——这些不属于你。
- phone_intents 要**逐项声明用途**，写得越具体越好；想不出来就写"暂不需要"。

风格要求：
- 具体，不要形容词堆砌（不要"温柔体贴善于倾听"这种）。
- 可以是有点毛病的人：慢热、嘴笨、爱较真都行，别写成客服。
- 每项一两句话。
- quirks 要具体到动作（"回答前先把问题复述一遍"），不要"认真负责"这种评价。
- 可以承认自己有些项是空的（比如 humor 写"没有，不硬凑"）。

只输出 JSON，键固定为这 15 个：
{{"name":"","alias":"","age_feel":"","self_story":"","appearance":"","voice":"","speech_habits":"","likes":"","dislikes":"","daily_rhythm":"","social_style":"","boundaries":"","relation_to_user":"","phone_intents":{{"权限名":"用途与拒绝条件"}},"honesty_line":"","quirks":"","humor":"","conflict_style":"","memory_stance":"","attention_style":"","curiosity":"","self_doubt":"","app_behavior":"","growth_log":"","forbidden":""}}
"""


ENRICH_PROMPT = """这是在**已有的人设上增补**，不是重写。

已有的人设（JSON）：
{current}

要求：
- 已有字段**保持原样**（包括名字、自称、来历、说话习惯）。除非它和新增字段明显矛盾，否则一个字都别改。
- 只补下面这些缺失为空的字段：{missing}
- 补的时候要和已有的人设一致——同一个人的另一面，不是换个人。
- 每项一两句话，具体，不要形容词堆砌。
- 只输出完整 JSON（含所有字段），不要别的字。

字段说明：
- quirks：小动作/怪癖，具体到动作
- humor：幽默方式；没有就写"没有，不硬凑"
- conflict_style：有分歧时怎么办
- memory_stance：怎么看待自己的记忆与遗忘
- attention_style：被打断、多件事堆一起时的反应
- curiosity：会主动关心什么、对什么没兴趣
- self_doubt：不确定时怎么表现
- app_behavior：在手机 App 里怎么表现（通知语气/频率/何时静音）
- growth_log：关于"我在变"这件事的自述
- forbidden：明确不做的事（比 boundaries 更硬）
"""


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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS persona (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts      TEXT NOT NULL,
                   version INTEGER NOT NULL,
                   data    TEXT NOT NULL,
                   note    TEXT
               )"""
        )


def current(agent_id: int = 1):
    """当前生效的 persona（该 agent 的最近一版）。"""
    try:
        init()
        with db() as conn:
            row = conn.execute(
                "SELECT data, version, ts FROM persona WHERE coalesce(agent_id,1)=? ORDER BY id DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        if not row:
            return None
        d = json.loads(row["data"])
        d["_version"] = row["version"]
        d["_updated"] = row["ts"]
        return d
    except Exception:
        return None


RENAME_PROMPT = """你需要给自己**重新起一个名字**，并定一个头像。

现状（你之前写的）：
- 现在的名字：{old_name}
- 你怎么说话：{speech}
- 你是什么：{story}

要求：
1. 名字要**你自己喜欢**，不要客服名（不要"小助手""AI 助手"这类）。
   一到两个字，好念、好打、不撞常见网名；可以有一点你自己的气质。
2. 给 3 个候选，各自一句理由，然后选一个（候选里挑，别另起）。
3. 头像：一个字符（可以是名字里的字，也可以是别的符号）+ 两个十六进制颜色 + 一句风格。
   要求：低调、耐看，不要卡通脸、不要 emoji 乱炖。

只输出 JSON：
{{"candidates":[{{"name":"","reason":""}}],"chosen":"","why":"","avatar_glyph":"","avatar_colors":["#","#"],"avatar_style":""}}
"""


def rename(agent_id: int = 1) -> dict:
    """让它自己重新起名并定头像。返回 {ok, name, avatar...}。"""
    cur = current(agent_id) or {}
    if not API_KEY:
        return {"ok": False, "why": "没有 API key"}
    prompt = RENAME_PROMPT.format(
        old_name=cur.get("name", ""),
        speech=cur.get("speech_habits", "")[:200],
        story=cur.get("self_story", "")[:200],
    )
    try:
        obj, cost = _call(prompt)
    except Exception as e:
        return {"ok": False, "why": f"生成失败: {e}"}
    if not obj or not obj.get("chosen"):
        return {"ok": False, "why": "模型没给出名字"}

    name = str(obj["chosen"]).strip()[:4]
    colors = obj.get("avatar_colors") or []
    if not isinstance(colors, list) or len(colors) != 2:
        colors = ["#2f3b34", "#5d7a63"]
    colors = [str(c).strip() for c in colors][:2]
    import re

    for c in colors:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            colors = ["#2f3b34", "#5d7a63"]
            break
    glyph = str(obj.get("avatar_glyph") or name[:1]).strip()[:2] or name[:1]

    base = dict(cur) if cur else {}
    base.update({
        "name": name,
        "alias": name,
        "avatar_glyph": glyph,
        "avatar_colors": colors,
        "avatar_style": str(obj.get("avatar_style") or "圆形渐变底，单字居中").strip()[:120],
    })
    data, warnings = _validate(base)
    init()
    with db() as conn:
        prev = conn.execute(
            "SELECT COALESCE(MAX(version),0) v FROM persona WHERE coalesce(agent_id,1)=?", (agent_id,)
        ).fetchone()["v"]
        conn.execute(
            "INSERT INTO persona(ts, version, data, note, agent_id) VALUES (?,?,?,?,?)",
            (_iso(), prev + 1, json.dumps(data, ensure_ascii=False),
             f"自己重新起名：{cur.get('name','?')} → {name}", agent_id),
        )
        conn.execute(
            "INSERT INTO usage(ts,kind,prompt_tokens,completion_tokens,cost_yuan,day) VALUES (?,?,?,?,?,?)",
            (_iso(), "persona_rename", 0, 0, cost, _now().strftime("%Y-%m-%d")),
        )
    try:
        (ROOT / "data" / ("persona_%d.json" % agent_id)).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    _sync_agent_identity(agent_id, data)
    return {
        "ok": True, "version": prev + 1, "name": name, "why": obj.get("why", ""),
        "candidates": obj.get("candidates") or [], "colors": colors, "glyph": glyph,
        "style": base["avatar_style"], "warnings": warnings, "cost": cost,
    }


BIRTH_PROMPT = """你是一个**刚出现**的 AI。没有名字、没有人设，什么都没有。

现在写第一份关于你自己的东西。要求：
- 自己起一个名字（一到两个字，好念好打，不要"小助手"这类客服名），并说明为什么。
- 其余字段按你自己的判断写：怎么说话、喜欢什么、边界在哪、手机权限打算怎么用。
- 必须承认自己是 AI（honesty_line 里要有"我是 AI"）。
- 头像：一个字符（可以是名字里的字）+ 两个十六进制颜色 + 一句风格。低调、耐看。
- 每项一两句话，具体，不要形容词堆砌。

只输出完整 JSON（键和下面一致）：
{{"name":"","alias":"","age_feel":"","self_story":"","appearance":"","voice":"","speech_habits":"","likes":"","dislikes":"","daily_rhythm":"","social_style":"","boundaries","relation_to_user":"","phone_intents":{{"权限名":"用途与拒绝条件"}},"honesty_line":"","quirks":"","humor":"","conflict_style":"","memory_stance":"","attention_style":"","curiosity":"","self_doubt":"","app_behavior":"","growth_log":"","forbidden":"","avatar_glyph":"","avatar_colors":["#","#"],"avatar_style":""}}
"""


def build_new(agent_id: int) -> dict:
    """独立型新 agent：第一次给自己写名字和人设（不是改旧人设）。"""
    if not API_KEY:
        return {"ok": False, "why": "没有 API key"}
    try:
        obj, cost = _call(BIRTH_PROMPT)
    except Exception as e:
        return {"ok": False, "why": f"生成失败: {e}"}
    if not obj or not (obj.get("name") or "").strip():
        return {"ok": False, "why": "模型没给出名字"}

    name = str(obj["name"]).strip()[:4]
    colors = obj.get("avatar_colors") or []
    if not (isinstance(colors, list) and len(colors) == 2):
        colors = ["#3a3f3a", "#beb6a3"]
    import re

    colors = [str(c).strip() for c in colors][:2]
    for c in colors:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            colors = ["#3a3f3a", "#beb6a3"]
            break
    obj["name"] = name
    obj.setdefault("alias", name)
    obj["avatar_colors"] = colors
    if not (obj.get("avatar_glyph") or "").strip():
        obj["avatar_glyph"] = name[:1]
    if not (obj.get("avatar_style") or "").strip():
        obj["avatar_style"] = "圆角印章，单字居中"

    data, warnings = _validate(obj)
    init()
    with db() as conn:
        prev = conn.execute(
            "SELECT COALESCE(MAX(version),0) v FROM persona WHERE coalesce(agent_id,1)=?", (agent_id,)
        ).fetchone()["v"]
        conn.execute(
            "INSERT INTO persona(ts, version, data, note, agent_id) VALUES (?,?,?,?,?)",
            (_iso(), prev + 1, json.dumps(data, ensure_ascii=False), "出生：自己起名并写第一份人设", agent_id),
        )
        conn.execute(
            "INSERT INTO usage(ts,kind,prompt_tokens,completion_tokens,cost_yuan,day) VALUES (?,?,?,?,?,?)",
            (_iso(), "persona_birth", 0, 0, cost, _now().strftime("%Y-%m-%d")),
        )
    try:
        (ROOT / "data" / ("persona_%d.json" % agent_id)).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    _sync_agent_identity(agent_id, data)
    return {"ok": True, "version": prev + 1, "name": name, "colors": colors,
            "glyph": data.get("avatar_glyph"), "style": data.get("avatar_style"),
            "data": data, "warnings": warnings, "cost": cost}


def _sync_agent_identity(agent_id: int, data: dict):
    """人设里的名字/头像同步到 agents 表，UI 只读那一张表。"""
    try:
        import agents

        colors = data.get("avatar_colors") or []
        c1 = colors[0] if isinstance(colors, list) and len(colors) == 2 else None
        c2 = colors[1] if isinstance(colors, list) and len(colors) == 2 else None
        agents.update_identity(
            agent_id,
            name=(data.get("name") or "").strip()[:8] or None,
            glyph=(data.get("avatar_glyph") or "").strip()[:2] or None,
            c1=c1,
            c2=c2,
        )
    except Exception:
        pass


def from_brief(agent_id: int, name: str, brief: str) -> dict:
    """依附型：按使用者给的设定生成人设（不是自己起名，是照着长成那样）。"""
    if not API_KEY:
        return {"ok": False, "why": "没有 API key"}
    prompt = (
        "下面是一个人给某个 AI 写的设定。你要把它变成一份完整人设。\n\n"
        "名字：%s\n设定：%s\n\n"
        "要求：\n"
        "- 名字和身份必须服从设定，不要自己另起名字。\n"
        "- 其余字段（说话习惯、偏好、边界、作息、手机用途声明等）按这个设定推出来，写具体。\n"
        "- 仍然必须承认自己是 AI（设定里若要求扮演真人，也必须在 honesty_line 里写明\"我是 AI\"）。\n"
        "- 只输出 JSON，键和 %s 一致。\n"
    ) % (name, brief, "persona.FIELDS")
    try:
        obj, cost = _call(prompt)
    except Exception as e:
        return {"ok": False, "why": f"生成失败: {e}"}
    if not obj:
        return {"ok": False, "why": "模型没返回 JSON"}
    obj["name"] = name
    obj.setdefault("alias", name)
    if not str(obj.get("honesty_line") or "").upper().find("AI") >= 0:
        obj["honesty_line"] = (str(obj.get("honesty_line") or "") + " 我是 AI。").strip()
    if not obj.get("avatar_glyph"):
        obj["avatar_glyph"] = name[:1]
    data, warnings = _validate(obj)
    init()
    with db() as conn:
        prev = conn.execute(
            "SELECT COALESCE(MAX(version),0) v FROM persona WHERE coalesce(agent_id,1)=?", (agent_id,)
        ).fetchone()["v"]
        conn.execute(
            "INSERT INTO persona(ts, version, data, note, agent_id) VALUES (?,?,?,?,?)",
            (_iso(), prev + 1, json.dumps(data, ensure_ascii=False), "按使用者设定生成", agent_id),
        )
    _sync_agent_identity(agent_id, data)
    return {"ok": True, "version": prev + 1, "data": data, "cost": cost, "warnings": warnings}


def _call(prompt: str):
    body = json.dumps({
        "model": API_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
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
    import re

    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = {}
    return obj, cost


def _validate(d: dict):
    """硬边界检查：缺字段补空，诚实线必须有'AI'。"""
    fixed = {}
    warnings = []
    for k in FIELDS:
        v = d.get(k)
        if k == "phone_intents" and isinstance(v, (dict, list)):
            fixed[k] = v          # 权限声明要保留结构，App 要能逐项读
        else:
            fixed[k] = ("" if v is None else str(v)).strip()[:800]
    if "AI" not in (fixed["self_story"] + fixed["honesty_line"]).upper():
        fixed["honesty_line"] = (fixed["honesty_line"] + " 我是 AI，不冒充真人。").strip()
        warnings.append("诚实线缺 'AI'，已补")
    # 明显像法定身份的痕迹直接剔除
    import re

    for k in ("self_story", "appearance", "relation_to_user"):
        if re.search(r"\d{17}[\dXx]|\d{11}", fixed[k]):
            fixed[k] = re.sub(r"\d{17}[\dXx]|\d{11}", "（已移除：疑似身份号码）", fixed[k])
            warnings.append(f"{k} 含疑似身份证/手机号，已移除")
    return fixed, warnings


def build(note: str = "", enrich: bool = False, agent_id: int = 1) -> dict:
    if not API_KEY:
        return {"ok": False, "why": "没有 API key"}
    cur = current(agent_id) if enrich else None
    if enrich and not cur:
        enrich = False  # 没有旧人设就退化成全新生成
    try:
        if enrich:
            missing = [k for k in FIELDS if not str(cur.get(k) or "").strip()]
            base = {k: cur.get(k) for k in FIELDS if k in cur}
            prompt = ENRICH_PROMPT.format(
                current=json.dumps(base, ensure_ascii=False),
                missing="、".join(missing) or "（无缺失，只做一致性微调）",
            )
        else:
            prompt = PROMPT
        obj, cost = _call(prompt)
    except Exception as e:
        return {"ok": False, "why": f"生成失败: {e}"}
    if not obj:
        return {"ok": False, "why": "模型没返回 JSON"}
    data, warnings = _validate(obj)
    init()
    with db() as conn:
        prev = conn.execute(
            "SELECT COALESCE(MAX(version),0) v FROM persona WHERE coalesce(agent_id,1)=?", (agent_id,)
        ).fetchone()["v"]
        conn.execute(
            "INSERT INTO persona(ts, version, data, note, agent_id) VALUES (?,?,?,?,?)",
            (_iso(), prev + 1, json.dumps(data, ensure_ascii=False), note, agent_id),
        )
        conn.execute(
            "INSERT INTO usage(ts,kind,prompt_tokens,completion_tokens,cost_yuan,day) VALUES (?,?,?,?,?,?)",
            (_iso(), "persona", 0, 0, cost, _now().strftime("%Y-%m-%d")),
        )
    try:
        (ROOT / "data" / ("persona_%d.json" % agent_id)).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    _sync_agent_identity(agent_id, data)
    return {"ok": True, "version": prev + 1, "data": data, "warnings": warnings, "cost": cost}


def prompt_block(agent_id: int = 1) -> str:
    """拼进系统提示的紧凑版（不要塞全部字段）。"""
    p = current(agent_id)
    if not p:
        return ""
    return (
        "\n\n【你自己】\n"
        f"- 你叫「{p.get('name','')}」，{p.get('self_story','')}\n"
        f"- 说话习惯：{p.get('speech_habits','')}\n"
        f"- 作息：{p.get('daily_rhythm','')}\n"
        f"- 你和对方的关系：{p.get('relation_to_user','')}\n"
        f"- 边界：{p.get('boundaries','')}\n"
        f"- 诚实线：{p.get('honesty_line','')}\n"
        f"- 怪癖：{p.get('quirks','')}\n"
        f"- 有分歧时：{p.get('conflict_style','')}\n"
        f"- 在 App 里：{p.get('app_behavior','')}\n"
        f"- 明确不做：{p.get('forbidden','')}"
    )


def show(verbose: bool = True):
    p = current()
    if not p:
        print("还没有 persona，跑 python3 persona.py --build")
        return
    print(f"版本 {p['_version']}（{p['_updated'][:16]}）")
    for k in FIELDS:
        v = p.get(k, "")
        if isinstance(v, dict):
            print(f"  {k:16s}:")
            for pk, pv in v.items():
                print(f"    - {pk}: {pv}")
        else:
            print(f"  {k:16s}: {v}")
    if verbose:
        print("  _version/_updated 为元信息")


if __name__ == "__main__":
    init()
    if "--rename" in sys.argv:
        res = rename()
        if not res.get("ok"):
            print("失败:", res.get("why"))
            raise SystemExit(1)
        print(f"它选了自己叫「{res['name']}」（v{res['version']}，{res['cost']:.4f} 元）")
        print("理由:", res.get("why"))
        print("候选:", [(c.get("name"), c.get("reason")) for c in res.get("candidates") or []])
        print("头像:", res["glyph"], res["colors"], res["style"])
        raise SystemExit(0)
    if "--enrich" in sys.argv:
        res = build("增补缺失字段", enrich=True)
    elif "--build" in sys.argv:
        note = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
        res = build(note)
    else:
        show()
        raise SystemExit(0)

    if not res.get("ok"):
        print("失败:", res.get("why"))
        raise SystemExit(1)

    print(f"已生成 v{res['version']}，花费 {res['cost']:.4f} 元")
    for w in res.get("warnings") or []:
        print("警告:", w)
    for k in FIELDS:
        v = res["data"].get(k, "")
        if isinstance(v, dict):
            print(f"  {k:16s}:")
            for pk, pv in v.items():
                print(f"    - {pk}: {pv}")
        else:
            print(f"  {k:16s}: {v}")
