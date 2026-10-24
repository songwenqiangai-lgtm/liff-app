import os, io, csv, json, re, time, hmac, hashlib, ipaddress, secrets, base64
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass  # 没有 .env 时使用系统环境变量

from database import get_conn, content_db, init_db, seed_items, DATA_DIR
from llm import chat_json, chat_text
from prompts import (feedback_messages, ask_messages, pronounce_messages,
                     feedback_messages_zh, writing_feedback_messages,
                     ask_messages_zh, pronounce_messages_zh)
import fidelity_config as FC
import analytics as AN
import asr as ASR

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "frontend"))
TEACHER_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "teacher"))

LINE_CHANNEL_ID = os.getenv("LINE_CHANNEL_ID", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-change-me")
TEACHER_PASSWORD = os.getenv("TEACHER_PASSWORD", "teacher123")

LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"

MODULES = ["pattern", "vocab", "chat", "writing"]
PHASE_MODULES = {"P0": [], "P1": MODULES, "P2": [], "P3": MODULES,
                 "T1": [], "T2": []}
PHASE_LABELS = {
    "P0": "ปกติ (Baseline)",
    "P1": "เฉพาะครู AI",
    "P2": "เฉพาะครูผู้สอน",
    "P3": "ครู AI + ครูผู้สอน",
    "T1": "延时检验 1",
    "T2": "延时检验 2",
}
PHASE_NOTE_TH = {
    "P0": "ยังไม่เปิดใช้งานในช่วงนี้",
    "P2": "ครู AI พักผ่อนช่วงนี้ พบกันเร็ว ๆ นี้!",
    "T1": "ช่วงตรวจสอบผลระยะยาว (延时检验)",
    "T2": "ช่วงตรวจสอบผลระยะยาว (延时检验)",
}
MODULE_LABELS = {
    "pattern": "ฝึกแบบประโยค (句型操练)",
    "vocab": "บัตรคำศัพท์ (词汇闪卡)",
    "chat": "ฝึกสนทนา (会话练习)",
    "writing": "เขียนสั้น (写作反馈)",
}

# 班级 → 教材（内容集）：学生按班级看到对应教材的课程
CLASS_CONTENT = {
    "M4/1": "M41", "M4/2": "FH1",
    "M5/1": "M51", "M5/2": "FH2",
    "M6/1": "M61", "M6/2": "FH2",
    "C6/1": "G6A",   # 中文班 · 统编版六年级上册（母语语文）
}

# 中文班（母语语文）：界面模式、教学组织方式与时区
NATIVE_CLASSES = {"C6/1"}
CLASS_TZ = {"C6/1": "+8 hours"}        # 学生在中国大陆
CLASS_MODE = {"C6/1": "teaching"}      # teaching=常规教学（模块全开）；research=研究阶段控制
CLASS_LABELS = {"C6/1": "中文六年级 · 统编六上"}


def is_native(class_id: str) -> bool:
    return class_id in NATIVE_CLASSES


def class_tz(class_id: str) -> str:
    return CLASS_TZ.get(class_id, FC.TZ_OFFSET)


# 交互部分（练习题/考试）只在 P1/P3 开放；静态部分（单词/课文/语法）始终可见
INTERACTIVE_PHASES = {"P1", "P3", "N0"}
SECTION_KEYS = ["vocab", "text", "grammar", "exercise", "exam"]


def student_book(student) -> str:
    return CLASS_CONTENT.get(student["class_id"], "M41")


def assigned_lessons(class_id: str) -> set:
    """该班教师已分配的课次集合（未分配则空集 → 学生看不到课）"""
    rows = get_conn().execute(
        "SELECT lesson_no FROM class_lessons WHERE class_id=?", (class_id,)).fetchall()
    return {r["lesson_no"] for r in rows}


def phase_sections(phase: str, class_id: str = "") -> dict:
    # 中文班是常规教学：所有模块始终开放，不受研究阶段控制
    if class_id and is_native(class_id):
        return {k: True for k in ("vocab", "text", "grammar", "exercise",
                                  "exam", "pronounce", "hanzi", "poem", "writing")}
    interactive = phase in INTERACTIVE_PHASES
    return {k: interactive for k in ("exercise", "exam")} | {
        "vocab": True, "text": True, "grammar": True,
    }


def seed_native_classes():
    """确保中文班在 classes / phase_config 里有配置（幂等）。"""
    conn = get_conn()
    for cid, book in CLASS_CONTENT.items():
        native = is_native(cid)
        conn.execute(
            """INSERT INTO classes(class_id,grade,track,current_phase,phase_start_date,mode,ui_mode,tz_offset)
               VALUES(?,?,?,?,date('now'),?,?,?)
               ON CONFLICT(class_id) DO UPDATE SET
                 mode=excluded.mode, ui_mode=excluded.ui_mode, tz_offset=excluded.tz_offset""",
            (cid, _int_grade(cid), book, "N0" if native else "P0",
             CLASS_MODE.get(cid, "research"),
             "native" if native else "l2",
             class_tz(cid)))
        if native:
            conn.execute(
                """INSERT INTO phase_config(class_id,phase,ai_enabled,teacher_response_allowed,start_time)
                   VALUES(?, 'N0', 1, 1, datetime('now'))
                   ON CONFLICT(class_id) DO NOTHING""", (cid,))
    conn.commit()


def _int_grade(class_id: str):
    m = re.search(r"(\d)", class_id or "")
    return int(m.group(1)) if m else None

def seed_demo_config():
    """检测到"全新数据库"时自动铺一份可用配置。

    Render 免费档磁盘是临时的，每次重新部署都会清空 app.db —— 课程分配与
    阶段设置一并丢失，学生进去只看到空列表。这里在**无学生且无阶段配置**
    时自动应用默认配置，并给教师留一条通知，避免静默改变实验设置。

    正式做实验时把环境变量 AUTO_SEED_DEMO 设为 0 即可关闭。
    """
    if os.getenv("AUTO_SEED_DEMO", "1") != "1":
        return
    conn = get_conn()
    if conn.execute("SELECT COUNT(*) c FROM phase_config").fetchone()["c"]:
        return
    if conn.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]:
        return  # 有学生说明不是全新库（可能是被清空后重建），不擅自改
    first_lessons = {"M41": ["1", "2", "3"], "FH1": ["1", "2", "3"], "M51": ["16", "17", "18"],
                     "FH2": ["1", "2", "3"], "M61": ["13", "14", "15"]}
    for class_id, book in CLASS_CONTENT.items():
        if is_native(class_id):
            continue  # 中文班走常规教学配置，不铺研究阶段
        conn.execute(
            """INSERT INTO phase_config(class_id,phase,ai_enabled,teacher_response_allowed,start_time)
               VALUES(?,?,1,0,datetime('now'))
               ON CONFLICT(class_id) DO NOTHING""",
            (class_id, "P1"))
        for ln in first_lessons.get(book, ["1", "2", "3"]):
            conn.execute(
                "INSERT OR IGNORE INTO class_lessons(class_id,book,lesson_no) VALUES(?,?,?)",
                (class_id, book, ln))
    conn.commit()
    AN.notify("teacher", "all", "phase",
              "🔧 检测到全新数据库，已自动应用演示配置",
              "6 个班已设为 P1 纯AI 并各分配前 3 课。请到「阶段管理」和「课程分配」确认是否符合本次实验设计。")


app = FastAPI(title="Thai-Chinese Tutor LIFF")
init_db()
seed_items()
seed_demo_config()   # 先铺二语班演示配置（中文班在其内部被跳过）
seed_native_classes()  # 再注册 C6/1（N0 常规教学）


# ---------- 通用工具 ----------
def make_token(student_id: int) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), str(student_id).encode(), hashlib.sha256).hexdigest()[:16]
    return f"{student_id}.{sig}"


def parse_token(request: Request) -> int:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    token = auth[7:]
    try:
        sid, sig = token.split(".")
    except ValueError:
        raise HTTPException(401, "bad token")
    expect = hmac.new(SESSION_SECRET.encode(), sid.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expect):
        raise HTTPException(401, "bad token")
    return int(sid)


def get_student(student_id: int):
    row = get_conn().execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not row:
        raise HTTPException(401, "unknown student")
    return row


def current_phase(class_id: str) -> str:
    row = get_conn().execute("SELECT phase FROM phase_config WHERE class_id=?", (class_id,)).fetchone()
    return row["phase"] if row else "P0"


