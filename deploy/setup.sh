#!/usr/bin/env bash
# 一键部署脚本 —— 在服务器 /opt/liff-app 下以 root 执行： bash deploy/setup.sh
# 前提：代码已复制到 /opt/liff-app，且已手动配置好 /opt/liff-app/backend/.env
set -euo pipefail
APP_DIR=/opt/liff-app
DOMAIN="${1:-}"   # 用法: bash deploy/setup.sh my.domain.com

echo "== [1/6] 系统更新 + 基础包 =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx curl git

echo "== [2/6] Python 虚拟环境 + 依赖 =="
cd "$APP_DIR/backend"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "== [3/6] 校验 .env 是否存在且可达 =="
if [ ! -f "$APP_DIR/backend/.env" ]; then
  cp "$APP_DIR/deploy/.env.production.example" "$APP_DIR/backend/.env"
  echo ">>> 已生成 .env 模板。请先编辑填写正式值，再重新运行本脚本："
  echo "    nano /opt/liff-app/backend/.env"
  exit 1
fi
echo "    .env 已就绪"

echo "== [4/6] 权限 + systemd 服务 =="
chown -R www-data:www-data "$APP_DIR"
cp "$APP_DIR/deploy/liff-app.service" /etc/systemd/system/liff-app.service
mkdir -p "$APP_DIR/backend"
touch "$APP_DIR/backend/uvicorn.log"
chown www-data:www-data "$APP_DIR/backend/uvicorn.log"
systemctl daemon-reload
systemctl enable liff-app
systemctl restart liff-app || true

echo "== [5/6] 本机冒烟（不经域名，直连 8000）=="
sleep 2
curl -s -o /dev/null -w "    http://127.0.0.1:8000/teacher/ -> %{http_code}\n" http://127.0.0.1:8000/teacher/ || echo "    (未通，可查日志 journalctl -u liff-app)"

echo "== [6/6] HTTPS/域名 前置检查 =="
if [ -n "$DOMAIN" ]; then
  echo "    域名: $DOMAIN （请先在域名商解析 A 记录 -> $APP_DIR 填入后再 certbot）"
  echo "    证书步骤稍后单独跑: certbot --nginx -d $DOMAIN"
else
  echo "    未提供域名 -> LINE 需要 HTTPS，暂无法接入；可先 http://本机IP:8000 预览教师端"
fi

echo ""
echo "完成。排查日志: journalctl -u liff-app -e"