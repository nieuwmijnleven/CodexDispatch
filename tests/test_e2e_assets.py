from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_dispatch.database import Database


ROOT = Path(__file__).resolve().parents[1]


class EndToEndAssetTests(unittest.TestCase):
    def test_e2e_host_acceptance_help_loads(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/e2e-host-acceptance.py"), "--help"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("PHASE 12", result.stdout)
        self.assertIn("--capture-baseline", result.stdout)
        self.assertIn("--verify-baseline", result.stdout)

    def test_empty_state_baseline_round_trip_with_zero_minimums(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database_path = root / "state" / "codex-dispatch.db"
            database = Database(database_path)
            database.open()
            database.close()
            env_file = root / "codex-dispatch.env"
            env_file.write_text(
                f"CODEX_DISPATCH_DB_PATH={database_path}\n",
                encoding="utf-8",
            )
            baseline = root / "baseline.json"
            common = [
                sys.executable,
                str(ROOT / "scripts/e2e-host-acceptance.py"),
                "--env-file",
                str(env_file),
                "--minimum-sessions",
                "0",
                "--minimum-workspaces",
                "0",
                "--minimum-live",
                "0",
                "--minimum-managed",
                "0",
                "--minimum-completed-jobs",
                "0",
                "--minimum-sent-deliveries",
                "0",
                "--skip-systemd",
            ]
            captured = subprocess.run(
                [*common, "--capture-baseline", str(baseline)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            verified = subprocess.run(
                [*common, "--verify-baseline", str(baseline)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertIn("PHASE 12 acceptance: PASS", captured.stdout)
        self.assertIn("reboot_baseline=PASS", verified.stdout)


if __name__ == "__main__":
    unittest.main()