def log_event(student, event_type, module=None, item_id=None, input_text=None,
              hint_level=0, ai_latency_ms=None, response_json=None, detail=None):
    conn = get_conn()
    conn.execute(
        """INSERT INTO logs(student_id,class_id,phase,module,item_id,event_type,
           input_text,hint_level,ai_latency_ms,response_json,detail)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            student["id"], student["class_id"], current_phase(student["class_id"]),
            module, item_id, event_type, input_text, hint_level,
            ai_latency_ms, json.dumps(response_json, ensure_ascii=False) if response_json else None,
            json.dumps(detail, ensure_ascii=False) if detail else None,
        ),
    )
    conn.commit()


def log_teacher_action(action_type, target_class=None, detail=None, teacher_id="teacher"):
    conn = get_conn()
    conn.execute(
        "INSERT INTO teacher_actions(teacher_id, action_type, target_class, detail) VALUES(?,?,?,?)",
        (teacher_id, action_type, target_class,
         json.dumps(detail, ensure_ascii=False) if detail else None),
    )
    conn.commit()


def upsert_error(student_id, source, ref_id, q_type=None, knowledge_tag=None):
    """中文班错题本：同一道题/同一个字错误次数累加。"""
    if ref_id is None:
        return
    conn = get_conn()
    conn.execute(
        """INSERT INTO error_items(student_id,source,ref_id,q_type,knowledge_tag)
           VALUES(?,?,?,?,?)
           ON CONFLICT(student_id,source,ref_id) DO UPDATE SET
             wrong_count=wrong_count+1,
             q_type=COALESCE(excluded.q_type, error_items.q_type),
             knowledge_tag=COALESCE(excluded.knowledge_tag, error_items.knowledge_tag),
             last_wrong_at=datetime('now'),
             mastered=0""",
        (student_id, source, ref_id, q_type, knowledge_tag))
    conn.commit()


def _add_handoff(student_id, class_id, t_type, direction, trigger,
                 ai_action=None, source="auto_platform", auto=0):
    conn = get_conn()
    exists = conn.execute(
        "SELECT COUNT(*) c FROM handoff_events WHERE student_id=? AND t_type=? AND closed=0",
        (student_id, t_type)).fetchone()["c"]
    if exists:
        return False
    conn.execute(
        "INSERT INTO handoff_events(student_id,class_id,t_type,direction,trigger,ai_action,source,auto_triggered) VALUES(?,?,?,?,?,?,?,?)",
        (student_id, class_id, t_type, direction, trigger, ai_action, source, auto))
    conn.commit()
    # F6/F7：新交接（T3 已由回避预警单独通知）→ 提醒教师处理
    if t_type != "T3":
        st = conn.execute("SELECT display_name FROM students WHERE id=?", (student_id,)).fetchone()
        AN.notify("teacher", "all", "handoff",
                  f"🔁 {class_id or ''} {st['display_name'] if st else student_id} 触发 {t_type} 交接",
                  trigger or "")
    return True


def _add_flag(student_id, flag_type):
    conn = get_conn()
    exists = conn.execute(
        "SELECT COUNT(*) c FROM avoidance_flags WHERE student_id=? AND flag_type=? AND resolved=0",
        (student_id, flag_type)).fetchone()["c"]
    if exists:
        return False
    conn.execute(
        "INSERT INTO avoidance_flags(student_id, flag_type) VALUES(?,?)", (student_id, flag_type))
    # 回避预警触发 → 自动建 T3 交接
    st = conn.execute("SELECT class_id FROM students WHERE id=?", (student_id,)).fetchone()
    _add_handoff(student_id, st["class_id"] if st else None, "T3", "hard_to_soft",
                 trigger=f"回避预警：{FC.FLAG_TYPE_LABELS.get(flag_type, flag_type)}",
                 ai_action="平台自动标记回避模式", auto=1)
    conn.commit()
    return True


def scan_avoidance():
    """回避检测：极短作答 / 连续未提交 / 会话骤停。返回本次新建的 flag 学生 id 列表。

    检测逻辑在 analytics.detect_avoidance()（统一走泰国时区），这里只负责落库与通知。
    """
    created = []
    for student_id, flag_type in AN.detect_avoidance():
        if _add_flag(student_id, flag_type):
            created.append(student_id)
            _notify_teacher_for_flag(student_id, flag_type)
    return created


def _notify_teacher_for_flag(student_id, flag_type):
    """F6：新回避预警 → 写一条教师通知"""
    st = get_conn().execute(
        "SELECT display_name, class_id FROM students WHERE id=?", (student_id,)
    ).fetchone()
    if not st:
        return
    label = FC.FLAG_TYPE_LABELS.get(flag_type, flag_type)
    AN.notify(
        "teacher", "all", "avoidance",
        f"⚠️ {st['class_id']} {st['display_name']} 触发回避预警",
        f"类型：{label}。建议课间单独关心。",
    )


def scan_t2_handoffs():
    """F7：AI 反馈后超过 T2_IDLE_HOURS 小时仍未提交修订 → 自动建 T2 反馈授权交接"""
    created = 0
    for student_id, item_id, class_id in AN.detect_t2_pending():
        if _add_handoff(student_id, class_id, "T2", "hard_to_soft",
                        trigger=f"item={item_id} AI 反馈后 {FC.T2_IDLE_HOURS}h 未提交修订",
                        ai_action="AI 已给出反馈，等待学生审读采纳", auto=1):
            created += 1
    return created


def scan_motivation():
    """F12：连续 MOTIVATION_IDLE_DAYS 天未登录 → 给学生写一条温和鼓励通知"""
    created = 0
    for student_id, gap in AN.detect_idle_students():
        if AN.notify("student", student_id, "motivation",
                     "我们想念你了！",
                     f"已经 {gap} 天没来学中文啦，今天来复习一下吧～ 点击继续学习"):
            created += 1
    return created


def scan_phase_reminders():
    """F8：阶段结束前 PHASE_REMIND_DAYS 天 → 提醒教师准备下一阶段"""
    created = 0
    for class_id, phase, days_left in AN.detect_phase_reminders():
        if AN.notify("teacher", "all", "phase",
                     f"🔄 {class_id} 的 {phase} 阶段还有 {days_left} 天结束",
                     "请检查下一阶段配置、准备保真度清单、确认学生端通知文案。", dedup=False):
            created += 1
    return created


def run_all_scans():
    """教师打开总览时统一触发（替代定时任务，Render 免费档会休眠）"""
    return {
        "avoidance": len(scan_avoidance()),
        "t2": scan_t2_handoffs(),
        "motivation": scan_motivation(),
        "phase": scan_phase_reminders(),
    }


def maybe_create_t1(student, item_id, verdict):
    """提示阶梯用尽仍错 或 同一知识点 3 次错误 → 自动建 T1 求助升级交接"""
    conn = get_conn()
    if verdict == "correct":
        return
    hint_used = conn.execute(
        "SELECT COUNT(*) c FROM logs WHERE student_id=? AND item_id=? AND event_type='hint_request' AND hint_level>=3",
        (student["id"], item_id)).fetchone()["c"]
    err_streak = conn.execute(
        """SELECT COUNT(*) c FROM logs WHERE student_id=? AND item_id=? AND event_type='input_submit'
           AND response_json LIKE '%incorrect%' ORDER BY id DESC LIMIT 3""",
        (student["id"], item_id)).fetchone()["c"]
    if hint_used >= 1 or err_streak >= 3:
        _add_handoff(student["id"], student["class_id"], "T1", "hard_to_soft",
                     trigger=f"item={item_id} 提示阶梯用尽后仍错(hint≥3:{hint_used}) / 连续错({err_streak})",
                     ai_action="AI 提示阶梯已用尽，仍判 incorrect", auto=1)


def teacher_guard(request: Request):
    token = request.headers.get("X-Teacher-Token", "")
    if not TEACHER_PASSWORD or token != TEACHER_PASSWORD:
        raise HTTPException(401, "bad teacher token")


# ---------- 教师端日志筛选 ----------
LOG_FILTER_FIELDS = ["class_id", "student_id", "phase", "module", "event_type"]


def log_filters(args: dict) -> tuple:
    """从查询参数构建安全的 WHERE 子句 + 参数列表（仅白名单字段，参数化查询）"""
    where, params = [], []
    for f in LOG_FILTER_FIELDS:
        v = args.get(f)
        if v:
            where.append(f"l.{f}=?")
            params.append(v)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def parse_log_row(r) -> dict:
    """日志行：追加 response_json 的解析字段（verdict/error_type），供表格展示与导出"""
    d = dict(r)
    parsed = {}
    if d.get("response_json"):
        try:
            parsed = json.loads(d["response_json"])
        except (ValueError, TypeError):
            parsed = {}
    d["verdict"] = parsed.get("verdict", "")
    d["error_type"] = parsed.get("error_type", "")
    return d


FEEDBACK_CSV_FIELDS = ["verdict", "error_type", "feedback_th", "hint1_th",
                       "hint2_th", "example_zh", "answer_zh"]


def feedback_columns(response_json) -> list:
    """从 response_json 提取导出用解析列；解析失败或非提交事件时返回空串"""
    if not response_json:
        return [""] * len(FEEDBACK_CSV_FIELDS)
    try:
        parsed = json.loads(response_json)
    except (ValueError, TypeError):
        parsed = {}
    return [parsed.get(f, "") for f in FEEDBACK_CSV_FIELDS]


# ---------- 调试后门（仅本机，正式部署自动失效） ----------
def _is_loopback(request: Request) -> bool:
    host = request.url.hostname or ""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def dev_enabled(request: Request) -> bool:
    # 双保险：环境开关默认开 + 必须是本机回环地址
    if os.getenv("DEV_LOGIN_ENABLED", "1") != "1":
        return False
    return _is_loopback(request)


DEVELOPER_CHOICES = ["M4/1", "M4/2", "M5/1", "M5/2", "M6/1", "M6/2"]

DEV_LAUNCHER_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>调试入口（仅本机）</title></head><body style="font-family:sans-serif;max-width:520px;margin:40px auto;padding:0 16px">
<h2>🔧 学生端调试入口</h2>
<p style="color:#888">仅本机(localhost)可用。会写入一条测试学生记录，正式实验前请清理。</p>
<h3>选择已有学生</h3>
<select id="sel"></select>
<button id="btn-use">用此学生进入</button>
<h3>或新建测试学生</h3>
<input id="name" placeholder="显示名（如：测试A）">
<select id="cls"></select>
<button id="btn-new">新建并进入</button>
<p id="msg" style="color:#c00"></p>
<script>
const TRACK = {"M4/1":1,"M4/2":2,"M5/1":1,"M5/2":2,"M6/1":1,"M6/2":2};
const clsEl=document.getElementById("cls");
for(const c of __CLASSES__){const o=document.createElement("option");o.value=c;o.textContent=c;clsEl.appendChild(o);}
async function refresh(){const r=await fetch("/dev/students");const d=await r.json();
const sel=document.getElementById("sel");sel.innerHTML="";
d.students.forEach(s=>{const o=document.createElement("option");o.value=s.id;o.textContent=s.display_name+" ("+s.class_id+"/track"+s.track+")";sel.appendChild(o);});}
document.getElementById("btn-use").onclick=async()=>{const sid=document.getElementById("sel").value;if(!sid)return alert("请先选择学生");await login({mode:"existing",student_id:parseInt(sid)});};
document.getElementById("btn-new").onclick=async()=>{const name=document.getElementById("name").value.trim()||"测试学生";const cls=document.getElementById("cls").value;if(!cls)return alert("请选择班级");const track=TRACK[cls]||1;await login({mode:"create",display_name:name,class_id:cls,track});};
async function login(body){const msg=document.getElementById("msg");msg.textContent="";try{
const r=await fetch("/dev/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
const d=await r.json();if(!r.ok)throw new Error(d.detail||"失败");
localStorage.setItem("session_token",d.token);localStorage.setItem("class_id",d.student.class_id);localStorage.setItem("track",d.student.track);
location.href="/";}catch(e){msg.textContent=e.message;}}
refresh();
</script></body></html>""".replace("__CLASSES__", json.dumps(DEVELOPER_CHOICES))


# ---------- 请求模型 ----------
class AuthReq(BaseModel):
    id_token: str
    class_id: Optional[str] = None
    track: Optional[int] = None


class LoginReq(BaseModel):
    class_id: str
    student_no: str
    password: str


class FeedbackReq(BaseModel):
    module: str
    item_id: int
    input_text: str


class HintReq(BaseModel):
    module: str
    item_id: int
    hint_level: int  # 1=提示 2=强提示 3=例句 4=答案


class PhaseReq(BaseModel):
    class_id: str
    phase: str


# ---------- 学生端接口 ----------
@app.post("/api/auth")
async def api_auth(req: AuthReq):
    """验证 LINE id_token，绑定/查找学生，签发会话 token"""
    if not LINE_CHANNEL_ID:
        raise HTTPException(500, "LINE_CHANNEL_ID not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            LINE_VERIFY_URL,
            data={"id_token": req.id_token, "client_id": LINE_CHANNEL_ID},
        )
    if r.status_code != 200:
        raise HTTPException(401, "LINE token verify failed")
    payload = r.json()
    line_uid = payload.get("sub")
    if not line_uid:
        raise HTTPException(401, "no sub in token")
    display_name = payload.get("name") or "นักเรียน"

    conn = get_conn()
    row = conn.execute("SELECT * FROM students WHERE line_user_id=?", (line_uid,)).fetchone()
    if row is None:
        if not req.class_id:
            raise HTTPException(400, "class_id_required")
        conn.execute(
            "INSERT INTO students(line_user_id,display_name,class_id,track) VALUES(?,?,?,?)",
            (line_uid, display_name, req.class_id, req.track or 1),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM students WHERE line_user_id=?", (line_uid,)).fetchone()
    return {
        "token": make_token(row["id"]),
        "student": dict(row),
    }


@app.post("/api/login")
def api_login(req: LoginReq):
    """表单登录：班级 + 学号 + 密码。

    首次登录自动建档，**初始密码 = 学号**；建档后学生可在「个人资料」里
    改密码、改姓名、传头像、写签名。老账号若还没设过 password，回退用学号校验。
    """
    if req.class_id not in CLASS_CONTENT:
        raise HTTPException(400, "unknown class")
    sno = (req.student_no or "").strip()
    pwd = (req.password or "").strip()
    if not sno or not pwd:
        raise HTTPException(400, "学号和密码不能为空")
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM students WHERE class_id=? AND student_no=?",
        (req.class_id, sno)).fetchone()
    if row is not None:
        expected = row["password"] or row["student_no"] or ""
        if pwd != expected:
            raise HTTPException(401, "密码错误")
        student = row
    else:
        # 首次登录：初始密码必须等于学号
        if pwd != sno:
            raise HTTPException(401, "首次登录请用学号作为密码")
        track = CLASS_CONTENT[req.class_id]
        cur = conn.execute(
            """INSERT INTO students(line_user_id,display_name,class_id,track,student_no,password)
               VALUES(?,?,?,?,?,?)""",
            (f"sn-{req.class_id}-{sno}", sno, req.class_id, track, sno, sno))
        conn.commit()
        student = conn.execute("SELECT * FROM students WHERE id=?", (cur.lastrowid,)).fetchone()
    # F12：距上次登录超过阈值 → 先写一条温和鼓励通知（必须在本次登录入库前判断）
    prev = conn.execute(
        """SELECT MAX(created_at) m FROM logs
           WHERE student_id=? AND event_type='student_login'""",
        (student["id"],)).fetchone()["m"]
    if prev:
        gap = conn.execute(
            f"SELECT CAST(julianday(date('now','{FC.TZ_OFFSET}')) - julianday(date(?,'{FC.TZ_OFFSET}')) AS INT)",
            (prev,)).fetchone()[0]
        if gap is not None and gap >= FC.MOTIVATION_IDLE_DAYS:
            AN.notify("student", student["id"], "motivation", "我们想念你了！",
                      f"已经 {gap} 天没来学中文啦，今天来复习一下吧～")
    log_event(student, "student_login")
    # F1：登录时懒生成本周报告（同周已生成则直接读缓存）
    # 中文班用独立的语文周报（native_report），不生成二语班口径的周报
    if not is_native(student["class_id"]):
        try:
            AN.generate_weekly_report(student["id"])
        except Exception:
            pass  # 报告生成失败不能阻断登录
    return {"token": make_token(student["id"]), "student": dict(student),
            "ui_mode": "native" if is_native(student["class_id"]) else "l2",
            "class_name": CLASS_LABELS.get(student["class_id"], student["class_id"]),
            "unread": AN.unread_count("student", student["id"])}


@app.get("/api/me")
def api_me(request: Request):
    sid = parse_token(request)
    student = get_student(sid)
    out = dict(student)
    out["ui_mode"] = "native" if is_native(student["class_id"]) else "l2"
    out["class_name"] = CLASS_LABELS.get(student["class_id"], student["class_id"])
    return out


# ---------- 个人资料 ----------

class ProfileReq(BaseModel):
    display_name: Optional[str] = None
    signature: Optional[str] = None


class AvatarReq(BaseModel):
    filename: str = ""
    file_b64: str


class PasswordReq(BaseModel):
    old_password: str
    new_password: str


def _public_profile(student) -> dict:
    d = dict(student)
    d.pop("password", None)      # 密码绝不外传
    d.pop("line_user_id", None)
    return d


@app.get("/api/profile")
def api_profile_get(request: Request):
    return _public_profile(get_student(parse_token(request)))


@app.post("/api/profile")
def api_profile_set(req: ProfileReq, request: Request):
    """更新姓名 / 个性签名"""
    student = get_student(parse_token(request))
    fields, params = [], []
    if req.display_name is not None:
        n = req.display_name.strip()
        if not n:
            raise HTTPException(400, "姓名不能为空")
        if len(n) > 40:
            raise HTTPException(400, "姓名最多 40 个字")
        fields.append("display_name=?")
        params.append(n)
    if req.signature is not None:
        s = req.signature.strip()
        if len(s) > 80:
            raise HTTPException(400, "个性签名最多 80 个字")
        fields.append("signature=?")
        params.append(s)
    if not fields:
        raise HTTPException(400, "没有要更新的内容")
    params.append(student["id"])
    conn = get_conn()
    conn.execute("UPDATE students SET %s WHERE id=?" % ",".join(fields), params)
    conn.commit()
    return {"ok": True, "profile": _public_profile(
        conn.execute("SELECT * FROM students WHERE id=?", (student["id"],)).fetchone())}


@app.post("/api/profile/avatar")
def api_profile_avatar(req: AvatarReq, request: Request):
    """上传头像。前端已用 canvas 缩到 256px，这里再校验格式与大小。"""
    student = get_student(parse_token(request))
    raw = req.file_b64 or ""
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(400, "图片数据有误，请重新选择")
    if not data:
        raise HTTPException(400, "图片为空")
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "图片太大（请控制在 2MB 内）")
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:3] == b"GIF":
        ext = "gif"
    else:
        raise HTTPException(400, "只支持 JPG / PNG / GIF 图片")
    d = os.path.join(UPLOAD_DIR, "avatars")
    os.makedirs(d, exist_ok=True)
    fname = "av_%d_%d.%s" % (student["id"], int(time.time()), ext)
    with open(os.path.join(d, fname), "wb") as f:
        f.write(data)
    url = "/uploads/avatars/" + fname
    conn = get_conn()
    conn.execute("UPDATE students SET avatar_url=? WHERE id=?", (url, student["id"]))
    conn.commit()
    log_event(student, "avatar_upload")
    return {"ok": True, "avatar_url": url}


