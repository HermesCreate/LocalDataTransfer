#!/bin/bash
cd "$(dirname "$0")" || exit 1
PIDFILE=".run/server.pid"
if [ -s "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")" && echo "已停止 Data Transfer 后台 (PID $(cat "$PIDFILE"))。"
  rm -f "$PIDFILE" .run/server.url
else
  echo "没有找到正在运行的 Data Transfer 后台。"
fi
