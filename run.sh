#!/bin/bash
# Hourly update: pull the latest feed snapshot, translate new items, render.
# Called from cron. Safe to run manually.
#
# 渲染是「全或无」的：先渲染到 .hwsignal-new，校验通过才替换线上目录。
# 全量重建：HW_REBUILD=1 /opt/hwsignal/run.sh（配合 rsync --delete 清理孤儿页）
set -uo pipefail

cd "$(dirname "$0")"

# deploy key used for `git pull`; override with GIT_KEY=/path/to/key
GIT_KEY="${GIT_KEY:-/root/.ssh/hwsignal_deploy}"
export GIT_SSH_COMMAND="ssh -i $GIT_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# never let two runs overlap (translation can take minutes)
exec 9>/tmp/hwsignal.lock
flock -n 9 || { echo "$(date -Is) another run in progress, skipping"; exit 0; }

log() { echo "$(date -Is) $*"; }

log start

git pull -q --ff-only || log "git pull failed, rendering local snapshot"

set -a
. /etc/hwsignal/env
set +a

OUT_DIR="${HW_OUT:-/var/www/hwsignal}"
TMP="$(dirname "$OUT_DIR")/.$(basename "$OUT_DIR")-new"
REBUILD="${HW_REBUILD:-0}"

rm -rf "$TMP"
ARGS=()
[ "$REBUILD" = "1" ] && ARGS+=(--rebuild)

if ! HW_OUT="$TMP" python3 generate.py "${ARGS[@]}"; then
  log "ERROR 渲染失败，保留线上版本"
  rm -rf "$TMP"
  exit 1
fi

SZ=$(stat -c %s "$TMP/index.html" 2>/dev/null || echo 0)
if [ "$SZ" -lt 3000 ]; then
  log "ERROR 产出异常 index=${SZ}B，保留线上版本"
  rm -rf "$TMP"
  exit 1
fi
if [ ! -f "$TMP/feed.xml" ]; then
  log "ERROR 缺 feed.xml，保留线上版本"
  rm -rf "$TMP"
  exit 1
fi

if [ "$REBUILD" = "1" ]; then
  rsync -a --delete "$TMP"/ "$OUT_DIR"/ || { log "ERROR 全量同步失败"; rm -rf "$TMP"; exit 1; }
else
  cp -a "$TMP"/. "$OUT_DIR"/ || { log "ERROR 增量复制失败"; rm -rf "$TMP"; exit 1; }
fi
rm -rf "$TMP"

log "done mode=$([ "$REBUILD" = "1" ] && echo full || echo incremental) index=${SZ}B pages=$(find "$OUT_DIR" -name '*.html' | wc -l)"
