from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_dispatch.adapters.discord import (
    DISCORD_MESSAGE_LIMIT,
    DiscordAdapter,
    DiscordAdapterError,
    DiscordAuthorizer,
    DiscordDependencyError,
    DiscordNotReadyError,
)
from codex_dispatch.config import Settings


BASE_ENV = {
    "DISCORD_BOT_TOKEN": "test-token",
    "DISCORD_CONTROL_CHANNEL_ID": "200",
    "DISCORD_ALLOWED_GUILD_IDS": "100",
    "DISCORD_ALLOWED_CHANNEL_IDS": "200",
    "DISCORD_ALLOWED_USER_IDS": "300",
}


def settings() -> Settings:
    return Settings.from_env(BASE_ENV)


def message(
    *,
    message_id: int = 400,
    guild_id: int | None = 100,
    channel_id: int = 200,
    parent_channel_id: int | None = None,
    user_id: int = 300,
    content: str = "next task",
    bot: bool = False,
    webhook_id: int | None = None,
    system: bool = False,
):
    guild = None if guild_id is None else SimpleNamespace(id=guild_id)
    channel = SimpleNamespace(id=channel_id, parent_id=parent_channel_id, parent=None)
    author = SimpleNamespace(id=user_id, bot=bot)
    return SimpleNamespace(
        id=message_id,
        guild=guild,
        channel=channel,
        author=author,
        content=content,
        webhook_id=webhook_id,
        is_system=lambda: system,
    )


class DiscordAuthorizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authorizer = DiscordAuthorizer(settings())

    def test_allows_direct_allowed_channel(self) -> None:
        decision = self.authorizer.authorize_raw(message())
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_allows_thread_when_parent_channel_is_allowed(self) -> None:
        raw = message(channel_id=201, parent_channel_id=200)
        decision = self.authorizer.authorize_raw(raw)
        self.assertTrue(decision.allowed)
        normalized = self.authorizer.normalize(raw)
        self.assertEqual(normalized.channel_id, 201)
        self.assertEqual(normalized.parent_channel_id, 200)

    def test_rejects_dm(self) -> None:
        self.assertEqual(
            self.authorizer.authorize_raw(message(guild_id=None)).reason,
            "dm_or_missing_guild",
        )

    def test_rejects_unknown_guild(self) -> None:
        self.assertEqual(
            self.authorizer.authorize_raw(message(guild_id=101)).reason,
            "guild_not_allowed",
        )

    def test_rejects_unknown_channel_and_parent(self) -> None:
        self.assertEqual(
            self.authorizer.authorize_raw(
                message(channel_id=201, parent_channel_id=202)
            ).reason,
            "channel_not_allowed",
        )

    def test_rejects_unknown_user(self) -> None:
        self.assertEqual(
            self.authorizer.authorize_raw(message(user_id=301)).reason,
            "user_not_allowed",
        )

    def test_rejects_bot_webhook_system_and_empty_messages(self) -> None:
        cases = [
            (message(bot=True), "bot_author"),
            (message(webhook_id=500), "webhook_message"),
            (message(system=True), "system_message"),
            (message(content="  "), "empty_text"),
        ]
        for raw, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.authorizer.authorize_raw(raw).reason, reason)

    def test_normalize_strips_text_and_preserves_only_stable_metadata(self) -> None:
        normalized = self.authorizer.normalize(message(content="  do work  "))
        self.assertEqual(normalized.message_id, 400)
        self.assertEqual(normalized.guild_id, 100)
        self.assertEqual(normalized.channel_id, 200)
        self.assertEqual(normalized.user_id, 300)
        self.assertEqual(normalized.content, "do work")


class DiscordAdapterAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_message_reaches_handler(self) -> None:
        handler = AsyncMock()
        adapter = DiscordAdapter(settings(), handler)
        await adapter.handle_message(message())
        handler.assert_awaited_once()
        incoming = handler.await_args.args[0]
        self.assertEqual(incoming.message_id, 400)
        self.assertEqual(incoming.content, "next task")

    async def test_rejected_message_never_reaches_handler(self) -> None:
        handler = AsyncMock()
        adapter = DiscordAdapter(settings(), handler)
        await adapter.handle_message(message(user_id=999))
        handler.assert_not_awaited()

    async def test_handler_failure_is_contained(self) -> None:
        handler = AsyncMock(side_effect=RuntimeError("handler failed"))
        adapter = DiscordAdapter(settings(), handler)
        await adapter.handle_message(message())
        handler.assert_awaited_once()

    async def test_start_uses_reconnect_and_clears_client_after_exit(self) -> None:
        client = SimpleNamespace(start=AsyncMock(), is_closed=lambda: True)
        adapter = DiscordAdapter(settings(), AsyncMock())
        with patch.object(adapter, "_build_client", return_value=client):
            await adapter.start()
        client.start.assert_awaited_once_with("test-token", reconnect=True)
        self.assertIsNone(adapter.client)

    async def test_close_closes_active_client(self) -> None:
        client = SimpleNamespace(is_closed=lambda: False, close=AsyncMock())
        adapter = DiscordAdapter(settings(), AsyncMock())
        adapter._client = client
        await adapter.close()
        client.close.assert_awaited_once()

    async def test_send_text_requires_connected_client(self) -> None:
        adapter = DiscordAdapter(settings(), AsyncMock())
        with self.assertRaises(DiscordNotReadyError):
            await adapter.send_text(200, "hello")

    async def test_send_text_uses_cached_channel(self) -> None:
        sent: list[str] = []

        class FakeChannel:
            id = 200
            parent_id = None
            parent = None
            guild = SimpleNamespace(id=100)

            async def send(self, content: str, **kwargs):
                sent.append(content)
                return SimpleNamespace(id=8001)

        channel = FakeChannel()
        client = SimpleNamespace(
            is_closed=lambda: False,
            get_channel=lambda channel_id: channel,
            fetch_channel=AsyncMock(),
        )
        adapter = DiscordAdapter(settings(), AsyncMock())
        adapter._client = client
        message_id = await adapter.send_text(200, " hello ")
        self.assertEqual(message_id, 8001)
        self.assertEqual(sent, ["hello"])
        client.fetch_channel.assert_not_awaited()

    async def test_send_text_mentions_only_configured_users_for_alerts(self) -> None:
        channel = SimpleNamespace(
            id=200,
            parent_id=None,
            parent=None,
            guild=SimpleNamespace(id=100),
            send=AsyncMock(return_value=SimpleNamespace(id=8003)),
        )
        client = SimpleNamespace(
            is_closed=lambda: False,
            get_channel=lambda channel_id: channel,
            fetch_channel=AsyncMock(),
        )
        adapter = DiscordAdapter(settings(), AsyncMock())
        adapter._client = client
        discord_stub = SimpleNamespace(
            AllowedMentions=lambda **kwargs: SimpleNamespace(**kwargs),
        )
        with patch.object(adapter, "_load_discord", return_value=discord_stub):
            message_id = await adapter.send_text(
                200,
                "done @everyone <@&999>",
                mention_allowed_users=True,
            )

        self.assertEqual(message_id, 8003)
        args, kwargs = channel.send.await_args
        self.assertEqual(args, ("<@300> done @everyone <@&999>",))
        mentions = kwargs["allowed_mentions"]
        self.assertTrue(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.replied_user)

    async def test_send_text_fetches_uncached_channel(self) -> None:
        channel = SimpleNamespace(
            id=200,
            parent_id=None,
            parent=None,
            guild=SimpleNamespace(id=100),
            send=AsyncMock(return_value=SimpleNamespace(id=8002)),
        )
        client = SimpleNamespace(
            is_closed=lambda: False,
            get_channel=lambda channel_id: None,
            fetch_channel=AsyncMock(return_value=channel),
        )
        adapter = DiscordAdapter(settings(), AsyncMock())
        adapter._client = client
        message_id = await adapter.send_text(200, "hello", nonce=42)
        self.assertEqual(message_id, 8002)
        client.fetch_channel.assert_awaited_once_with(200)
        channel.send.assert_awaited_once_with("hello", nonce=42)

    async def test_send_text_rejects_unallowed_channel(self) -> None:
        channel = SimpleNamespace(
            id=999,
            parent_id=None,
            parent=None,
            guild=SimpleNamespace(id=100),
            send=AsyncMock(),
        )
        client = SimpleNamespace(
            is_closed=lambda: False,
            get_channel=lambda channel_id: channel,
            fetch_channel=AsyncMock(),
        )
        adapter = DiscordAdapter(settings(), AsyncMock())
        adapter._client = client
        with self.assertRaises(DiscordAdapterError):
            await adapter.send_text(999, "hello")
        channel.send.assert_not_awaited()

    async def test_send_text_rejects_invalid_content(self) -> None:
        adapter = DiscordAdapter(settings(), AsyncMock())
        with self.assertRaises(ValueError):
            await adapter.send_text(200, "")
        with self.assertRaises(ValueError):
            await adapter.send_text(200, "x" * (DISCORD_MESSAGE_LIMIT + 1))

    async def test_create_thread_returns_created_thread_id(self) -> None:
        created_thread = SimpleNamespace(
            id=777,
            parent_id=200,
            parent=None,
            guild=SimpleNamespace(id=100),
        )
        channel = SimpleNamespace(
            id=200,
            parent_id=None,
            parent=None,
            guild=SimpleNamespace(id=100),
            create_thread=AsyncMock(return_value=created_thread),
        )
        client = SimpleNamespace(
            is_closed=lambda: False,
            get_channel=lambda channel_id: channel,
            fetch_channel=AsyncMock(),
        )
        adapter = DiscordAdapter(settings(), AsyncMock())
        adapter._client = client

        thread_id = await adapter.create_thread(200, "project / abc12345")

        self.assertEqual(thread_id, 777)
        channel.create_thread.assert_awaited_once_with(
            name="project / abc12345",
            reason="Codex Dispatch session mapping",
        )

    async def test_create_thread_requires_direct_allowed_parent(self) -> None:
        channel = SimpleNamespace(
            id=201,
            parent_id=200,
            parent=None,
            guild=SimpleNamespace(id=100),
            create_thread=AsyncMock(),
        )
        client = SimpleNamespace(
            is_closed=lambda: False,
            get_channel=lambda channel_id: channel,
            fetch_channel=AsyncMock(),
        )
        adapter = DiscordAdapter(settings(), AsyncMock())
        adapter._client = client

        with self.assertRaises(DiscordAdapterError):
            await adapter.create_thread(201, "project / abc12345")
        channel.create_thread.assert_not_awaited()

    async def test_allowed_application_command_reaches_handler(self) -> None:
        handler = AsyncMock(return_value="status result")
        adapter = DiscordAdapter(settings(), AsyncMock(), handler)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild_id=100,
            user=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=701, parent_id=200, parent=None),
            response=response,
        )
        await adapter.handle_command(interaction, "status")
        handler.assert_awaited_once()
        request = handler.await_args.args[0]
        self.assertEqual(request.name, "status")
        self.assertEqual(request.channel_id, 701)
        response.send_message.assert_awaited_once_with("status result", ephemeral=True)

    async def test_cancel_command_passes_job_id(self) -> None:
        handler = AsyncMock(return_value="cancelled")
        adapter = DiscordAdapter(settings(), AsyncMock(), handler)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild_id=100,
            user=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=701, parent_id=200, parent=None),
            response=response,
        )
        await adapter.handle_command(interaction, "cancel", job_id=42)
        request = handler.await_args.args[0]
        self.assertEqual(request.job_id, 42)

    async def test_mode_command_passes_requested_mode(self) -> None:
        handler = AsyncMock(return_value="mode changed")
        adapter = DiscordAdapter(settings(), AsyncMock(), handler)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild_id=100,
            user=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=701, parent_id=200, parent=None),
            response=response,
        )
        await adapter.handle_command(interaction, "mode", mode="managed")
        request = handler.await_args.args[0]
        self.assertEqual(request.mode, "managed")

    async def test_auto_command_passes_requested_state(self) -> None:
        handler = AsyncMock(return_value="auto")
        adapter = DiscordAdapter(settings(), AsyncMock(), handler)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild_id=100,
            user=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=701, parent_id=200, parent=None),
            response=response,
        )
        await adapter.handle_command(interaction, "auto", auto_state="on")
        request = handler.await_args.args[0]
        self.assertEqual(request.auto_state, "on")

    async def test_unauthorized_application_command_is_rejected(self) -> None:
        handler = AsyncMock(return_value="never")
        adapter = DiscordAdapter(settings(), AsyncMock(), handler)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(
            guild_id=100,
            user=SimpleNamespace(id=999),
            channel=SimpleNamespace(id=701, parent_id=200, parent=None),
            response=response,
        )
        await adapter.handle_command(interaction, "queue")
        handler.assert_not_awaited()
        response.send_message.assert_awaited_once()

    async def test_missing_discord_dependency_has_clear_error(self) -> None:
        adapter = DiscordAdapter(settings(), AsyncMock())
        with patch(
            "codex_dispatch.adapters.discord.importlib.import_module",
            side_effect=ModuleNotFoundError("discord"),
        ):
            with self.assertRaises(DiscordDependencyError):
                adapter._build_client()


if __name__ == "__main__":
    unittest.main()
