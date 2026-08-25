from __future__ import annotations

import os
from pathlib import Path
import pwd
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SystemdAssetTests(unittest.TestCase):
    def test_service_template_has_required_lifecycle_directives(self) -> None:
        text = (ROOT / "systemd/codex-dispatch.service.in").read_text(encoding="utf-8")
        required = (
            "User=@SERVICE_USER@",
            "Group=@SERVICE_GROUP@",
            "WorkingDirectory=@PROJECT_DIR@",
            "EnvironmentFile=/etc/codex-dispatch/codex-dispatch.env",
            "EnvironmentFile=/etc/codex-dispatch/notify.env",
            "EnvironmentFile=/etc/codex-dispatch/secret.env",
            "ExecStart=@VENV_PYTHON@ -m codex_dispatch",
            "Restart=on-failure",
            "KillSignal=SIGTERM",
            "KillMode=mixed",
            "RuntimeDirectory=codex-dispatch",
            "RuntimeDirectoryMode=0700",
            "StateDirectory=codex-dispatch",
            "StateDirectoryMode=0700",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=full",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
            "ProtectHostname=true",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "WantedBy=multi-user.target",
        )
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, text)
        self.assertNotIn("User=root", text)

    def test_systemd_environment_templates_split_secrets_from_runtime_settings(self) -> None:
        config_text = (ROOT / "systemd/codex-dispatch.env.example").read_text(encoding="utf-8")
        notify_text = (ROOT / "systemd/codex-dispatch-notify.env.example").read_text(encoding="utf-8")
        secret_text = (ROOT / "systemd/codex-dispatch.secret.env.example").read_text(encoding="utf-8")
        self.assertIn("CODEX_DISPATCH_DB_PATH=/var/lib/codex-dispatch/codex-dispatch.db", config_text)
        self.assertIn("CODEX_ALLOWED_ROOTS=", config_text)
        self.assertNotIn("CODEX_ALLOWED_ROOTS=@SERVICE_HOME@", config_text)
        self.assertIn("CODEX_DISPATCH_CODEX_BIN=@CODEX_BIN@", config_text)
        self.assertNotIn("DISCORD_BOT_TOKEN", config_text)
        self.assertNotIn("CODEX_DISPATCH_NOTIFY_SOCKET=", config_text)
        self.assertIn("CODEX_DISPATCH_NOTIFY_SOCKET=/run/codex-dispatch/notify.sock", notify_text)
        self.assertNotIn("DISCORD_BOT_TOKEN", notify_text)
        self.assertIn("DISCORD_BOT_TOKEN=", secret_text)
        self.assertNotIn("CODEX_ALLOWED_ROOTS", secret_text)

    def test_installer_enforces_config_notify_and_secret_file_permissions(self) -> None:
        text = (ROOT / "scripts/install-service.sh").read_text(encoding="utf-8")
        self.assertIn('install -m 0640 -o root -g "$SERVICE_GROUP" "$tmp_env" "$ENV_FILE"', text)
        self.assertIn('install -m 0640 -o root -g "$SERVICE_GROUP" "$tmp_notify_env" "$NOTIFY_ENV_FILE"', text)
        self.assertIn('install -m 0600 -o root -g root "$tmp_secret_env" "$SECRET_ENV_FILE"', text)
        self.assertIn('chmod 0640 "$ENV_FILE"', text)
        self.assertIn('chmod 0640 "$NOTIFY_ENV_FILE"', text)
        self.assertIn('chmod 0600 "$SECRET_ENV_FILE"', text)

    def test_shell_scripts_parse_with_bash(self) -> None:
        for relative in (
            "scripts/install-service.sh",
            "scripts/upgrade-service.sh",
            "scripts/uninstall-service.sh",
        ):
            with self.subTest(script=relative):
                subprocess.run(
                    ["bash", "-n", str(ROOT / relative)],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

    def test_installer_dry_run_requires_no_root_mutation(self) -> None:
        current = pwd.getpwuid(os.getuid()).pw_name
        service_user = current if current != "root" else "nobody"
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/install-service.sh"),
                "--dry-run",
                "--user",
                service_user,
                "--python",
                sys.executable,
                "--codex-bin",
                "/bin/true",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("dry-run: PASS", result.stdout)
        self.assertIn(f"user:        {service_user}", result.stdout)
        self.assertIn("codex:       /bin/true", result.stdout)


if __name__ == "__main__":
    unittest.main()
