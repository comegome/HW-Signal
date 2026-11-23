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

# Kuma push 心跳：成功 up，失败 down；完全不 push 时 90 分钟也会报警。
# 地址在 /etc/hwsignal/env 的 HW_KUMA_PUSH。2026-11-22 起置空：更新监控改由 a2 的
# site-freshness-check.py 读公开页面（脚本自报成功 ≠ 页面真的更新）。
push_kuma() {  # $1=up|down  $2=msg
  [ -n "${HW_KUMA_PUSH:-}" ] || return 0
  curl -fsS -m 8 -G --data-urlencode "status=$1" --data-urlencode "msg=$2" \
    "$HW_KUMA_PUSH" >/dev/null 2>&1 || true
}

log start

git pull -q --ff-only || log "git pull failed, rendering local snapshot"

# 快照在 data 分支（每次 force push 单提交，不污染 main 历史）
if git fetch -q origin data && git show origin/data:items-raw.json > data/items-raw.json.tmp 2>/dev/null; then
  mv data/items-raw.json.tmp data/items-raw.json
  log "快照已更新 $(stat -c %s data/items-raw.json)B"
else
  rm -f data/items-raw.json.tmp
  if [ ! -f data/items-raw.json ]; then
    push_kuma down "no-snapshot"
    log "ERROR 取不到快照且本地没有 items-raw.json"
    exit 1
  fi
  log "WARN 取快照失败，沿用本地快照"
fi

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
  push_kuma down "render-failed"
  log "ERROR 渲染失败，保留线上版本"
  rm -rf "$TMP"
  exit 1
fi

SZ=$(stat -c %s "$TMP/index.html" 2>/dev/null || echo 0)
# 判内容不判字节：首页只放「当天」，清晨天然可能 0 条，按尺寸会误报
if ! grep -q '</html>' "$TMP/index.html" 2>/dev/null || ! grep -q 'class="list-head"' "$TMP/index.html" 2>/dev/null; then
  push_kuma down "index-broken"
  log "ERROR 首页不完整（缺 </html> 或 list-head），保留线上版本"
  rm -rf "$TMP"
  exit 1
fi
if ! grep -q '<rss' "$TMP/feed.xml" 2>/dev/null; then
  push_kuma down "no-feed"
  log "ERROR feed.xml 缺失或不完整，保留线上版本"
  rm -rf "$TMP"
  exit 1
fi

if [ "$REBUILD" = "1" ]; then
  rsync -a --delete "$TMP"/ "$OUT_DIR"/ || { push_kuma down "rsync-failed"; log "ERROR 全量同步失败"; rm -rf "$TMP"; exit 1; }
else
  cp -a "$TMP"/. "$OUT_DIR"/ || { push_kuma down "copy-failed"; log "ERROR 增量复制失败"; rm -rf "$TMP"; exit 1; }
fi
rm -rf "$TMP"

push_kuma up "ok"
log "done mode=$([ "$REBUILD" = "1" ] && echo full || echo incremental) index=${SZ}B pages=$(find "$OUT_DIR" -name '*.html' | wc -l)"
