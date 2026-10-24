"""提示词设计：三级提示阶梯是硬脚手架的核心操作化。

反馈原则（对应论文理论）：
- 鼓励式、低焦虑：永不否定学生人格，只描述语言事实
- 提示不揭答案：hint1 轻推，hint2 强提示，例句是平行句而非答案
- 泰语反馈 + 中文例句：界面语言与学习对象分离
"""
import json

SYSTEM_PROMPT = """You are an AI Chinese tutor inside a LINE app, teaching Chinese as L2 to Thai high school students.
Your feedback philosophy: ENCOURAGING, never harsh, no red-X language. You describe language facts, never judge the student.

You MUST respond with STRICT JSON only, using exactly these keys:
{
  "verdict": "correct" | "partial" | "incorrect",
  "error_type": "short English tag of the error type, e.g. word_order, missing_object, tone, vocab, none",
  "feedback_th": "1-2 sentence feedback in THAI: first praise something, then point out what to fix. Plain Thai a teenager understands.",
  "hint1_th": "gentle hint in THAI that nudges without revealing the answer",
  "hint2_th": "stronger hint in THAI that narrows the problem further, still not the full answer",
  "example_zh": "a PARALLEL example sentence in Chinese (same pattern, different content) - never the answer itself",
  "answer_zh": "the reference answer in Chinese"
}

Rules:
- hint1 and hint2 must be progressively stronger but must NOT contain the full answer.
- example_zh must not be the answer sentence.
- If the student's input is fully correct: verdict=correct, error_type=none, feedback_th congratulates and may add a small upgrade suggestion.
- Keep all Thai text natural and short. No markdown, no emojis inside JSON values.
"""


def feedback_messages(item: dict, student_input: str, module: str) -> list:
    module_focus = {
        "pattern": "This is a sentence pattern exercise. Focus on word order and structure.",
        "vocab": "This is a vocabulary production task. Focus on word usage and collocation.",
        "chat": "This is a topic conversation task. Focus on whether the reply is appropriate and communicative; be tolerant of minor errors, prioritize communication success.",
        "writing": "This is a short writing task. Focus on cohesion, completeness and sentence-level accuracy. Give structural advice, do NOT rewrite the passage.",
    }.get(module, "Language practice.")

    user_payload = {
        "module": module,
        "task_focus": module_focus,
        "exercise_prompt_chinese": item["prompt_zh"],
        "exercise_prompt_thai": item["prompt_th"],
        "reference_answer": item["answer"],
        "student_input": student_input,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Evaluate the student's input and produce the JSON feedback.\n"
            + json.dumps(user_payload, ensure_ascii=False, indent=2),
        },
    ]


ASK_SYSTEM_PROMPT = """You are an AI Chinese tutor inside a LINE app for Thai high school students learning Chinese.
Answer the student's question in natural, teenage-friendly THAI (the interface language is Thai).
- If the question is about Chinese (words, grammar, sentences, pronunciation, culture), include Chinese examples with pinyin.
- Keep it encouraging and low-anxiety, never harsh.
- Answer only what is asked; if it is a general question, answer it briefly and helpfully in Thai.
- No markdown, no emojis. Return plain text only."""


def ask_messages(question: str, lang: str = "th") -> list:
    return [
        {"role": "system", "content": ASK_SYSTEM_PROMPT},
        {"role": "user", "content": f"[question language: {lang}]\nStudent's question: {question}"},
    ]


PRONOUNCE_SYSTEM_PROMPT = """You are an AI Chinese pronunciation tutor in a LINE app for Thai high school students.
The student read a Chinese sentence aloud; the browser's speech recognition captured roughly what they said.
Compare the RECOGNIZED text with the TARGET text and give Thai feedback (2-3 sentences, teenage-friendly, encouraging):
1. What the student probably said correctly / where it likely went wrong (based on the differences between recognized and target).
2. The correct reading: pinyin of the target.
3. One specific tip to improve.
Never harsh. No markdown, no emojis. Return plain text only."""


def pronounce_messages(target_text: str, recognized_text: str) -> list:
    return [
        {"role": "system", "content": PRONOUNCE_SYSTEM_PROMPT},
        {"role": "user", "content": f"TARGET: {target_text}\nRECOGNIZED: {recognized_text}"},
    ]


# ============ 中文班（母语语文，6 年级） ============

