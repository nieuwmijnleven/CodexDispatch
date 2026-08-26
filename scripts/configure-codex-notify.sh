#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" -eq 0 ]]; then
  echo "ERROR: run this as the Codex user, not root" >&2
  exit 77
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
CONFIG_FILE="${CODEX_CONFIG_FILE:-$HOME/.codex/config.toml}"
PYTHON_ENTRY="$PROJECT_DIR/.venv/bin/python"
NOTIFY_ENTRY="$PROJECT_DIR/bin/codex-notify.py"

[[ -x "$PYTHON_ENTRY" ]] || { echo "ERROR: virtualenv Python is unavailable: $PYTHON_ENTRY" >&2; exit 2; }
[[ -f "$NOTIFY_ENTRY" ]] || { echo "ERROR: notify wrapper is unavailable: $NOTIFY_ENTRY" >&2; exit 2; }
[[ -f "$CONFIG_FILE" ]] || { echo "ERROR: Codex config does not exist: $CONFIG_FILE" >&2; exit 2; }
[[ -w "$CONFIG_FILE" ]] || { echo "ERROR: Codex config is not writable by current user: $CONFIG_FILE" >&2; exit 77; }

TARGET="notify = [\"$PYTHON_ENTRY\", \"$NOTIFY_ENTRY\"]"

# Only a single-line top-level notify setting is modified automatically. A
# multi-line/custom notify value is intentionally left for manual review.
table_seen=0
notify_count=0
notify_line=0
line_no=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line_no=$((line_no + 1))
  if [[ "$table_seen" -eq 0 && "$line" =~ ^[[:space:]]*\[ ]]; then
    table_seen=1
  fi
  if [[ "$table_seen" -eq 0 && "$line" =~ ^[[:space:]]*notify[[:space:]]*= ]]; then
    notify_count=$((notify_count + 1))
    notify_line="$line_no"
  fi
done < "$CONFIG_FILE"

if [[ "$notify_count" -gt 1 ]]; then
  echo "ERROR: multiple top-level notify settings found; edit $CONFIG_FILE manually" >&2
  exit 78
fi

if [[ "$notify_count" -eq 1 ]]; then
  current="$(awk -v n="$notify_line" 'NR == n {print; exit}' "$CONFIG_FILE")"
  if [[ "$current" == "$TARGET" ]]; then
    echo "Codex notify configuration: PASS (already configured)"
    exit 0
  fi
  if [[ "$current" == *"["* && "$current" != *"]"* ]]; then
    echo "ERROR: existing notify setting appears multi-line; edit $CONFIG_FILE manually" >&2
    exit 78
  fi
fi

config_dir="$(dirname -- "$CONFIG_FILE")"
tmp="$(mktemp "$config_dir/.config.toml.codex-dispatch.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

inserted=0
table_seen=0
line_no=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line_no=$((line_no + 1))
  if [[ "$notify_count" -eq 1 && "$line_no" -eq "$notify_line" ]]; then
    printf '%s\n' "$TARGET" >> "$tmp"
    inserted=1
    continue
  fi
  if [[ "$notify_count" -eq 0 && "$inserted" -eq 0 && "$line" =~ ^[[:space:]]*\[ ]]; then
    printf '%s\n\n' "$TARGET" >> "$tmp"
    inserted=1
  fi
  printf '%s\n' "$line" >> "$tmp"
done < "$CONFIG_FILE"

if [[ "$inserted" -eq 0 ]]; then
  printf '\n%s\n' "$TARGET" >> "$tmp"
fi

"$PYTHON_ENTRY" -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$tmp"
chmod --reference="$CONFIG_FILE" "$tmp"
mv -f "$tmp" "$CONFIG_FILE"
trap - EXIT

echo "Codex notify configuration: PASS"
echo "  config: $CONFIG_FILE"
echo "  callback: Codex Dispatch notify bridge"
