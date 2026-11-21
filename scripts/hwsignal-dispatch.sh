#!/bin/bash
# 外部兜底：手动触发 HWSignal 的 fetch 工作流，等它结束后在本机 pull + 渲染。
# GitHub 的 schedule 不可靠，这里不依赖它。
set -u
AUTH=/etc/hwsignal/curl-auth
API=https://api.github.com/repos/comegome/HW-Signal/actions/workflows/fetch.yml
ACCEPT='Accept: application/vnd.github+json'

latest_id() {
  curl -s -K "$AUTH" -H "$ACCEPT" "$API/runs?per_page=1" |
    python3 -c 'import json,sys; r=json.load(sys.stdin).get("workflow_runs",[]); print(r[0]["id"] if r else 0)'
}

# 基线：触发前最新的 run id，之后只认 id 更大的 run
BASE=$(latest_id)

CODE=$(curl -s -o /dev/null -w '%{http_code}' -K "$AUTH" -H "$ACCEPT" \
  -X POST -d '{"ref":"main"}' "$API/dispatches")
if [ "$CODE" != "204" ]; then
  echo "$(date -u +%FT%TZ) dispatch 失败: HTTP $CODE" >&2
  exit 1
fi

# 最多等 15 分钟，等到新 run 完成；超时不等于失败，照常渲染
STATE=timeout
for _ in $(seq 30); do
  sleep 30
  NEW=$(curl -s -K "$AUTH" -H "$ACCEPT" "$API/runs?per_page=5" |
    python3 -c 'import json,sys
b=int(sys.argv[1])
for r in json.load(sys.stdin).get("workflow_runs",[]):
    if r["id"]>b:
        print(r["status"], r.get("conclusion") or "-"); break
else: print("none")' "$BASE")
  case "$NEW" in
    completed*) STATE="$NEW"; break ;;
  esac
done
echo "$(date -u +%FT%TZ) fetch run: $STATE (base=$BASE)"

# 与小时任务共用的 pull + 渲染脚本（带 flock）
/opt/hwsignal/run.sh
