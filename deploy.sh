#!/bin/bash
# Install HW Signal on a fresh server.
#
#   ./deploy.sh /opt/hwsignal
#
# Idempotent: safe to re-run for updates.
set -euo pipefail

DEST="${1:-/opt/hwsignal}"
REPO="git@github.com:comegome/HWSignal.git"
ENV_DIR=/etc/hwsignal
WWW=/var/www/hwsignal

if [ ! -d "$DEST/.git" ]; then
  echo "==> clone $REPO -> $DEST"
  sudo mkdir -p "$(dirname "$DEST")"
  sudo chown "$(id -u):$(id -g)" "$(dirname "$DEST")"
  git clone "$REPO" "$DEST"
else
  echo "==> pull $DEST"
  git -C "$DEST" pull --ff-only
fi

echo "==> env file"
sudo mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_DIR/env" ]; then
  sudo tee "$ENV_DIR/env" >/dev/null <<'EOF'
# magpie caller key for translation
MAGPIE_KEY=
EOF
  sudo chmod 600 "$ENV_DIR/env"
  echo "    created $ENV_DIR/env — fill in MAGPIE_KEY, then re-run"
else
  echo "    exists, leaving untouched"
fi

echo "==> output dir"
sudo mkdir -p "$WWW"
sudo chown "$(id -u):$(id -g)" "$WWW"

echo "==> cron"
CRON_LINE='23 * * * * cd '"$DEST"' && set -a && . /etc/hwsignal/env && set +a && HW_OUT='"$WWW"' python3 generate.py >> /var/log/hwsignal.log 2>&1'
( crontab -l 2>/dev/null | grep -vF "cd $DEST && set -a" ; echo "$CRON_LINE" ) | crontab -
crontab -l | grep -F "$DEST"

echo
echo "Next:"
echo "  1. put MAGPIE_KEY into $ENV_DIR/env"
echo "  2. run once:  cd $DEST && set -a && . $ENV_DIR/env && set +a && HW_OUT=$WWW python3 generate.py"
echo "  3. point Caddy at $WWW"