@app.post("/api/profile/password")
def api_profile_password(req: PasswordReq, request: Request):
    """改密码，需验证原密码。"""
    student = get_student(parse_token(request))
    old = (req.old_password or "").strip()
    new = (req.new_password or "").strip()
    expected = student["password"] or student["student_no"] or ""
    if old != expected:
        raise HTTPException(401, "原密码错误")
    if len(new) < 4:
        raise HTTPException(400, "新密码至少 4 位")
    if len(new) > 32:
        raise HTTPException(400, "新密码最多 32 位")
    if new == old:
        raise HTTPException(400, "新密码和原密码相同")
    conn = get_conn()
    conn.execute("UPDATE students SET password=? WHERE id=?", (new, student["id"]))
    conn.commit()
    log_event(student, "password_change")
    return {"ok": True}


@app.get("/api/phase")
def api_phase(request: Request):
    student = get_student(parse_token(request))
    phase = current_phase(student["class_id"])
    modules = [
        {"key": m, "label": MODULE_LABELS[m], "available": m in PHASE_MODULES.get(phase, [])}
        for m in MODULES
    ]
    return {"phase": phase, "phase_label": PHASE_LABELS.get(phase, phase),
            "note": PHASE_NOTE_TH.get(phase, ""), "modules": modules,
            "sections": phase_sections(phase)}


@app.get("/api/items")
def api_items(request: Request, module: str):
    student = get_student(parse_token(request))
    phase = current_phase(student["class_id"])
    if module not in PHASE_MODULES.get(phase, []):
        raise HTTPException(403, "module not available in current phase")
    if module not in MODULES:
        raise HTTPException(404, "unknown module")
    rows = content_db().execute(
        "SELECT id,prompt_zh,prompt_th FROM items WHERE module=? AND book=? ORDER BY id",
        (module, student_book(student)),
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


def _norm_answer(s: str) -> str:
    """归一化答案用于精确比对：只保留字词，忽略标点、空白、大小写。

    "你好！" / "你好" / " 你好。 " 视为同一个答案。
    """
    return re.sub(r"[\W_]+", "", (s or "").lower())


@app.post("/api/feedback")
async def api_feedback(req: FeedbackReq, request: Request):
    student = get_student(parse_token(request))
    native = is_native(student["class_id"])
    phase = current_phase(student["class_id"])
    if native:
        item = content_db().execute(
            "SELECT * FROM items WHERE id=?", (req.item_id,)).fetchone()
    else:
        if req.module not in PHASE_MODULES.get(phase, []):
            raise HTTPException(403, "module not available in current phase")
        if req.module not in MODULES:
            raise HTTPException(404, "unknown module")
        item = content_db().execute(
            "SELECT * FROM items WHERE id=? AND module=?", (req.item_id, req.module)
        ).fetchone()
    if not item:
        raise HTTPException(404, "item not found")

    # 精确匹配 → 秒判，不调 AI。常见答案占大多数，省 20-30 秒且不会失败。
    # 答得不一样时才交给 AI 判断是否"部分正确"。
    mine = _norm_answer(req.input_text)
    std = _norm_answer(item["answer"])
    if mine and std and mine == std:
        result = {
            "verdict": "correct",
            "error_type": "none",
            "feedback_zh": "完全正确！答到点子上了。" if native else "",
            "feedback_th": "ถูกต้อง! ตอบตรงกับที่เรียนมาเลย เก่งมาก",
            "hint1_zh": "", "hint2_zh": "", "hint1_th": "", "hint2_th": "",
            "example_zh": "", "answer_zh": item["answer"] or "",
            "graded_by": "exact_match",
        }
        latency = 0
    else:
        t0 = time.time()
        if native:
            result = await chat_json(
                feedback_messages_zh(dict(item), req.input_text), provider="mm")
            if not result.get("feedback_zh"):
                result["feedback_zh"] = "AI 老师暂时没连上，先把你的答案记下来，稍后再看反馈。"
        else:
            result = await chat_json(feedback_messages(dict(item), req.input_text, req.module), provider="mm")
        result.setdefault("graded_by", "ai")
        latency = int((time.time() - t0) * 1000)
    log_event(student, "input_submit", req.module, req.item_id, req.input_text,
              ai_latency_ms=latency, response_json=result)
    conn = get_conn()
    conn.execute(
        "INSERT INTO feedback_events(student_id, feedback_time, adopted) VALUES(?, datetime('now'), ?)",
        (student["id"], "partial" if result.get("verdict") in ("partial", "incorrect") else "full"))
    conn.commit()
    # 中文班同时维护错题本（练习/测验主观题）
    if native and result.get("verdict") in ("partial", "incorrect"):
        upsert_error(student["id"], "exercise", req.item_id,
                     item["q_type"] if "q_type" in item.keys() else None,
                     item["tags"] if "tags" in item.keys() else None)
    # 回避检测：极短作答（仅二语班研究口径）
    if not native and len(req.input_text) < FC.SHORT_ANSWER_MIN:
        _add_flag(student["id"], "too_short_answer")
    # 交接自动标记：T1 求助升级（仅二语班）
    if not native:
        maybe_create_t1(student, req.item_id, result.get("verdict", ""))
    return result


@app.post("/api/hint")
def api_hint(req: HintReq, request: Request):
    """记录提示阶梯的揭盖事件（hint_level: 1-4）"""
    student = get_student(parse_token(request))
    log_event(student, "hint_request", req.module, req.item_id, None, hint_level=req.hint_level)
    conn = get_conn()
    conn.execute(
        "INSERT INTO hint_events(student_id, hint_level, task_id) VALUES(?,?,?)",
        (student["id"], req.hint_level, req.item_id))
    conn.commit()
    return {"ok": True}


class AsrReq(BaseModel):
    audio_b64: str
    sample_rate: int = 16000


@app.post("/api/asr")
async def api_asr(req: AsrReq, request: Request):
    """录音转文字。服务器端识别，不依赖浏览器语音 API（LINE 里也能用）。"""
    student = get_student(parse_token(request))
    raw = req.audio_b64 or ""
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(400, "音频数据有误，请重新录音")
    if len(data) < 1000:
        raise HTTPException(400, "录音太短，请按住说完一整句")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "录音太长（最多 10MB）")
    text = await ASR.transcribe(data, rate=req.sample_rate or 16000)
    log_event(student, "asr_transcribe", input_text=text or None,
              detail={"bytes": len(data)})
    return {"text": text}


@app.get("/api/progress")
def api_progress(request: Request):
    student = get_student(parse_token(request))
    conn = get_conn()
    sid = student["id"]
    total_submit = conn.execute(
        "SELECT COUNT(*) c FROM logs WHERE student_id=? AND event_type='input_submit'", (sid,)
    ).fetchone()["c"]
    total_hint = conn.execute(
        "SELECT COUNT(*) c FROM logs WHERE student_id=? AND event_type='hint_request'", (sid,)
    ).fetchone()["c"]
    hint_answer = conn.execute(
        "SELECT COUNT(*) c FROM logs WHERE student_id=? AND event_type='hint_request' AND hint_level=4", (sid,)
    ).fetchone()["c"]
    days = conn.execute(
        "SELECT COUNT(DISTINCT date(created_at)) c FROM logs WHERE student_id=?", (sid,)
    ).fetchone()["c"]
    by_module = conn.execute(
        """SELECT module, COUNT(*) c FROM logs
           WHERE student_id=? AND event_type='input_submit' GROUP BY module""", (sid,)
    ).fetchall()
    return {
        "display_name": student["display_name"],
        "submissions": total_submit,
        "hint_requests": total_hint,
        "answer_views": hint_answer,
        "active_days": days,
        "by_module": {r["module"]: r["c"] for r in by_module},
    }


@app.post("/api/session_start")
def api_session_start(request: Request):
    student = get_student(parse_token(request))
    log_event(student, "session_start")
    return {"ok": True}


# ---------- 按课学习接口 ----------
@app.get("/api/lessons")
def api_lessons(request: Request):
    student = get_student(parse_token(request))
    class_id = student["class_id"]
    phase = current_phase(class_id)
    book = student_book(student)
    conn = content_db()
    assigned = assigned_lessons(class_id)

    if is_native(class_id):
        # 中文班：常规教学，全部课次可见；按单元分组返回
        rows = conn.execute(
            """SELECT lesson_no, title, unit, lesson_type FROM lessons
               WHERE book=? ORDER BY CAST(lesson_no AS INTEGER)""", (book,)).fetchall()
        units = {}
        for r in rows:
            u = r["unit"] or 0
            units.setdefault(u, []).append({
                "lesson_no": r["lesson_no"], "title": r["title"],
                "type": r["lesson_type"] or "reading"})
        writings = conn.execute(
            "SELECT id, unit, title FROM writing_tasks WHERE book=? ORDER BY unit, id",
            (book,)).fetchall()
        wmap = {}
        for w in writings:
            wmap.setdefault(w["unit"] or 0, []).append(dict(w))
        return {
            "book": book, "ui_mode": "native",
            "sections": phase_sections(phase, class_id),
            "units": [{"unit": u, "lessons": ls, "writings": wmap.get(u, [])}
                      for u, ls in sorted(units.items())],
            "lessons": [dict(r) for r in rows],   # 兼容旧前端
        }

    if assigned:
        ph = ",".join("?" * len(assigned))
        lessons = conn.execute(
            f"SELECT lesson_no, title FROM lessons WHERE book=? AND lesson_no IN ({ph}) ORDER BY CAST(lesson_no AS INTEGER)",
            (book, *sorted(assigned, key=int)),
        ).fetchall()
    else:
        lessons = []  # 教师未分配 → 学生看不到任何课
    return {"book": book, "ui_mode": "l2", "sections": phase_sections(phase),
            "lessons": [dict(r) for r in lessons]}


@app.get("/api/lesson")
def api_lesson(request: Request, lesson: str):
    student = get_student(parse_token(request))
    class_id = student["class_id"]
    phase = current_phase(class_id)
    assigned = assigned_lessons(class_id)
    if not is_native(class_id) and lesson not in assigned:
        raise HTTPException(403, "lesson not assigned")
    book = student_book(student)
    conn = content_db()

    if is_native(class_id):
        lesson_row = conn.execute(
            "SELECT lesson_no, title, unit FROM lessons WHERE book=? AND lesson_no=?",
            (book, lesson)).fetchone()
        if not lesson_row:
            raise HTTPException(404, "lesson not found")
        unit = lesson_row["unit"]
        hanzi = [dict(r) for r in conn.execute(
            """SELECT id, char, pinyin, radical, strokes, structure, words,
                      require_write, difficulty_tags, note_zh
               FROM hanzi WHERE book=? AND lesson_no=? ORDER BY id""",
            (book, lesson)).fetchall()]
        vocab = [dict(r) for r in conn.execute(
            """SELECT id, word, pinyin, meaning_zh, example_zh, note_zh
               FROM vocab WHERE book=? AND lesson_no=? ORDER BY id""",
            (book, lesson)).fetchall()]
        text_row = conn.execute(
            "SELECT id, title, body_zh FROM texts WHERE book=? AND lesson_no=?",
            (book, lesson)).fetchone()
        text = dict(text_row) if text_row else None
        grammar = [dict(r) for r in conn.execute(
            """SELECT point, explanation_zh, example_zh FROM grammar_points
               WHERE book=? AND lesson_no=? ORDER BY id""",
            (book, lesson)).fetchall()]
        exercises = [dict(r) for r in conn.execute(
            """SELECT id, module, prompt_zh, answer, explanation_th, q_type,
                      score, passage_id, tags
               FROM items WHERE book=? AND lesson_no=? ORDER BY module, id""",
            (book, lesson)).fetchall()]
        exam = [dict(r) for r in conn.execute(
            """SELECT id, question_zh, option_a, option_b, option_c, option_d,
                      score, q_type, passage_id
               FROM exams WHERE book=? AND lesson_no=? ORDER BY id""",
            (book, lesson)).fetchall()]
        poems = [dict(r) for r in conn.execute(
            """SELECT id, title, author, dynasty, genre, body, notes_json,
                      translation, recite_required, dictation_points
               FROM poems WHERE book=? AND lesson_no=? ORDER BY id""",
            (book, lesson)).fetchall()]
        for p in poems:
            try:
                p["notes"] = json.loads(p.pop("notes_json") or "[]")
            except Exception:
                p["notes"] = []
        writing = []
        if unit:
            writing = [dict(r) for r in conn.execute(
                "SELECT id, unit, title, prompt, requirements, min_words FROM writing_tasks "
                "WHERE book=? AND unit=? ORDER BY id", (book, unit)).fetchall()]
        return {
            "lesson_no": lesson, "title": lesson_row["title"], "unit": unit,
            "book": book, "ui_mode": "native",
            "sections": phase_sections(phase, class_id),
            "hanzi": hanzi, "vocab": vocab, "text": text, "grammar": grammar,
            "exercises": exercises, "exam": exam, "poems": poems, "writing": writing,
        }

    vocab = []
    for r in conn.execute(
            "SELECT id,word,pinyin,meaning_th,example_zh FROM vocab WHERE book=? AND lesson_no=? ORDER BY id",
            (book, lesson)).fetchall():
        d = dict(r)
        d["audio_url"] = f"/audio/vocab/{r['id']}.mp3"
        vocab.append(d)
    text_row = conn.execute(
        "SELECT id,title,body_zh,body_th FROM texts WHERE book=? AND lesson_no=?",
        (book, lesson)).fetchone()
    text = dict(text_row) if text_row else None
    if text:
        text["audio_url"] = f"/audio/texts/{text_row['id']}.mp3"
    grammar = [dict(r) for r in conn.execute(
        "SELECT point,explanation_th,example_zh,example_th FROM grammar_points WHERE book=? AND lesson_no=? ORDER BY id",
        (book, lesson)).fetchall()]
    exercises = [dict(r) for r in conn.execute(
        "SELECT id,module,prompt_zh,prompt_th,answer,explanation_th FROM items WHERE book=? AND lesson_no=? ORDER BY module,id",
        (book, lesson)).fetchall()]
    exam = [dict(r) for r in conn.execute(
        "SELECT id,question_zh,question_th,option_a,option_b,option_c,option_d FROM exams WHERE book=? AND lesson_no=? ORDER BY id",
        (book, lesson)).fetchall()]
    return {
        "lesson_no": lesson, "book": book, "sections": phase_sections(phase),
        "vocab": vocab, "text": text, "grammar": grammar,
        "exercises": exercises, "exam": exam,
    }


