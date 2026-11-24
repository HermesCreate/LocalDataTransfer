#!/bin/bash
# Data Transfer launcher: double-click in Finder.
# Starts the backend detached (nohup), waits for /api/health, then opens the browser.
cd "$(dirname "$0")" || exit 1
APP_DIR="$(pwd -P)"
RUN_DIR="$APP_DIR/.run"
mkdir -p "$RUN_DIR"

PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then echo "找不到 python3，请先安装 Python 3。"; read -r -p "按回车关闭"; exit 1; fi

WANT_VERSION="$("$PY" -c 'import re,sys;print(re.search(r"^VERSION = \"(\w+)\"",open("transfer_tool.py").read(),re.M).group(1))')"
BASE_PORT="${DT_PORT:-8765}"

health() { # $1=port -> prints "version|app_dir|pid|test_mode" or nothing
  curl -fsS --max-time 1 "http://127.0.0.1:$1/api/health" 2>/dev/null | "$PY" -c 'import json,sys
try:
  d=json.load(sys.stdin); print("%s|%s|%s|%s"%(d.get("version"),d.get("app_dir",""),d.get("pid",""),d.get("test_mode",False)))
except Exception: pass'
}

# 1) Reuse a running server of the SAME version from THIS folder (not test mode).
for ((p=BASE_PORT; p<BASE_PORT+8; p++)); do
  IFS='|' read -r ver dir pid tm <<<"$(health "$p")"
  [ -z "$ver" ] && continue
  if [ "$dir" = "$APP_DIR" ] && [ "$tm" = "False" ]; then
    if [ "$ver" = "$WANT_VERSION" ]; then
      URL="http://127.0.0.1:$p/"; echo "后台已在运行：$URL (PID $pid)"; open "$URL"; exit 0
    fi
    # 2) An old version of this tool: stop it so the page never loads old features.
    echo "发现旧版本后台 v$ver (PID $pid)，正在停止……"; kill "$pid" 2>/dev/null; sleep 1
  fi
done

# 3) Start a fresh detached server.
rm -f "$RUN_DIR/server.url"
nohup "$PY" "$APP_DIR/transfer_tool.py" --no-browser --port "$BASE_PORT" --url-file "$RUN_DIR/server.url" \
  >"$RUN_DIR/server.log" 2>&1 </dev/null &
SERVER_PID=$!
disown "$SERVER_PID" 2>/dev/null
echo "$SERVER_PID" >"$RUN_DIR/server.pid"

for _ in $(seq 1 50); do
  if [ -s "$RUN_DIR/server.url" ]; then
    URL="$(cat "$RUN_DIR/server.url")"; PORT="${URL##*:}"; PORT="${PORT%/}"
    IFS='|' read -r ver _ _ _ <<<"$(health "$PORT")"
    if [ "$ver" = "$WANT_VERSION" ]; then
      echo "Data Transfer v$ver 已启动：$URL (PID $SERVER_PID)"
      echo "可以关闭此终端窗口，后台会继续运行。停止服务：双击“停止传输工具.command”。"
      open "$URL"; exit 0
    fi
  fi
  sleep 0.2
done
echo "后台启动失败，日志如下："; cat "$RUN_DIR/server.log"; exit 1
