#!/usr/bin/env bash
# 服务器上的控制命令：一条命令看状态 / 日志 / 花费 / 它读了什么
#
# 用法：
#   ./deploy/ctl.sh           状态
#   ./deploy/ctl.sh logs      最近网关日志
#   ./deploy/ctl.sh srv       最近服务端日志
#   ./deploy/ctl.sh spend     今天和最近几天的花费
#   ./deploy/ctl.sh readings  它最近读了什么
#   ./deploy/ctl.sh restart   重启两个服务
#   ./deploy/ctl.sh check     一次性看全（状态+日志+花费）

set -uo pipefail
APP="$HOME/companion"
cd "$APP" || { echo "找不到 $APP"; exit 1; }

PORT=$(grep -E '^COMPANION_PORT=' .env 2>/dev/null | cut -d= -f2)
PORT=${PORT:-8787}
TOKEN=$(cat data/token.txt 2>/dev/null || true)

case "${1:-status}" in
  status)
    echo "== 服务 =="
    systemctl is-active companion
    echo "== 健康 =="
    curl -s "http://127.0.0.1:${PORT}/api/health?t=${TOKEN}" || echo "(无响应)"
    echo
    ;;
  logs)
    ;;
  srv)
    tail -n "${2:-25}" logs/server.out.log
    ;;
  spend)
    python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("data/companion.db")
cur = conn.execute(
    "SELECT day, kind, ROUND(SUM(cost_yuan),4), COUNT(*) FROM usage "
    "GROUP BY day, kind ORDER BY day DESC LIMIT 20"
)
rows = cur.fetchall()
if not rows:
    print("还没有花费记录")
else:
    for day, kind, yuan, n in rows:
        print(f"{day}  {kind:14s}  {yuan:>8.4f} 元  ({n} 次)")
PY
    ;;
  readings)
    python3 curiosity.py --list 2>/dev/null | head -12
    ;;
  memory)
    python3 memory.py --stats
    ;;
  backfill)
    set -a; . ./.env; set +a
    python3 memory.py --backfill && python3 memory.py
    ;;
  inner)
    set -a; . ./.env; set +a
    python3 inner.py
    ;;
  inner-audit)
    set -a; . ./.env; set +a
    python3 inner.py --audit
    ;;
  proactive)
    set -a; . ./.env; set +a
    python3 proactive.py --stats
    ;;
  judge)
    set -a; . ./.env; set +a
    python3 proactive.py
    ;;
  research)
    set -a; . ./.env; set +a
    python3 research.py --show
    ;;
  research-one)
    set -a; . ./.env; set +a
    python3 research.py
    ;;
  persona)
    set -a; . ./.env; set +a
    python3 persona.py
    ;;
  persona-build)
    set -a; . ./.env; set +a
    python3 persona.py --build
    ;;
  restart)
    sudo systemctl restart companion && echo "已重启"
    ;;
  check)
    "$0" status
    echo "== 最近网关 =="; "$0" logs 8
    echo "== 最近花费 =="; "$0" spend
    ;;
  *)
    echo "用法: $0 [status|logs|srv|spend|readings|memory|backfill|inner|inner-audit|proactive|judge|restart|check]"
    ;;
esac
