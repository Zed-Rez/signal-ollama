#!/usr/bin/env bash
# Install signal-ollama as a systemd --user service.
# Requires: SIGNAL_ACCOUNT (your linked number). Optional: SIGNAL_OWNER, etc.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/signal-ollama.service"

: "${SIGNAL_ACCOUNT:?Set SIGNAL_ACCOUNT to your linked Signal number, e.g. export SIGNAL_ACCOUNT=+15551234567}"
SIGNAL_OWNER="${SIGNAL_OWNER:-$SIGNAL_ACCOUNT}"

echo "Installing signal-ollama service:"
echo "  bridge:  $HERE/bridge.py"
echo "  account: ${SIGNAL_ACCOUNT:0:5}…"

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=signal-ollama bridge (Signal <-> local Ollama)
After=network-online.target

[Service]
Type=simple
Environment=SIGNAL_ACCOUNT=$SIGNAL_ACCOUNT
Environment=SIGNAL_OWNER=$SIGNAL_OWNER
${SIGNAL_URL:+Environment=SIGNAL_URL=$SIGNAL_URL}
${OLLAMA_URL:+Environment=OLLAMA_URL=$OLLAMA_URL}
${SIGNAL_OLLAMA_SYSTEM:+Environment=SIGNAL_OLLAMA_SYSTEM=$SIGNAL_OLLAMA_SYSTEM}
ExecStart=$PY $HERE/bridge.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# survive logout / start on boot
loginctl enable-linger "$USER" >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user enable --now signal-ollama.service

echo
systemctl --user --no-pager status signal-ollama.service | head -6 || true
echo
echo "Done. Tail logs with:  journalctl --user -u signal-ollama.service -f"
echo "Then text /help to your number's Note to Self."
