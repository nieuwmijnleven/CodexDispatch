#!/usr/bin/env python3
"""Source-checkout wrapper for the Codex Dispatch external notify client."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_dispatch.notify_client import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
