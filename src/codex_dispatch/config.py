"""Environment-backed configuration for Codex Dispatch."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


class SettingsError(ValueError):
    """Raised when Codex Dispatch configuration is invalid."""


_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


def _get(env: Mapping[str, str], key: str, default: str = "") -> str:
    return env.get(key, default).strip()


def _parse_log_level(value: str) -> str:
    level = value.upper() or "INFO"
    if level not in _VALID_LOG_LEVELS:
        allowed = ", ".join(sorted(_VALID_LOG_LEVELS))
        raise SettingsError(
            f"CODEX_DISPATCH_LOG_LEVEL must be one of {allowed}; got {value!r}"
        )
    return level


def _parse_path(value: str, *, key: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SettingsError(f"{key} must be an absolute path; got {value!r}")
    return path.resolve(strict=False)


def _parse_positive_int(value: str, *, key: str, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise SettingsError(f"{key} must be a positive integer") from exc
    if parsed <= 0:
        raise SettingsError(f"{key} must be a positive integer")
    return parsed


def _parse_positive_float(value: str, *, key: str, default: float) -> float:
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise SettingsError(f"{key} must be a positive number") from exc
    if parsed <= 0:
        raise SettingsError(f"{key} must be a positive number")
    return parsed


def _default_notify_socket_path(env: Mapping[str, str]) -> Path:
    runtime_dir = _get(env, "XDG_RUNTIME_DIR")
    if runtime_dir:
        base = _parse_path(runtime_dir, key="XDG_RUNTIME_DIR")
    else:
        base = Path(f"/run/user/{os.getuid()}")
    return (base / "codex-dispatch" / "notify.sock").resolve(strict=False)


def _parse_allowed_roots(value: str) -> tuple[Path, ...]:
    if not value:
        return ()

    roots: list[Path] = []
    seen: set[Path] = set()
    for raw_root in value.split(os.pathsep):
        raw_root = raw_root.strip()
        if not raw_root:
            continue
        root = _parse_path(raw_root, key="CODEX_ALLOWED_ROOTS")
        if root not in seen:
            roots.append(root)
            seen.add(root)
    return tuple(roots)


def _parse_snowflake_list(value: str, *, key: str) -> tuple[int, ...]:
    if not value:
        return ()

    result: list[int] = []
    seen: set[int] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            parsed = int(item, 10)
        except ValueError as exc:
            raise SettingsError(f"{key} must contain comma-separated integers") from exc
        if parsed <= 0:
            raise SettingsError(f"{key} values must be positive integers")
        if parsed not in seen:
            result.append(parsed)
            seen.add(parsed)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated Codex Dispatch settings.

    Discord values remain optional at parse time so configuration can be inspected in
    isolation. The PHASE 2+ service calls ``require_discord()`` before startup.
    """

    log_level: str
    database_path: Path
    allowed_roots: tuple[Path, ...]
    discord_bot_token: str | None
    discord_control_channel_id: int | None
    discord_allowed_guild_ids: tuple[int, ...]
    discord_allowed_channel_ids: tuple[int, ...]
    discord_allowed_user_ids: tuple[int, ...]
    notify_socket_path: Path
    notify_max_payload_bytes: int
    discord_delivery_retry_attempts: int
    discord_delivery_retry_base_seconds: float
    codex_binary: str
    codex_queue_timeout_seconds: float
    codex_capability_timeout_seconds: float
    codex_prompt_max_chars: int
    managed_exec_timeout_seconds: float
    managed_output_max_bytes: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ

        database_path = _parse_path(
            _get(env, "CODEX_DISPATCH_DB_PATH", "/var/lib/codex-dispatch/codex-dispatch.db"),
            key="CODEX_DISPATCH_DB_PATH",
        )
        token = _get(env, "DISCORD_BOT_TOKEN") or None
        raw_control_channel_id = _get(env, "DISCORD_CONTROL_CHANNEL_ID")
        control_channel_id: int | None = None
        if raw_control_channel_id:
            try:
                control_channel_id = int(raw_control_channel_id, 10)
            except ValueError as exc:
                raise SettingsError(
                    "DISCORD_CONTROL_CHANNEL_ID must be a positive integer"
                ) from exc
            if control_channel_id <= 0:
                raise SettingsError(
                    "DISCORD_CONTROL_CHANNEL_ID must be a positive integer"
                )
        raw_notify_socket = _get(env, "CODEX_DISPATCH_NOTIFY_SOCKET")
        notify_socket_path = (
            _parse_path(raw_notify_socket, key="CODEX_DISPATCH_NOTIFY_SOCKET")
            if raw_notify_socket
            else _default_notify_socket_path(env)
        )

        return cls(
            log_level=_parse_log_level(_get(env, "CODEX_DISPATCH_LOG_LEVEL", "INFO")),
            database_path=database_path,
            allowed_roots=_parse_allowed_roots(_get(env, "CODEX_ALLOWED_ROOTS")),
            discord_bot_token=token,
            discord_control_channel_id=control_channel_id,
            discord_allowed_guild_ids=_parse_snowflake_list(
                _get(env, "DISCORD_ALLOWED_GUILD_IDS"),
                key="DISCORD_ALLOWED_GUILD_IDS",
            ),
            discord_allowed_channel_ids=_parse_snowflake_list(
                _get(env, "DISCORD_ALLOWED_CHANNEL_IDS"),
                key="DISCORD_ALLOWED_CHANNEL_IDS",
            ),
            discord_allowed_user_ids=_parse_snowflake_list(
                _get(env, "DISCORD_ALLOWED_USER_IDS"),
                key="DISCORD_ALLOWED_USER_IDS",
            ),
            notify_socket_path=notify_socket_path,
            notify_max_payload_bytes=_parse_positive_int(
                _get(env, "CODEX_DISPATCH_NOTIFY_MAX_BYTES"),
                key="CODEX_DISPATCH_NOTIFY_MAX_BYTES",
                default=262_144,
            ),
            discord_delivery_retry_attempts=_parse_positive_int(
                _get(env, "CODEX_DISPATCH_DISCORD_RETRY_ATTEMPTS"),
                key="CODEX_DISPATCH_DISCORD_RETRY_ATTEMPTS",
                default=3,
            ),
            discord_delivery_retry_base_seconds=_parse_positive_float(
                _get(env, "CODEX_DISPATCH_DISCORD_RETRY_BASE_SECONDS"),
                key="CODEX_DISPATCH_DISCORD_RETRY_BASE_SECONDS",
                default=0.5,
            ),
            codex_binary=_get(env, "CODEX_DISPATCH_CODEX_BIN", "codex") or "codex",
            codex_queue_timeout_seconds=_parse_positive_float(
                _get(env, "CODEX_DISPATCH_CODEX_QUEUE_TIMEOUT_SECONDS"),
                key="CODEX_DISPATCH_CODEX_QUEUE_TIMEOUT_SECONDS",
                default=10.0,
            ),
            codex_capability_timeout_seconds=_parse_positive_float(
                _get(env, "CODEX_DISPATCH_CODEX_CAPABILITY_TIMEOUT_SECONDS"),
                key="CODEX_DISPATCH_CODEX_CAPABILITY_TIMEOUT_SECONDS",
                default=5.0,
            ),
            codex_prompt_max_chars=_parse_positive_int(
                _get(env, "CODEX_DISPATCH_CODEX_PROMPT_MAX_CHARS"),
                key="CODEX_DISPATCH_CODEX_PROMPT_MAX_CHARS",
                default=8_000,
            ),
            managed_exec_timeout_seconds=_parse_positive_float(
                _get(env, "CODEX_DISPATCH_MANAGED_TIMEOUT_SECONDS"),
                key="CODEX_DISPATCH_MANAGED_TIMEOUT_SECONDS",
                default=21_600.0,
            ),
            managed_output_max_bytes=_parse_positive_int(
                _get(env, "CODEX_DISPATCH_MANAGED_OUTPUT_MAX_BYTES"),
                key="CODEX_DISPATCH_MANAGED_OUTPUT_MAX_BYTES",
                default=262_144,
            ),
        )

    def require_notify(self) -> None:
        """Validate settings required before accepting Codex notify events."""

        if not self.allowed_roots:
            raise SettingsError(
                "Codex notify configuration is incomplete; missing: CODEX_ALLOWED_ROOTS"
            )

    def require_discord(self) -> None:
        """Validate settings required before the Discord adapter is started."""

        missing: list[str] = []
        if not self.discord_bot_token:
            missing.append("DISCORD_BOT_TOKEN")
        if self.discord_control_channel_id is None:
            missing.append("DISCORD_CONTROL_CHANNEL_ID")
        if not self.discord_allowed_guild_ids:
            missing.append("DISCORD_ALLOWED_GUILD_IDS")
        if not self.discord_allowed_channel_ids:
            missing.append("DISCORD_ALLOWED_CHANNEL_IDS")
        if not self.discord_allowed_user_ids:
            missing.append("DISCORD_ALLOWED_USER_IDS")
        if missing:
            raise SettingsError(
                "Discord configuration is incomplete; missing: " + ", ".join(missing)
            )
        assert self.discord_control_channel_id is not None
        if self.discord_control_channel_id not in self.discord_allowed_channel_ids:
            raise SettingsError(
                "DISCORD_CONTROL_CHANNEL_ID must also appear in "
                "DISCORD_ALLOWED_CHANNEL_IDS"
            )


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load and validate Codex Dispatch settings."""

    return Settings.from_env(environ)
