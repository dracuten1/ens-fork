#!/bin/sh
# Focused unit pack for MCP spawn-time tool binding.
set -u
cd "$(dirname "$0")/../.." || exit 1

export PYTHONDONTWRITEBYTECODE=1
unset DATABASE_URL
for var in $(env | sed -n 's/^\(POSTGRES_[^=]*\)=.*/\1/p'); do
    unset "$var"
done

# Python subprocess timeout works on macOS and POSIX without GNU timeout.
.venv/bin/python - <<'PY'
import os
import subprocess
import sys

command = [
    ".venv/bin/python", "-m", "pytest",
    "tests/unit/tools/test_tool_spawn_mcp_binding.py",
    "--tb=short", "-q", "-p", "no:cacheprovider",
]
try:
    result = subprocess.run(command, timeout=110, check=False)
except subprocess.TimeoutExpired:
    print("TIMEOUT: pytest exceeded internal 110s limit", flush=True)
    raise SystemExit(124)
if result.returncode == 0:
    print("PASS: focused MCP spawn binding unit tests", flush=True)
else:
    print(f"FAIL: pytest exited {result.returncode}", flush=True)
    raise SystemExit(1)
PY
