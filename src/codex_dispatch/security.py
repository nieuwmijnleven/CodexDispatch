"""Security primitives shared across Codex Dispatch boundaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import os
from pathlib import Path
import re


class WorkspaceSecurityError(ValueError):
    """Raised when a workspace path cannot be trusted for Codex execution."""


class RuntimeSecurityError(ValueError):
    """Raised when deployment boundaries expose Codex Dispatch internals to Codex."""


_SYSTEM_WORKSPACE_DENYLIST = tuple(
    Path(value) for value in ("/etc", "/usr", "/boot", "/root", "/proc", "/sys", "/dev", "/run")
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(DISCORD_BOT_TOKEN\s*=\s*)([^\s]+)"),
    re.compile(r"(?i)(Authorization\s*:\s*(?:Bot|Bearer)\s+)([^\s]+)"),
)


def resolve_allowed_workspace(
    path: str | Path,
    allowed_roots: Sequence[Path],
) -> Path:
    """Resolve an existing directory and require it to remain inside an allowed root.

    This function is intentionally reusable at both notify-ingress and dispatch time.
    Re-validating immediately before a subprocess is created protects against stale
    database paths and common symlink/path replacement mistakes after registration.
    """

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceSecurityError("workspace must be an absolute path")
    if not allowed_roots:
        raise WorkspaceSecurityError("no workspace roots are configured")

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceSecurityError("workspace does not resolve to an existing path") from exc
    if not resolved.is_dir():
        raise WorkspaceSecurityError("workspace must resolve to a directory")

    for configured_root in allowed_roots:
        try:
            root = Path(configured_root).expanduser().resolve(strict=True)
        except OSError:
            continue
        if not root.is_dir():
            continue
        if resolved == root or resolved.is_relative_to(root):
            return resolved

    raise WorkspaceSecurityError("workspace is outside CODEX_ALLOWED_ROOTS")


def validate_runtime_security(
    *,
    allowed_roots: Sequence[Path],
    database_path: Path,
    codex_binary: str,
    project_root: Path,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Reject workspace roots that can modify orchestrator state or credentials.

    A remote Discord prompt is effectively allowed to ask Codex to modify anything
    beneath ``CODEX_ALLOWED_ROOTS``. Those roots therefore must not contain Codex
    Dispatch itself, its database, the Codex executable, or common user credential
    directories. The normalized roots are returned for callers that need them.
    """

    if not allowed_roots:
        raise RuntimeSecurityError("CODEX_ALLOWED_ROOTS must not be empty")

    normalized: list[Path] = []
    for raw_root in allowed_roots:
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            raise RuntimeSecurityError("CODEX_ALLOWED_ROOTS entries must be absolute")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeSecurityError("CODEX_ALLOWED_ROOTS entries must exist") from exc
        if not resolved.is_dir():
            raise RuntimeSecurityError("CODEX_ALLOWED_ROOTS entries must be directories")
        if resolved == Path(resolved.anchor):
            raise RuntimeSecurityError("filesystem root cannot be used as CODEX_ALLOWED_ROOTS")
        if any(
            resolved == denied or resolved.is_relative_to(denied)
            for denied in _SYSTEM_WORKSPACE_DENYLIST
        ):
            raise RuntimeSecurityError(
                "system directory cannot be used as CODEX_ALLOWED_ROOTS"
            )
        normalized.append(resolved)

    effective_home = (Path.home() if home is None else Path(home)).expanduser().resolve(strict=False)
    protected: list[tuple[str, Path]] = [
        ("Codex Dispatch project", Path(project_root).resolve(strict=False)),
        ("Codex Dispatch database", Path(database_path).resolve(strict=False)),
        ("Codex credentials", (effective_home / ".codex").resolve(strict=False)),
        ("SSH credentials", (effective_home / ".ssh").resolve(strict=False)),
    ]
    binary_path = Path(codex_binary).expanduser()
    if binary_path.is_absolute():
        protected.append(("Codex executable", binary_path.resolve(strict=False)))

    for root in normalized:
        for label, protected_path in protected:
            if protected_path == root or protected_path.is_relative_to(root):
                raise RuntimeSecurityError(
                    f"CODEX_ALLOWED_ROOTS overlaps protected path: {label}"
                )
    return tuple(normalized)


def sanitized_codex_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment for Codex children without orchestrator credentials.

    Codex authentication variables such as CODEX_HOME or OPENAI_API_KEY are not
    removed because they may be required by the CLI. Only Codex Dispatch control
    data and Discord credentials are stripped.
    """

    source = os.environ if environ is None else environ
    return {
        key: value
        for key, value in source.items()
        if not key.startswith("DISCORD_")
        and not key.startswith("CODEX_DISPATCH_")
        and key != "CODEX_ALLOWED_ROOTS"
    }


def redact_sensitive_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact known runtime secrets and common credential-shaped log fragments."""

    redacted = text
    for secret in secrets:
        value = secret.strip()
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted
