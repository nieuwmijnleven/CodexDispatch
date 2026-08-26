#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="codex-dispatch.service"
CONFIG_DIR="/etc/codex-dispatch"
ENV_FILE="$CONFIG_DIR/codex-dispatch.env"
NOTIFY_ENV_FILE="$CONFIG_DIR/notify.env"
SECRET_ENV_FILE="$CONFIG_DIR/secret.env"
INSTALL_META="$CONFIG_DIR/install.conf"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
START_SERVICE=0
DRY_RUN=0
SERVICE_USER=""
PYTHON_BIN="/usr/bin/python3"
CODEX_BIN=""

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/install-service.sh [options]

Options:
  --user USER       Linux user that owns Codex auth/session state.
                    Defaults to SUDO_USER when invoked through sudo.
  --python PATH     Python 3 executable (default: /usr/bin/python3).
  --codex-bin PATH  Absolute Codex executable path. Auto-detected when omitted.
  --start           Start/restart the service after installation.
  --dry-run         Validate and print the resolved installation without changing host state.
  -h, --help        Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --user)
      [[ $# -ge 2 ]] || { echo "ERROR: --user requires a value" >&2; exit 64; }
      SERVICE_USER="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || { echo "ERROR: --python requires a value" >&2; exit 64; }
      PYTHON_BIN="$2"
      shift 2
      ;;
    --codex-bin)
      [[ $# -ge 2 ]] || { echo "ERROR: --codex-bin requires a value" >&2; exit 64; }
      CODEX_BIN="$2"
      shift 2
      ;;
    --start)
      START_SERVICE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
UNIT_TEMPLATE="$PROJECT_DIR/systemd/codex-dispatch.service.in"
ENV_TEMPLATE="$PROJECT_DIR/systemd/codex-dispatch.env.example"
NOTIFY_ENV_TEMPLATE="$PROJECT_DIR/systemd/codex-dispatch-notify.env.example"
SECRET_ENV_TEMPLATE="$PROJECT_DIR/systemd/codex-dispatch.secret.env.example"

[[ -f "$UNIT_TEMPLATE" ]] || { echo "ERROR: missing $UNIT_TEMPLATE" >&2; exit 2; }
[[ -f "$ENV_TEMPLATE" ]] || { echo "ERROR: missing $ENV_TEMPLATE" >&2; exit 2; }
[[ -f "$NOTIFY_ENV_TEMPLATE" ]] || { echo "ERROR: missing $NOTIFY_ENV_TEMPLATE" >&2; exit 2; }
[[ -f "$SECRET_ENV_TEMPLATE" ]] || { echo "ERROR: missing $SECRET_ENV_TEMPLATE" >&2; exit 2; }
[[ -f "$PROJECT_DIR/pyproject.toml" ]] || { echo "ERROR: missing pyproject.toml" >&2; exit 2; }
[[ "$PYTHON_BIN" = /* ]] || { echo "ERROR: --python must be an absolute path" >&2; exit 64; }
[[ -x "$PYTHON_BIN" ]] || { echo "ERROR: Python is not executable: $PYTHON_BIN" >&2; exit 2; }

if [[ -z "$SERVICE_USER" ]]; then
  SERVICE_USER="${SUDO_USER:-}"
fi
if [[ -z "$SERVICE_USER" ]]; then
  echo "ERROR: service user is unknown; pass --user USER" >&2
  exit 64
fi
if [[ "$SERVICE_USER" == "root" ]]; then
  echo "ERROR: Codex Dispatch must not run as root" >&2
  exit 64
fi
if [[ ! "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "ERROR: invalid service user: $SERVICE_USER" >&2
  exit 64
fi

PASSWD_ENTRY="$(getent passwd "$SERVICE_USER" || true)"
[[ -n "$PASSWD_ENTRY" ]] || { echo "ERROR: Linux user does not exist: $SERVICE_USER" >&2; exit 2; }
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SERVICE_HOME="$(printf '%s\n' "$PASSWD_ENTRY" | cut -d: -f6)"
[[ "$SERVICE_HOME" = /* ]] || { echo "ERROR: invalid home directory for $SERVICE_USER" >&2; exit 2; }

for path_value in "$PROJECT_DIR" "$VENV_DIR" "$PYTHON_BIN"; do
  if [[ "$path_value" =~ [[:space:]] ]]; then
    echo "ERROR: systemd installation paths must not contain whitespace: $path_value" >&2
    exit 64
  fi
done

if [[ -z "$CODEX_BIN" ]]; then
  for candidate in \
    "$SERVICE_HOME/.npm-global/bin/codex" \
    "$SERVICE_HOME/.local/bin/codex" \
    "/usr/local/bin/codex" \
    "/usr/bin/codex"; do
    if [[ -x "$candidate" ]]; then
      CODEX_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$CODEX_BIN" ]]; then
  echo "ERROR: Codex executable was not found; pass --codex-bin /absolute/path/to/codex" >&2
  exit 2
fi
[[ "$CODEX_BIN" = /* ]] || { echo "ERROR: --codex-bin must be an absolute path" >&2; exit 64; }
if [[ "$DRY_RUN" -eq 0 && ! -x "$CODEX_BIN" ]]; then
  echo "ERROR: Codex is not executable: $CODEX_BIN" >&2
  exit 2
fi
if [[ "$CODEX_BIN" =~ [[:space:]] ]]; then
  echo "ERROR: Codex executable path must not contain whitespace" >&2
  exit 64
fi

render_template() {
  local source="$1"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//@SERVICE_USER@/$SERVICE_USER}"
    line="${line//@SERVICE_GROUP@/$SERVICE_GROUP}"
    line="${line//@SERVICE_HOME@/$SERVICE_HOME}"
    line="${line//@PROJECT_DIR@/$PROJECT_DIR}"
    line="${line//@VENV_PYTHON@/$VENV_PYTHON}"
    line="${line//@CODEX_BIN@/$CODEX_BIN}"
    printf '%s\n' "$line"
  done < "$source"
}

replace_main_env_key() {
  local key="$1"
  local value="$2"
  local tmp line found=0
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      "$key="*)
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
        found=1
        ;;
      *) printf '%s\n' "$line" >> "$tmp" ;;
    esac
  done < "$ENV_FILE"
  if [[ "$found" -eq 0 ]]; then
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  install -m 0640 -o root -g "$SERVICE_GROUP" "$tmp" "$ENV_FILE"
  rm -f "$tmp"
}

print_summary() {
  cat <<EOF
Codex Dispatch service installation
  user:        $SERVICE_USER
  group:       $SERVICE_GROUP
  project:     $PROJECT_DIR
  venv:        $VENV_DIR
  python:      $PYTHON_BIN
  codex:       $CODEX_BIN
  env file:    $ENV_FILE
  notify env:  $NOTIFY_ENV_FILE
  secret env:  $SECRET_ENV_FILE
  unit:        $UNIT_PATH
  start now:   $START_SERVICE
EOF
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  print_summary
  echo "dry-run: PASS"
  exit 0
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: installation requires root; run through sudo" >&2
  exit 77
fi

if ! runuser -u "$SERVICE_USER" -- test -w "$PROJECT_DIR/src"; then
  echo "ERROR: $SERVICE_USER cannot write $PROJECT_DIR/src, which is required for the editable package install." >&2
  echo "Fix the checkout ownership before retrying, for example:" >&2
  echo "  chown -R $SERVICE_USER:$SERVICE_GROUP $PROJECT_DIR" >&2
  exit 77
fi

install -d -m 0750 -o root -g "$SERVICE_GROUP" "$CONFIG_DIR"

tmp_env="$(mktemp)"
tmp_notify_env="$(mktemp)"
tmp_secret_env="$(mktemp)"
trap 'rm -f "$tmp_env" "$tmp_notify_env" "$tmp_secret_env" "${tmp_unit:-}" "${tmp_meta:-}"' EXIT

LEGACY_DISCORD_TOKEN=""
LEGACY_TOKEN_PRESENT=0
if [[ -e "$ENV_FILE" ]] && grep -q '^DISCORD_BOT_TOKEN=' "$ENV_FILE"; then
  LEGACY_TOKEN_PRESENT=1
  LEGACY_DISCORD_TOKEN="$(awk -F= '$1 == "DISCORD_BOT_TOKEN" {print substr($0, length($1) + 2); exit}' "$ENV_FILE")"
fi

if [[ ! -e "$ENV_FILE" ]]; then
  render_template "$ENV_TEMPLATE" > "$tmp_env"
  install -m 0640 -o root -g "$SERVICE_GROUP" "$tmp_env" "$ENV_FILE"
  echo "Created $ENV_FILE; set workspace roots and Discord IDs before starting the service."
elif [[ "$LEGACY_TOKEN_PRESENT" -eq 1 ]]; then
  awk '$0 !~ /^DISCORD_BOT_TOKEN=/' "$ENV_FILE" > "$tmp_env"
  install -m 0640 -o root -g "$SERVICE_GROUP" "$tmp_env" "$ENV_FILE"
  echo "Removed legacy Discord token from $ENV_FILE"
else
  chown root:"$SERVICE_GROUP" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
  echo "Preserved existing $ENV_FILE"
fi

# The selected Codex executable is part of the non-secret deployment contract.
# Refresh it on every install/upgrade so a previous VS Code/npm/standalone path
# cannot remain stale after --codex-bin changes.
replace_main_env_key CODEX_DISPATCH_CODEX_BIN "$CODEX_BIN"
echo "Updated CODEX_DISPATCH_CODEX_BIN in $ENV_FILE"

if [[ ! -e "$NOTIFY_ENV_FILE" ]]; then
  render_template "$NOTIFY_ENV_TEMPLATE" > "$tmp_notify_env"
  install -m 0640 -o root -g "$SERVICE_GROUP" "$tmp_notify_env" "$NOTIFY_ENV_FILE"
  echo "Created $NOTIFY_ENV_FILE (non-secret notify settings)."
else
  chown root:"$SERVICE_GROUP" "$NOTIFY_ENV_FILE"
  chmod 0640 "$NOTIFY_ENV_FILE"
  echo "Preserved existing $NOTIFY_ENV_FILE"
fi

if [[ ! -e "$SECRET_ENV_FILE" ]]; then
  render_template "$SECRET_ENV_TEMPLATE" > "$tmp_secret_env"
  if [[ -n "$LEGACY_DISCORD_TOKEN" ]]; then
    printf '%s\n' "DISCORD_BOT_TOKEN=$LEGACY_DISCORD_TOKEN" > "$tmp_secret_env"
  fi
  install -m 0600 -o root -g root "$tmp_secret_env" "$SECRET_ENV_FILE"
  echo "Created $SECRET_ENV_FILE; set the Discord Bot token before starting the service."
else
  if [[ -n "$LEGACY_DISCORD_TOKEN" ]] && grep -Eq '^DISCORD_BOT_TOKEN=$' "$SECRET_ENV_FILE"; then
    printf '%s\n' "DISCORD_BOT_TOKEN=$LEGACY_DISCORD_TOKEN" > "$tmp_secret_env"
    install -m 0600 -o root -g root "$tmp_secret_env" "$SECRET_ENV_FILE"
    echo "Migrated legacy Discord token into $SECRET_ENV_FILE"
  else
    chown root:root "$SECRET_ENV_FILE"
    chmod 0600 "$SECRET_ENV_FILE"
    echo "Preserved existing $SECRET_ENV_FILE"
  fi
fi

tmp_unit="$(mktemp)"
render_template "$UNIT_TEMPLATE" > "$tmp_unit"
install -m 0644 -o root -g root "$tmp_unit" "$UNIT_PATH"

tmp_meta="$(mktemp)"
printf '%s\n' \
  "SERVICE_USER=$SERVICE_USER" \
  "SERVICE_GROUP=$SERVICE_GROUP" \
  "PROJECT_DIR=$PROJECT_DIR" \
  "VENV_DIR=$VENV_DIR" \
  "PYTHON_BIN=$PYTHON_BIN" \
  "CODEX_BIN=$CODEX_BIN" > "$tmp_meta"
install -m 0644 -o root -g root "$tmp_meta" "$INSTALL_META"

if [[ ! -x "$VENV_PYTHON" ]]; then
  runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# A partially-created venv can have a working Python executable but no pip
# module/launcher (for example, after an interrupted install). Validate pip
# explicitly and repair it with ensurepip before installing the project.
if ! runuser -u "$SERVICE_USER" -- "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
  echo "Virtual environment is missing pip; repairing it with ensurepip."
  if ! runuser -u "$SERVICE_USER" -- "$VENV_PYTHON" -m ensurepip --upgrade; then
    echo "ERROR: cannot bootstrap pip in $VENV_DIR; ensure Python venv/ensurepip support is installed" >&2
    exit 2
  fi
fi
runuser -u "$SERVICE_USER" -- "$VENV_PYTHON" -m pip install -e "$PROJECT_DIR"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

if [[ "$START_SERVICE" -eq 1 ]]; then
  missing=0
  for key in \
    CODEX_ALLOWED_ROOTS \
    DISCORD_CONTROL_CHANNEL_ID \
    DISCORD_ALLOWED_GUILD_IDS \
    DISCORD_ALLOWED_CHANNEL_IDS \
    DISCORD_ALLOWED_USER_IDS; do
    if grep -Eq "^${key}=$" "$ENV_FILE"; then
      echo "ERROR: $key is empty in $ENV_FILE" >&2
      missing=1
    fi
  done
  if grep -Eq '^DISCORD_BOT_TOKEN=$' "$SECRET_ENV_FILE"; then
    echo "ERROR: DISCORD_BOT_TOKEN is empty in $SECRET_ENV_FILE" >&2
    missing=1
  fi
  if [[ "$missing" -ne 0 ]]; then
    echo "Service was installed and enabled but not started. Complete config/secret env files first." >&2
    exit 78
  fi
  systemctl restart "$SERVICE_NAME"
fi

print_summary
echo "installation: PASS"
