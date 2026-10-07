# -*- coding: utf-8 -*-
"""统计与诊断（F1–F12）。

设计原则：
- 只做数据计算，不写业务副作用；写库统一由 main.py 调用本模块的 detect_* 结果后完成
- 所有"按天/按周"统计一律走泰国时区 `date(col,'+7 hours')`（见 FC.TZ_OFFSET），
  否则泰国学生晚间 17:00 之后的行为会被算到第二天
- 内容侧数据（题目标签、课文、考试）一律查 content.db；行为侧查 app.db
"""
import json
import re
from datetime import datetime, timedelta, timezone

import fidelity_config as FC
from database import get_conn, content_db

TZ = timezone(timedelta(hours=7))


# ---------- 时间工具 ----------

def thai_now() -> datetime:
    """当前泰国时间（UTC+7）"""
    return datetime.now(TZ)


def thai_date(col: str) -> str:
    """SQL 片段：把 UTC 时间列转成泰国本地日期"""
    return f"date({col},'{FC.TZ_OFFSET}')"


def thai_days_ago(n: int) -> str:
    """n 天前的泰国日期（字符串）"""
    return (thai_now() - timedelta(days=n)).date().isoformat()


def week_bounds(ref: datetime = None):
    """本周（周一为一周之始）的泰国日期区间，返回 (start, end) 字符串"""
    ref = ref or thai_now()
    start = (ref - timedelta(days=ref.weekday())).date()
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


# ---------- 行为统计 ----------

def _verdict_of(response_json):
    """从 logs.response_json 里取出 AI 判定"""
    if not response_json:
        return ""
    try:
        return (json.loads(response_json) or {}).get("verdict", "") or ""
    except Exception:
        return ""


def student_metrics(sid: int, start: str, end: str) -> dict:
    """区间内的学习指标（周报 F1 的核心）"""
    conn = get_conn()
    d = thai_date("created_at")
    rows = conn.execute(
        f"""SELECT event_type, module, hint_level, response_json, {d} AS day
            FROM logs WHERE student_id=? AND {d} BETWEEN ? AND ?""",
        (sid, start, end),
    ).fetchall()

    submits = hints = answer_views = 0
    correct = partial = incorrect = 0
    days, by_module = set(), {}
    for r in rows:
        et = r["event_type"]
        days.add(r["day"])
        if et == "input_submit":
            submits += 1
            by_module[r["module"]] = by_module.get(r["module"], 0) + 1
            v = _verdict_of(r["response_json"])
            if v == "correct":
                correct += 1
            elif v == "partial":
                partial += 1
            elif v == "incorrect":
                incorrect += 1
        elif et == "hint_request":
            hints += 1
            if r["hint_level"] == 4:
                answer_views += 1

    graded = correct + partial + incorrect
    return {
        "week_start": start,
        "week_end": end,
        "submissions": submits,
        "hint_requests": hints,
        "answer_views": answer_views,
        "active_days": len(days),
        "correct": correct,
        "partial": partial,
        "incorrect": incorrect,
        # 正确率把 partial 记半分，避免"部分正确"被当成完全错误
        "accuracy": round((correct + partial * 0.5) / graded * 100) if graded else None,
        "hint_per_submit": round(hints / submits, 2) if submits else None,
        "by_module": by_module,
    }


def compare_weeks(sid: int) -> dict:
    """本周 vs 上周（周报 F1 的"进步/退步"对比）"""
    cur_start, cur_end = week_bounds()
    prev_ref = thai_now() - timedelta(days=7)
    prev_start, prev_end = week_bounds(prev_ref)
    cur = student_metrics(sid, cur_start, cur_end)
    prev = student_metrics(sid, prev_start, prev_end)
    cur["prev"] = prev
    cur["delta"] = {
        "submissions": cur["submissions"] - prev["submissions"],
        "accuracy": (
            None if cur["accuracy"] is None or prev["accuracy"] is None
            else cur["accuracy"] - prev["accuracy"]
        ),
        "hint_requests": cur["hint_requests"] - prev["hint_requests"],
    }
    return cur


