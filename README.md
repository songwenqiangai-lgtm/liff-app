# Thai-Chinese Tutor LIFF App（AI辅助二语教学硬脚手架平台）

研究定制版 MVP：FastAPI 后端 + LIFF 前端 + 教师仪表盘，含日志埋点与阶段开关。

## 你需要填的 6 个空（只有这些是代码之外的）

| # | 要填什么 | 在哪里 |
|---|---|---|
| 1 | LIFF ID | `frontend/assets/app.js` 第一行 `LIFF_ID` |
| 2 | LINE Login 频道的 Channel ID | `.env` 的 `LINE_CHANNEL_ID` |
| 3 | 你的 LLM API Key | `.env` 的 `LLM_API_KEY` |
| 4 | LLM 服务地址与模型名 | `.env` 的 `LLM_BASE_URL` / `LLM_MODEL` |
| 5 | 教师仪表盘密码 | `.env` 的 `TEACHER_PASSWORD` |
| 6 | 真实教学内容 | `content/items_track*.csv`（已放示例，替换成教材内容即可） |

## 目录结构

```
liff-app/
├── backend/          # FastAPI 后端
│   ├── main.py       # 全部接口
│   ├── database.py   # SQLite（学生/日志/阶段/题目）
│   ├── llm.py        # LLM 调用（OpenAI兼容格式）
│   ├── prompts.py    # 提示词（三级提示阶梯的核心）
│   └── requirements.txt
├── frontend/         # 学生端（LINE 内打开）
│   ├── index.html    # 首页（模块入口+阶段横幅）
│   ├── practice.html # 练习页（提示阶梯交互）
│   ├── progress.html # 我的进度
│   └── assets/       # app.js（LIFF逻辑）、style.css
├── teacher/          # 教师端（电脑浏览器访问 /teacher/）
│   └── index.html    # 日志查看+阶段开关+CSV导出
└── content/          # 教学内容CSV（按轨分文件）
```

## 第一部分：LINE 后台配置（约20分钟）

1. 打开 https://developers.line.biz 注册/登录（用你的 LINE 账号）
2. 创建 Provider（如 "RSU-Research"）
3. 在 Provider 下创建 **LINE Login** 频道（类型选 Web app）
4. 进入频道 -> **LIFF** 标签页 -> Add，设置：
   - Size: **Full**
   - Scope: 勾选 `profile`、`openid`
   - Endpoint URL: 先填 `https://你的域名/`（本地调试见下文 ngrok）
5. 创建后复制 **LIFF ID**（形如 `2001234567-AbCdEfGh`）填入 `frontend/assets/app.js`
6. 复制频道的 **Channel ID**（纯数字）填入 `.env` 的 `LINE_CHANNEL_ID`
7. （可选，推荐）创建 LINE Official Account，把 LIFF 链接挂到菜单，学生加好友即用

## 第二部分：本地运行

```bash
cd backend
pip install -r requirements.txt
# Windows PowerShell:
$env:LINE_CHANNEL_ID="你的频道ID"; $env:LLM_API_KEY="你的key"; $env:LLM_MODEL="模型名"; $env:TEACHER_PASSWORD="你的密码"
uvicorn main:app --reload --port 8000
```

浏览器打开 http://localhost:8000/teacher/ 测试仪表盘（LIFF 学生端需 HTTPS 域名才能测）。

## 第三部分：本地调试 LIFF（需要 HTTPS）

LINE 要求 LIFF endpoint 是 HTTPS。本地调试：

```bash
# 装 ngrok 或用 cloudflared tunnel
ngrok http 8000
# 把 ngrok 给的 https://xxxx.ngrok-free.app 填到 LIFF 后台的 Endpoint URL
# 然后手机 LINE 里打开你的 LIFF 链接测试
```

## 第四部分：部署到 Render（免费档）

1. 把整个 liff-app 文件夹推到 GitHub 私有仓库
2. Render.com -> New Web Service -> 选仓库
   - Build Command: `pip install -r backend/requirements.txt`
   - Start Command: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
3. Environment 里填 .env 的全部变量
4. 部署完成后的 https://xxxx.onrender.com 填回 LIFF 后台 Endpoint URL
5. 注意：Render 免费档会休眠，正式实验前一周升级到付费档（约$7/月），或用 Railway

## 阶段开关逻辑（研究保真度）

| 阶段 | 学生端 | 教师端 |
|---|---|---|
| P0 基线 | 只显示进度页 | 全班默认 P0 |
| P1 纯AI | 4个模块全开 | 教师不干预（协议约束） |
| P2 纯教师 | 模块隐藏（"AI老师休假中"） | 课堂加强辅导 |
| P3 协同 | 4个模块全开 | 按 AI 后台数据课堂跟进 |

切换在 /teacher/ 一键完成，**切换时间戳写入日志**。

## 日志字段（对应论文变量）

`student_id / class_id / track / phase / module / item_id / event_type / input_text / hint_level(1-4) / ai_latency_ms / created_at`

- `hint_level`：1=提示 2=强提示 3=例句 4=答案（能动性H3/H4的核心行为指标）
- `event_type`：session_start / input_submit / hint_request
- CSV 导出：教师端一键，列名即论文变量

## 已知边界（诚实说明）

- 语音模块未包含（第二版：录音->Whisper/Azure语音评测）
- 本代码为研究 MVP，首次部署预计需要一轮调试
- LLM 接口用 OpenAI 兼容格式；若你的 API 是 Anthropic 原生格式，改 `llm.py` 的请求体即可
- 数据库 SQLite 单文件，60-120 人规模足够；定期备份 app.db
