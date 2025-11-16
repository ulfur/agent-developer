#!/usr/bin/env bash
set -euo pipefail

REPO_PATH="${NIGHTSHIFT_REPO_PATH:-/workspaces/nightshift}"
if [[ ! -d "${REPO_PATH}" ]]; then
  echo "[nightshift] repository not found at ${REPO_PATH}" >&2
  exit 1
fi
if [[ ! -d "${REPO_PATH}/backend" ]]; then
  echo "[nightshift] backend sources missing under ${REPO_PATH}" >&2
  exit 1
fi
cd "${REPO_PATH}"
exec "$@"
