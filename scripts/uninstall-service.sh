#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="codex-dispatch.service"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
CONFIG_DIR="/etc/codex-dispatch"
STATE_DIR="/var/lib/codex-dispatch"
REMOVE_CONFIG=0
REMOVE_STATE=0
REMOVE_VENV=0

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/uninstall-service.sh [options]

By default the systemd unit is removed while config and SQLite state are preserved.

Options:
  --purge-config  Remove /etc/codex-dispatch.
  --purge-state   Remove /var/lib/codex-dispatch.
  --remove-venv   Remove the installed project .venv recorded in install.conf.
  --purge         Equivalent to --purge-config --purge-state --remove-venv.
  -h, --help      Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --purge-config) REMOVE_CONFIG=1 ;;
    --purge-state) REMOVE_STATE=1 ;;
    --remove-venv) REMOVE_VENV=1 ;;
    --purge) REMOVE_CONFIG=1; REMOVE_STATE=1; REMOVE_VENV=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done

if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: uninstall requires root; run through sudo" >&2
  exit 77
fi

VENV_DIR=""
if [[ -r "$CONFIG_DIR/install.conf" ]]; then
  VENV_DIR="$(awk -F= '$1 == "VENV_DIR" {print substr($0, length($1) + 2); exit}' "$CONFIG_DIR/install.conf")"
fi

systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "$UNIT_PATH"
systemctl daemon-reload
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

if [[ "$REMOVE_VENV" -eq 1 && -n "$VENV_DIR" ]]; then
  case "$VENV_DIR" in
    /*/.venv) rm -rf -- "$VENV_DIR" ;;
    *) echo "WARNING: refusing to remove unexpected venv path: $VENV_DIR" >&2 ;;
  esac
fi
if [[ "$REMOVE_STATE" -eq 1 ]]; then
  rm -rf -- "$STATE_DIR"
fi
if [[ "$REMOVE_CONFIG" -eq 1 ]]; then
  rm -rf -- "$CONFIG_DIR"
fi

cat <<EOF
Codex Dispatch service removed.
  config preserved: $((1 - REMOVE_CONFIG))
  state preserved:  $((1 - REMOVE_STATE))
  venv preserved:   $((1 - REMOVE_VENV))
EOF

echo "uninstall: PASS"
