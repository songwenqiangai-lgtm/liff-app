# -*- coding: utf-8 -*-
"""用火山豆包语音合成 2.0（Doubao-Seed-TTS-2.0）给 content.db 的单词和课文生成中文配音。

走 Agent Plan 专属端点 openspeech.bytedance.com/api/v3/plan/tts/unidirectional，
鉴权用 Agent Plan 的 ark- key（X-Api-Key 头），Resource-Id 固定 seed-tts-2.0。

输出到 backend/audio/vocab/<id>.mp3 和 backend/audio/texts/<id>.mp3，
按 content.db 的行 id 命名（与 /api/lesson 返回的 audio_url 对应），可断点续跑。
"""
import base64
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

TTS_URL = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
RESOURCE_ID = "seed-tts-2.0"
DEFAULT_VOICE = "zh_female_qingxinnvsheng_uranus_bigtts"

API_KEY = os.getenv("TTS_API_KEY", "")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.getenv("CONTENT_DB", os.path.join(BASE, "content", "content.db"))
AUDIO = os.getenv("AUDIO_DIR", os.path.join(BASE, "backend", "audio"))
VOICE = os.getenv("TTS_VOICE", DEFAULT_VOICE)
WORKERS = int(os.getenv("TTS_WORKERS", "4"))
RETRY = 3

_lock = threading.Lock()
_done = [0]
_fail = [0]


def synth(text: str) -> bytes:
    """调一次 TTS，返回 mp3 字节。响应是流式 NDJSON，逐行拼接 base64 音频。"""
    body = {"req_params": {
        "text": text,
        "speaker": VOICE,
        "audio_params": {"format": "mp3", "sample_rate": 24000},
    }}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(TTS_URL, data=data, method="POST")
    req.add_header("X-Api-Key", API_KEY)
    req.add_header("X-Api-Resource-Id", RESOURCE_ID)
    req.add_header("Content-Type", "application/json")
    raw = urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "replace")
    chunks, err = [], None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        if j.get("code") not in (0, None):
            err = "code=%s %s" % (j.get("code"), j.get("message"))
        if j.get("data"):
            chunks.append(base64.b64decode(j["data"]))
    if not chunks:
        raise RuntimeError(err or "no audio in response")
    return b"".join(chunks)


def one(kind: str, rid, text: str):
    """生成一条；已存在则跳过。"""
    path = os.path.join(AUDIO, kind, "%s.mp3" % rid)
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        with _lock:
            _done[0] += 1
        return
    last = None
    for attempt in range(RETRY):
        try:
            audio = synth(text)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(audio)
            os.replace(tmp, path)
            with _lock:
                _done[0] += 1
                n = _done[0]
                if n % 100 == 0:
                    print("  已生成 %d 条 (最新 %s/%s)" % (n, kind, rid), flush=True)
            return
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    with _lock:
        _fail[0] += 1
    print("  ✗ %s/%s 失败: %s" % (kind, rid, str(last)[:90]), flush=True)


def main():
    if not API_KEY:
        print("缺少 TTS_API_KEY 环境变量")
        sys.exit(1)
    print("音色: %s" % VOICE)
    print("并发: %d" % WORKERS)
    for k in ("vocab", "texts"):
        os.makedirs(os.path.join(AUDIO, k), exist_ok=True)

    conn = sqlite3.connect(DB)
    jobs = [("vocab", r[0], r[1]) for r in conn.execute("SELECT id, word FROM vocab ORDER BY id")]
    jobs += [("texts", r[0], r[1]) for r in conn.execute("SELECT id, body_zh FROM texts ORDER BY id")]
    print("总任务: %d 条（vocab %d + texts %d）" % (
        len(jobs),
        sum(1 for j in jobs if j[0] == "vocab"),
        sum(1 for j in jobs if j[0] == "texts")))
    print("（已存在的会自动跳过）", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(one, k, rid, txt) for k, rid, txt in jobs]
        for _ in as_completed(futs):
            pass
    dt = time.time() - t0
    print()
    print("完成: 成功/跳过 %d 条, 失败 %d 条, 耗时 %.1f 分钟" % (_done[0], _fail[0], dt / 60))


if __name__ == "__main__":
    main()