# ---------- F2 薄弱点诊断 ----------

def parse_tags(tags: str):
    """解析 content.db 的 items.tags，格式 "模块:知识点,第N课"。

    返回 (模块, 知识点, 课次) 三元组，缺项为 None。
    """
    if not tags:
        return None, None, None
    module = point = lesson = None
    for part in (p.strip() for p in tags.split(",")):
        if not part:
            continue
        if re.fullmatch(r"第.+课", part):
            lesson = part[1:-1]
        elif ":" in part or "：" in part:
            sep = ":" if ":" in part else "："
            module, _, point = part.partition(sep)
            module, point = module.strip(), point.strip()
    return module, point, lesson


def weak_points(sid: int, limit: int = 5) -> list:
    """按知识点聚合学生的错误作答，返回错误最多的前 N 个。

    错误来源：logs 中 event_type='input_submit' 且 response_json.verdict
    为 incorrect / partial 的记录；知识点取自 content.db 的 items.tags。
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT item_id, response_json FROM logs
           WHERE student_id=? AND event_type='input_submit' AND item_id IS NOT NULL""",
        (sid,),
    ).fetchall()

    tally = {}   # (module, point) -> {count, lesson, item_id, module_key}
    for r in rows:
        if _verdict_of(r["response_json"]) not in ("incorrect", "partial"):
            continue
        tally.setdefault(r["item_id"], 0)
        tally[r["item_id"]] += 1

    if not tally:
        return []

    # 一次性取出这些题目的标签
    ids = list(tally)
    ph = ",".join("?" * len(ids))
    items = content_db().execute(
        f"SELECT id, tags FROM items WHERE id IN ({ph})", ids
    ).fetchall()
    tag_map = {it["id"]: it["tags"] for it in items}

    agg = {}
    for item_id, n in tally.items():
        module, point, lesson = parse_tags(tag_map.get(item_id))
        if not point:
            continue
        key = (module or "其他", point)
        cur = agg.setdefault(key, {"count": 0, "lesson": lesson, "item_ids": []})
        cur["count"] += n
        cur["item_ids"].append(item_id)
        if lesson and not cur["lesson"]:
            cur["lesson"] = lesson

    out = [
        {
            "module": m, "point": p,
            "module_label": FC.WEAK_POINT_MODULE_LABELS.get(m, m),
            "count": v["count"], "lesson": v["lesson"],
        }
        for (m, p), v in agg.items()
        if v["count"] >= FC.WEAK_POINT_MIN_COUNT
    ]
    out.sort(key=lambda x: (-x["count"], x["point"] or ""))
    return out[:limit]


def save_diagnostics(sid: int, phase: str, points: list):
    conn = get_conn()
    conn.execute(
        "INSERT INTO diagnostics(student_id,phase,weak_points_json) VALUES(?,?,?)",
        (sid, phase, json.dumps(points, ensure_ascii=False)),
    )
    conn.commit()


# ---------- F3 个性化学习建议 ----------

def build_suggestions(points: list) -> list:
    """按薄弱点生成建议。每条含中泰双语文案与可点击跳转目标。"""
    out = []
    for p in points:
        tpl = FC.SUGGESTION_TEMPLATES.get(p["module"]) or FC.SUGGESTION_FALLBACK
        fmt = {"point": p["point"], "lesson": p["lesson"] or "—"}
        out.append({
            "zh": tpl["zh"].format(**fmt),
            "th": tpl["th"].format(**fmt),
            "weak_point": f"{p['module']}:{p['point']}",
            "lesson": p["lesson"],
            "module": tpl["module"],
        })
    if not out:
        out.append({
            "zh": FC.SUGGESTION_FALLBACK["zh"], "th": FC.SUGGESTION_FALLBACK["th"],
            "weak_point": None, "lesson": None, "module": FC.SUGGESTION_FALLBACK["module"],
        })
    return out


