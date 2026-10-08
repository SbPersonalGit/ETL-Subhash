#!/usr/bin/env python3
"""Safely push and promote the current Git branch through an ordered chain."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_TARGETS = ("Puneet_Dev", "BH-Waygate-Migration")


class GitError(RuntimeError):
    """Raised when a Git command fails."""


def git(*args: str, capture: bool = False, check: bool = True) -> str:
    command = ["git", *args]
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise GitError(f"{' '.join(command)} failed ({result.returncode}){suffix}")
    return (result.stdout or "").strip()


def ref_exists(ref: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref], check=False
    )
    return result.returncode == 0


def switch_to_target(branch: str, remote: str) -> None:
    if ref_exists(f"refs/heads/{branch}"):
        git("switch", branch)
    else:
        git("switch", "--track", "-c", branch, f"{remote}/{branch}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Push the current branch, then promote it through an ordered list "
            "of target branches. The default is a dry run."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="fetch, merge, and push (without this flag, only print the plan)",
    )
    parser.add_argument("--remote", default="origin", help="Git remote (default: origin)")
    parser.add_argument(
        "--targets",
        nargs="+",
        default=list(DEFAULT_TARGETS),
        metavar="BRANCH",
        help=(
            "ordered target branches (default: Puneet_Dev BH-Waygate-Migration). "
            "Example: --targets Dev QA Release"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    git("rev-parse", "--is-inside-work-tree", capture=True)
    repository_root = Path(git("rev-parse", "--show-toplevel", capture=True))
    original_branch = git("branch", "--show-current", capture=True)
    if not original_branch:
        raise GitError("Detached HEAD is not supported; switch to a branch first.")
    targets = tuple(dict.fromkeys(str(branch).strip() for branch in args.targets if str(branch).strip()))
    if not targets:
        raise GitError("Provide at least one target branch with --targets.")
    if len(targets) != len(args.targets):
        raise GitError("Target branch names must be non-empty and unique.")
    for branch in (original_branch, *targets):
        git("check-ref-format", "--branch", branch, capture=True)
    if original_branch in targets:
        raise GitError(
            f"Current branch is {original_branch}. Run this from the feature/source "
            "branch and do not repeat it in --targets."
        )

    chain = (original_branch, *targets)
    promotions = tuple(zip(chain, chain[1:]))
    print(f"Repository: {repository_root}")
    print(f"Remote:     {args.remote}")
    print("Promotion plan:")
    print(f"  push {original_branch} -> {args.remote}/{original_branch}")
    for source, target in promotions:
        print(f"  {args.remote}/{source} -> {target} -> {args.remote}/{target}")

    if not args.execute:
        print("Dry run only. Re-run with --execute to fetch, merge, and push.")
        return 0

    if git("status", "--porcelain", "--untracked-files=normal", capture=True):
        raise GitError("Worktree is not clean. Commit or stash changes first.")

    merge_in_progress = False
    try:
        git("fetch", "--prune", args.remote)
        for branch in targets:
            if not ref_exists(f"refs/remotes/{args.remote}/{branch}"):
                raise GitError(
                    f"Remote branch '{args.remote}/{branch}' does not exist."
                )
        git("push", "--set-upstream", args.remote, original_branch)

        for source, target in promotions:
            print(f"\nPromoting {source} -> {target}")
            switch_to_target(target, args.remote)
            git("pull", "--ff-only", args.remote, target)
            merge_in_progress = True
            git("merge", "--no-edit", f"{args.remote}/{source}")
            merge_in_progress = False
            git("push", args.remote, target)

        print("\nAll branch promotions completed successfully.")
        return 0
    finally:
        git_dir = Path(git("rev-parse", "--git-dir", capture=True))
        if not git_dir.is_absolute():
            git_dir = repository_root / git_dir
        if merge_in_progress and (git_dir / "MERGE_HEAD").exists():
            subprocess.run(["git", "merge", "--abort"], check=False)
        current_branch = git("branch", "--show-current", capture=True, check=False)
        if current_branch != original_branch:
            subprocess.run(["git", "switch", original_branch], check=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GitError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
