#!/usr/bin/env bash
cd "$HOME/companion" || exit 1
echo "=== 启动记录 ==="
grep -nE '同伴后端已启动|\[daily\]' logs/server.out.log | tail -5
echo
echo "=== 服务端最后 8 行 ==="
tail -8 logs/server.out.log
echo
echo "=== 研究结论分布 ==="
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("data/companion.db")
print(dict(c.execute("select verdict, count(*) from research_notes group by verdict").fetchall()))
for row in c.execute("select topic, verdict from research_notes order by id"):
    print("  %-10s %s" % row)
PY
echo
echo "=== 主动消息历史 ==="
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("data/companion.db")
print("按状态:", c.execute("select state, count(*) from intentions group by state").fetchall())
for r in c.execute("select id, state, t_sent, t_acked, substr(content,1,60) from intentions order by id"):
    print(" ", r)
PY
