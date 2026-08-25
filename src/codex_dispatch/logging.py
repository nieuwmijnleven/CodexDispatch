"""Logging bootstrap for Codex Dispatch."""

from __future__ import annotations

from collections.abc import Iterable
import logging as std_logging
import sys

from .security import redact_sensitive_text


class RedactingFormatter(std_logging.Formatter):
    """Formatter that removes known credentials from the final rendered record."""

    def __init__(self, fmt: str, *, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(value for value in secrets if value)

    def format(self, record: std_logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_sensitive_text(rendered, self._secrets)


def configure_logging(level: str = "INFO", *, secrets: Iterable[str] = ()) -> None:
    """Configure process-wide logging with stable format and credential redaction."""

    numeric_level = getattr(std_logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Unknown logging level: {level!r}")

    handler = std_logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            secrets=secrets,
        )
    )
    root = std_logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)