def ensure_suggestions(sid: int, points: list) -> list:
    """取该生现有建议；薄弱点组合发生变化时重新生成（幂等）。

    点击记录不丢：suggestion_click 事件同时写进 logs，研究数据以 logs 为准。
    """
    conn = get_conn()
    sig = "|".join(sorted(f"{p['module']}:{p['point']}" for p in points)) or "(none)"
    rows = conn.execute(
        """SELECT id, suggestion_text, related_weak_points, related_lesson_id,
                  action_href, clicked_at
           FROM suggestions WHERE student_id=? ORDER BY id""",
        (sid,),
    ).fetchall()
    stored = "|".join(sorted({(r["related_weak_points"] or "(none)") for r in rows}))
    if rows and stored == sig:
        return [dict(r) for r in rows]

    conn.execute("DELETE FROM suggestions WHERE student_id=?", (sid,))
    for s in build_suggestions(points):
        href = None
        if s["lesson"]:
            href = f"lesson.html?lesson={s['lesson']}&module={s['module']}"
        conn.execute(
            """INSERT INTO suggestions(student_id,suggestion_text,related_weak_points,
                                       related_lesson_id,action_href)
               VALUES(?,?,?,?,?)""",
            (sid, json.dumps({"zh": s["zh"], "th": s["th"]}, ensure_ascii=False),
             s["weak_point"], s["lesson"], href),
        )
    conn.commit()
    return ensure_suggestions(sid, points)


# ---------- F9 能力雷达图 ----------

def radar_scores(sid: int) -> dict:
    """五维能力指标，0–100；无数据返回 None（前端显示"暂无数据"）。

    说明：这是**基于平台行为的指标**，不是标准化测验分数。
    听力=会话题正确率 / 口语=跟读活跃度+口语CAF / 阅读=课文语法活跃度
    写作=写作题正确率 / 综合=考试平均分
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT module, response_json FROM logs
           WHERE student_id=? AND event_type='input_submit'""",
        (sid,),
    ).fetchall()

    per_module = {}   # module -> [correct, graded]
    for r in rows:
        v = _verdict_of(r["response_json"])
        if v not in ("correct", "partial", "incorrect"):
            continue
        slot = per_module.setdefault(r["module"], [0, 0])
        slot[1] += 1
        if v == "correct":
            slot[0] += 1
        elif v == "partial":
            slot[0] += 0.5

    def rate(module):
        slot = per_module.get(module)
        if not slot or not slot[1]:
            return None
        return round(slot[0] / slot[1] * 100)

    # 跟读次数：20 次封顶记满分
    speak_cnt = conn.execute(
        "SELECT COUNT(*) c FROM logs WHERE student_id=? AND event_type='pronounce_check'",
        (sid,),
    ).fetchone()["c"]
    speaking = min(100, round(speak_cnt / 20 * 100)) if speak_cnt else None

    # 口语 CAF：有评分则与跟读活跃度取平均
    caf = conn.execute(
        "SELECT caf_scores FROM oral_samples WHERE student_id=? AND caf_scores IS NOT NULL",
        (sid,),
    ).fetchall()
    caf_vals = []
    for r in caf:
        try:
            d = json.loads(r["caf_scores"])
            vals = [float(v) for v in d.values() if isinstance(v, (int, float))]
            if vals:
                caf_vals.append(sum(vals) / len(vals))
        except Exception:
            pass
    if caf_vals:
        caf_score = round(sum(caf_vals) / len(caf_vals) / 5 * 100)
        speaking = caf_score if speaking is None else round((speaking + caf_score) / 2)

    # 阅读：词汇/句型类模块的提交占比（与已判定提交总数同分母，保证 0–100）
    graded_total = sum(s[1] for s in per_module.values())
    read_cnt = sum(s[1] for m, s in per_module.items() if m in ("vocab", "pattern"))
    reading = round(read_cnt / graded_total * 100) if graded_total else None

    # 综合：考试平均分
    exams = conn.execute(
        """SELECT response_json FROM logs
           WHERE student_id=? AND event_type='exam_submit'""",
        (sid,),
    ).fetchall()
    scores = []
    for r in exams:
        try:
            s = (json.loads(r["response_json"]) or {}).get("score")
            if isinstance(s, (int, float)):
                scores.append(float(s))
        except Exception:
            pass
    overall = round(sum(scores) / len(scores)) if scores else None

    dims = {
        "listening": rate("chat"),
        "speaking": speaking,
        "reading": reading,
        "writing": rate("writing"),
        "overall": overall,
    }
    return {
        "dimensions": [{"key": k, "label": lbl, "value": dims.get(k)}
                       for k, lbl in FC.RADAR_DIMENSIONS],
        "note": "基于平台学习行为的指标，非标准化测验分数",
    }


