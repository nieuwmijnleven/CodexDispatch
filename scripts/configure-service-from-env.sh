#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="/etc/codex-dispatch"
MAIN_ENV="$CONFIG_DIR/codex-dispatch.env"
SECRET_ENV="$CONFIG_DIR/secret.env"
INSTALL_META="$CONFIG_DIR/install.conf"
SOURCE_ENV=""
SERVICE_USER=""

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/configure-service-from-env.sh [--env-file PATH] [--user USER]

Safely imports only the Codex Dispatch workspace/Discord settings from a local
.env file into the systemd deployment files. The source file is parsed as data;
it is never sourced or executed.

Imported non-secret keys:
  CODEX_ALLOWED_ROOTS
  DISCORD_CONTROL_CHANNEL_ID
  DISCORD_ALLOWED_GUILD_IDS
  DISCORD_ALLOWED_CHANNEL_IDS
  DISCORD_ALLOWED_USER_IDS

Imported secret key:
  DISCORD_BOT_TOKEN -> /etc/codex-dispatch/secret.env (root:root 0600)
EOF
}

while (($#)); do
  case "$1" in
    --env-file)
      [[ $# -ge 2 ]] || { echo "ERROR: --env-file requires a value" >&2; exit 64; }
      SOURCE_ENV="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || { echo "ERROR: --user requires a value" >&2; exit 64; }
      SERVICE_USER="$2"
      shift 2
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
if [[ -z "$SOURCE_ENV" ]]; then
  SOURCE_ENV="$PROJECT_DIR/.env"
fi

[[ "$EUID" -eq 0 ]] || { echo "ERROR: configuration requires root; run through sudo" >&2; exit 77; }
[[ -f "$SOURCE_ENV" ]] || { echo "ERROR: source env file does not exist: $SOURCE_ENV" >&2; exit 2; }
[[ -f "$MAIN_ENV" ]] || { echo "ERROR: run scripts/install-service.sh first; missing $MAIN_ENV" >&2; exit 2; }
[[ -f "$SECRET_ENV" ]] || { echo "ERROR: run scripts/install-service.sh first; missing $SECRET_ENV" >&2; exit 2; }

if [[ -z "$SERVICE_USER" && -f "$INSTALL_META" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      SERVICE_USER=*) SERVICE_USER="${line#SERVICE_USER=}"; break ;;
    esac
  done < "$INSTALL_META"
fi
if [[ -z "$SERVICE_USER" ]]; then
  SERVICE_USER="${SUDO_USER:-}"
fi
[[ -n "$SERVICE_USER" ]] || { echo "ERROR: service user is unknown; pass --user USER" >&2; exit 64; }
[[ "$SERVICE_USER" != "root" ]] || { echo "ERROR: service user must not be root" >&2; exit 64; }
getent passwd "$SERVICE_USER" >/dev/null || { echo "ERROR: Linux user does not exist: $SERVICE_USER" >&2; exit 2; }
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

read_env_value() {
  local key="$1"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      "$key="*) printf '%s' "${line#*=}"; return 0 ;;
    esac
  done < "$SOURCE_ENV"
  return 1
}

require_value() {
  local key="$1"
  local value
  if ! value="$(read_env_value "$key")" || [[ -z "$value" ]]; then
    echo "ERROR: $key is missing or empty in $SOURCE_ENV" >&2
    exit 78
  fi
  printf '%s' "$value"
}

replace_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp found=0 line
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      "$key="*)
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
        found=1
        ;;
      *) printf '%s\n' "$line" >> "$tmp" ;;
    esac
  done < "$file"
  if [[ "$found" -eq 0 ]]; then
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  install -m 0640 -o root -g "$SERVICE_GROUP" "$tmp" "$file"
  rm -f "$tmp"
}

CODEX_ALLOWED_ROOTS_VALUE="$(require_value CODEX_ALLOWED_ROOTS)"
CONTROL_CHANNEL_VALUE="$(require_value DISCORD_CONTROL_CHANNEL_ID)"
GUILD_IDS_VALUE="$(require_value DISCORD_ALLOWED_GUILD_IDS)"
CHANNEL_IDS_VALUE="$(require_value DISCORD_ALLOWED_CHANNEL_IDS)"
USER_IDS_VALUE="$(require_value DISCORD_ALLOWED_USER_IDS)"
BOT_TOKEN_VALUE="$(require_value DISCORD_BOT_TOKEN)"

replace_key "$MAIN_ENV" CODEX_ALLOWED_ROOTS "$CODEX_ALLOWED_ROOTS_VALUE"
replace_key "$MAIN_ENV" DISCORD_CONTROL_CHANNEL_ID "$CONTROL_CHANNEL_VALUE"
replace_key "$MAIN_ENV" DISCORD_ALLOWED_GUILD_IDS "$GUILD_IDS_VALUE"
replace_key "$MAIN_ENV" DISCORD_ALLOWED_CHANNEL_IDS "$CHANNEL_IDS_VALUE"
replace_key "$MAIN_ENV" DISCORD_ALLOWED_USER_IDS "$USER_IDS_VALUE"

secret_tmp="$(mktemp)"
trap 'rm -f "$secret_tmp"' EXIT
printf 'DISCORD_BOT_TOKEN=%s\n' "$BOT_TOKEN_VALUE" > "$secret_tmp"
install -m 0600 -o root -g root "$secret_tmp" "$SECRET_ENV"

# Re-assert deployment permissions after the update.
chown root:"$SERVICE_GROUP" "$MAIN_ENV"
chmod 0640 "$MAIN_ENV"
chown root:root "$SECRET_ENV"
chmod 0600 "$SECRET_ENV"

echo "Codex Dispatch service configuration imported successfully."
echo "  source: $SOURCE_ENV"
echo "  main:   $MAIN_ENV"
echo "  secret: $SECRET_ENV"
echo "No secret values were printed."
