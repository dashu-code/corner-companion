"""TTS 模块：Edge 音色（微软，免费）→ mp3 字节流，带磁盘缓存。

为什么走服务器而不是手机系统 TTS：
- 手机系统 TTS 音色机械（ColorOS 自带引擎）
- Edge 音色质量接近主流 AI 应用的朗读
- 音频走加密通道传输，和聊天内容同等级

设计要点：
- 改一行环境变量就能换音色，不用重打包 App
- 同一句话缓存成文件，重复不重复请求
- edge_tts 没装时优雅降级（返回 503，App 回退到系统 TTS）
"""

import asyncio
import hashlib
import io
import os
import re
import sys
import time

# ---- 配置：默认值 → 环境变量 → data/tts_settings.json（网页里改的存这里）----
ENGINE = os.environ.get("TTS_ENGINE", "edge").strip().lower()
VOICE = os.environ.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural").strip()
RATE = os.environ.get("TTS_RATE", "+0%").strip()       # 负值更慢；默认不调
PITCH = os.environ.get("TTS_PITCH", "+0Hz").strip()    # 负值更低；默认不调（压低会像男声）

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(ROOT, "data", "tts_settings.json")

# 网页设置页里可选的中文音色（标签是人话，便于挑）
VOICE_CATALOG = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓 · 温柔（推荐）", "gender": "女"},
    {"id": "zh-CN-XiaoyiNeural",   "name": "晓伊 · 活泼（音调归零才正常）", "gender": "女"},
    {"id": "zh-TW-HsiaoChenNeural", "name": "曉臻 · 台湾腔", "gender": "女"},
    {"id": "zh-TW-HsiaoYuNeural",   "name": "曉雨 · 台湾甜", "gender": "女"},
]

# 语速 / 音调的预设（网页里用下拉，不做任意值，避免调出怪声）
RATE_PRESETS = ["+0%", "-5%", "-10%", "-15%", "-20%", "-30%", "+5%", "+10%"]
PITCH_PRESETS = ["+0Hz", "-5Hz", "-10Hz", "-15Hz", "-20Hz", "-30Hz", "+5Hz", "+10Hz"]


def _load_settings():
    """读 data/tts_settings.json 覆盖环境变量。"""
    global VOICE, RATE, PITCH
    try:
        import json
        if os.path.isfile(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("voice"):
                VOICE = str(d["voice"]).strip()
            if d.get("rate"):
                RATE = str(d["rate"]).strip()
            if d.get("pitch"):
                PITCH = str(d["pitch"]).strip()
    except Exception as e:
        print(f"[tts] 读设置失败: {e}", file=sys.stderr)


def save_settings(voice: str = "", rate: str = "", pitch: str = "") -> dict:
    global VOICE, RATE, PITCH
    import json
    if voice:
        VOICE = voice.strip()
    if rate:
        RATE = rate.strip()
    if pitch:
        PITCH = pitch.strip()
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"voice": VOICE, "rate": RATE, "pitch": PITCH}, f, ensure_ascii=False)
    return info()


def voices() -> dict:
    return {"voices": VOICE_CATALOG, "rate_presets": RATE_PRESETS,
            "pitch_presets": PITCH_PRESETS, "current": info()}


_load_settings()
CACHE_DIR = os.path.join(ROOT, "data", "tts_cache")
CACHE_MAX_FILES = int(os.environ.get("TTS_CACHE_MAX_FILES", "500"))

_available = None


def available() -> bool:
    global _available
    if _available is None:
        try:
            import edge_tts  # noqa: F401
            _available = True
        except Exception:
            _available = False
    return _available


def _cache_path(text: str, voice: str, rate: str, pitch: str) -> str:
    key = f"{voice}|{rate}|{pitch}|{text}".encode("utf-8")
    h = hashlib.sha256(key).hexdigest()[:32]
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, h + ".mp3")


def _trim_cache():
    try:
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".mp3")]
        if len(files) <= CACHE_MAX_FILES:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[: len(files) - CACHE_MAX_FILES]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


def _synth_blocking(text: str, voice: str, rate: str, pitch: str) -> bytes:
    import edge_tts

    async def run() -> bytes:
        buf = io.BytesIO()
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    # 在独立事件循环里跑（服务端是多线程 HTTP，每线程一个循环安全）
    return asyncio.run(run())



MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "2000"))
CHUNK_CHARS = int(os.environ.get("TTS_CHUNK_CHARS", "110"))


def _split_chunks(text: str, limit: int = None) -> list:
    """按句子切成不超过 limit 字的小段，保证长回复能念全。"""
    limit = limit or CHUNK_CHARS
    text = re.sub(r"[\r\n]+", "，", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;…])", text)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:limit])
            p = p[limit:]
        if len(cur) + len(p) <= limit:
            cur += p
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [text[:limit]]


def chunks(text: str) -> list:
    """给外部用的分段接口（和前端的分段规则保持一致）。"""
    return _split_chunks(text)


def synth(text: str, voice: str = "", rate: str = "", pitch: str = "") -> bytes:
    """返回 mp3 字节。失败抛异常，由调用方转成 HTTP 错误。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    if not available():
        raise RuntimeError("TTS 引擎不可用（edge-tts 未安装）")

    v = (voice or VOICE).strip()
    r = (rate or RATE).strip()
    p = (pitch or PITCH).strip()

    path = _cache_path(text, v, r, p)
    if os.path.isfile(path) and os.path.getsize(path) > 512:
        with open(path, "rb") as f:
            return f.read()

    # 只合成传入的这一段（不要拼多段：MP3 拼接后播放器会按首段时长截断）
    data = _synth_blocking(text, v, r, p)
    if len(data) < 512:
        raise RuntimeError("合成结果异常（太小）")
    try:
        with open(path, "wb") as f:
            f.write(data)
        _trim_cache()
    except OSError:
        pass
    return data


def info() -> dict:
    return {"engine": ENGINE, "voice": VOICE, "rate": RATE, "pitch": PITCH,
            "available": available(), "cache_dir": CACHE_DIR}
