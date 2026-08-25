"""Validated Unix-socket receiver for Codex external notify events."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import socket
import stat
import struct
from typing import Any

from .security import WorkspaceSecurityError, resolve_allowed_workspace


LOGGER = logging.getLogger(__name__)

_AGENT_TURN_COMPLETE = "agent-turn-complete"
_HEADER = struct.Struct("!I")
_MAX_ID_LENGTH = 256
_MAX_CLIENT_LENGTH = 128
_MAX_MESSAGE_COUNT = 256


class NotifyValidationError(ValueError):
    """Raised when a Codex notify payload is malformed or unauthorized."""


class NotifyServerError(RuntimeError):
    """Raised when the local notify server cannot be started safely."""


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    """Validated Codex completion event passed to the orchestrator."""

    event_type: str
    thread_id: str
    turn_id: str
    cwd: Path
    client: str | None
    input_messages: tuple[str, ...]
    last_assistant_message: str | None

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.thread_id, self.turn_id, self.event_type)


def _text(
    payload: Mapping[str, Any],
    key: str,
    *,
    required: bool,
    max_length: int,
) -> str | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise NotifyValidationError(f"{key} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise NotifyValidationError(f"{key} exceeds the maximum length")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise NotifyValidationError(f"{key} contains control characters")
    return value


def _resolve_notify_cwd(raw_cwd: str, allowed_roots: Sequence[Path]) -> Path:
    try:
        return resolve_allowed_workspace(raw_cwd, allowed_roots)
    except WorkspaceSecurityError as exc:
        message = str(exc).replace("workspace", "cwd", 1)
        raise NotifyValidationError(message) from exc


def parse_notify_payload(
    payload: Mapping[str, Any],
    allowed_roots: Sequence[Path],
) -> NotifyEvent:
    """Validate an external notify object and return its normalized event."""

    event_type = _text(payload, "type", required=True, max_length=64)
    if event_type != _AGENT_TURN_COMPLETE:
        raise NotifyValidationError("unsupported notify event type")

    thread_id = _text(payload, "thread-id", required=True, max_length=_MAX_ID_LENGTH)
    turn_id = _text(payload, "turn-id", required=True, max_length=_MAX_ID_LENGTH)
    raw_cwd = _text(payload, "cwd", required=True, max_length=4096)
    client = _text(payload, "client", required=False, max_length=_MAX_CLIENT_LENGTH)
    assert thread_id is not None
    assert turn_id is not None
    assert raw_cwd is not None

    raw_inputs = payload.get("input-messages", [])
    if raw_inputs is None:
        raw_inputs = []
    if not isinstance(raw_inputs, list) or any(
        not isinstance(item, str) for item in raw_inputs
    ):
        raise NotifyValidationError("input-messages must be a list of strings")
    if len(raw_inputs) > _MAX_MESSAGE_COUNT:
        raise NotifyValidationError("input-messages contains too many entries")

    last_message = payload.get("last-assistant-message")
    if last_message is not None and not isinstance(last_message, str):
        raise NotifyValidationError("last-assistant-message must be a string")

    return NotifyEvent(
        event_type=event_type,
        thread_id=thread_id,
        turn_id=turn_id,
        cwd=_resolve_notify_cwd(raw_cwd, allowed_roots),
        client=client,
        input_messages=tuple(raw_inputs),
        last_assistant_message=last_message,
    )


NotifyHandler = Callable[[NotifyEvent], Awaitable[None]]


class NotifyServer:
    """Receive one length-prefixed JSON notify event per Unix-socket connection."""

    def __init__(
        self,
        socket_path: Path,
        allowed_roots: Sequence[Path],
        handler: NotifyHandler,
        *,
        max_payload_bytes: int = 262_144,
        dedupe_limit: int = 4096,
        read_timeout_seconds: float = 1.0,
    ) -> None:
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        if dedupe_limit <= 0:
            raise ValueError("dedupe_limit must be positive")
        if read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")

        self.socket_path = socket_path
        self.allowed_roots = tuple(allowed_roots)
        self.handler = handler
        self.max_payload_bytes = max_payload_bytes
        self.dedupe_limit = dedupe_limit
        self.read_timeout_seconds = read_timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    @property
    def is_running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        """Create the private runtime directory and bind the Unix socket."""

        if self._server is not None:
            raise NotifyServerError("notify server is already running")

        parent = self.socket_path.parent
        try:
            parent_existed = parent.exists()
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not parent_existed:
                parent.chmod(0o700)
            parent_stat = parent.stat()
        except OSError as exc:
            raise NotifyServerError("cannot prepare notify socket directory") from exc

        if not stat.S_ISDIR(parent_stat.st_mode):
            raise NotifyServerError("notify socket parent is not a directory")
        if parent_stat.st_uid != os.geteuid():
            raise NotifyServerError("notify socket directory must be owned by the service user")
        if stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise NotifyServerError("notify socket directory must be private (mode 0700)")

        try:
            if self.socket_path.exists() or self.socket_path.is_symlink():
                mode = self.socket_path.lstat().st_mode
                if not stat.S_ISSOCK(mode):
                    raise NotifyServerError(
                        "refusing to replace non-socket notify path"
                    )

                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.settimeout(0.1)
                    probe.connect(str(self.socket_path))
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
                except OSError as exc:
                    raise NotifyServerError(
                        "cannot determine whether notify socket is active"
                    ) from exc
                else:
                    raise NotifyServerError("notify socket is already active")
                finally:
                    probe.close()

                self.socket_path.unlink()
        except NotifyServerError:
            raise
        except OSError as exc:
            raise NotifyServerError("cannot inspect stale notify socket") from exc

        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=str(self.socket_path),
            )
            self.socket_path.chmod(0o600)
        except OSError as exc:
            self._server = None
            raise NotifyServerError("cannot bind notify Unix socket") from exc

        LOGGER.info("notify.server_started socket=%s", self.socket_path)

    async def close(self) -> None:
        """Stop accepting notify events and remove the socket file."""

        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()

        try:
            if self.socket_path.exists() or self.socket_path.is_symlink():
                if stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                    self.socket_path.unlink()
        except OSError:
            LOGGER.warning("notify.socket_cleanup_failed socket=%s", self.socket_path)

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        try:
            header = await asyncio.wait_for(
                reader.readexactly(_HEADER.size),
                timeout=self.read_timeout_seconds,
            )
        except (asyncio.IncompleteReadError, TimeoutError) as exc:
            raise NotifyValidationError("incomplete notify frame header") from exc

        (size,) = _HEADER.unpack(header)
        if size <= 0 or size > self.max_payload_bytes:
            raise NotifyValidationError("notify payload size is invalid")

        try:
            return await asyncio.wait_for(
                reader.readexactly(size),
                timeout=self.read_timeout_seconds,
            )
        except (asyncio.IncompleteReadError, TimeoutError) as exc:
            raise NotifyValidationError("incomplete notify payload") from exc

    async def _reply(self, writer: asyncio.StreamWriter, response: bytes) -> None:
        writer.write(response)
        try:
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass

    def _remember(self, key: tuple[str, str, str]) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self.dedupe_limit:
            self._seen.popitem(last=False)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await self._read_frame(reader)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NotifyValidationError("notify payload is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise NotifyValidationError("notify payload must be a JSON object")

            event = parse_notify_payload(payload, self.allowed_roots)
            if event.dedupe_key in self._seen:
                LOGGER.info(
                    "notify.duplicate thread_id=%s turn_id=%s",
                    event.thread_id,
                    event.turn_id,
                )
                await self._reply(writer, b"DUPLICATE\n")
                return

            try:
                await self.handler(event)
            except Exception:
                LOGGER.exception(
                    "notify.handler_failed thread_id=%s turn_id=%s",
                    event.thread_id,
                    event.turn_id,
                )
                await self._reply(writer, b"ERROR handler\n")
                return

            self._remember(event.dedupe_key)
            LOGGER.info(
                "notify.received thread_id=%s turn_id=%s client=%s",
                event.thread_id,
                event.turn_id,
                event.client or "unknown",
            )
            await self._reply(writer, b"ACK\n")
        except NotifyValidationError as exc:
            LOGGER.warning("notify.rejected reason=%s", exc)
            await self._reply(writer, b"ERROR validation\n")
        except Exception:
            LOGGER.exception("notify.connection_failed")
            await self._reply(writer, b"ERROR internal\n")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