class ExamSubmitReq(BaseModel):
    lesson_no: str
    answers: list  # [{exam_id:int, chosen:"A"|"B"|"C"|"D"}]


@app.post("/api/exam/submit")
def api_exam_submit(req: ExamSubmitReq, request: Request):
    student = get_student(parse_token(request))
    native = is_native(student["class_id"])
    phase = current_phase(student["class_id"])
    if not native and phase not in INTERACTIVE_PHASES:
        raise HTTPException(403, "exam not available in current phase")
    if not native and req.lesson_no not in assigned_lessons(student["class_id"]):
        raise HTTPException(403, "lesson not assigned")
    book = student_book(student)
    conn = content_db()
    answer_map = {a.get("exam_id"): (a.get("chosen") or "").upper() for a in req.answers}
    rows = conn.execute(
        "SELECT * FROM exams WHERE book=? AND lesson_no=? ORDER BY id", (book, req.lesson_no)
    ).fetchall()
    results, correct_count = [], 0
    for r in rows:
        chosen = answer_map.get(r["id"], "")
        correct = bool(chosen) and chosen == r["correct"]
        if correct:
            correct_count += 1
        results.append({
            "exam_id": r["id"], "question_zh": r["question_zh"],
            "chosen": chosen, "correct_letter": r["correct"],
            "is_correct": correct,
            "explanation": r["explanation_th"], "explanation_th": r["explanation_th"],
            "q_type": r["q_type"] if "q_type" in r.keys() else None,
        })
        if native and not correct and chosen:
            upsert_error(student["id"], "exam", r["id"],
                         r["q_type"] if "q_type" in r.keys() else None, None)
    total = len(rows)
    score = round(correct_count / total * 100) if total else 0
    log_event(student, "exam_submit", input_text=None,
              response_json={"lesson_no": req.lesson_no, "score": score,
                             "total": total, "correct": correct_count})
    return {"score": score, "total": total, "correct": correct_count, "results": results}


# ---------- 询问 AI 与发音评测 ----------
class AskReq(BaseModel):
    question: str
    lang: str = "th"  # th / en / zh


@app.post("/api/ask")
async def api_ask(req: AskReq, request: Request):
    student = get_student(parse_token(request))
    native = is_native(student["class_id"])
    phase = current_phase(student["class_id"])
    if not native and phase not in INTERACTIVE_PHASES:
        raise HTTPException(403, "AI not available in current phase")
    q = req.question.strip()
    if not q:
        raise HTTPException(400, "empty question")
    if native:
        # 中文班：始终中文回答，走中文模型
        answer = await chat_text(ask_messages_zh(q), provider="mm")
    else:
        # 分流：中文提问用 MiniMax，泰/英用 DeepSeek
        provider = "mm" if req.lang == "zh" else None
        answer = await chat_text(ask_messages(q, req.lang), provider=provider)
    log_event(student, "ask_question", input_text=q,
              response_json={"lang": "zh" if native else req.lang, "answer": answer})
    return {"answer": answer}


class PronounceReq(BaseModel):
    target_text: str
    recognized_text: str


@app.post("/api/pronounce")
async def api_pronounce(req: PronounceReq, request: Request):
    student = get_student(parse_token(request))
    native = is_native(student["class_id"])
    phase = current_phase(student["class_id"])
    if not native and phase not in INTERACTIVE_PHASES:
        raise HTTPException(403, "AI not available in current phase")
    if not req.target_text.strip():
        raise HTTPException(400, "empty target")
    msgs = (pronounce_messages_zh(req.target_text, req.recognized_text)
            if native else pronounce_messages(req.target_text, req.recognized_text))
    feedback = await chat_text(msgs, provider="mm")
    log_event(student, "pronounce_check",
              input_text=req.recognized_text,
              response_json={"target": req.target_text, "feedback": feedback})
    return {"feedback": feedback, "recognized": req.recognized_text}


# ---------- 教师端接口 ----------
@app.get("/teacher/data")
def teacher_data(request: Request):
    teacher_guard(request)
    q = request.query_params
    conn = get_conn()
    classes = [
        dict(r) for r in conn.execute(
            """SELECT s.class_id, COUNT(*) n_students, COALESCE(p.phase,'P0') phase
               FROM students s LEFT JOIN phase_config p ON p.class_id=s.class_id
               GROUP BY s.class_id"""
        ).fetchall()
    ]
    students = [
        dict(r) for r in conn.execute(
            "SELECT id,display_name,class_id,track,created_at FROM students ORDER BY class_id,id"
        ).fetchall()
    ]

    where, params = log_filters(q)
    total = conn.execute(
        "SELECT COUNT(*) c FROM logs l" + where, params
    ).fetchone()["c"]
    per_page = min(int(q.get("per_page", 100)), 500)
    page = max(int(q.get("page", 1)), 1)
    pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, pages)
    rows = conn.execute(
        """SELECT l.*, s.display_name FROM logs l
           LEFT JOIN students s ON s.id=l.student_id""" + where +
        " ORDER BY l.id DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    logs = [parse_log_row(r) for r in rows]
    return {"classes": classes, "students": students, "logs": logs,
            "total": total, "page": page, "per_page": per_page, "pages": pages,
            "phases": list(PHASE_LABELS.keys()), "phase_labels": PHASE_LABELS}


@app.post("/teacher/phase")
def teacher_phase(req: PhaseReq, request: Request):
    teacher_guard(request)
    if req.phase not in PHASE_MODULES:
        raise HTTPException(400, "unknown phase")
    conn = get_conn()
    ai_enabled = 1 if req.phase in FC.INTERACTIVE_PHASES_SET else 0
    teacher_resp = 1 if req.phase in ("P2", "P3") else 0
    conn.execute(
        """INSERT INTO phase_config(class_id,phase,updated_at,ai_enabled,teacher_response_allowed,start_time,end_time)
           VALUES(?,?,datetime('now'),?,?,datetime('now'),NULL)
           ON CONFLICT(class_id) DO UPDATE SET phase=excluded.phase, updated_at=excluded.updated_at,
             ai_enabled=excluded.ai_enabled, teacher_response_allowed=excluded.teacher_response_allowed,
             start_time=excluded.start_time""",
        (req.class_id, req.phase, ai_enabled, teacher_resp),
    )
    conn.commit()
    log_teacher_action("phase_switch", target_class=req.class_id,
                       detail={"to_phase": req.phase, "ai_enabled": ai_enabled,
                               "teacher_response_allowed": teacher_resp})
    return {"ok": True, "class_id": req.class_id, "phase": req.phase,
            "ai_enabled": ai_enabled, "teacher_response_allowed": teacher_resp}


class LessonAssignReq(BaseModel):
    class_id: str
    lessons: list  # [lesson_no...]


@app.get("/teacher/lesson_assign")
def teacher_lesson_assign(request: Request, class_id: str):
    teacher_guard(request)
    book = CLASS_CONTENT.get(class_id, "M41")
    conn = content_db()
    lessons = conn.execute(
        "SELECT lesson_no, title FROM lessons WHERE book=? ORDER BY CAST(lesson_no AS INTEGER)", (book,)
    ).fetchall()
    assigned = assigned_lessons(class_id)
    return {"class_id": class_id, "book": book,
            "lessons": [{"lesson_no": l["lesson_no"], "title": l["title"],
                         "assigned": l["lesson_no"] in assigned} for l in lessons]}


@app.post("/teacher/lesson_assign")
def teacher_lesson_assign_set(req: LessonAssignReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    conn.execute("DELETE FROM class_lessons WHERE class_id=?", (req.class_id,))
    book = CLASS_CONTENT.get(req.class_id, "M41")
    for ln in req.lessons:
        conn.execute(
            "INSERT INTO class_lessons(class_id, book, lesson_no) VALUES(?,?,?)",
            (req.class_id, book, str(ln)))
    conn.commit()
    log_teacher_action("lesson_assign", target_class=req.class_id,
                       detail={"lessons": req.lessons})
    return {"ok": True, "assigned": len(req.lessons)}


@app.get("/teacher/export.csv")
def teacher_export(request: Request):
    teacher_guard(request)
    where, params = log_filters(request.query_params)
    conn = get_conn()
    rows = conn.execute(
        """SELECT l.id, s.display_name, l.student_id, l.class_id, l.phase, l.module,
                  l.item_id, l.event_type, l.input_text, l.hint_level,
                  l.ai_latency_ms, l.response_json, l.created_at
           FROM logs l LEFT JOIN students s ON s.id=l.student_id""" + where +
        " ORDER BY l.id", params
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["log_id", "student_name", "student_id", "class_id", "phase", "module",
         "item_id", "event_type", "input_text", "hint_level", "ai_latency_ms", "created_at"]
        + FEEDBACK_CSV_FIELDS
    )
    for r in rows:
        writer.writerow([r["id"], r["display_name"],
                         r["student_id"], r["class_id"], r["phase"], r["module"], r["item_id"],
                         r["event_type"], r["input_text"], r["hint_level"], r["ai_latency_ms"],
                         r["created_at"]]
                        + feedback_columns(r["response_json"]))
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=logs_export.csv"},
    )


# ---------- 教师端研究接口 ----------
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.get("/teacher/overview")
def teacher_overview(request: Request):
    teacher_guard(request)
    # F6/F7/F8/F12：打开总览时统一跑一遍扫描（替代定时任务，Render 免费档会休眠）
    try:
        run_all_scans()
    except Exception:
        pass  # 扫描失败不能阻断总览
    conn = get_conn()
    classes = []
    for r in conn.execute(
            """SELECT p.class_id, p.phase, p.ai_enabled, p.teacher_response_allowed,
               (SELECT COUNT(*) FROM students s WHERE s.class_id=p.class_id) n_students
               FROM phase_config p ORDER BY p.class_id""").fetchall():
        cid = r["class_id"]
        active = conn.execute("SELECT COUNT(DISTINCT student_id) c FROM logs WHERE class_id=? AND created_at>=datetime('now','-7 days')", (cid,)).fetchone()["c"]
        weekly = conn.execute("SELECT COUNT(*) c FROM logs WHERE class_id=? AND created_at>=datetime('now','-7 days')", (cid,)).fetchone()["c"]
        open_h = conn.execute("SELECT COUNT(*) c FROM handoff_events WHERE class_id=? AND closed=0", (cid,)).fetchone()["c"]
        classes.append({"class_id": cid, "phase": r["phase"], "ai_enabled": r["ai_enabled"],
                        "teacher_response_allowed": r["teacher_response_allowed"], "n_students": r["n_students"],
                        "active_7d": active, "weekly_interactions": weekly, "open_handoffs": open_h})
    return {"classes": classes,
            "open_handoffs_total": conn.execute("SELECT COUNT(*) c FROM handoff_events WHERE closed=0").fetchone()["c"],
            "open_avoidance": conn.execute("SELECT COUNT(*) c FROM avoidance_flags WHERE resolved=0").fetchone()["c"],
            "unread_notifications": AN.unread_count("teacher", "all"),
            "phases": FC.PHASE_LIST, "phase_labels": FC.PHASE_LABELS}


@app.get("/teacher/student_detail")
def teacher_student_detail(request: Request, student_id: int, phase: str = ""):
    teacher_guard(request)
    conn = get_conn()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        raise HTTPException(404, "student not found")
    pw = " AND phase=?" if phase else ""
    pp = [phase] if phase else []
    base = [student_id] + pp
    sessions = conn.execute(f"SELECT COUNT(*) c FROM logs WHERE student_id=? AND event_type='session_start'{pw}", base).fetchone()["c"]
    submits = conn.execute(f"SELECT COUNT(*) c FROM logs WHERE student_id=? AND event_type='input_submit'{pw}", base).fetchone()["c"]
    hints = {r["hint_level"]: r["c"] for r in conn.execute(
        f"SELECT hint_level, COUNT(*) c FROM logs WHERE student_id=? AND event_type='hint_request'{pw} GROUP BY hint_level", base).fetchall()}
    # 主动提问：原先统计 'followup_question'，但该 event_type 从未被写入过（恒为 0）。
    # 改用真实存在的 'ask_question'（询问 AI 板块的提问）。
    follows = conn.execute(f"SELECT COUNT(*) c FROM logs WHERE student_id=? AND event_type='ask_question'{pw}", base).fetchone()["c"]
    adoption = {"full": 0, "partial": 0, "rejected": 0}
    for r in conn.execute("SELECT adopted, COUNT(*) c FROM feedback_events WHERE student_id=? GROUP BY adopted", (student_id,)).fetchall():
        if r["adopted"] in adoption:
            adoption[r["adopted"]] = r["c"]
    total_fb = sum(adoption.values())
    # F4：反馈决策理由分布（H3 能动性中介的行为证据）
    decisions = {r["reason_type"]: r["c"] for r in conn.execute(
        "SELECT reason_type, COUNT(*) c FROM decision_reasons WHERE student_id=? GROUP BY reason_type",
        (student_id,)).fetchall()}
    return {"student_id": student_id, "display_name": student["display_name"], "class_id": student["class_id"],
            "sessions": sessions, "submits": submits, "hints_by_level": hints, "followup_count": follows,
            "adoption": adoption, "adoption_rate": round(adoption["full"] / total_fb * 100) if total_fb else None,
            "decisions": decisions, "decision_labels": FC.DECISION_REASON_LABELS,
            "flag_labels": FC.FLAG_TYPE_LABELS}


@app.get("/teacher/handoffs")
def teacher_handoffs(request: Request, t_type: str = "", class_id: str = "", student_id: int = 0, open_only: int = 0):
    teacher_guard(request)
    conn = get_conn()
    where, params = [], []
    if t_type: where.append("h.t_type=?"); params.append(t_type)
    if class_id: where.append("h.class_id=?"); params.append(class_id)
    if student_id: where.append("h.student_id=?"); params.append(student_id)
    if open_only: where.append("h.closed=0")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        """SELECT h.*, s.display_name FROM handoff_events h
           LEFT JOIN students s ON s.id=h.student_id""" + w + " ORDER BY h.id DESC LIMIT 500", params).fetchall()
    return {"handoffs": [dict(r) for r in rows], "t_labels": FC.T_TYPE_LABELS,
            "direction_labels": FC.HANDOFF_DIRECTION_LABELS}


