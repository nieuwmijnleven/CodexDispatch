#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

if python3 -c 'import pytest' >/dev/null 2>&1; then
  exec python3 -m pytest "$@"
fi

printf '%s\n' 'warning: pytest is not installed; running unittest fallback' >&2
exec python3 -m unittest discover -s tests -p 'test_*.py' -v
