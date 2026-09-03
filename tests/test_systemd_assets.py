from __future__ import annotations

import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
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
            "Restart=always",
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

    def test_installer_refreshes_selected_codex_binary_in_existing_main_env(self) -> None:
        text = (ROOT / "scripts/install-service.sh").read_text(encoding="utf-8")
        self.assertIn('replace_main_env_key CODEX_DISPATCH_CODEX_BIN "$CODEX_BIN"', text)
        self.assertIn('Updated CODEX_DISPATCH_CODEX_BIN in $ENV_FILE', text)

    def test_installer_repairs_partial_venv_without_pip_launcher(self) -> None:
        text = (ROOT / "scripts/install-service.sh").read_text(encoding="utf-8")
        self.assertIn('"$VENV_PYTHON" -m pip --version', text)
        self.assertIn('"$VENV_PYTHON" -m ensurepip --upgrade', text)
        self.assertIn('"$VENV_PYTHON" -m pip install -e "$PROJECT_DIR"', text)

    def test_installer_fails_early_when_service_user_cannot_write_checkout(self) -> None:
        text = (ROOT / "scripts/install-service.sh").read_text(encoding="utf-8")
        self.assertIn('runuser -u "$SERVICE_USER" -- test -w "$PROJECT_DIR/src"', text)
        self.assertIn('cannot write $PROJECT_DIR/src', text)
        self.assertIn('chown -R $SERVICE_USER:$SERVICE_GROUP $PROJECT_DIR', text)
        self.assertNotIn('"$VENV_DIR/bin/pip" install -e "$PROJECT_DIR"', text)

    def test_upgrade_restores_previously_active_service_on_install_failure(self) -> None:
        text = (ROOT / "scripts/upgrade-service.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ "$was_active" -eq 1 ]]; then', text)
        self.assertIn('if systemctl start "$SERVICE_NAME"; then', text)
        self.assertIn("previously active service was restored", text)
        self.assertNotIn("upgrade failed; service remains stopped", text)

    def test_shell_scripts_parse_with_bash(self) -> None:
        for relative in (
            "scripts/install-service.sh",
            "scripts/configure-service-from-env.sh",
            "scripts/configure-codex-notify.sh",
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

    def test_env_importer_is_allowlisted_and_does_not_source_secret_file(self) -> None:
        text = (ROOT / "scripts/configure-service-from-env.sh").read_text(encoding="utf-8")
        for key in (
            "CODEX_ALLOWED_ROOTS",
            "DISCORD_CONTROL_CHANNEL_ID",
            "DISCORD_ALLOWED_GUILD_IDS",
            "DISCORD_ALLOWED_CHANNEL_IDS",
            "DISCORD_ALLOWED_USER_IDS",
            "DISCORD_BOT_TOKEN",
        ):
            self.assertIn(key, text)
        self.assertNotIn("source \"$SOURCE_ENV\"", text)
        self.assertNotIn(". \"$SOURCE_ENV\"", text)
        self.assertIn('install -m 0600 -o root -g root "$secret_tmp" "$SECRET_ENV"', text)
        self.assertIn("No secret values were printed.", text)

    def test_codex_notify_configurator_is_idempotent_and_top_level(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("notify configurator intentionally rejects root")
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('model = "test"\n\n[projects."/tmp"]\ntrust_level = "trusted"\n', encoding="utf-8")
            env = dict(os.environ)
            env["CODEX_CONFIG_FILE"] = str(config)
            command = ["bash", str(ROOT / "scripts/configure-codex-notify.sh")]
            first = subprocess.run(command, env=env, check=True, capture_output=True, text=True)
            second = subprocess.run(command, env=env, check=True, capture_output=True, text=True)
            text = config.read_text(encoding="utf-8")
            notify_pos = text.index("notify = [")
            table_pos = text.index('[projects."/tmp"]')
            self.assertLess(notify_pos, table_pos)
            self.assertEqual(text.count("notify = ["), 1)
            self.assertIn("Codex notify configuration: PASS", first.stdout)
            self.assertIn("already configured", second.stdout)

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