class HandoffReq(BaseModel):
    student_id: int
    class_id: str = ""
    t_type: str
    direction: str = "hard_to_soft"
    trigger: str = ""
    ai_action: str = ""
    teacher_action: str = ""
    source: str = "manual_teacher"


@app.post("/teacher/handoff")
def teacher_handoff_create(req: HandoffReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO handoff_events(student_id,class_id,t_type,direction,trigger,ai_action,teacher_action,source)
           VALUES(?,?,?,?,?,?,?,?)""",
        (req.student_id, req.class_id or None, req.t_type, req.direction,
         req.trigger, req.ai_action, req.teacher_action, req.source))
    conn.commit()
    if req.t_type in ("T2", "T4"):
        log_teacher_action("calibrate_feedback_S2" if req.t_type == "T2" else "inject_practice_T4",
                           target_class=req.class_id or None, detail={"handoff_id": cur.lastrowid, "t_type": req.t_type})
    return {"ok": True, "id": cur.lastrowid}


class HandoffRespondReq(BaseModel):
    teacher_action: str = ""
    closed: bool = True


@app.post("/teacher/handoff/{hid}/respond")
def teacher_handoff_respond(hid: int, req: HandoffRespondReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    row = conn.execute("SELECT * FROM handoff_events WHERE id=?", (hid,)).fetchone()
    if not row:
        raise HTTPException(404, "handoff not found")
    conn.execute(
        "UPDATE handoff_events SET teacher_action=?, closed=?, closed_at=datetime('now') WHERE id=?",
        (req.teacher_action, 1 if req.closed else 0, hid))
    conn.commit()
    return {"ok": True}


@app.get("/teacher/avoidance")
def teacher_avoidance(request: Request, resolved: int = 0):
    teacher_guard(request)
    conn = get_conn()
    w = "WHERE af.resolved=0" if resolved == 0 else ""
    rows = conn.execute(
        f"""SELECT af.*, s.display_name, s.class_id FROM avoidance_flags af
            LEFT JOIN students s ON s.id=af.student_id {w}
            ORDER BY af.triggered_at DESC LIMIT 500""").fetchall()
    return {"flags": [dict(r) for r in rows], "flag_labels": FC.FLAG_TYPE_LABELS}


@app.post("/teacher/avoidance/scan")
def teacher_avoidance_scan(request: Request):
    teacher_guard(request)
    created = scan_avoidance()
    return {"ok": True, "created": created}


@app.post("/teacher/avoidance/{fid}/resolve")
def teacher_avoidance_resolve(fid: int, request: Request):
    teacher_guard(request)
    conn = get_conn()
    cur = conn.execute("UPDATE avoidance_flags SET resolved=1, resolved_at=datetime('now') WHERE id=?", (fid,))
    conn.commit()
    return {"ok": True, "updated": cur.rowcount}


class InjectReq(BaseModel):
    class_id: str
    content: str
    note: str = ""


@app.post("/teacher/inject")
def teacher_inject(req: InjectReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    conn.execute(
        """INSERT INTO handoff_events(class_id,t_type,direction,trigger,ai_action,teacher_action,source)
           VALUES(?,?,?,?,?,?,?)""",
        (req.class_id, "T4", "soft_to_hard", "教师在软区发现共性问题",
         "AI 后续操练分派", req.content, "manual_teacher"))
    conn.commit()
    log_teacher_action("inject_practice_T4", target_class=req.class_id,
                       detail={"content": req.content, "note": req.note})
    return {"ok": True}


@app.get("/teacher/fidelity")
def teacher_fidelity(request: Request, class_id: str, phase: str, lesson_date: str = ""):
    teacher_guard(request)
    if phase not in FC.FIDELITY_ITEMS:
        raise HTTPException(400, "unknown phase")
    conn = get_conn()
    items = FC.FIDELITY_ITEMS[phase]
    rows = conn.execute(
        """SELECT * FROM fidelity_checklists WHERE class_id=? AND phase=?
           ORDER BY filled_at DESC""", (class_id, phase)).fetchall()
    return {"phase": phase, "items": items, "submitted": [dict(r) for r in rows],
            "phases": FC.PHASE_LIST, "phase_labels": FC.PHASE_LABELS}


class FidelityReq(BaseModel):
    class_id: str
    phase: str
    lesson_date: str
    checked: list
    filled_by: str = "teacher"


@app.post("/teacher/fidelity")
def teacher_fidelity_create(req: FidelityReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    if req.phase not in FC.FIDELITY_ITEMS:
        raise HTTPException(400, "unknown phase")
    exists = conn.execute(
        "SELECT COUNT(*) c FROM fidelity_checklists WHERE class_id=? AND phase=? AND lesson_date=?",
        (req.class_id, req.phase, req.lesson_date)).fetchone()["c"]
    if exists:
        raise HTTPException(409, "该课时保真度清单已提交，不可修改；请追加修正记录")
    items = FC.FIDELITY_ITEMS[req.phase]
    checked_set = set(int(i) for i in req.checked)
    checked_items = [items[i] for i in range(len(items)) if i in checked_set]
    score = round(len(checked_items) / len(items) * 100, 1) if items else 0
    cur = conn.execute(
        "INSERT INTO fidelity_checklists(class_id,phase,lesson_date,items,score,filled_by) VALUES(?,?,?,?,?,?)",
        (req.class_id, req.phase, req.lesson_date, json.dumps(checked_items, ensure_ascii=False), score, req.filled_by))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "score": score}


class FidelityCorrectReq(BaseModel):
    note: str


@app.post("/teacher/fidelity/{cid}/correct")
def teacher_fidelity_correct(cid: int, req: FidelityCorrectReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    conn.execute("INSERT INTO fidelity_corrections(checklist_id, note) VALUES(?,?)", (cid, req.note))
    conn.commit()
    return {"ok": True}


class AssessmentReq(BaseModel):
    rows: list  # [{student_id, test_type, phase_or_wave, score, rater_id, rater_blind}]


@app.get("/teacher/assessments")
def teacher_assessments(request: Request, class_id: str = "", test_type: str = ""):
    teacher_guard(request)
    conn = get_conn()
    where, params = [], []
    if class_id: where.append("s.class_id=?"); params.append(class_id)
    if test_type: where.append("a.test_type=?"); params.append(test_type)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"""SELECT a.*, s.display_name, s.class_id FROM assessments a
            LEFT JOIN students s ON s.id=a.student_id {w} ORDER BY a.id DESC LIMIT 1000""", params).fetchall()
    return {"assessments": [dict(r) for r in rows], "test_types": FC.TEST_TYPE_LABELS}


@app.post("/teacher/assessments")
def teacher_assessments_create(req: AssessmentReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    n = 0
    for r in req.rows:
        if not r.get("student_id"):
            continue
        conn.execute(
            """INSERT INTO assessments(student_id,test_type,phase_or_wave,score,rater_id,rater_blind,task_source)
               VALUES(?,?,?,?,?,?,?)""",
            (r["student_id"], r.get("test_type", ""), r.get("phase_or_wave", ""),
             r.get("score"), r.get("rater_id", ""), 1 if r.get("rater_blind") else 0,
             r.get("task_source") or "ai"))
        n += 1
    conn.commit()
    return {"ok": True, "inserted": n}


class OralSampleReq(BaseModel):
    student_id: int
    wave: str = ""
    rater1_id: str = ""
    rater2_id: str = ""
    filename: str = "sample"
    file_b64: str = ""


@app.post("/teacher/oral_sample")
def teacher_oral_sample(req: OralSampleReq, request: Request):
    teacher_guard(request)
    if req.file_b64:
        data = base64.b64decode(req.file_b64)
        fname = f"oral_{req.student_id}_{int(time.time())}_{os.path.basename(req.filename or 'sample')}"
        path = os.path.join(UPLOAD_DIR, fname)
        with open(path, "wb") as f:
            f.write(data)
        file_url = f"/uploads/{fname}"
    else:
        file_url = req.filename
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO oral_samples(student_id,wave,file_url,rater1_id,rater2_id) VALUES(?,?,?,?,?)",
        (req.student_id, req.wave, file_url, req.rater1_id, req.rater2_id))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "file_url": file_url}


@app.get("/teacher/oral_samples")
def teacher_oral_samples(request: Request, for_rating: int = 0):
    teacher_guard(request)
    conn = get_conn()
    rows = conn.execute("SELECT * FROM oral_samples ORDER BY id DESC LIMIT 500").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if for_rating:
            d["sample_code"] = f"S{r['id']:03d}"
            d.pop("student_id", None)
            d.pop("wave", None)
        out.append(d)
    return {"samples": out, "caf_fields": FC.ORAL_CAF_FIELDS}


class OralScoreReq(BaseModel):
    sample_id: int
    rater_id: str
    complexity: float
    accuracy: float
    fluency: float


@app.post("/teacher/oral_score")
def teacher_oral_score(req: OralScoreReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    row = conn.execute("SELECT * FROM oral_samples WHERE id=?", (req.sample_id,)).fetchone()
    if not row:
        raise HTTPException(404, "sample not found")
    if req.rater_id not in (row["rater1_id"], row["rater2_id"]):
        raise HTTPException(403, "rater not assigned to this sample")
    scores = json.loads(row["caf_scores"]) if row["caf_scores"] else {}
    scores[f"rater_{req.rater_id}"] = {"complexity": req.complexity, "accuracy": req.accuracy, "fluency": req.fluency}
    conn.execute("UPDATE oral_samples SET caf_scores=? WHERE id=?",
                 (json.dumps(scores, ensure_ascii=False), req.sample_id))
    conn.commit()
    return {"ok": True}


EXPORT_TABLES = {
    "logs": {"ts": "created_at", "has_class": True},
    "students": {"ts": "created_at", "has_class": True},
    "phase_config": {"ts": "updated_at", "has_class": True},
    "hint_events": {"ts": "timestamp", "has_class": False},
    "feedback_events": {"ts": "feedback_time", "has_class": False},
    "avoidance_flags": {"ts": "triggered_at", "has_class": False},
    "handoff_events": {"ts": "timestamp", "has_class": True},
    "teacher_actions": {"ts": "timestamp", "has_class": False},
    "fidelity_checklists": {"ts": "filled_at", "has_class": True},
    "assessments": {"ts": "created_at", "has_class": False},
    "oral_samples": {"ts": "created_at", "has_class": False},
    # 中文班
    "assignments": {"ts": "created_at", "has_class": True},
    "assignment_submissions": {"ts": "updated_at", "has_class": False},
    "dictation_events": {"ts": "created_at", "has_class": False},
    "recitation_events": {"ts": "created_at", "has_class": False},
    "writing_submissions": {"ts": "created_at", "has_class": False},
    "writing_revisions": {"ts": "created_at", "has_class": False},
    "reading_logs": {"ts": "created_at", "has_class": False},
    "error_items": {"ts": "last_wrong_at", "has_class": False},
}


@app.get("/teacher/export2")
def teacher_export2(request: Request, table: str, from_: str = "", to: str = "", class_id: str = ""):
    teacher_guard(request)
    if table not in EXPORT_TABLES:
        raise HTTPException(400, f"unknown table; allowed: {list(EXPORT_TABLES)}")
    meta = EXPORT_TABLES[table]
    conn = get_conn()
    where, params = [], []
    ts = meta["ts"]
    if from_: where.append(f"{ts}>=?"); params.append(from_)
    if to: where.append(f"{ts}<=?"); params.append(to)
    if class_id and meta["has_class"]:
        where.append("class_id=?"); params.append(class_id)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"SELECT * FROM {table}{w} ORDER BY {ts} DESC LIMIT 50000", params).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    if rows:
        writer.writerow(rows[0].keys())
        for r in rows:
            writer.writerow([v if not isinstance(v, dict) else json.dumps(v, ensure_ascii=False) for v in r])
    buf.seek(0)
    log_teacher_action("export_data", target_class=class_id or None,
                       detail={"table": table, "rows": len(rows), "from": from_, "to": to})
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={table}_export.csv"})


# ---------- 调试后门路由（仅本机；正式部署 host 不是回环地址会自动 404） ----------
class DevLoginReq(BaseModel):
    mode: str  # "existing" | "create"
    student_id: Optional[int] = None
    display_name: Optional[str] = None
    class_id: Optional[str] = None
    track: Optional[int] = None


@app.get("/dev")
@app.get("/dev/")
def dev_page(request: Request):
    if not dev_enabled(request):
        raise HTTPException(404, "dev mode off")
    return HTMLResponse(DEV_LAUNCHER_HTML)


@app.get("/dev/students")
def dev_students(request: Request):
    if not dev_enabled(request):
        raise HTTPException(404, "dev mode off")
    rows = get_conn().execute(
        "SELECT id,display_name,class_id,track FROM students ORDER BY id"
    ).fetchall()
    return {"students": [dict(r) for r in rows]}


@app.post("/dev/login")
def dev_login(req: DevLoginReq, request: Request):
    if not dev_enabled(request):
        raise HTTPException(404, "dev mode off")
    conn = get_conn()
    if req.mode == "existing":
        if not req.student_id:
            raise HTTPException(400, "student_id required")
        row = conn.execute("SELECT * FROM students WHERE id=?", (req.student_id,)).fetchone()
        if not row:
            raise HTTPException(404, "student not found")
    else:  # create
        line_id = f"dev-local-{int(time.time()*1000)}-{secrets.token_hex(4)}"
        cur = conn.execute(
            "INSERT INTO students(line_user_id,display_name,class_id,track) VALUES(?,?,?,?)",
            (line_id, req.display_name or "测试学生", req.class_id or "M4/1", req.track or 1),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM students WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"token": make_token(row["id"]), "student": dict(row)}


# ============ 功能增强接口（F1–F12） ============


class FeedbackReasonReq(BaseModel):
    reason_type: str                      # agree / disagree / not_sure
    reason_text: str = ""
    item_id: Optional[int] = None


class OfflineTaskReq(BaseModel):
    student_id: int
    task_type: str                        # oral / writing
    phase: str = ""
    score: float = 0


@app.get("/api/weekly_report")
def api_weekly_report(request: Request, force: int = 0):
    """F1 学生学习周报（懒生成，同周缓存）"""
    student = get_student(parse_token(request))
    m = AN.generate_weekly_report(student["id"], force=bool(force))
    m["display_name"] = student["display_name"]
    return m


@app.get("/api/diagnostics")
def api_diagnostics(request: Request):
    """F2 薄弱点诊断"""
    student = get_student(parse_token(request))
    points = AN.weak_points(student["id"])
    AN.save_diagnostics(student["id"], current_phase(student["class_id"]), points)
    return {"weak_points": points,
            "module_labels": FC.WEAK_POINT_MODULE_LABELS,
            "min_count": FC.WEAK_POINT_MIN_COUNT}


@app.get("/api/suggestions")
def api_suggestions(request: Request):
    """F3 个性化学习建议（首次访问时按薄弱点生成并入库）"""
    student = get_student(parse_token(request))
    points = AN.weak_points(student["id"])
    return {"suggestions": AN.ensure_suggestions(student["id"], points)}


@app.post("/api/suggestions/{suggestion_id}/click")
def api_suggestion_click(suggestion_id: int, request: Request):
    """F3 记录学生点击了某条建议（能动性证据）"""
    student = get_student(parse_token(request))
    conn = get_conn()
    conn.execute(
        "UPDATE suggestions SET clicked_at=datetime('now') WHERE id=? AND student_id=? AND clicked_at IS NULL",
        (suggestion_id, student["id"]))
    conn.commit()
    log_event(student, "suggestion_click", detail={"suggestion_id": suggestion_id})
    return {"ok": True}


@app.post("/api/feedback/reason")
def api_feedback_reason(req: FeedbackReasonReq, request: Request):
    """F4 反馈采纳理由收集（H3 能动性中介的行为证据）"""
    student = get_student(parse_token(request))
    if req.reason_type not in FC.DECISION_REASON_LABELS:
        raise HTTPException(400, "unknown reason_type")
    conn = get_conn()
    # 关联该生最近一条反馈事件（feedback_events 不存 item_id，故取最近一条）
    fe = conn.execute(
        "SELECT id FROM feedback_events WHERE student_id=? ORDER BY id DESC LIMIT 1",
        (student["id"],)).fetchone()
    fe_id = fe["id"] if fe else None
    cur = conn.execute(
        """INSERT INTO decision_reasons(feedback_event_id,student_id,reason_type,reason_text)
           VALUES(?,?,?,?)""",
        (fe_id, student["id"], req.reason_type, req.reason_text or ""))
    reason_id = cur.lastrowid
    if fe_id:
        conn.execute(
            """UPDATE feedback_events SET decision_reason_id=?,
               reject_reason=?, adopted=?
               WHERE id=?""",
            (reason_id, req.reason_text or req.reason_type,
             "rejected" if req.reason_type == "disagree" else "full", fe_id))
    conn.commit()
    log_event(student, "feedback_decision", req.item_id and "exercise" or None, req.item_id,
              detail={"reason_type": req.reason_type, "reason_text": req.reason_text})
    return {"ok": True, "reason_id": reason_id}


@app.get("/api/radar")
def api_radar(request: Request):
    """F9 能力雷达图"""
    student = get_student(parse_token(request))
    return AN.radar_scores(student["id"])


@app.get("/api/offline_task")
def api_offline_task(request: Request):
    """F10 T1/T2 阶段的无 AI 线下迁移任务说明"""
    student = get_student(parse_token(request))
    phase = current_phase(student["class_id"])
    active = phase in ("T1", "T2")
    conn = get_conn()
    done = conn.execute(
        """SELECT task_type, score, submitted_at FROM task_offline
           WHERE student_id=? AND phase=? ORDER BY id DESC""",
        (student["id"], phase)).fetchall()
    return {"phase": phase, "active": active,
            "tasks": [{"key": k, **v} for k, v in FC.OFFLINE_TASKS.items()],
            "submitted": [dict(d) for d in done]}


@app.get("/api/notifications")
def api_notifications(request: Request):
    """站内通知中心（学生）"""
    student = get_student(parse_token(request))
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, kind, title, body, read, created_at FROM notifications
           WHERE user_type='student' AND user_id=? ORDER BY id DESC LIMIT 50""",
        (str(student["id"]),)).fetchall()
    return {"notifications": [dict(r) for r in rows],
            "unread": AN.unread_count("student", student["id"]),
            "kind_labels": FC.NOTIFICATION_KINDS}


