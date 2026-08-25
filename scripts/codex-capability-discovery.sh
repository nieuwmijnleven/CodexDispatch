#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/codex-capability-discovery.sh [options]

Passive Codex CLI capability discovery. This command does not start a Codex
turn and does not send a prompt to an existing session.

Options:
  --codex PATH          Explicit Codex binary path. Defaults to $CODEX_BIN or PATH lookup.
  --notify-probe FILE   Inspect a JSONL file produced by bin/codex-notify-probe.py.
  --help                Show this help.

The report is written to stdout so callers can save it wherever appropriate.
EOF
}

CODEX_OVERRIDE="${CODEX_BIN:-}"
NOTIFY_PROBE_FILE=""

while (($# > 0)); do
  case "$1" in
    --codex)
      [[ $# -ge 2 ]] || { echo "error: --codex requires a path" >&2; exit 64; }
      CODEX_OVERRIDE="$2"
      shift 2
      ;;
    --notify-probe)
      [[ $# -ge 2 ]] || { echo "error: --notify-probe requires a file" >&2; exit 64; }
      NOTIFY_PROBE_FILE="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

if [[ -n "$CODEX_OVERRIDE" ]]; then
  CODEX="$CODEX_OVERRIDE"
elif command -v codex >/dev/null 2>&1; then
  CODEX="$(command -v codex)"
else
  CODEX=""
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

run_help_probe() {
  local name="$1"
  shift
  local output="$TMP_DIR/$name.txt"
  if "$@" --help >"$output" 2>&1; then
    printf 'yes'
  else
    printf 'no'
  fi
}

contains_option() {
  local file="$1"
  local option="$2"
  if grep -F -- "$option" "$file" >/dev/null 2>&1; then
    printf 'yes'
  else
    printf 'no'
  fi
}

printf '# Codex Capability Discovery Report\n\n'
printf -- '- generated_at_utc: `%s`\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
printf -- '- probe_user: `%s`\n' "$(id -un 2>/dev/null || printf unknown)"
printf -- '- probe_uid: `%s`\n' "$(id -u 2>/dev/null || printf unknown)"
printf -- '- host: `%s`\n' "$(hostname 2>/dev/null || printf unknown)"
printf -- '- os: `%s`\n' "$(uname -srmo 2>/dev/null || uname -a 2>/dev/null || printf unknown)"
printf -- '- shell: `%s`\n' "${SHELL:-unknown}"
printf -- '- path: `%s`\n\n' "$PATH"

if [[ -z "$CODEX" ]]; then
  cat <<'EOF'
## Result

- codex_binary_found: `no`
- status: `BLOCKED`

`codex` was not found in the probe user's PATH. Run this script as the same
Linux user that normally runs Codex, or pass `--codex /absolute/path/to/codex`.
EOF
  exit 2
fi

if [[ ! -x "$CODEX" ]]; then
  printf '## Result\n\n- codex_binary_found: `yes`\n- codex_binary: `%s`\n- executable: `no`\n- status: `BLOCKED`\n' "$CODEX"
  exit 2
fi

VERSION_OUTPUT="$($CODEX --version 2>&1 || true)"
TOP_LEVEL="$(run_help_probe top-level "$CODEX")"
QUEUE="$(run_help_probe queue "$CODEX" queue)"
EXEC="$(run_help_probe exec "$CODEX" exec)"
EXEC_RESUME="$(run_help_probe exec-resume "$CODEX" exec resume)"
DOCTOR="$(run_help_probe doctor "$CODEX" doctor)"

QUEUE_THREAD="no"
QUEUE_MESSAGE="no"
if [[ "$QUEUE" == "yes" ]]; then
  QUEUE_THREAD="$(contains_option "$TMP_DIR/queue.txt" '--thread')"
  QUEUE_MESSAGE="$(contains_option "$TMP_DIR/queue.txt" '--message')"
fi

CONFIG_FILE="${CODEX_HOME:-$HOME/.codex}/config.toml"
NOTIFY_CONFIGURED="unknown"
if [[ -r "$CONFIG_FILE" ]]; then
  if grep -Eq '^[[:space:]]*notify[[:space:]]*=' "$CONFIG_FILE"; then
    NOTIFY_CONFIGURED="yes"
  else
    NOTIFY_CONFIGURED="no"
  fi
fi

cat <<EOF
## Installed CLI

- codex_binary_found: \`yes\`
- codex_binary: \`$CODEX\`
- codex_version: \`${VERSION_OUTPUT//$'\n'/ }\`
- top_level_help: \`$TOP_LEVEL\`

## Capability Matrix

| Capability | Detected | Evidence |
| --- | --- | --- |
| \`codex queue\` | \`$QUEUE\` | \`codex queue --help\` |
| queue \`--thread\` option | \`$QUEUE_THREAD\` | queue help text |
| queue \`--message\` option | \`$QUEUE_MESSAGE\` | queue help text |
| \`codex exec\` | \`$EXEC\` | \`codex exec --help\` |
| \`codex exec resume\` | \`$EXEC_RESUME\` | \`codex exec resume --help\` |
| \`codex doctor\` | \`$DOCTOR\` | \`codex doctor --help\` |
| readable Codex config | \`$([[ -r "$CONFIG_FILE" ]] && printf yes || printf no)\` | \`$CONFIG_FILE\` |
| external notify configured | \`$NOTIFY_CONFIGURED\` | only checks whether a top-level notify key exists; value is not printed |

## Safety

This was a passive probe. It did not create a Codex session, enqueue a message,
resume a thread, run \`codex doctor\`, or modify \`~/.codex/config.toml\`.
EOF

if [[ -n "$NOTIFY_PROBE_FILE" ]]; then
  printf '\n## Notify Probe Summary\n\n'
  if [[ ! -r "$NOTIFY_PROBE_FILE" ]]; then
    printf -- '- file: `%s`\n- readable: `no`\n' "$NOTIFY_PROBE_FILE"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$NOTIFY_PROBE_FILE" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
print(f"- file: `{path}`")
print(f"- event_count: `{len(lines)}`")
if not lines:
    print("- status: `EMPTY`")
    raise SystemExit(0)

try:
    event = json.loads(lines[-1])
except json.JSONDecodeError as exc:
    print("- status: `INVALID_JSONL`")
    print(f"- error: `{exc}`")
    raise SystemExit(0)

print(f"- last_event_type: `{event.get('type', 'missing')}`")
for key in ("thread_id_present", "turn_id_present", "cwd_present", "client_present"):
    value = event.get(key, "missing")
    print(f"- {key}: `{str(value).lower() if isinstance(value, bool) else value}`")
print(f"- input_message_count: `{event.get('input_message_count', 'missing')}`")
print(f"- last_assistant_message_present: `{str(event.get('last_assistant_message_present', 'missing')).lower()}`")
PY
  else
    printf -- '- file: `%s`\n- readable: `yes`\n- status: `PYTHON3_UNAVAILABLE`\n' "$NOTIFY_PROBE_FILE"
  fi
fi
