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