@app.get("/api/notifications/unread_count")
def api_notifications_unread(request: Request):
    student = get_student(parse_token(request))
    return {"unread": AN.unread_count("student", student["id"])}


@app.post("/api/notifications/{nid}/read")
def api_notification_read(nid: int, request: Request):
    student = get_student(parse_token(request))
    conn = get_conn()
    conn.execute(
        "UPDATE notifications SET read=1 WHERE id=? AND user_type='student' AND user_id=?",
        (nid, str(student["id"])))
    conn.commit()
    return {"ok": True, "unread": AN.unread_count("student", student["id"])}


# ---------- 教师端增强接口 ----------

@app.get("/teacher/weekly_report")
def teacher_weekly_report(request: Request):
    """F5 教师班级周报（打开时生成）"""
    teacher_guard(request)
    return AN.teacher_weekly_report()


@app.post("/teacher/scan")
def teacher_scan(request: Request):
    """F6/F7/F8/F12 手动触发全部扫描器"""
    teacher_guard(request)
    result = run_all_scans()
    log_teacher_action("run_scan", detail=result)
    return {"ok": True, **result}


@app.get("/teacher/notifications")
def teacher_notifications(request: Request):
    """教师通知中心"""
    teacher_guard(request)
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, kind, title, body, read, created_at FROM notifications
           WHERE user_type='teacher' AND user_id='all' ORDER BY id DESC LIMIT 100""").fetchall()
    return {"notifications": [dict(r) for r in rows],
            "unread": AN.unread_count("teacher", "all"),
            "kind_labels": FC.NOTIFICATION_KINDS}


@app.post("/teacher/notifications/{nid}/read")
def teacher_notification_read(nid: int, request: Request):
    teacher_guard(request)
    conn = get_conn()
    conn.execute(
        "UPDATE notifications SET read=1 WHERE id=? AND user_type='teacher' AND user_id='all'",
        (nid,))
    conn.commit()
    return {"ok": True, "unread": AN.unread_count("teacher", "all")}


@app.post("/teacher/offline_task")
def teacher_offline_task(req: OfflineTaskReq, request: Request):
    """F10 录入线下迁移任务成绩（task_source 固定 offline）"""
    teacher_guard(request)
    if req.task_type not in FC.OFFLINE_TASKS:
        raise HTTPException(400, "unknown task_type")
    conn = get_conn()
    conn.execute(
        """INSERT INTO task_offline(student_id,phase,task_type,score,task_source)
           VALUES(?,?,?,?,'offline')""",
        (req.student_id, req.phase, req.task_type, req.score))
    # 同时写入 assessments，便于统一导出与统计
    conn.execute(
        """INSERT INTO assessments(student_id,test_type,phase_or_wave,score,task_source)
           VALUES(?,'transfer_task',?,?,'offline')""",
        (req.student_id, req.phase, req.score))
    conn.commit()
    log_teacher_action("offline_task_score", detail={"student_id": req.student_id,
                                                     "task_type": req.task_type,
                                                     "score": req.score})
    return {"ok": True}


# ============================================================
# 中文班（母语语文）专属接口
# ============================================================

def _parse_json(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _need_native(student):
    if not is_native(student["class_id"]):
        raise HTTPException(403, "native class only")


# ---------- 听写 ----------

@app.get("/api/dictation")
def api_dictation(request: Request, lesson: str, scope: str = "all"):
    student = get_student(parse_token(request))
    _need_native(student)
    book = student_book(student)
    conn = content_db()
    chars, words = [], []
    if scope in ("all", "chars"):
        chars = [dict(r) for r in conn.execute(
            """SELECT id, char, pinyin, radical, strokes, structure, words, note_zh
               FROM hanzi WHERE book=? AND lesson_no=? ORDER BY id""",
            (book, lesson)).fetchall()]
    if scope in ("all", "words"):
        words = [dict(r) for r in conn.execute(
            """SELECT id, word, pinyin, meaning_zh FROM vocab
               WHERE book=? AND lesson_no=? ORDER BY id""",
            (book, lesson)).fetchall()]
    return {"lesson_no": lesson, "chars": chars, "words": words}


class DictationSubmitReq(BaseModel):
    lesson_no: str
    scope: str = "words"
    mode: str = "pinyin2word"
    results: list  # [{target,target_type,ref_id,input_text,is_correct,wrong_part,duration_ms}]


@app.post("/api/dictation/submit")
def api_dictation_submit(req: DictationSubmitReq, request: Request):
    student = get_student(parse_token(request))
    _need_native(student)
    conn = get_conn()
    total = correct = 0
    for r in req.results:
        ok = 1 if r.get("is_correct") else 0
        total += 1
        correct += ok
        conn.execute(
            """INSERT INTO dictation_events(student_id,lesson_no,target,target_type,
               ref_id,mode,input_text,is_correct,wrong_part,duration_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (student["id"], req.lesson_no, r.get("target", ""),
             r.get("target_type", "word"), r.get("ref_id"), req.mode,
             r.get("input_text", ""), ok, r.get("wrong_part", ""),
             r.get("duration_ms")))
        if not ok:
            src = "dictation_word" if r.get("target_type") == "word" else "dictation"
            upsert_error(student["id"], src, r.get("ref_id"),
                         "字词", r.get("target", ""))
    conn.commit()
    score = round(correct / total * 100) if total else 0
    log_event(student, "dictation_submit", module="dictation",
              response_json={"lesson_no": req.lesson_no, "mode": req.mode,
                             "total": total, "correct": correct, "score": score})
    return {"total": total, "correct": correct, "score": score}


# ---------- 古诗文背诵 / 默写 ----------

@app.get("/api/poems")
def api_poems(request: Request, unit: int = 0, lesson: str = ""):
    student = get_student(parse_token(request))
    _need_native(student)
    book = student_book(student)
    sql = "SELECT * FROM poems WHERE book=?"
    args = [book]
    if unit:
        sql += " AND unit=?"; args.append(unit)
    if lesson:
        sql += " AND lesson_no=?"; args.append(lesson)
    sql += " ORDER BY unit, lesson_no, id"
    rows = content_db().execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["notes"] = _parse_json(d.pop("notes_json", None), [])
        stat = get_conn().execute(
            """SELECT MAX(score) best, COUNT(*) attempts FROM recitation_events
               WHERE student_id=? AND poem_id=?""",
            (student["id"], d["id"])).fetchone()
        d["best_score"] = stat["best"]
        d["attempts"] = stat["attempts"] or 0
        out.append(d)
    return {"poems": out}


class RecitationReq(BaseModel):
    poem_id: int
    mode: str = "full_dictation"
    input_text: str = ""


def _norm_text(s: str) -> str:
    return re.sub(r"[\s，。！？、；：“”‘’（）《》…—·,.!?;:\"'()\[\]]", "", s or "")


