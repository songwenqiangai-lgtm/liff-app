import os, json, re, httpx

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass  # 没有 .env 时使用系统环境变量

# OpenAI 兼容格式（适用于 OpenAI / 各类兼容代理 / DeepSeek / Moonshot / 火山方舟等）
# 若使用 Anthropic 原生 API，请改写 chat_json 内的请求体
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# 第二供应商（MiniMax，处理中文内容）
MM_BASE_URL = os.getenv("LLM_BASE_URL_MM", "https://api.minimaxi.com/v1").rstrip("/")
MM_API_KEY = os.getenv("LLM_API_KEY_MM", "")
MM_MODEL = os.getenv("LLM_MODEL_MM", "MiniMax-Text-01")


def provider_conf(provider):
    """provider: None=默认(DeepSeek) / "mm"=MiniMax(中文)"""
    if provider == "mm":
        return MM_BASE_URL, MM_API_KEY, MM_MODEL
    return BASE_URL, API_KEY, MODEL

FALLBACK_RESULT = {
    "verdict": "error",
    "error_type": "config",
    "feedback_th": "ระบบยังไม่พร้อม (LLM_API_KEY ยังไม่ได้ตั้งค่า) โปรดแจ้งครู",
    "hint1_th": "",
    "hint2_th": "",
    "example_zh": "",
    "answer_zh": "",
}


def visible_text(text: str) -> str:
    """Return the final answer only; provider reasoning is not student content."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", text, flags=re.S | re.I)
    # An interrupted thinking block has no displayable final answer.
    text = re.sub(r"<think\b[^>]*>.*$", "", text, flags=re.S | re.I)
    return text.strip()


def parse_json(text: str) -> dict:
    text = visible_text(text)
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception:
        out = dict(FALLBACK_RESULT)
        out["error_type"] = "parse"
        out["feedback_th"] = "ขออภัย ครู AI ตอบผิดรูปแบบ ลองอีกครั้งนะ"
        return out


async def chat_json(messages: list, temperature: float = 0.3, timeout: float = 90, provider=None) -> dict:
    """调用 LLM 并强制解析为 JSON。任何异常都降级为错误占位，不中断学生端。"""
    try:
        return await _chat_json(messages, temperature, timeout, provider)
    except Exception:
        out = dict(FALLBACK_RESULT)
        out["error_type"] = "unexpected"
        return out


async def _chat_json(messages: list, temperature: float, timeout: float, provider) -> dict:
    """provider: None=默认(泰/英) / "mm"=中文分支"""
    base_url, api_key, model = provider_conf(provider)
    if not api_key:
        out = dict(FALLBACK_RESULT)
        out["error_type"] = "config" if provider is None else "mm_config"
        return out
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    base_payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if provider == "mm":
        base_payload["reasoning_split"] = True
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 先尝试 json_object 模式，若服务不支持则降级重试
        try:
            r = await client.post(
                f"{base_url}/chat/completions",
                json={**base_payload, "response_format": {"type": "json_object"}},
                headers=headers,
            )
            if r.status_code == 400:
                r = await client.post(
                    f"{base_url}/chat/completions", json=base_payload, headers=headers
                )
        except httpx.HTTPError:
            out = dict(FALLBACK_RESULT)
            out["error_type"] = "network"
            return out
    if r.status_code != 200:
        out = dict(FALLBACK_RESULT)
        out["error_type"] = f"http_{r.status_code}"
        return out
    try:
        text = r.json()["choices"][0]["message"]["content"]
    except Exception:
        out = dict(FALLBACK_RESULT)
        out["error_type"] = "empty"
        return out
    return parse_json(text)


async def chat_text(messages: list, temperature: float = 0.5, timeout: float = 90, provider=None) -> str:
    """调用 LLM 返回纯文本回答。任何异常都降级为泰语提示，绝不把 500 抛给学生。

    兜底范围故意用 Exception 而不是 httpx.HTTPError：httpx 的 InvalidURL 等
    并不继承 HTTPError，此前会在 Render 上穿透成 500。
    """
    try:
        return await _chat_text(messages, temperature, timeout, provider)
    except Exception:
        return "ครู AI ตอบไม่ได้ชั่วคราว ลองอีกครั้งนะ"


async def _chat_text(messages: list, temperature: float, timeout: float, provider) -> str:
    """provider: None=默认(泰/英) / "mm"=中文分支"""
    base_url, api_key, model = provider_conf(provider)
    if not api_key:
        return "ระบบยังไม่พร้อม (LLM_API_KEY ยังไม่ได้ตั้งค่า) โปรดแจ้งครู"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if provider == "mm":
        payload["reasoning_split"] = True
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
    except httpx.HTTPError:
        return "เชื่อมต่อไม่ได้ ลองอีกครั้งนะ"
    if r.status_code != 200:
        return f"ครู AI ตอบไม่ได้ (รหัส {r.status_code}) ลองอีกครั้ง"
    try:
        return visible_text(r.json()["choices"][0]["message"]["content"]) or "ครู AI ตอบผิดรูปแบบ ลองอีกครั้งนะ"
    except Exception:
        return "ครู AI ตอบผิดรูปแบบ ลองอีกครั้งนะ"
