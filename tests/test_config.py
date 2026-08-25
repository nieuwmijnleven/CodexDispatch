from __future__ import annotations

from pathlib import Path
import unittest

from codex_dispatch.config import SettingsError, load_settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_valid_for_bootstrap(self) -> None:
        settings = load_settings({})

        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(
            settings.database_path,
            Path("/var/lib/codex-dispatch/codex-dispatch.db"),
        )
        self.assertEqual(settings.allowed_roots, ())
        self.assertIsNone(settings.discord_bot_token)
        self.assertIsNone(settings.discord_control_channel_id)
        self.assertEqual(settings.notify_max_payload_bytes, 262_144)
        self.assertEqual(settings.notify_socket_path.name, "notify.sock")
        self.assertEqual(settings.codex_binary, "codex")
        self.assertEqual(settings.codex_queue_timeout_seconds, 10.0)
        self.assertEqual(settings.codex_capability_timeout_seconds, 5.0)
        self.assertEqual(settings.codex_prompt_max_chars, 8_000)
        self.assertEqual(settings.managed_exec_timeout_seconds, 21_600.0)
        self.assertEqual(settings.managed_output_max_bytes, 262_144)
        self.assertEqual(settings.discord_delivery_retry_attempts, 3)
        self.assertEqual(settings.discord_delivery_retry_base_seconds, 0.5)

    def test_allowed_roots_are_normalized_and_deduplicated(self) -> None:
        settings = load_settings(
            {
                "CODEX_ALLOWED_ROOTS": "/srv/projects:/srv/projects:/opt/work",
            }
        )

        self.assertEqual(
            settings.allowed_roots,
            (Path("/srv/projects"), Path("/opt/work")),
        )

    def test_relative_database_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "absolute path"):
            load_settings({"CODEX_DISPATCH_DB_PATH": "data/codex-dispatch.db"})

    def test_invalid_log_level_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "CODEX_DISPATCH_LOG_LEVEL"):
            load_settings({"CODEX_DISPATCH_LOG_LEVEL": "verbose"})

    def test_discord_id_lists_are_parsed_and_deduplicated(self) -> None:
        settings = load_settings(
            {
                "DISCORD_ALLOWED_GUILD_IDS": "100,200,100",
                "DISCORD_ALLOWED_CHANNEL_IDS": "300",
                "DISCORD_ALLOWED_USER_IDS": "400,500",
            }
        )

        self.assertEqual(settings.discord_allowed_guild_ids, (100, 200))
        self.assertEqual(settings.discord_allowed_channel_ids, (300,))
        self.assertEqual(settings.discord_allowed_user_ids, (400, 500))

    def test_invalid_discord_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(SettingsError, "comma-separated integers"):
            load_settings({"DISCORD_ALLOWED_USER_IDS": "123,not-an-id"})

    def test_notify_socket_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(SettingsError, "absolute path"):
            load_settings({"CODEX_DISPATCH_NOTIFY_SOCKET": "run/notify.sock"})

    def test_notify_max_payload_must_be_positive(self) -> None:
        with self.assertRaisesRegex(SettingsError, "positive integer"):
            load_settings({"CODEX_DISPATCH_NOTIFY_MAX_BYTES": "0"})

    def test_codex_queue_settings_must_be_positive(self) -> None:
        for key in (
            "CODEX_DISPATCH_CODEX_QUEUE_TIMEOUT_SECONDS",
            "CODEX_DISPATCH_CODEX_CAPABILITY_TIMEOUT_SECONDS",
            "CODEX_DISPATCH_CODEX_PROMPT_MAX_CHARS",
            "CODEX_DISPATCH_MANAGED_TIMEOUT_SECONDS",
            "CODEX_DISPATCH_MANAGED_OUTPUT_MAX_BYTES",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                SettingsError, "positive"
            ):
                load_settings({key: "0"})

    def test_codex_binary_whitespace_falls_back_to_codex(self) -> None:
        settings = load_settings({"CODEX_DISPATCH_CODEX_BIN": "   "})
        self.assertEqual(settings.codex_binary, "codex")

    def test_discord_retry_settings_must_be_positive(self) -> None:
        with self.assertRaisesRegex(SettingsError, "positive integer"):
            load_settings({"CODEX_DISPATCH_DISCORD_RETRY_ATTEMPTS": "0"})
        with self.assertRaisesRegex(SettingsError, "positive number"):
            load_settings({"CODEX_DISPATCH_DISCORD_RETRY_BASE_SECONDS": "0"})

    def test_require_notify_requires_allowed_roots(self) -> None:
        settings = load_settings({})

        with self.assertRaisesRegex(SettingsError, "CODEX_ALLOWED_ROOTS"):
            settings.require_notify()

    def test_require_notify_accepts_allowed_roots(self) -> None:
        settings = load_settings({"CODEX_ALLOWED_ROOTS": "/tmp"})

        settings.require_notify()

    def test_require_discord_reports_all_missing_fields(self) -> None:
        settings = load_settings({})

        with self.assertRaises(SettingsError) as context:
            settings.require_discord()

        message = str(context.exception)
        self.assertIn("DISCORD_BOT_TOKEN", message)
        self.assertIn("DISCORD_CONTROL_CHANNEL_ID", message)
        self.assertIn("DISCORD_ALLOWED_GUILD_IDS", message)
        self.assertIn("DISCORD_ALLOWED_CHANNEL_IDS", message)
        self.assertIn("DISCORD_ALLOWED_USER_IDS", message)

    def test_require_discord_accepts_complete_configuration(self) -> None:
        settings = load_settings(
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "DISCORD_CONTROL_CHANNEL_ID": "200",
                "DISCORD_ALLOWED_GUILD_IDS": "100",
                "DISCORD_ALLOWED_CHANNEL_IDS": "200",
                "DISCORD_ALLOWED_USER_IDS": "300",
            }
        )

        settings.require_discord()

    def test_control_channel_must_be_in_channel_allowlist(self) -> None:
        settings = load_settings(
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "DISCORD_CONTROL_CHANNEL_ID": "201",
                "DISCORD_ALLOWED_GUILD_IDS": "100",
                "DISCORD_ALLOWED_CHANNEL_IDS": "200",
                "DISCORD_ALLOWED_USER_IDS": "300",
            }
        )

        with self.assertRaisesRegex(SettingsError, "must also appear"):
            settings.require_discord()


if __name__ == "__main__":
    unittest.main()