@app.post("/api/recitation/submit")
def api_recitation_submit(req: RecitationReq, request: Request):
    import difflib
    student = get_student(parse_token(request))
    _need_native(student)
    poem = content_db().execute("SELECT * FROM poems WHERE id=?", (req.poem_id,)).fetchone()
    if not poem:
        raise HTTPException(404, "poem not found")
    target = _norm_text(poem["body"])
    mine = _norm_text(req.input_text)
    ratio = difflib.SequenceMatcher(None, target, mine).ratio() if target else 0
    score = round(ratio * 100, 1)
    # 找出漏写的字（按字频差集）
    from collections import Counter
    tc, mc = Counter(target), Counter(mine)
    missing = []
    for ch, n in tc.items():
        if mc.get(ch, 0) < n:
            missing.append(ch)
    detail = {"target_len": len(target), "input_len": len(mine),
              "missing_chars": missing[:50], "ratio": ratio}
    conn = get_conn()
    conn.execute(
        """INSERT INTO recitation_events(student_id,poem_id,lesson_no,mode,score,detail_json)
           VALUES(?,?,?,?,?,?)""",
        (student["id"], req.poem_id, poem["lesson_no"], req.mode, score,
         json.dumps(detail, ensure_ascii=False)))
    conn.commit()
    if score < 80:
        upsert_error(student["id"], "recitation", req.poem_id, "古诗文默写", poem["title"])
    log_event(student, "recitation_submit", module="recitation",
              response_json={"poem_id": req.poem_id, "mode": req.mode, "score": score})
    return {"score": score, "detail": detail}


# ---------- 习作 ----------

@app.get("/api/writing/tasks")
def api_writing_tasks(request: Request, unit: int = 0):
    student = get_student(parse_token(request))
    _need_native(student)
    book = student_book(student)
    sql = "SELECT * FROM writing_tasks WHERE book=?"
    args = [book]
    if unit:
        sql += " AND unit=?"; args.append(unit)
    sql += " ORDER BY unit, id"
    rows = content_db().execute(sql, args).fetchall()
    return {"tasks": [dict(r) for r in rows]}


@app.get("/api/writing/task/{tid}")
def api_writing_task(tid: int, request: Request):
    student = get_student(parse_token(request))
    _need_native(student)
    task = content_db().execute(
        "SELECT * FROM writing_tasks WHERE id=?", (tid,)).fetchone()
    if not task:
        raise HTTPException(404, "task not found")
    d = dict(task)
    d["rubric"] = _parse_json(d.get("rubric_json"), [])
    d["tips"] = _parse_json(d.get("tips_json"), [])
    mine = [dict(r) for r in get_conn().execute(
        """SELECT id, draft_no, word_count, status, ai_scores_json,
                  teacher_scores_json, teacher_comment, submitted_at, created_at
           FROM writing_submissions WHERE student_id=? AND task_id=?
           ORDER BY draft_no""", (student["id"], tid)).fetchall()]
    return {"task": d, "mine": mine}


class WritingSaveReq(BaseModel):
    task_id: int
    content: str = ""
    outline: str = ""
    draft_no: int = 1
    duration_seconds: int = 0


@app.post("/api/writing/save")
def api_writing_save(req: WritingSaveReq, request: Request):
    student = get_student(parse_token(request))
    _need_native(student)
    wc = len(_norm_text(req.content))
    conn = get_conn()
    row = conn.execute(
        """SELECT id, status FROM writing_submissions
           WHERE student_id=? AND task_id=? AND draft_no=?""",
        (student["id"], req.task_id, req.draft_no)).fetchone()
    if row and row["status"] == "draft":
        conn.execute(
            """UPDATE writing_submissions SET content=?, outline=?, word_count=?,
               duration_seconds=?, created_at=datetime('now') WHERE id=?""",
            (req.content, req.outline, wc, req.duration_seconds, row["id"]))
        sid = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO writing_submissions(student_id,task_id,draft_no,outline,content,
               word_count,duration_seconds,status) VALUES(?,?,?,?,?,?,?, 'draft')""",
            (student["id"], req.task_id, req.draft_no, req.outline, req.content,
             wc, req.duration_seconds))
        sid = cur.lastrowid
    conn.commit()
    return {"id": sid, "word_count": wc}


class WritingSubmitReq(BaseModel):
    task_id: int
    assignment_id: Optional[int] = None
    content: str
    outline: str = ""
    draft_no: int = 1
    duration_seconds: int = 0


@app.post("/api/writing/submit")
async def api_writing_submit(req: WritingSubmitReq, request: Request):
    student = get_student(parse_token(request))
    _need_native(student)
    task = content_db().execute(
        "SELECT * FROM writing_tasks WHERE id=?", (req.task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "task not found")
    wc = len(_norm_text(req.content))
    if wc < 50:
        raise HTTPException(400, "作文太短了，再写一些吧")

    t0 = time.time()
    fb = await chat_json(writing_feedback_messages(dict(task), req.content), provider="mm")
    latency = int((time.time() - t0) * 1000)
    ok = isinstance(fb.get("scores"), dict) and "总分" in fb.get("scores", {})

    conn = get_conn()
    # 同稿次已存在则更新，否则插入
    row = conn.execute(
        """SELECT id FROM writing_submissions
           WHERE student_id=? AND task_id=? AND draft_no=?""",
        (student["id"], req.task_id, req.draft_no)).fetchone()
    scores_json = json.dumps(fb.get("scores") or {}, ensure_ascii=False)
    fb_json = json.dumps(fb, ensure_ascii=False) if ok else None
    status = "ai_reviewed" if ok else "submitted"
    if row:
        sid = row["id"]
        conn.execute(
            """UPDATE writing_submissions SET content=?, outline=?, word_count=?,
               duration_seconds=?, ai_scores_json=?, ai_feedback_json=?, status=?,
               submitted_at=datetime('now'), created_at=datetime('now') WHERE id=?""",
            (req.content, req.outline, wc, req.duration_seconds, scores_json,
             fb_json, status, sid))
    else:
        cur = conn.execute(
            """INSERT INTO writing_submissions(student_id,task_id,assignment_id,draft_no,
               outline,content,word_count,duration_seconds,ai_scores_json,ai_feedback_json,
               status,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (student["id"], req.task_id, req.assignment_id, req.draft_no, req.outline,
             req.content, wc, req.duration_seconds, scores_json, fb_json, status))
        sid = cur.lastrowid
    conn.commit()
    if req.assignment_id:
        _mark_homework_submitted(student, req.assignment_id,
                                 (fb.get("scores") or {}).get("总分"))
    log_event(student, "writing_submit", module="writing",
              ai_latency_ms=latency,
              response_json={"task_id": req.task_id, "draft_no": req.draft_no,
                             "word_count": wc, "ok": ok})
    return {"id": sid, "word_count": wc, "ok": ok, "feedback": fb if ok else None,
            "message": None if ok else "作文已保存，AI 批改暂时不可用，老师会尽快批阅。"}


