#!/bin/bash
# Start a SEPARATE test-mode instance (default port 8775) that uses demo SSH hosts
# served by tests/fake_ssh.sh. It never contacts real servers and never reads ~/.ssh/config.
# Usage: scripts/run_test_mode.sh [bwlimit, e.g. 20m]   (stop: kill $(cat .run/test_server.pid))
cd "$(dirname "$0")/.." || exit 1
APP_DIR="$(pwd -P)"; mkdir -p .run sample_remote
export DT_SSH_CONFIG="$APP_DIR/tests/fixtures/ssh_config_demo"
export DT_SSH_PROGRAM="$APP_DIR/tests/fake_ssh.sh"
export FAKE_REMOTE_ROOT="$APP_DIR/sample_remote"
export DT_CONFIG_PATH="$APP_DIR/.run/test_mode_config.json"
BW="${1:-}"
[ -n "$BW" ] && export DT_EXTRA_RSYNC_ARGS="--bwlimit=$BW"
rm -f .run/test_server.url
nohup python3 transfer_tool.py --no-browser --port "${DT_TEST_PORT:-8775}" --url-file .run/test_server.url \
  > .run/test_server.log 2>&1 </dev/null &
echo $! > .run/test_server.pid
for _ in $(seq 1 50); do [ -s .run/test_server.url ] && break; sleep 0.2; done
echo "test-mode server: $(cat .run/test_server.url 2>/dev/null) PID $(cat .run/test_server.pid)"
