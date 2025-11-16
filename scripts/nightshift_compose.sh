#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
WORKSPACES_HOST="${NIGHTSHIFT_WORKSPACES_HOST:-$ROOT_DIR/workspaces}"
REPO_HOST="${NIGHTSHIFT_REPO_HOST_PATH:-$ROOT_DIR}"
ROUTER_STATE_HOST="${TRAEFIK_DYNAMIC_HOST_PATH:-$ROOT_DIR/data/router}"
ROUTER_CERTS_HOST="${TRAEFIK_CERTS_HOST_PATH:-$ROUTER_STATE_HOST/certs}"

mkdir -p "${WORKSPACES_HOST}"
mkdir -p "${ROUTER_STATE_HOST}" "${ROUTER_CERTS_HOST}"
export NIGHTSHIFT_WORKSPACES_HOST="${WORKSPACES_HOST}"
export NIGHTSHIFT_REPO_HOST_PATH="${REPO_HOST}"
export TRAEFIK_DYNAMIC_HOST_PATH="${ROUTER_STATE_HOST}"
export TRAEFIK_CERTS_HOST_PATH="${ROUTER_CERTS_HOST}"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required to run this command" >&2
    exit 127
fi

compose() {
    docker compose -f "${ROOT_DIR}/${COMPOSE_FILE}" "$@"
}

if [[ $# -lt 1 ]]; then
    echo "usage: scripts/nightshift_compose.sh <command> [args...]" >&2
    echo "commands: up, down, logs, config, smoke" >&2
    exit 1
fi

COMMAND="$1"
shift || true

case "${COMMAND}" in
    up)
        compose up -d "$@"
        ;;
    down)
        compose down "$@"
        ;;
    logs)
        compose logs -f "$@"
        ;;
    config)
        compose config "$@"
        ;;
    smoke)
        compose config >/dev/null
        compose build backend frontend
        compose run --rm backend ./scripts/docker_backend_selfcheck.sh
        compose run --rm backend python scripts/router_config.py --check-only
        compose run --rm frontend /usr/local/bin/frontend-selfcheck
        ;;
    *)
        echo "unknown command: ${COMMAND}" >&2
        exit 1
        ;;
esac
