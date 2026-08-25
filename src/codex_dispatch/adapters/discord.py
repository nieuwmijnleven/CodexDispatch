"""Discord Gateway adapter.

The module deliberately defers importing ``discord.py`` until runtime so the core
package and unit tests remain importable in minimal environments. Production
startup still fails clearly when the dependency is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
import importlib
import logging
from typing import Any

from ..config import Settings


LOGGER = logging.getLogger(__name__)
DISCORD_MESSAGE_LIMIT = 2000


class DiscordAdapterError(RuntimeError):
    """Base class for Discord adapter failures."""


class DiscordDependencyError(DiscordAdapterError):
    """Raised when discord.py is not installed."""


class DiscordNotReadyError(DiscordAdapterError):
    """Raised when an outbound operation is attempted before startup."""


@dataclass(frozen=True, slots=True)
class DiscordIncomingMessage:
    """Sanitized metadata for an inbound Discord text message."""

    message_id: int
    guild_id: int | None
    channel_id: int
    parent_channel_id: int | None
    user_id: int
    content: str


@dataclass(frozen=True, slots=True)
class DiscordCommandRequest:
    name: str
    guild_id: int
    channel_id: int
    parent_channel_id: int | None
    user_id: int
    job_id: int | None = None
    mode: str | None = None
    auto_state: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordAuthorization:
    """Authorization decision for an inbound Discord message."""

    allowed: bool
    reason: str


MessageHandler = Callable[[DiscordIncomingMessage], Awaitable[None]]
CommandHandler = Callable[[DiscordCommandRequest], Awaitable[str]]
ReadyHandler = Callable[[], Awaitable[None]]


def _snowflake(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parent_channel_id(channel: Any) -> int | None:
    parent_id = _snowflake(getattr(channel, "parent_id", None))
    if parent_id is not None:
        return parent_id
    parent = getattr(channel, "parent", None)
    return _snowflake(getattr(parent, "id", None))


class DiscordAuthorizer:
    """Fail-closed Discord Guild/Channel/User allowlist."""

    def __init__(self, settings: Settings) -> None:
        settings.require_discord()
        self._guild_ids = frozenset(settings.discord_allowed_guild_ids)
        self._channel_ids = frozenset(settings.discord_allowed_channel_ids)
        self._user_ids = frozenset(settings.discord_allowed_user_ids)

    def authorize_raw(self, message: Any) -> DiscordAuthorization:
        """Authorize a discord.py Message without trusting message content."""

        author = getattr(message, "author", None)
        if author is None:
            return DiscordAuthorization(False, "missing_author")
        if bool(getattr(author, "bot", False)):
            return DiscordAuthorization(False, "bot_author")
        if _snowflake(getattr(message, "webhook_id", None)) is not None:
            return DiscordAuthorization(False, "webhook_message")

        is_system = getattr(message, "is_system", None)
        if callable(is_system):
            try:
                if bool(is_system()):
                    return DiscordAuthorization(False, "system_message")
            except Exception:
                LOGGER.warning("discord.system_message_check_failed", exc_info=True)
                return DiscordAuthorization(False, "system_message_check_failed")

        guild = getattr(message, "guild", None)
        guild_id = _snowflake(getattr(guild, "id", None))
        if guild_id is None:
            return DiscordAuthorization(False, "dm_or_missing_guild")
        if guild_id not in self._guild_ids:
            return DiscordAuthorization(False, "guild_not_allowed")

        user_id = _snowflake(getattr(author, "id", None))
        if user_id is None or user_id not in self._user_ids:
            return DiscordAuthorization(False, "user_not_allowed")

        channel = getattr(message, "channel", None)
        channel_id = _snowflake(getattr(channel, "id", None))
        if channel_id is None:
            return DiscordAuthorization(False, "missing_channel")
        parent_id = _parent_channel_id(channel)
        if channel_id not in self._channel_ids and parent_id not in self._channel_ids:
            return DiscordAuthorization(False, "channel_not_allowed")

        content = getattr(message, "content", "")
        if not isinstance(content, str) or not content.strip():
            return DiscordAuthorization(False, "empty_text")

        return DiscordAuthorization(True, "allowed")

    def authorize_identity(
        self,
        guild_id: int | None,
        user_id: int | None,
        channel: Any,
    ) -> DiscordAuthorization:
        if guild_id is None or guild_id not in self._guild_ids:
            return DiscordAuthorization(False, "guild_not_allowed")
        if user_id is None or user_id not in self._user_ids:
            return DiscordAuthorization(False, "user_not_allowed")
        channel_id = _snowflake(getattr(channel, "id", None))
        if channel_id is None:
            return DiscordAuthorization(False, "missing_channel")
        parent_id = _parent_channel_id(channel)
        if channel_id not in self._channel_ids and parent_id not in self._channel_ids:
            return DiscordAuthorization(False, "channel_not_allowed")
        return DiscordAuthorization(True, "allowed")

    def authorize_channel(self, channel: Any) -> DiscordAuthorization:
        """Authorize an outbound guild channel or thread."""

        guild = getattr(channel, "guild", None)
        guild_id = _snowflake(getattr(guild, "id", None))
        if guild_id is None or guild_id not in self._guild_ids:
            return DiscordAuthorization(False, "guild_not_allowed")

        channel_id = _snowflake(getattr(channel, "id", None))
        if channel_id is None:
            return DiscordAuthorization(False, "missing_channel")
        parent_id = _parent_channel_id(channel)
        if channel_id not in self._channel_ids and parent_id not in self._channel_ids:
            return DiscordAuthorization(False, "channel_not_allowed")
        return DiscordAuthorization(True, "allowed")

    def normalize(self, message: Any) -> DiscordIncomingMessage:
        """Convert an authorized discord.py Message into a stable internal shape."""

        decision = self.authorize_raw(message)
        if not decision.allowed:
            raise DiscordAdapterError(
                f"cannot normalize unauthorized Discord message: {decision.reason}"
            )

        guild = message.guild
        channel = message.channel
        author = message.author
        message_id = _snowflake(getattr(message, "id", None))
        guild_id = _snowflake(getattr(guild, "id", None))
        channel_id = _snowflake(getattr(channel, "id", None))
        user_id = _snowflake(getattr(author, "id", None))
        if None in (message_id, guild_id, channel_id, user_id):
            raise DiscordAdapterError("authorized Discord message contains invalid snowflakes")

        return DiscordIncomingMessage(
            message_id=message_id,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=_parent_channel_id(channel),
            user_id=user_id,
            content=message.content.strip(),
        )


class DiscordAdapter:
    """discord.py-backed connection with fail-closed inbound authorization."""

    def __init__(
        self,
        settings: Settings,
        on_message: MessageHandler,
        on_command: CommandHandler | None = None,
        on_ready: ReadyHandler | None = None,
    ) -> None:
        settings.require_discord()
        self._settings = settings
        self._on_message = on_message
        self._on_command = on_command
        self._on_ready = on_ready
        self._authorizer = DiscordAuthorizer(settings)
        self._client: Any | None = None

    @property
    def client(self) -> Any | None:
        return self._client

    def _load_discord(self) -> Any:
        try:
            return importlib.import_module("discord")
        except ModuleNotFoundError as exc:
            raise DiscordDependencyError(
                "discord.py is required for the Discord adapter; install the project "
                "runtime dependencies first"
            ) from exc

    def _build_client(self) -> Any:
        discord = self._load_discord()
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        adapter = self

        class CodexDispatchDiscordClient(discord.Client):
            async def setup_hook(self) -> None:
                tree = getattr(self, "command_tree", None)
                if tree is None:
                    return
                for guild_id in adapter._settings.discord_allowed_guild_ids:
                    guild = discord.Object(id=guild_id)
                    tree.copy_global_to(guild=guild)
                    await tree.sync(guild=guild)
                LOGGER.info(
                    "discord.commands_synced guilds=%s",
                    len(adapter._settings.discord_allowed_guild_ids),
                )

            async def on_ready(self) -> None:
                user_id = _snowflake(getattr(getattr(self, "user", None), "id", None))
                LOGGER.info("discord.ready user_id=%s", user_id or "unknown")
                if adapter._on_ready is not None:
                    try:
                        await adapter._on_ready()
                    except Exception:
                        LOGGER.exception("discord.ready_handler_failed")

            async def on_disconnect(self) -> None:
                LOGGER.warning("discord.disconnected")

            async def on_resumed(self) -> None:
                LOGGER.info("discord.resumed")

            async def on_message(self, message: Any) -> None:
                await adapter.handle_message(message)

        client = CodexDispatchDiscordClient(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if self._on_command is not None:
            tree = discord.app_commands.CommandTree(client)
            client.command_tree = tree

            @tree.command(name="status", description="Show the mapped Codex session status")
            async def status_command(interaction: Any) -> None:
                await adapter.handle_command(interaction, "status")

            @tree.command(name="queue", description="Show queued and active Codex jobs")
            async def queue_command(interaction: Any) -> None:
                await adapter.handle_command(interaction, "queue")

            @tree.command(name="cancel", description="Cancel a queued or active managed Codex job")
            async def cancel_command(interaction: Any, job_id: int) -> None:
                await adapter.handle_command(interaction, "cancel", job_id=job_id)

            @tree.command(name="mode", description="Switch this Codex session between live and managed")
            async def mode_command(interaction: Any, mode: str) -> None:
                await adapter.handle_command(interaction, "mode", mode=mode)

            @tree.command(name="auto", description="Enable or disable sequential queued-job auto-run")
            async def auto_command(interaction: Any, state: str) -> None:
                await adapter.handle_command(interaction, "auto", auto_state=state)

        return client

    async def start(self) -> None:
        """Connect to Discord and stay connected until closed.

        discord.py handles Gateway reconnects when ``reconnect=True``.
        """

        if self._client is not None:
            raise DiscordAdapterError("Discord adapter is already started")
        self._client = self._build_client()
        token = self._settings.discord_bot_token
        if token is None:  # guarded by require_discord; defensive only
            raise DiscordAdapterError("Discord bot token is unavailable")
        try:
            await self._client.start(token, reconnect=True)
        finally:
            self._client = None

    async def close(self) -> None:
        client = self._client
        if client is not None and not client.is_closed():
            await client.close()

    async def handle_message(self, message: Any) -> None:
        """Authorize, normalize, and dispatch one Discord message."""

        decision = self._authorizer.authorize_raw(message)
        if not decision.allowed:
            LOGGER.info(
                "discord.message_rejected reason=%s message_id=%s",
                decision.reason,
                _snowflake(getattr(message, "id", None)) or "unknown",
            )
            return

        incoming = self._authorizer.normalize(message)
        LOGGER.info(
            "discord.message_received message_id=%s guild_id=%s channel_id=%s "
            "parent_channel_id=%s user_id=%s content_length=%d",
            incoming.message_id,
            incoming.guild_id,
            incoming.channel_id,
            incoming.parent_channel_id or "none",
            incoming.user_id,
            len(incoming.content),
        )
        try:
            await self._on_message(incoming)
        except Exception:
            LOGGER.exception(
                "discord.message_handler_failed message_id=%s", incoming.message_id
            )

    async def handle_command(
        self,
        interaction: Any,
        name: str,
        *,
        job_id: int | None = None,
        mode: str | None = None,
        auto_state: str | None = None,
    ) -> None:
        """Authorize and route one Discord application command."""

        handler = self._on_command
        response = getattr(interaction, "response", None)
        send_message = getattr(response, "send_message", None)
        if handler is None or not callable(send_message):
            return

        guild_id = _snowflake(getattr(interaction, "guild_id", None))
        user = getattr(interaction, "user", None)
        user_id = _snowflake(getattr(user, "id", None))
        channel = getattr(interaction, "channel", None)
        decision = self._authorizer.authorize_identity(guild_id, user_id, channel)
        if not decision.allowed:
            LOGGER.info(
                "discord.command_rejected name=%s reason=%s user_id=%s",
                name,
                decision.reason,
                user_id or "unknown",
            )
            await send_message("⚠️ 이 명령을 사용할 권한이 없습니다.", ephemeral=True)
            return

        channel_id = _snowflake(getattr(channel, "id", None))
        assert guild_id is not None
        assert user_id is not None
        assert channel_id is not None
        request = DiscordCommandRequest(
            name=name,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=_parent_channel_id(channel),
            user_id=user_id,
            job_id=job_id,
            mode=mode,
            auto_state=auto_state,
        )
        try:
            text = await handler(request)
        except Exception:
            LOGGER.exception("discord.command_handler_failed name=%s", name)
            await send_message("⚠️ 명령 처리 중 오류가 발생했습니다.", ephemeral=True)
            return
        await send_message(text[:DISCORD_MESSAGE_LIMIT], ephemeral=True)

    async def send_text(
        self,
        channel_id: int,
        content: str,
        *,
        nonce: int | None = None,
    ) -> int:
        """Send plain text and return the confirmed Discord message snowflake."""

        if channel_id <= 0:
            raise ValueError("channel_id must be a positive Discord snowflake")
        text = content.strip()
        if not text:
            raise ValueError("Discord message content must not be empty")
        if len(text) > DISCORD_MESSAGE_LIMIT:
            raise ValueError(
                f"Discord message content exceeds {DISCORD_MESSAGE_LIMIT} characters"
            )

        client = self._client
        if client is None or client.is_closed():
            raise DiscordNotReadyError("Discord adapter is not connected")

        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        decision = self._authorizer.authorize_channel(channel)
        if not decision.allowed:
            raise DiscordAdapterError(
                f"refusing outbound Discord channel {channel_id}: {decision.reason}"
            )

        send = getattr(channel, "send", None)
        if not callable(send):
            raise DiscordAdapterError(
                f"Discord channel {channel_id} does not support text messages"
            )
        if nonce is not None and nonce <= 0:
            raise ValueError("nonce must be a positive integer")
        message = await send(text, nonce=nonce) if nonce is not None else await send(text)
        message_id = _snowflake(getattr(message, "id", None))
        if message_id is None:
            raise DiscordAdapterError("Discord send returned an invalid message id")
        return message_id

    async def create_thread(self, parent_channel_id: int, name: str) -> int:
        """Create a Discord thread under the configured control channel."""

        if parent_channel_id <= 0:
            raise ValueError("parent_channel_id must be a positive Discord snowflake")
        thread_name = name.strip()
        if not thread_name:
            raise ValueError("Discord thread name must not be empty")
        if len(thread_name) > 100:
            raise ValueError("Discord thread name exceeds 100 characters")

        client = self._client
        if client is None or client.is_closed():
            raise DiscordNotReadyError("Discord adapter is not connected")

        channel = client.get_channel(parent_channel_id)
        if channel is None:
            channel = await client.fetch_channel(parent_channel_id)
        decision = self._authorizer.authorize_channel(channel)
        if not decision.allowed or _parent_channel_id(channel) is not None:
            raise DiscordAdapterError(
                f"refusing Discord thread parent {parent_channel_id}: "
                f"{decision.reason if not decision.allowed else 'parent must be a direct allowed channel'}"
            )

        create_thread = getattr(channel, "create_thread", None)
        if not callable(create_thread):
            raise DiscordAdapterError(
                f"Discord channel {parent_channel_id} does not support threads"
            )

        created = await create_thread(
            name=thread_name,
            reason="Codex Dispatch session mapping",
        )
        thread = getattr(created, "thread", created)
        thread_id = _snowflake(getattr(thread, "id", None))
        if thread_id is None:
            raise DiscordAdapterError("Discord did not return a valid thread ID")

        created_decision = self._authorizer.authorize_channel(thread)
        if not created_decision.allowed:
            raise DiscordAdapterError(
                f"created Discord thread {thread_id} is outside the allowlist"
            )
        return thread_id
