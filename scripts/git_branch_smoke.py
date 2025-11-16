#!/usr/bin/env python3
"""Dry-run helper that validates the prompt branching workflow."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from git_branching import GitBranchError, PromptBranchDiscipline  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-id", default=uuid.uuid4().hex, help="Prompt ID to encode into the branch name.")
    parser.add_argument(
        "--prompt",
        default="Nightshift git discipline smoke test",
        help="Prompt title used to derive the branch slug.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually create and delete the branch instead of only logging the git commands.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = logging.getLogger("git_branch_smoke")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    discipline = PromptBranchDiscipline(
        REPO_ROOT,
        logger=logger,
        dry_run_mutations=not args.execute,
    )
    try:
        session = discipline.begin_run(args.prompt_id, args.prompt)
    except GitBranchError as exc:
        logger.error("Git workflow check failed: %s", exc)
        return 1
    notes: list[str] = []
    if session:
        notes.extend(session.notes)
        logger.info(
            "%s branch %s",
            "Created" if args.execute else "Would create",
            session.branch_name,
        )
    else:
        logger.info("Branch discipline disabled; nothing to test.")
    try:
        cleanup_result = discipline.finalize_run(session)
        if cleanup_result:
            notes.extend(cleanup_result.notes)
            if cleanup_result.notes:
                logger.info("%s", cleanup_result.notes[-1])
    except GitBranchError as exc:
        logger.error("Cleanup failed: %s", exc)
        return 2
    if notes:
        print("Git workflow notes:")
        for note in notes:
            print(f"- {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