# ---------- F1 学生学习周报 ----------

def generate_weekly_report(sid: int, force: bool = False) -> dict:
    """本周报告：指标**实时计算**，同时把最新快照覆盖进 weekly_reports。

    早期版本用 INSERT OR IGNORE 冻结首次结果，学生周中再打开看到的还是
    登录那一刻的旧数据。改为每次重算并覆盖快照——研究数据以 logs 为准，
    快照只作留档。
    """
    start, end = week_bounds()
    metrics = compare_weeks(sid)
    metrics["weak_points"] = weak_points(sid)
    conn = get_conn()
    conn.execute(
        """INSERT INTO weekly_reports(student_id, week_start, metrics_json, generated_at)
           VALUES(?,?,?,datetime('now'))
           ON CONFLICT(student_id, week_start)
           DO UPDATE SET metrics_json=excluded.metrics_json, generated_at=excluded.generated_at""",
        (sid, start, json.dumps(metrics, ensure_ascii=False)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT generated_at FROM weekly_reports WHERE student_id=? AND week_start=?",
        (sid, start),
    ).fetchone()
    metrics["generated_at"] = row["generated_at"] if row else None
    metrics["cached"] = False
    return metrics


# ---------- 通知（站内通知中心） ----------

def notify(user_type: str, user_id, kind: str, title: str, body: str = "",
           dedup: bool = True):
    """写一条站内通知。dedup=True 时，同用户同类型未读的不重复创建。"""
    conn = get_conn()
    uid = str(user_id)
    if dedup:
        exists = conn.execute(
            """SELECT 1 FROM notifications
               WHERE user_type=? AND user_id=? AND kind=? AND read=0 LIMIT 1""",
            (user_type, uid, kind),
        ).fetchone()
        if exists:
            return False
    conn.execute(
        "INSERT INTO notifications(user_type,user_id,kind,title,body) VALUES(?,?,?,?,?)",
        (user_type, uid, kind, title, body),
    )
    conn.commit()
    return True


def unread_count(user_type: str, user_id) -> int:
    return get_conn().execute(
        "SELECT COUNT(*) c FROM notifications WHERE user_type=? AND user_id=? AND read=0",
        (user_type, str(user_id)),
    ).fetchone()["c"]


# ---------- 扫描器：只做检测，返回待处理项；写库由 main.py 完成 ----------

def detect_avoidance() -> list:
    """检测回避行为。返回 [(student_id, flag_type), ...]

    两类：
    - no_submit_2d：活跃班（P1/P3）近 7 天有活动，但最后一次提交早于阈值
    - session_abort：开始会话后 SESSION_ABORT_MINUTES 内无任何后续事件
    """
    conn = get_conn()
    d = thai_date("created_at")
    out = []

    # ① 连续未提交
    active = conn.execute(
        """SELECT s.id FROM students s
           JOIN phase_config pc ON pc.class_id = s.class_id
           WHERE pc.phase IN ('P1','P3')"""
    ).fetchall()
    for r in active:
        sid = r["id"]
        last = conn.execute(
            """SELECT MAX(created_at) m FROM logs
               WHERE student_id=? AND event_type='input_submit'""",
            (sid,),
        ).fetchone()["m"]
        recent = conn.execute(
            f"""SELECT COUNT(*) c FROM logs
                WHERE student_id=? AND {d} >= date('now','{FC.TZ_OFFSET}','-7 days')""",
            (sid,),
        ).fetchone()["c"]
        if not recent:
            continue  # 近 7 天完全没活动 → 由 motivation 提醒负责，不算回避
        if not last:
            continue
        days = conn.execute(
            f"SELECT CAST(julianday(date('now','{FC.TZ_OFFSET}')) - julianday(date(?,'{FC.TZ_OFFSET}')) AS INT)",
            (last,),
        ).fetchone()[0]
        if days is not None and days >= FC.NO_SUBMIT_DAYS:
            out.append((sid, "no_submit_2d"))

    # ② 会话骤停（只看近 7 天的会话，避免反复标记历史会话）
    starts = conn.execute(
        f"""SELECT student_id, created_at FROM logs
            WHERE event_type='session_start'
              AND {d} >= date('now','{FC.TZ_OFFSET}','-7 days')"""
    ).fetchall()
    for r in starts:
        nxt = conn.execute(
            f"""SELECT COUNT(*) c FROM logs
                WHERE student_id=? AND created_at > ?
                  AND created_at <= datetime(?, '+{FC.SESSION_ABORT_MINUTES} minutes')""",
            (r["student_id"], r["created_at"], r["created_at"]),
        ).fetchone()["c"]
        if nxt == 0:
            out.append((r["student_id"], "session_abort"))

    # 同一学生同一类型只报一次（写库侧还有一层去重，这里先收敛）
    return list(dict.fromkeys(out))


def detect_t2_pending() -> list:
    """F7：AI 判错后超过 T2_IDLE_HOURS 小时仍无新提交 → 待建 T2 反馈授权交接。

    只针对**判错的题**（verdict 为 incorrect/partial）。答对时学生本就不需要
    修订，若也触发会把交接队列淹没。写库侧还有一层去重：同一学生至多一条
    未闭环的 T2。

    返回 [(student_id, item_id, class_id), ...]
    """
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT l.student_id, l.item_id, s.class_id, l.response_json
            FROM logs l JOIN students s ON s.id = l.student_id
            WHERE l.event_type='input_submit'
              AND datetime(l.created_at,'+{FC.T2_IDLE_HOURS} hours') < datetime('now')
              AND NOT EXISTS (
                SELECT 1 FROM logs n
                WHERE n.student_id=l.student_id AND n.item_id=l.item_id
                  AND n.event_type='input_submit' AND n.created_at > l.created_at)"""
    ).fetchall()
    seen, out = set(), []
    for r in rows:
        if _verdict_of(r["response_json"]) not in ("incorrect", "partial"):
            continue
        key = (r["student_id"], r["item_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append((r["student_id"], r["item_id"], r["class_id"]))
    return out


def detect_idle_students(days: int = None) -> list:
    """F12：连续 days 天未登录的学生。返回 [(student_id, 天数), ...]"""
    days = days or FC.MOTIVATION_IDLE_DAYS
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT s.id, MAX(l.created_at) m FROM students s
            LEFT JOIN logs l ON l.student_id = s.id AND l.event_type='student_login'
            GROUP BY s.id"""
    ).fetchall()
    out = []
    for r in rows:
        if not r["m"]:
            continue
        gap = conn.execute(
            f"SELECT CAST(julianday(date('now','{FC.TZ_OFFSET}')) - julianday(date(?,'{FC.TZ_OFFSET}')) AS INT)",
            (r["m"],),
        ).fetchone()[0]
        if gap is not None and gap >= days:
            out.append((r["id"], gap))
    return out


