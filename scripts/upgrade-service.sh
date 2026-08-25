#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="codex-dispatch.service"
INSTALL_META="/etc/codex-dispatch/install.conf"

if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: upgrade requires root; run through sudo" >&2
  exit 77
fi
if [[ ! -r "$INSTALL_META" ]]; then
  echo "ERROR: missing installation metadata: $INSTALL_META" >&2
  exit 2
fi

read_meta() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted {print substr($0, length($1) + 2); exit}' "$INSTALL_META"
}

SERVICE_USER="$(read_meta SERVICE_USER)"
PROJECT_DIR="$(read_meta PROJECT_DIR)"
PYTHON_BIN="$(read_meta PYTHON_BIN)"
CODEX_BIN="$(read_meta CODEX_BIN)"

for value in "$SERVICE_USER" "$PROJECT_DIR" "$PYTHON_BIN" "$CODEX_BIN"; do
  [[ -n "$value" ]] || { echo "ERROR: incomplete $INSTALL_META" >&2; exit 2; }
done
[[ -f "$PROJECT_DIR/scripts/install-service.sh" ]] || {
  echo "ERROR: installed project is unavailable: $PROJECT_DIR" >&2
  exit 2
}

was_active=0
if systemctl is-active --quiet "$SERVICE_NAME"; then
  was_active=1
  systemctl stop "$SERVICE_NAME"
fi

if ! bash "$PROJECT_DIR/scripts/install-service.sh" \
  --user "$SERVICE_USER" \
  --python "$PYTHON_BIN" \
  --codex-bin "$CODEX_BIN"; then
  echo "ERROR: upgrade failed; service remains stopped" >&2
  exit 1
fi

if [[ "$was_active" -eq 1 ]]; then
  systemctl start "$SERVICE_NAME"
fi

systemctl --no-pager --full status "$SERVICE_NAME" || true
echo "upgrade: PASS"
