#!/usr/bin/env bash
# 服务器端一次性安装：Node 22 + systemd 服务 + 启动
#
# 为什么需要：服务器只有干净的 Ubuntu，没有 Node；而且要让服务常驻、崩溃自动重启。
# 做完会得到什么：一个 systemd 服务 companion，开机自启，挂了自动拉起。

set -euo pipefail

APP_DIR="$HOME/companion"
TARBALL="$HOME/companion.tar.gz"

echo "== 1. 解包 =="
if [ -f "$TARBALL" ]; then
  mkdir -p "$APP_DIR"
  tar -xzf "$TARBALL" -C "$APP_DIR"
  echo "   解开到 $APP_DIR"
else
  echo "   [跳过] 没找到 $TARBALL（假设目录已经存在）"
fi

cd "$APP_DIR"

echo "== 2. 保护配置文件 =="
chmod 600 .env 2>/dev/null || true
chmod 700 data 2>/dev/null || true

# 服务器上只让本机网关访问，不对公网开 8787（要手机网页版再改回 0.0.0.0 + 开安全组）
if ! grep -q "^COMPANION_HOST=" .env; then
  echo "COMPANION_HOST=127.0.0.1" >> .env
  echo "   已设 COMPANION_HOST=127.0.0.1（只本机可访问）"
fi

# 清掉从本地带来的 pid，避免误导
rm -f data/server.pid data/gateway.pid

echo "== 3. 装 Node 22 =="
if command -v node >/dev/null 2>&1 && [ "$(node -v | cut -d. -f1 | tr -d v)" -ge 22 ]; then
  echo "   已有 $(node -v)"
else
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
  echo "   装好 $(node -v)"
fi

echo "== 4. 写 systemd 服务 =="
sudo tee /etc/systemd/system/companion.service >/dev/null <<EOF
[Unit]
Description=Companion server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=/usr/bin/python3 $APP_DIR/server.py
Restart=always
RestartSec=5
StandardOutput=append:$APP_DIR/logs/server.out.log
StandardError=append:$APP_DIR/logs/server.err.log

[Install]
WantedBy=multi-user.target
EOF


mkdir -p "$APP_DIR/logs"

echo "== 5. 启动 =="
sudo systemctl daemon-reload
sudo systemctl enable --now companion.service
sleep 3
sleep 4

echo "== 6. 状态 =="
sudo systemctl --no-pager --lines=0 status companion.service || true

echo
echo "== 健康检查 =="
PORT=$(grep -E '^COMPANION_PORT=' .env | cut -d= -f2)
PORT=${PORT:-8787}
TOKEN=$(cat data/token.txt 2>/dev/null || true)
curl -s "http://127.0.0.1:${PORT}/api/health?t=${TOKEN}" || echo "(健康检查失败，看 logs/)"
echo
echo
echo "完成。查看日志："
echo "  sudo journalctl -u companion -n 50 --no-pager"