def detect_phase_reminders() -> list:
    """F8：阶段 end_time 距今不足 PHASE_REMIND_DAYS 天的班级。

    返回 [(class_id, phase, 剩余天数), ...]
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT class_id, phase, end_time FROM phase_config
           WHERE end_time IS NOT NULL"""
    ).fetchall()
    out = []
    for r in rows:
        left = conn.execute(
            f"SELECT CAST(julianday(date(?,'{FC.TZ_OFFSET}')) - julianday(date('now','{FC.TZ_OFFSET}')) AS INT)",
            (r["end_time"],),
        ).fetchone()[0]
        if left is not None and 0 <= left <= FC.PHASE_REMIND_DAYS:
            out.append((r["class_id"], r["phase"], left))
    return out


def phase_days_elapsed(class_id: str):
    """班级当前阶段已持续天数（班级周报 F5 用）"""
    conn = get_conn()
    row = conn.execute(
        "SELECT start_time FROM phase_config WHERE class_id=?", (class_id,)
    ).fetchone()
    if not row or not row["start_time"]:
        return None
    return conn.execute(
        f"SELECT CAST(julianday(date('now','{FC.TZ_OFFSET}')) - julianday(date(?,'{FC.TZ_OFFSET}')) AS INT)",
        (row["start_time"],),
    ).fetchone()[0]