@app.get("/api/writing/mine")
def api_writing_mine(request: Request):
    student = get_student(parse_token(request))
    _need_native(student)
    rows = get_conn().execute(
        """SELECT * FROM writing_submissions WHERE student_id=?
           ORDER BY created_at DESC LIMIT 50""",
        (student["id"],)).fetchall()
    # writing_tasks 在内容库，跨库在 Python 侧合并
    cdb = content_db()
    tasks = {r["id"]: dict(r) for r in cdb.execute(
        "SELECT id, title, unit, min_words FROM writing_tasks").fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        t = tasks.get(d.get("task_id"), {})
        d["title"] = t.get("title")
        d["unit"] = t.get("unit")
        d["min_words"] = t.get("min_words")
        d["ai_scores"] = _parse_json(d.pop("ai_scores_json", None), None)
        d["teacher_scores"] = _parse_json(d.pop("teacher_scores_json", None), None)
        d["ai_feedback"] = _parse_json(d.pop("ai_feedback_json", None), None)
        out.append(d)
    return {"submissions": out}


# ---------- 作业 ----------

@app.get("/api/homework")
def api_homework(request: Request):
    student = get_student(parse_token(request))
    conn = get_conn()
    rows = conn.execute(
        """SELECT a.*, s.status sub_status, s.submitted_at, s.score, s.detail_json
           FROM assignments a
           LEFT JOIN assignment_submissions s
             ON s.assignment_id = a.id AND s.student_id = ?
           WHERE a.class_id=? AND datetime(a.publish_at) <= datetime('now')
           ORDER BY COALESCE(a.due_at, a.publish_at) ASC, a.id DESC""",
        (student["id"], student["class_id"])).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ref"] = _parse_json(d.pop("ref_json", None), {})
        d.pop("detail_json", None)
        d["status"] = d.get("sub_status") or "pending"
        d["overdue"] = bool(d.get("due_at")) and d["status"] == "pending" and \
            d["due_at"] < datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        out.append(d)
    return {"assignments": out}


@app.get("/api/homework/{aid}")
def api_homework_detail(aid: int, request: Request):
    student = get_student(parse_token(request))
    conn = get_conn()
    a = conn.execute("SELECT * FROM assignments WHERE id=? AND class_id=?",
                     (aid, student["class_id"])).fetchone()
    if not a:
        raise HTTPException(404, "assignment not found")
    d = dict(a)
    d["ref"] = _parse_json(d.pop("ref_json", None), {})
    sub = conn.execute(
        "SELECT * FROM assignment_submissions WHERE assignment_id=? AND student_id=?",
        (aid, student["id"])).fetchone()
    d["submission"] = dict(sub) if sub else None
    return d


class HomeworkSubmitReq(BaseModel):
    score: Optional[float] = None
    detail: dict = {}


def _mark_homework_submitted(student, assignment_id: int, score=None):
    conn = get_conn()
    a = conn.execute("SELECT due_at FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    status = "submitted"
    if a and a["due_at"] and a["due_at"] < datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"):
        status = "late"
    conn.execute(
        """INSERT INTO assignment_submissions(assignment_id,student_id,status,submitted_at,score)
           VALUES(?,?,?,datetime('now'),?)
           ON CONFLICT(assignment_id,student_id) DO UPDATE SET
             status=excluded.status, submitted_at=excluded.submitted_at,
             score=COALESCE(excluded.score, assignment_submissions.score),
             updated_at=datetime('now')""",
        (assignment_id, student["id"], status, score))
    conn.commit()


@app.post("/api/homework/{aid}/submit")
def api_homework_submit(aid: int, req: HomeworkSubmitReq, request: Request):
    student = get_student(parse_token(request))
    _need_native(student)
    a = get_conn().execute("SELECT * FROM assignments WHERE id=? AND class_id=?",
                           (aid, student["class_id"])).fetchone()
    if not a:
        raise HTTPException(404, "assignment not found")
    _mark_homework_submitted(student, aid, req.score)
    log_event(student, "homework_submit", module=a["type"],
              response_json={"assignment_id": aid, "score": req.score})
    return {"ok": True}


# ---------- 错题本 ----------

@app.get("/api/errors")
def api_errors(request: Request, limit: int = 100):
    student = get_student(parse_token(request))
    _need_native(student)
    rows = get_conn().execute(
        """SELECT * FROM error_items WHERE student_id=? AND mastered=0
           ORDER BY last_wrong_at DESC LIMIT ?""",
        (student["id"], limit)).fetchall()
    items, exams, hanzi, poems, words = {}, {}, {}, {}, {}
    ids = {"exercise": set(), "exam": set(), "dictation": set(),
           "dictation_word": set(), "recitation": set()}
    for r in rows:
        if r["source"] in ids and r["ref_id"] is not None:
            ids[r["source"]].add(r["ref_id"])
    cdb = content_db()

    def fetch(table, id_set, key="id"):
        if not id_set:
            return {}
        ph = ",".join("?" * len(id_set))
        return {x["id"]: dict(x) for x in cdb.execute(
            f"SELECT * FROM {table} WHERE id IN ({ph})", list(id_set)).fetchall()}

    items = fetch("items", ids["exercise"])
    exams = fetch("exams", ids["exam"])
    hanzi = fetch("hanzi", ids["dictation"])
    poems = fetch("poems", ids["recitation"])
    words = fetch("vocab", ids["dictation_word"])
    out = []
    for r in rows:
        d = dict(r)
        ref = None
        if r["source"] == "exercise" and r["ref_id"] in items:
            it = items[r["ref_id"]]
            ref = {"text": it["prompt_zh"], "q_type": it.get("q_type"),
                   "lesson": it["lesson_no"], "answer": it.get("answer"),
                   "href": f"lesson.html?lesson={it['lesson_no']}&module=exercise"}
        elif r["source"] == "exam" and r["ref_id"] in exams:
            ex = exams[r["ref_id"]]
            ref = {"text": ex["question_zh"], "q_type": ex.get("q_type"),
                   "lesson": ex["lesson_no"], "answer": ex.get("correct"),
                   "href": f"lesson.html?lesson={ex['lesson_no']}&module=exam"}
        elif r["source"] == "dictation" and r["ref_id"] in hanzi:
            hz = hanzi[r["ref_id"]]
            ref = {"text": hz["char"], "pinyin": hz["pinyin"], "words": hz.get("words"),
                   "lesson": hz["lesson_no"],
                   "href": f"lesson.html?lesson={hz['lesson_no']}&module=hanzi"}
        elif r["source"] == "dictation_word" and r["ref_id"] in words:
            wd = words[r["ref_id"]]
            ref = {"text": wd["word"], "pinyin": wd["pinyin"],
                   "lesson": wd["lesson_no"],
                   "href": f"lesson.html?lesson={wd['lesson_no']}&module=hanzi"}
        elif r["source"] == "recitation" and r["ref_id"] in poems:
            pm = poems[r["ref_id"]]
            ref = {"text": pm["title"], "lesson": pm.get("lesson_no"),
                   "href": f"lesson.html?lesson={pm.get('lesson_no')}&module=poem"}
        d["ref"] = ref
        out.append(d)
    return {"errors": out}


@app.post("/api/errors/{eid}/master")
def api_error_master(eid: int, request: Request):
    student = get_student(parse_token(request))
    conn = get_conn()
    conn.execute("UPDATE error_items SET mastered=1 WHERE id=? AND student_id=?",
                 (eid, student["id"]))
    conn.commit()
    return {"ok": True}


# ---------- 语文周报 ----------

@app.get("/api/native_report")
def api_native_report(request: Request):
    student = get_student(parse_token(request))
    _need_native(student)
    tz = class_tz(student["class_id"])
    conn = get_conn()
    # 本周起止（按班级时区）
    row = conn.execute(
        f"""SELECT date('now','{tz}','weekday 0','-6 days') ws,
                   date('now','{tz}') we""").fetchone()
    start, end = row["ws"], row["we"]
    d = f"date(created_at,'{tz}')"

    logs = conn.execute(
        f"""SELECT module, response_json, {d} day FROM logs
            WHERE student_id=? AND {d} BETWEEN ? AND ?""",
        (student["id"], start, end)).fetchall()
    submits = accuracy = None
    correct = partial = graded = 0
    days = set()
    by_type = {}
    for r in logs:
        days.add(r["day"])
        if r["module"] == "dictation":
            continue
        if (r["response_json"] or "").find('"verdict"') >= 0:
            try:
                v = (json.loads(r["response_json"]) or {}).get("verdict")
            except Exception:
                v = None
            if v in ("correct", "partial", "incorrect"):
                graded += 1
                if v == "correct":
                    correct += 1
                elif v == "partial":
                    partial += 0.5
    if graded:
        accuracy = round((correct + partial) / graded * 100)

    dic = conn.execute(
        f"""SELECT COUNT(*) n, SUM(is_correct) c FROM dictation_events
            WHERE student_id=? AND {d} BETWEEN ? AND ?""",
        (student["id"], start, end)).fetchone()
    dic_acc = round((dic["c"] or 0) / dic["n"] * 100) if dic["n"] else None

    rec = conn.execute(
        f"""SELECT COUNT(*) n, AVG(score) s FROM recitation_events
            WHERE student_id=? AND {d} BETWEEN ? AND ?""",
        (student["id"], start, end)).fetchone()

    wr = conn.execute(
        f"""SELECT COUNT(*) n, AVG(json_extract(ai_scores_json,'$.总分')) s
            FROM writing_submissions
            WHERE student_id=? AND status!='draft' AND {d} BETWEEN ? AND ?""",
        (student["id"], start, end)).fetchone()

    hw = conn.execute(
        f"""SELECT COUNT(*) n, SUM(CASE WHEN s.status IN ('submitted','late','graded') THEN 1 ELSE 0 END) done
            FROM assignments a LEFT JOIN assignment_submissions s
              ON s.assignment_id=a.id AND s.student_id=?
            WHERE a.class_id=? AND COALESCE(date(a.due_at,'{tz}'), date(a.publish_at,'{tz}')) BETWEEN ? AND ?""",
        (student["id"], student["class_id"], start, end)).fetchone()

    weak = conn.execute(
        """SELECT q_type, knowledge_tag, SUM(wrong_count) n FROM error_items
           WHERE student_id=? AND mastered=0 GROUP BY q_type, knowledge_tag
           ORDER BY n DESC LIMIT 5""", (student["id"],)).fetchall()

    all_dic = conn.execute(
        """SELECT COUNT(*) n, SUM(is_correct) c FROM dictation_events WHERE student_id=?""",
        (student["id"],)).fetchone()
    all_write = conn.execute(
        """SELECT AVG(json_extract(ai_scores_json,'$.总分')) s FROM writing_submissions
           WHERE student_id=? AND ai_scores_json IS NOT NULL""",
        (student["id"],)).fetchone()
    all_rec = conn.execute(
        """SELECT AVG(score) s FROM recitation_events WHERE student_id=?""",
        (student["id"],)).fetchone()
    # 分模块（按 items 的 module 聚合判定）
    per_module = {}
    for r in logs:
        if not r["response_json"] or '"verdict"' not in (r["response_json"] or ""):
            continue
        try:
            v = (json.loads(r["response_json"]) or {}).get("verdict")
        except Exception:
            continue
        slot = per_module.setdefault(r["module"] or "其他", [0, 0])
        if v in ("correct", "partial", "incorrect"):
            slot[1] += 1
            slot[0] += 1 if v == "correct" else (0.5 if v == "partial" else 0)

    def rate(m):
        s = per_module.get(m)
        return round(s[0] / s[1] * 100) if s and s[1] else None

    radar = [
        {"key": "literacy", "label": "识字写字",
         "value": round((all_dic["c"] or 0) / all_dic["n"] * 100) if all_dic["n"] else None},
        {"key": "language", "label": "词句运用", "value": rate("词句段")},
        {"key": "reading", "label": "阅读理解", "value": rate("练习")},
        {"key": "writing", "label": "习作表达",
         "value": round(all_write["s"] / 50 * 100) if all_write["s"] else None},
        {"key": "recitation", "label": "积累背诵",
         "value": round(all_rec["s"]) if all_rec["s"] else None},
    ]
    return {
        "week_start": start, "week_end": end,
        "dictation": {"count": dic["n"] or 0,
                      "accuracy": dic_acc},
        "submissions": {"graded": graded, "accuracy": accuracy},
        "recitation": {"count": rec["n"] or 0,
                       "avg_score": round(rec["s"], 1) if rec["s"] else None},
        "writing": {"count": wr["n"] or 0,
                    "avg_score": round(wr["s"], 1) if wr["s"] else None},
        "homework": {"total": hw["n"] or 0, "done": hw["done"] or 0,
                     "rate": round((hw["done"] or 0) / hw["n"] * 100) if hw["n"] else None},
        "active_days": len(days),
        "weak_points": [dict(w) for w in weak],
        "radar": radar,
    }


# ---------- 教师端：作业 / 听写 / 作文 ----------

class AssignmentCreateReq(BaseModel):
    class_id: str
    title: str
    type: str = "exercise"
    ref: dict = {}
    instructions: str = ""
    due_at: str = ""


@app.get("/teacher/assignments")
def teacher_assignments(request: Request, class_id: str = ""):
    teacher_guard(request)
    conn = get_conn()
    sql = """SELECT a.*,
               (SELECT COUNT(*) FROM students s WHERE s.class_id=a.class_id) n_students,
               (SELECT COUNT(*) FROM assignment_submissions x
                 WHERE x.assignment_id=a.id AND x.status IN ('submitted','late','graded')) n_done
             FROM assignments a"""
    args = []
    if class_id:
        sql += " WHERE a.class_id=?"
        args.append(class_id)
    sql += " ORDER BY a.id DESC"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    for r in rows:
        r["ref"] = _parse_json(r.pop("ref_json", None), {})
    return {"assignments": rows}


@app.post("/teacher/assignments")
def teacher_assignment_create(req: AssignmentCreateReq, request: Request):
    teacher_guard(request)
    if req.class_id not in CLASS_CONTENT:
        raise HTTPException(400, "unknown class")
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO assignments(class_id,title,type,ref_json,instructions,due_at)
           VALUES(?,?,?,?,?,?)""",
        (req.class_id, req.title, req.type,
         json.dumps(req.ref, ensure_ascii=False), req.instructions,
         req.due_at or None))
    conn.commit()
    log_teacher_action("assignment_create", target_class=req.class_id,
                       detail={"title": req.title, "type": req.type})
    return {"id": cur.lastrowid}


@app.get("/teacher/assignment/{aid}/submissions")
def teacher_assignment_submissions(aid: int, request: Request):
    teacher_guard(request)
    conn = get_conn()
    a = conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    if not a:
        raise HTTPException(404, "assignment not found")
    rows = conn.execute(
        """SELECT s.id, s.display_name, s.student_no, x.status, x.submitted_at, x.score
           FROM students s
           LEFT JOIN assignment_submissions x
             ON x.student_id=s.id AND x.assignment_id=?
           WHERE s.class_id=? ORDER BY s.student_no""",
        (aid, a["class_id"])).fetchall()
    return {"assignment": {**dict(a), "ref": _parse_json(a["ref_json"], {})},
            "students": [dict(r) for r in rows]}


@app.get("/teacher/dictation")
def teacher_dictation(request: Request, class_id: str = "C6/1"):
    teacher_guard(request)
    conn = get_conn()
    tz = class_tz(class_id)
    d = f"date(de.created_at,'{tz}')"
    summary = conn.execute(
        f"""SELECT COUNT(*) n, SUM(is_correct) c FROM dictation_events de
            JOIN students s ON s.id=de.student_id
            WHERE s.class_id=? AND {d} >= date('now','{tz}','-7 days')""",
        (class_id,)).fetchone()
    top = conn.execute(
        f"""SELECT de.target, COUNT(*) wrong, MAX(de.created_at) last
            FROM dictation_events de JOIN students s ON s.id=de.student_id
            WHERE s.class_id=? AND de.is_correct=0
            GROUP BY de.target ORDER BY wrong DESC LIMIT 20""",
        (class_id,)).fetchall()
    per_student = conn.execute(
        f"""SELECT s.id, s.display_name, COUNT(de.id) n, SUM(de.is_correct) c
            FROM students s LEFT JOIN dictation_events de
              ON de.student_id=s.id AND {d} >= date('now','{tz}','-7 days')
            WHERE s.class_id=? GROUP BY s.id ORDER BY s.display_name""",
        (class_id,)).fetchall()
    return {
        "week": {"total": summary["n"] or 0,
                 "accuracy": round((summary["c"] or 0) / summary["n"] * 100) if summary["n"] else None},
        "top_wrong": [dict(r) for r in top],
        "students": [dict(r) for r in per_student],
    }


@app.get("/teacher/writings")
def teacher_writings(request: Request, class_id: str = "C6/1", status: str = ""):
    teacher_guard(request)
    conn = get_conn()
    sql = """SELECT w.*, s.display_name, s.student_no
             FROM writing_submissions w
             JOIN students s ON s.id=w.student_id
             WHERE s.class_id=?"""
    args = [class_id]
    if status:
        sql += " AND w.status=?"
        args.append(status)
    sql += " ORDER BY w.submitted_at DESC, w.id DESC LIMIT 200"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    # writing_tasks 在内容库，跨库在 Python 侧合并
    tasks = {r["id"]: dict(r) for r in content_db().execute(
        "SELECT id, title, unit FROM writing_tasks").fetchall()}
    for r in rows:
        t = tasks.get(r.get("task_id"), {})
        r["task_title"] = t.get("title")
        r["unit"] = t.get("unit")
        r["ai_scores"] = _parse_json(r.pop("ai_scores_json", None), None)
        r["ai_feedback"] = _parse_json(r.pop("ai_feedback_json", None), None)
        r["teacher_scores"] = _parse_json(r.pop("teacher_scores_json", None), None)
    return {"writings": rows}


class WritingReviewReq(BaseModel):
    submission_id: int
    scores: dict = {}
    comment: str = ""


@app.post("/teacher/writing/review")
def teacher_writing_review(req: WritingReviewReq, request: Request):
    teacher_guard(request)
    conn = get_conn()
    conn.execute(
        """UPDATE writing_submissions SET teacher_scores_json=?, teacher_comment=?,
           status='teacher_reviewed', reviewed_at=datetime('now') WHERE id=?""",
        (json.dumps(req.scores, ensure_ascii=False), req.comment, req.submission_id))
    conn.commit()
    log_teacher_action("writing_review", detail={"submission_id": req.submission_id})
    return {"ok": True}


@app.get("/teacher/native_overview")
def teacher_native_overview(request: Request, class_id: str = "C6/1"):
    teacher_guard(request)
    conn = get_conn()
    tz = class_tz(class_id)
    students = conn.execute(
        """SELECT id, display_name, student_no FROM students
           WHERE class_id=? ORDER BY student_no""", (class_id,)).fetchall()
    out = []
    for s in students:
        sid = s["id"]
        dic = conn.execute(
            f"""SELECT COUNT(*) n, SUM(is_correct) c FROM dictation_events
                WHERE student_id=? AND date(created_at,'{tz}') >= date('now','{tz}','-7 days')""",
            (sid,)).fetchone()
        hw = conn.execute(
            f"""SELECT COUNT(*) n,
                       SUM(CASE WHEN x.status IN ('submitted','late','graded') THEN 1 ELSE 0 END) done
                FROM assignments a LEFT JOIN assignment_submissions x
                  ON x.assignment_id=a.id AND x.student_id=?
                WHERE a.class_id=?""", (sid, class_id)).fetchone()
        wr = conn.execute(
            """SELECT COUNT(*) n, AVG(json_extract(ai_scores_json,'$.总分')) s
               FROM writing_submissions WHERE student_id=? AND status!='draft'""",
            (sid,)).fetchone()
        last = conn.execute(
            "SELECT MAX(created_at) m FROM logs WHERE student_id=?", (sid,)).fetchone()["m"]
        out.append({
            "id": sid, "display_name": s["display_name"], "student_no": s["student_no"],
            "dictation_accuracy": round((dic["c"] or 0) / dic["n"] * 100) if dic["n"] else None,
            "homework_rate": round((hw["done"] or 0) / hw["n"] * 100) if hw["n"] else None,
            "writing_count": wr["n"] or 0,
            "writing_avg": round(wr["s"], 1) if wr["s"] else None,
            "last_active": last,
        })
    return {"class_id": class_id, "students": out,
            "assignments": conn.execute(
                "SELECT COUNT(*) c FROM assignments WHERE class_id=?", (class_id,)).fetchone()["c"]}


# ---------- 静态页面（必须放最后） ----------
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/teacher", StaticFiles(directory=TEACHER_DIR, html=True), name="teacher")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