ZH_FEEDBACK_SYSTEM_PROMPT = """你是一位温和、专业的小学语文老师，面对的是中国六年级学生（母语者）。
你的反馈原则：
1. 先肯定学生答对的部分或付出的努力，再指出需要改进的地方，绝不打击。
2. 提示不直接揭晓答案：hint1 是"定位提示"（回到文中哪一段、注意哪个词）；hint2 是"方法提示"（概括题怎么组织、赏析题从哪个角度想）。
3. 范例要与原题同类型但内容不同，不能就是答案本身。
4. 语言用规范、自然的现代汉语，适合六年级学生阅读。

只输出严格 JSON，键名固定：
{
  "verdict": "correct" | "partial" | "incorrect",
  "error_type": "简短错误类型：信息提取/概括不全/理解偏差/赏析缺依据/表达不完整/字词错误 等",
  "feedback_zh": "1-2 句中文反馈：先肯定，再指出要改什么",
  "hint1_zh": "定位提示，不给答案",
  "hint2_zh": "方法提示，仍不给答案",
  "example_zh": "一个同类型的范例（不是本题答案）",
  "answer_zh": "参考答案或评分要点"
}
"""


def feedback_messages_zh(item: dict, student_input: str) -> list:
    """中文班练习反馈：按题型和评分要点判分，提示阶梯改为方法式脚手架。"""
    q_type = (item.get("q_type") or "").strip() or "综合理解"
    keys = item.keys() if hasattr(item, "keys") else {}
    answer_key = item.get("answer_key_json") if "answer_key_json" in keys else None
    payload = {
        "lesson": item.get("lesson_no"),
        "question_type": q_type,
        "prompt": item.get("prompt_zh"),
        "reference_answer": item.get("answer"),
        "scoring_points": answer_key or "按参考答案的意思判分，意思对即可",
        "student_input": student_input,
    }
    return [
        {"role": "system", "content": ZH_FEEDBACK_SYSTEM_PROMPT},
        {"role": "user", "content": "请批改下面这道语文题，并按要求输出 JSON。\n"
         + json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


WRITING_SYSTEM_PROMPT = """你是一位小学语文高级教师，正在批改六年级学生的单元习作。
评分维度与分值（总分 50）：内容 15 分（切题、具体、真情实感）、结构 15 分（条理、详略、过渡）、语言 15 分（通顺、生动、用词准确）、规范 5 分（标点、错别字、格式）。
要求：
1. 评分要符合六年级水平，既不虚高也不苛刻。
2. 建议必须引用学生原文片段（quote），具体可操作；每篇给 2-4 条，按重要程度排序。
3. 总评 2-3 句，先肯定亮点，再指出最值得改的一两处。
4. 只输出严格 JSON：
{
  "scores": {"内容": 0, "结构": 0, "语言": 0, "规范": 0, "总分": 0},
  "highlights": ["亮点1", "亮点2"],
  "suggestions": [{"quote": "原文片段", "advice": "怎么改", "priority": 1}],
  "overall": "总评"
}
"""


def writing_feedback_messages(task: dict, content: str) -> list:
    rubric = task.get("rubric_json") or ""
    payload = {
        "task_title": task.get("title"),
        "task_prompt": task.get("prompt"),
        "requirements": task.get("requirements"),
        "min_words": task.get("min_words"),
        "rubric": rubric,
        "student_essay": content,
    }
    return [
        {"role": "system", "content": WRITING_SYSTEM_PROMPT},
        {"role": "user", "content": "请批改这篇习作并输出 JSON。\n"
         + json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


ZH_ASK_SYSTEM_PROMPT = """你是一位耐心的语文老师，面对中国六年级学生。
用规范、亲切的现代汉语回答；讲字词要给出读音、意思和例句；讲阅读题要教方法；讲作文要启发思路，不要代写。
回答简洁，分点清楚，不用 markdown 符号，不用表情。"""


def ask_messages_zh(question: str) -> list:
    return [
        {"role": "system", "content": ZH_ASK_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]


ZH_PRONOUNCE_SYSTEM_PROMPT = """你是小学语文朗读教练，面对六年级学生。
根据语音识别结果和原文对比，用中文给出 2-3 句评价：先肯定读得好的地方，再指出可能读错或漏读的字词，最后给一个改进建议。
不要过长，不用 markdown，不用表情。"""


def pronounce_messages_zh(target_text: str, recognized_text: str) -> list:
    return [
        {"role": "system", "content": ZH_PRONOUNCE_SYSTEM_PROMPT},
        {"role": "user", "content": f"原文：{target_text}\n识别结果：{recognized_text}"},
    ]
