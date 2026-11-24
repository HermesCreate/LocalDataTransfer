#!/bin/bash
# TEST ONLY. Stand-in for `ssh` so rsync can be exercised end-to-end without touching
# any real server. Each "host" is a sandbox folder: $FAKE_REMOTE_ROOT/<host>/ (acts as $HOME).
# Hosts named bad-* simulate an unreachable server (exit 255, like ssh).
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p|-l|-i|-F|-J) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
host="$1"; shift
case "$host" in
  bad-*) echo "ssh: connect to host $host port 22: Connection refused (fake ssh)" >&2; exit 255 ;;
esac
root="${FAKE_REMOTE_ROOT:?FAKE_REMOTE_ROOT not set}"
mkdir -p "$root/$host" && cd "$root/$host" || exit 255
export HOME="$root/$host"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec /bin/sh -c "$*"