# ---------- F5 教师班级周报 ----------

def teacher_weekly_report() -> dict:
    """各班本周概览：阶段、持续天数、交接数+闭环率、回避预警、需关注学生。"""
    conn = get_conn()
    start, end = week_bounds()
    d = thai_date("timestamp")
    classes = conn.execute("SELECT class_id, phase FROM phase_config ORDER BY class_id").fetchall()

    out = []
    for c in classes:
        cid = c["class_id"]
        n_students = conn.execute(
            "SELECT COUNT(*) n FROM students WHERE class_id=?", (cid,)
        ).fetchone()["n"]

        hs = conn.execute(
            f"""SELECT COUNT(*) total, SUM(CASE WHEN closed=1 THEN 1 ELSE 0 END) closed
                FROM handoff_events WHERE class_id=? AND {d} BETWEEN ? AND ?""",
            (cid, start, end),
        ).fetchone()
        total = hs["total"] or 0
        closed = hs["closed"] or 0

        flags_new = conn.execute(
            f"""SELECT COUNT(*) n FROM avoidance_flags af
                JOIN students s ON s.id = af.student_id
                WHERE s.class_id=? AND {thai_date('af.triggered_at')} BETWEEN ? AND ?""",
            (cid, start, end),
        ).fetchone()["n"]
        flags_open = conn.execute(
            """SELECT COUNT(*) n FROM avoidance_flags af
               JOIN students s ON s.id = af.student_id
               WHERE s.class_id=? AND af.resolved=0""",
            (cid,),
        ).fetchone()["n"]

        # 需关注学生：未解决预警数 + 提示依赖度排序取前 3
        watch = conn.execute(
            f"""SELECT s.id, s.display_name,
                       (SELECT COUNT(*) FROM avoidance_flags a
                        WHERE a.student_id=s.id AND a.resolved=0) AS flags,
                       (SELECT COUNT(*) FROM hint_events h
                        WHERE h.student_id=s.id AND {thai_date('h.timestamp')} BETWEEN ? AND ?) AS hints
                FROM students s WHERE s.class_id=?
                ORDER BY flags DESC, hints DESC LIMIT 3""",
            (start, end, cid),
        ).fetchall()

        out.append({
            "class_id": cid, "phase": c["phase"],
            "phase_days": phase_days_elapsed(cid),
            "n_students": n_students,
            "handoffs_total": total, "handoffs_closed": closed,
            "close_rate": round(closed / total * 100) if total else None,
            "flags_new": flags_new, "flags_open": flags_open,
            "watch": [dict(w) for w in watch if (w["flags"] or w["hints"])],
        })

    return {"week_start": start, "week_end": end, "classes": out,
            "pending_phase_reminders": detect_phase_reminders()}
