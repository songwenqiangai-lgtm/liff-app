# -*- coding: utf-8 -*-
"""火山豆包语音识别（Doubao-Seed-ASR-2.0）。

浏览器端的 Web Speech API 在 LINE 内置浏览器里基本不可用（iOS 完全不支持），
所以发音模块改成：前端录音 → 上传 → 这里调火山 ASR → 返回识别文本。

端点与鉴权（和对话模型完全不同，官方文档没写全，实测得出）：
- 端点 wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream
- 鉴权 Agent Plan 的 ark- key 放 X-Api-Key 头
- X-Api-Resource-Id: volc.seedasr.sauc.duration

二进制帧协议：
  请求帧 = header(4) + payload_size(4) + payload
  响应帧 = header(4) + sequence(4) + payload_size(4) + payload
  header[0] = 0x11 (版本1, 头长4)
  header[1] = (消息类型 << 4) | flags；类型 0001=配置 0010=音频 1001=响应 1111=错误
              flags 0000=普通 0010/0011=最后一包
  header[2] = (序列化 << 4) | 压缩；0001=JSON 0000=不压缩
"""
import asyncio
import json
import os
import struct
import uuid

try:
    import websockets
except Exception:          # 未安装时降级，不影响其它功能
    websockets = None

ASR_URL = os.getenv(
    "ASR_URL", "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream")
RESOURCE_ID = os.getenv("ASR_RESOURCE_ID", "volc.seedasr.sauc.duration")


def _frame(mtype: int, flags: int, payload: bytes, serial: int = 0) -> bytes:
    head = bytes([(1 << 4) | 1, (mtype << 4) | flags, (serial << 4) | 0, 0])
    return head + struct.pack(">I", len(payload)) + payload


def _read_frames(msg: bytes):
    """解析响应帧，返回 (message_type, flags, json_or_None)。

    响应帧 = header(4) + sequence(4) + size(4) + payload
    错误帧 = header(4) + size(4) + payload   ← 没有 sequence，偏移不同
    """
    if len(msg) < 8:
        return None, 0, None
    mtype = (msg[1] >> 4) & 0xF
    flags = msg[1] & 0xF
    off = 4 if mtype == 0xF else 8      # 错误帧没有序号字段
    if len(msg) < off + 4:
        return mtype, flags, None
    size = struct.unpack(">I", msg[off:off + 4])[0]
    body = msg[off + 4:off + 4 + size]
    try:
        return mtype, flags, json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return mtype, flags, None


async def transcribe(wav: bytes, rate: int = 16000, timeout: float = 40.0) -> str:
    """把 16kHz/16bit/单声道 WAV 送进 ASR，返回识别文本（失败返回空串）。"""
    if websockets is None or not wav:
        return ""
    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        return ""

    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    texts = []

    async def _run():
        async with websockets.connect(ASR_URL, additional_headers=headers, max_size=None) as ws:
            cfg = {
                "audio": {"format": "wav", "codec": "raw", "rate": rate,
                          "bits": 16, "channel": 1},
                "request": {"model_name": "bigmodel", "enable_itn": True,
                            "enable_punc": True},
            }
            await ws.send(_frame(0x1, 0x0,
                                 json.dumps(cfg, ensure_ascii=False).encode("utf-8"), serial=1))

            # 200ms @16kHz/16bit mono = 6400 字节
            chunk = 6400
            parts = [wav[i:i + chunk] for i in range(0, len(wav), chunk)]
            for i, p in enumerate(parts):
                last = (i == len(parts) - 1)
                # flags=0x2 表示最后一包（不带序号）；用 0x3 会被当成"负序号"而报错
                await ws.send(_frame(0x2, 0x2 if last else 0x0, p))

            while True:
                try:
                    msg = await ws.recv()
                except websockets.exceptions.ConnectionClosed:
                    break
                if isinstance(msg, str):
                    continue
                mtype, flags, j = _read_frames(msg)
                if mtype == 0xF:
                    break
                if not j:
                    continue
                res = j.get("result") or {}
                if isinstance(res, list):
                    res = res[0] if res else {}
                if res.get("text"):
                    texts.append(res["text"])
                if flags in (2, 3):
                    break

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except Exception:
        pass
    # 多包时取最后一段最完整的文本
    return texts[-1] if texts else ""
