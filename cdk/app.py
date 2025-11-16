#!/usr/bin/env python3
"""
Entry point for the Nightshift CDK app.

Loads a per-instance configuration file (cdk/instances/<name>.yml) that defines
the AWS account, region, and any stack parameters, then synthesizes the
Nightshift infrastructure stack. Operators set the instance name via CDK
context (`-c instance=<name>`) or NIGHTSHIFT_INSTANCE env var.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import aws_cdk as cdk

from config import InstanceConfig, load_instance_config
from nightshift_stack import NightshiftStack


def _resolve_instance_name(app: cdk.App) -> str:
    """Resolve the target instance name from context or env vars."""
    instance_name = (
        app.node.try_get_context("instance")
        or os.environ.get("NIGHTSHIFT_INSTANCE")
        or ""
    ).strip()
    if not instance_name:
        raise SystemExit(
            "Missing instance identifier. Pass `-c instance=<name>` or set "
            "NIGHTSHIFT_INSTANCE before running cdk synth/deploy."
        )
    return instance_name


def _synth(app: cdk.App, config: InstanceConfig) -> None:
    """Instantiate the stack and synthesize the Cloud Assembly."""
    stack_id = f"Nightshift-{config.instance_slug}"
    stack = NightshiftStack(
        scope=app,
        construct_id=stack_id,
        config=config,
        env=config.aws_env,
        description="Nightshift infrastructure (Phase 1.1 bootstrap)",
    )
    for key, value in config.tags.items():
        cdk.Tags.of(stack).add(key, value)
    cdk.Tags.of(stack).add("nightshift:instance", config.instance_name)
    app.synth()


def main(argv: list[str]) -> int:
    app = cdk.App()
    instance_name = _resolve_instance_name(app)
    config = load_instance_config(instance_name)
    _synth(app, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
