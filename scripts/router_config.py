#!/usr/bin/env python3
"""Render the Traefik router config derived from the environment registry."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from environments import EnvironmentStore  # type: ignore  # pylint: disable=wrong-import-position
from router_config import RouterConfigBuilder  # type: ignore  # pylint: disable=wrong-import-position

DEFAULT_ENV_REGISTRY = REPO_ROOT / "data" / "environments.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "router" / "environments.yml"
DEFAULT_CERTS_DIR = REPO_ROOT / "data" / "router" / "certs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the dynamic Traefik config backed by the environment registry."
    )
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_ENV_REGISTRY),
        help="Path to data/environments.json (default: %(default)s).",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Destination for the generated YAML (default: %(default)s).",
    )
    parser.add_argument(
        "--certs-dir",
        default=str(DEFAULT_CERTS_DIR),
        help="Directory containing TLS certificates (default: %(default)s).",
    )
    parser.add_argument(
        "--certs-mount-path",
        default="/etc/traefik/certs",
        help="Container path where certs are mounted (default: %(default)s).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the rendered YAML to stdout instead of writing the file.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with a non-zero status when router warnings are encountered.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate and report warnings without touching the output file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry_path = Path(args.registry)
    output_path = Path(args.output)
    certs_dir = Path(args.certs_dir)
    store = EnvironmentStore(registry_path)
    builder = RouterConfigBuilder(certs_dir=certs_dir, certs_mount_path=args.certs_mount_path)
    config, warnings = builder.build(store.list_environments())
    for message in warnings:
        print(f"warning: {message}", file=sys.stderr)
    serialized = builder_yaml(config)
    if args.stdout:
        sys.stdout.write(serialized)
    if not args.check_only:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized, encoding="utf-8")
    if warnings and args.strict:
        return 2
    return 0


def builder_yaml(payload: dict) -> str:
    """Lazy import of yaml to match backend dependencies."""
    import yaml  # pylint: disable=import-outside-toplevel

    return yaml.safe_dump(payload, sort_keys=False)


if __name__ == "__main__":
    raise SystemExit(main())
