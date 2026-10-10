import sqlite3, os, csv, glob, re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 持久化数据目录。
# 部署到 Render 时把它指向挂载的持久磁盘（如 /var/data），这样重新部署
# 不会清空学生数据；本地开发不设此变量，行为与以前完全一致。
DATA_DIR = os.getenv("DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")
CONTENT_DB_PATH = os.path.normpath(os.path.join(BASE_DIR, "..", "content", "content.db"))
CONTENT_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "content"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_user_id TEXT UNIQUE NOT NULL,
  display_name TEXT,
  class_id TEXT,
  track INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS phase_config(
  class_id TEXT PRIMARY KEY,
  phase TEXT NOT NULL DEFAULT 'P0',
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  track INTEGER,
  module TEXT,
  prompt_zh TEXT,
  prompt_th TEXT,
  answer TEXT,
  tags TEXT
);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER,
  class_id TEXT,
  phase TEXT,
  module TEXT,
  item_id INTEGER,
  event_type TEXT,
  input_text TEXT,
  hint_level INTEGER DEFAULT 0,
  ai_latency_ms INTEGER,
  response_json TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_logs_student ON logs(student_id);
CREATE INDEX IF NOT EXISTS idx_logs_phase ON logs(phase);

-- ============ 研究协议表 ============
CREATE TABLE IF NOT EXISTS classes(
  class_id TEXT PRIMARY KEY,
  grade INTEGER,
  track TEXT,
  current_phase TEXT DEFAULT 'P0',
  phase_start_date TEXT
);
CREATE TABLE IF NOT EXISTS hint_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, timestamp TEXT DEFAULT (datetime('now')),
  hint_level INTEGER, task_id INTEGER, accepted TEXT
);
CREATE TABLE IF NOT EXISTS feedback_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, feedback_time TEXT DEFAULT (datetime('now')),
  revision_time TEXT, interval_seconds INTEGER,
  adopted TEXT, reject_reason TEXT
);
CREATE TABLE IF NOT EXISTS avoidance_flags(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, flag_type TEXT, triggered_at TEXT DEFAULT (datetime('now')),
  resolved INTEGER DEFAULT 0, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS handoff_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT DEFAULT (datetime('now')),
  student_id INTEGER, class_id TEXT, t_type TEXT, direction TEXT,
  trigger TEXT, ai_action TEXT, teacher_action TEXT,
  closed INTEGER DEFAULT 0, closed_at TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS teacher_actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  teacher_id TEXT, timestamp TEXT DEFAULT (datetime('now')),
  action_type TEXT, target_class TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS fidelity_checklists(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  class_id TEXT, phase TEXT, lesson_date TEXT,
  items TEXT, score REAL, filled_by TEXT,
  filled_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS fidelity_corrections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  checklist_id INTEGER, note TEXT, corrected_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS assessments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, test_type TEXT, phase_or_wave TEXT,
  score REAL, rater_id TEXT, rater_blind INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS oral_samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, wave TEXT, file_url TEXT,
  rater1_id TEXT, rater2_id TEXT, caf_scores TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS class_lessons(
  class_id TEXT, book TEXT, lesson_no TEXT,
  PRIMARY KEY(class_id, book, lesson_no)
);

-- ============ 功能增强表（F1–F12） ============
-- F1 学生周报缓存：同一学生同一周唯一，懒生成时 INSERT OR IGNORE
CREATE TABLE IF NOT EXISTS weekly_reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER NOT NULL, week_start TEXT NOT NULL,
  metrics_json TEXT, generated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(student_id, week_start)
);
-- F2 薄弱点诊断快照
CREATE TABLE IF NOT EXISTS diagnostics(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, phase TEXT,
  weak_points_json TEXT, generated_at TEXT DEFAULT (datetime('now'))
);
-- F3 个性化学习建议（记录是否被点击，用于能动性证据）
CREATE TABLE IF NOT EXISTS suggestions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, suggestion_text TEXT, related_weak_points TEXT,
  related_lesson_id TEXT, action_href TEXT,
  created_at TEXT DEFAULT (datetime('now')), clicked_at TEXT
);
-- F4 反馈采纳理由（H3 能动性中介的行为证据）
CREATE TABLE IF NOT EXISTS decision_reasons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feedback_event_id INTEGER, student_id INTEGER,
  reason_type TEXT, reason_text TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
-- 站内通知中心（学生/教师共用）
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_type TEXT NOT NULL, user_id TEXT NOT NULL,
  kind TEXT, title TEXT, body TEXT,
  read INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
-- F10 无 AI 迁移任务（线下任务成绩，task_source 固定 offline）
CREATE TABLE IF NOT EXISTS task_offline(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER, phase TEXT, task_type TEXT,
  score REAL, submitted_at TEXT DEFAULT (datetime('now')),
  task_source TEXT DEFAULT 'offline'
);

CREATE INDEX IF NOT EXISTS idx_weekly_student ON weekly_reports(student_id);
CREATE INDEX IF NOT EXISTS idx_diag_student ON diagnostics(student_id);
CREATE INDEX IF NOT EXISTS idx_sugg_student ON suggestions(student_id);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_type, user_id, read);
CREATE INDEX IF NOT EXISTS idx_offline_student ON task_offline(student_id);
CREATE INDEX IF NOT EXISTS idx_hint_student ON hint_events(student_id);
CREATE INDEX IF NOT EXISTS idx_fb_student ON feedback_events(student_id);
CREATE INDEX IF NOT EXISTS idx_avoid_student ON avoidance_flags(student_id);
CREATE INDEX IF NOT EXISTS idx_handoff_student ON handoff_events(student_id);
CREATE INDEX IF NOT EXISTS idx_handoff_class ON handoff_events(class_id);
CREATE INDEX IF NOT EXISTS idx_assess_student ON assessments(student_id);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.isolation_level = None  # 自动提交，避免跨连接读锁阻塞写入
    return conn


def content_db():
    """只读连接：统一内容数据库（题目/生词/课文/语法/考试）"""
    conn = sqlite3.connect(CONTENT_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """为已存在的旧表补新增列（幂等）"""
    def add_col(table, col, ddl):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    add_col("students", "student_no", "TEXT")
    add_col("phase_config", "ai_enabled", "INTEGER DEFAULT 1")
    add_col("phase_config", "teacher_response_allowed", "INTEGER DEFAULT 1")
    add_col("phase_config", "start_time", "TEXT")
    add_col("phase_config", "end_time", "TEXT")
    add_col("logs", "detail", "TEXT")
    # 功能增强：区分 AI 参与 vs 线下任务、关联决策理由、区分自动触发
    add_col("assessments", "task_source", "TEXT DEFAULT 'ai'")
    add_col("feedback_events", "decision_reason_id", "INTEGER")
    add_col("handoff_events", "auto_triggered", "INTEGER DEFAULT 0")
    conn.commit()


def seed_items(force: bool = False):
    """从 content/items_track*.csv 导入题目；默认只在空表时导入"""
    init_db()  # 防御：确保表已存在（应对文件系统同步延迟等异常）
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        if n > 0 and not force:
            return
        if force:
            conn.execute("DELETE FROM items")
        for f in sorted(glob.glob(os.path.join(CONTENT_DIR, "items_track*.csv"))):
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    conn.execute(
                        "INSERT INTO items(track,module,prompt_zh,prompt_th,answer,tags) VALUES(?,?,?,?,?,?)",
                        (
                            int(row["track"]),
                            row["module"],
                            row["prompt_zh"],
                            row["prompt_th"],
                            row["answer"],
                            row.get("tags", ""),
                        ),
                    )
