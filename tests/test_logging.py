from __future__ import annotations

from contextlib import redirect_stderr
import io
import logging
import unittest

from codex_dispatch.logging import configure_logging


class LoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        logging.basicConfig(level=logging.WARNING, force=True)

    def test_configure_logging_sets_root_level(self) -> None:
        configure_logging("DEBUG")
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_unknown_level_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown logging level"):
            configure_logging("TRACE")

    def test_runtime_secret_and_authorization_header_are_redacted(self) -> None:
        token = "super-secret-discord-token"
        stream = io.StringIO()
        with redirect_stderr(stream):
            configure_logging("INFO", secrets=(token,))
            logging.getLogger("security-test").error(
                "token=%s DISCORD_BOT_TOKEN=%s Authorization: Bot abc.def.ghi",
                token,
                token,
            )
        rendered = stream.getvalue()
        self.assertNotIn(token, rendered)
        self.assertNotIn("abc.def.ghi", rendered)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
