#!/bin/bash
# Hourly update: pull the latest feed snapshot, translate new items, render.
# Called from cron. Safe to run manually.
set -euo pipefail

cd "$(dirname "$0")"

# deploy key used for `git pull`; override with GIT_KEY=/path/to/key
GIT_KEY="${GIT_KEY:-/root/.ssh/hwsignal_deploy}"
export GIT_SSH_COMMAND="ssh -i $GIT_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# never let two runs overlap (translation can take minutes)
exec 9>/tmp/hwsignal.lock
flock -n 9 || { echo "$(date -Is) another run in progress, skipping"; exit 0; }

echo "$(date -Is) start"

git pull -q --ff-only || echo "git pull failed, rendering local snapshot"

set -a
. /etc/hwsignal/env
set +a

HW_OUT="${HW_OUT:-/var/www/hwsignal}" python3 generate.py
echo "$(date -Is) done"
