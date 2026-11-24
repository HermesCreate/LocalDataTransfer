#!/bin/bash
# Create realistic sample data inside the project: sample_data/mono-2 (videos + metadata).
# Usage: scripts/make_sample_data.sh [size_mb_per_video] [count]
cd "$(dirname "$0")/.." || exit 1
SIZE_MB="${1:-40}"; COUNT="${2:-6}"
OUT="sample_data/mono-2"
mkdir -p "$OUT/checked_videos" "$OUT/meta"
for i in $(seq 1 "$COUNT"); do
  f="$OUT/checked_videos/episode3_cam$(printf %02d "$i").mp4"
  [ -f "$f" ] && [ "$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f")" -eq $((SIZE_MB*1024*1024)) ] && continue
  dd if=/dev/urandom of="$f" bs=1048576 count="$SIZE_MB" status=none
done
for i in $(seq 1 "$COUNT"); do
  printf '{"clip": "episode3_cam%02d", "fps": 30, "checked": true}\n' "$i" > "$OUT/meta/episode3_cam$(printf %02d "$i").json"
done
echo "sample data ready: $(du -sh "$OUT" | cut -f1) in $OUT"
