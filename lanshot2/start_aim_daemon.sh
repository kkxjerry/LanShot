#!/bin/bash
# start_aim_daemon.sh - 启动 Google AI Mode 极速热通道守护进程

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$DIR/aim_daemon.log"
PID_FILE="$DIR/aim_daemon.pid"

if lsof -Pi :18888 -sTCP:LISTEN -t >/dev/null ; then
    echo "[AIM Daemon] ⚠️ 端口 18888 已经在运行中！"
    curl -s http://127.0.0.1:18888/health
    echo ""
    exit 0
fi

echo "[AIM Daemon] 正在启动 Google AI Mode 极速热通道守护进程..."
(ego-browser nodejs < "$DIR/aim_daemon.js") > "$LOG_FILE" 2>&1 &
DAEMON_PID=$!
echo "$DAEMON_PID" > "$PID_FILE"
echo "[AIM Daemon] 进程已在后台启动 (PID: $DAEMON_PID)，日志: $LOG_FILE"

echo "[AIM Daemon] 正在等待页面预热与服务就绪..."
for i in {1..25}; do
    if curl -s http://127.0.0.1:18888/health | grep -q '"warm":true'; then
        echo "======================================================="
        echo "✅ Google AI Mode 极速热通道就绪！(耗时 ~${i}s)"
        echo "   服务地址: http://127.0.0.1:18888"
        echo "   测试命令: $DIR/aim \"你的技术问题\""
        echo "======================================================="
        exit 0
    fi
    sleep 1
done

echo "[AIM Daemon] ⚠️ 等待就绪超时，请检查日志: cat $LOG_FILE"
