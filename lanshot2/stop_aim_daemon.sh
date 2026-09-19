#!/bin/bash
# stop_aim_daemon.sh - 停止 Google AI Mode 极速热通道守护进程

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/aim_daemon.pid"

echo "[AIM Daemon] 正在停止 Google AI Mode 极速热通道守护进程..."

PIDS=$(lsof -ti :18888)
if [ -n "$PIDS" ]; then
    kill -TERM $PIDS 2>/dev/null
    sleep 1
    kill -9 $PIDS 2>/dev/null
    echo "✅ 已释放端口 18888 并终止服务进程 ($PIDS)。"
else
    echo "ℹ️ 端口 18888 当前未被占用。"
fi

if [ -f "$PID_FILE" ]; then
    rm -f "$PID_FILE"
fi

echo "[AIM Daemon] 服务已停止。"
