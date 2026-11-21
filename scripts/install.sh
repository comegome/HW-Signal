#!/bin/bash
# 幂等安装 HW Signal 的运维脚本与定时任务（在目标机以 root 运行）
# 用法：sudo ./scripts/install.sh
set -uo pipefail

if [ "$(id -u)" != "0" ]; then echo "需要 root 权限"; exit 1; fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say() { printf '  %s\n' "$*"; }

install_if_diff() {  # src dst mode
  local src=$1 dst=$2 mode=$3
  mkdir -p "$(dirname "$dst")"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    say "未变化  $dst"
  else
    install -m "$mode" "$src" "$dst" && say "已更新  $dst"
  fi
}

echo "== 脚本 =="
install_if_diff "$SRC/scripts/hwsignal-dispatch.sh"   /usr/local/bin/hwsignal-dispatch.sh   755
install_if_diff "$SRC/scripts/hwsignal-db-backup.py"  /usr/local/sbin/hwsignal-db-backup.py 755
say "run.sh 由仓库直接运行：$SRC/run.sh"

echo "== 密钥/环境（不进仓库）=="
for f in /etc/hwsignal/env /etc/hwsignal/curl-auth; do
  if [ -f "$f" ]; then
    say "保留现有 $f"
  else
    mkdir -p /etc/hwsignal
    case "$f" in
      */env)       printf 'MAGPIE_KEY=\nHW_KUMA_PUSH=\n' > "$f";;
      */curl-auth) printf 'header = "Authorization: Bearer <GITHUB_TOKEN>"\n' > "$f";;
    esac
    chmod 600 "$f"
    say "已创建  $f（需要填写真实值）"
  fi
done

echo "== 定时任务 =="
install_if_diff "$SRC/scripts/cron/hwsignal-dispatch" /etc/cron.d/hwsignal-dispatch 644
install_if_diff "$SRC/scripts/cron/hwsignal-render"   /etc/cron.d/hwsignal-render   644
install_if_diff "$SRC/scripts/cron/hwsignal-db-backup" /etc/cron.d/hwsignal-db-backup 644
if crontab -l 2>/dev/null | grep -q '/opt/hwsignal/run.sh'; then
  say "WARN  root crontab 里还有一条 run.sh（与 /etc/cron.d/hwsignal-render 重复），建议删掉"
fi

echo "== 日志 =="
for f in /var/log/hwsignal.log /var/log/hwsignal-dispatch.log; do
  if [ -f "$f" ]; then say "存在    $f"; else install -m 644 /dev/null "$f" && say "已创建  $f"; fi
done

echo "== 校验 =="
bash -n /usr/local/bin/hwsignal-dispatch.sh && say "dispatch 语法 OK" || exit 1
bash -n "$SRC/run.sh" && say "run.sh 语法 OK" || exit 1
python3 -c "import ast;ast.parse(open('$SRC/generate.py').read())" && say "generate.py 语法 OK" || exit 1

cat <<'TIP'

下一步：
  1. 自检：cd /opt/hwsignal && sudo HW_OUT=/var/www/hwsignal python3 generate.py --doctor
  2. 手动跑一轮：sudo /opt/hwsignal/run.sh
  3. 全量重建：sudo HW_REBUILD=1 /opt/hwsignal/run.sh
TIP
