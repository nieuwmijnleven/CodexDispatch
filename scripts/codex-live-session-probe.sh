#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/codex-live-session-probe.sh --thread THREAD_ID [options]

ACTIVE probe for Codex live-session queue delivery. Unlike the passive
capability discovery script, this command sends a real message to an existing
Codex session and may cause a model turn.

Options:
  --thread ID       Required Codex thread/session identifier.
  --message TEXT    Probe message. Defaults to a no-file-change verification prompt.
  --codex PATH      Explicit Codex binary path. Defaults to $CODEX_BIN or PATH lookup.
  --help            Show this help.
EOF
}

THREAD_ID=""
MESSAGE="PHASE 0 Codex Dispatch queue probe. Do not modify files. Reply exactly: CODEX_DISPATCH_QUEUE_OK"
CODEX_OVERRIDE="${CODEX_BIN:-}"

while (($# > 0)); do
  case "$1" in
    --thread)
      [[ $# -ge 2 ]] || { echo "error: --thread requires an ID" >&2; exit 64; }
      THREAD_ID="$2"
      shift 2
      ;;
    --message)
      [[ $# -ge 2 ]] || { echo "error: --message requires text" >&2; exit 64; }
      MESSAGE="$2"
      shift 2
      ;;
    --codex)
      [[ $# -ge 2 ]] || { echo "error: --codex requires a path" >&2; exit 64; }
      CODEX_OVERRIDE="$2"
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

[[ -n "$THREAD_ID" ]] || { echo "error: --thread is required" >&2; exit 64; }

if [[ -n "$CODEX_OVERRIDE" ]]; then
  CODEX="$CODEX_OVERRIDE"
elif command -v codex >/dev/null 2>&1; then
  CODEX="$(command -v codex)"
else
  echo "error: codex not found; run as the Codex user or pass --codex PATH" >&2
  exit 69
fi

if ! "$CODEX" queue --help >/tmp/codex-dispatch-queue-help.$$ 2>&1; then
  rm -f /tmp/codex-dispatch-queue-help.$$
  echo "error: installed Codex does not expose 'codex queue'" >&2
  exit 69
fi

QUEUE_HELP="/tmp/codex-dispatch-queue-help.$$"
trap 'rm -f "$QUEUE_HELP"' EXIT

if ! grep -F -- '--thread' "$QUEUE_HELP" >/dev/null 2>&1 || ! grep -F -- '--message' "$QUEUE_HELP" >/dev/null 2>&1; then
  echo "error: installed 'codex queue' does not expose expected --thread/--message options" >&2
  echo "inspect with: codex queue --help" >&2
  exit 69
fi

printf 'Sending an ACTIVE queue probe to Codex thread %s\n' "$THREAD_ID"
printf 'This may cause a model turn. No shell interpolation is used.\n\n'

"$CODEX" queue --thread "$THREAD_ID" --message "$MESSAGE"

cat <<'EOF'

Queue command exited successfully.
Acceptance check: the target Codex session must receive the probe and answer
CODEX_DISPATCH_QUEUE_OK without modifying files. A successful CLI exit alone
does not prove the TUI consumed the queued message.
EOF
