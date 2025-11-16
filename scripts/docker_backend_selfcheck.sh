#!/usr/bin/env bash
set -euo pipefail
REPO_PATH="${NIGHTSHIFT_REPO_PATH:-/workspaces/nightshift}"
cd "${REPO_PATH}"
python -m compileall backend scope_guard.py git_branching.py >/dev/null
python - <<'PY'
import importlib
import backend.server  # noqa: F401
print("backend import ok")
PY
