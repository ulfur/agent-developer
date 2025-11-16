#!/usr/bin/env bash
# Helper to run AWS CDK commands from the repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CDK_DIR="${REPO_ROOT}/cdk"

if [[ ! -d "$CDK_DIR" ]]; then
  echo "cdk/ directory is missing. Did you check out the latest code?" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  cat <<'USAGE' >&2
Usage: scripts/cdk.sh <command> [options]
Example: scripts/cdk.sh synth -c instance=example-dev

Pass the target instance via '-c instance=<name>' or set NIGHTSHIFT_INSTANCE
before running synth/deploy/diff so the stack picks up the correct config file.
USAGE
  exit 1
fi

cd "$CDK_DIR"
CDK_BIN="${CDK_BIN:-cdk}"
if command -v "$CDK_BIN" >/dev/null 2>&1; then
  exec "$CDK_BIN" "$@"
elif command -v npx >/dev/null 2>&1; then
  exec npx cdk "$@"
else
  echo "Neither $CDK_BIN nor npx is available. Install the AWS CDK CLI first." >&2
  exit 1
fi
