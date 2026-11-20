#!/bin/bash
# Install HW Signal on a fresh server.
#
#   ./deploy.sh /opt/hwsignal
#
# Idempotent: safe to re-run for updates.
set -euo pipefail

DEST="${1:-/opt/hwsignal}"
REPO="git@github.com:comegome/HW-Signal.git"
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
CRON_LINE='23 * * * * '"$DEST"'/run.sh >> /var/log/hwsignal.log 2>&1'
( crontab -l 2>/dev/null | grep -vF "$DEST/run.sh" ; echo "$CRON_LINE" ) | crontab -
crontab -l | grep -F "$DEST/run.sh"

echo
echo "Next:"
echo "  1. put MAGPIE_KEY into $ENV_DIR/env"
echo "  2. run once:  $DEST/run.sh"
echo "  3. point Caddy at $WWW"
