#!/usr/bin/env bash
# 安全清理 —— 正式实验开始前执行一次：
#   1) 删除 /dev 调试后门代码（后端路由 + 前端 DEV_MODE 跳过）
#   2) 清空 dev-local- 调试学生及其日志
#   3) 班级阶段重置（可选）——由你决定
set -euo pipefail
APP_DIR=/opt/liff-app
PY="$APP_DIR/backend/venv/bin/python"

echo "== 1/3 移除后端 /dev 路由 =="
sed -i '/调试后门（仅本机/d' "$APP_DIR/backend/main.py"
sed -i '/DEV_LAUNCHER_HTML/d' "$APP_DIR/backend/main.py"
sed -i '/class DevLoginReq/,/^def teacher_guard/p' "$APP_DIR/backend/main.py" 2>/dev/null || true
# 用 python 精确删除 DevLoginReq 与三条 /dev 路由块（更稳）
$PY - <<'PYEOF'
import re,io
p="/opt/liff-app/backend/main.py"
s=open(p,encoding="utf-8").read()
# 删除 DEV_LAUNCHER_HTML 定义块
s=re.sub(r"DEVELOPER_CHOICES.*?\nDEV_LAUNCHER_HTML = .*?\n\n", "\n", s, flags=re.S)
# 删除 /dev 三条路由 + 模型
s=re.sub(r"@app\.get\(\"/dev\"\).*?\n# ---------- 静态页面[^\n]*", "# ---------- 静态页面（必须放最后）", s, flags=re.S)
# 删除 dev_enabled / _is_loopback 帮助函数
s=re.sub(r"# ---------- 调试后门（仅本机.*?]\n\n\n", "\n", s, flags=re.S)
open(p,"w",encoding="utf-8").write(s)
print("后端 /dev 已移除")
PYEOF

echo "== 2/3 移除前端 DEV_MODE 跳过 =="
sed -i '/本机调试后门：仅在 localhost/,+2d' "$APP_DIR/frontend/assets/app.js"
# 还原 boot() 第一段
sed -i '/DEV_MODE && localStorage.getItem("session_token")/{N;d;}' "$APP_DIR/frontend/assets/app.js"

echo "== 3/3 清理调试学生与日志 =="
$PY - <<'PYEOF'
import sqlite3
db="/opt/liff-app/backend/app.db"
c=sqlite3.connect(db); c.row_factory=sqlite3.Row
ids=[r["id"] for r in c.execute("SELECT id FROM students WHERE line_user_id LIKE 'dev-local-%'")]
ph=[",".join("?"*len(ids))]
c.executemany("DELETE FROM logs WHERE student_id=?", [(i,) for i in ids])
c.executemany("DELETE FROM students WHERE id=?", [(i,) for i in ids])
c.commit()
print(f"已清理 {len(ids)} 个调试学生及其日志")
PYEOF

echo "== 重载服务 =="
systemctl restart liff-app
echo "完成。正式实验前建议再人工 review 一次代码中的 'dev|debug|DEV_'。"