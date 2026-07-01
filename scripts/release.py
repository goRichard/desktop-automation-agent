"""Create the next semantic-version patch tag and optionally push it.

The script intentionally refuses dirty worktrees, non-main branches, and empty
releases. GitHub Actions turns the pushed tag into a GitHub Release.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_RE = re.compile(r'(?m)^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"\s*$')


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def next_patch_version() -> tuple[str, str]:
    content = PYPROJECT.read_text(encoding="utf-8")
    match = VERSION_RE.search(content)
    if not match:
        raise RuntimeError("Unable to find a semantic project version in pyproject.toml")
    major, minor, patch = (int(value) for value in match.groups())
    current = f"{major}.{minor}.{patch}"
    next_version = f"{major}.{minor}.{patch + 1}"
    return current, next_version


def latest_version_tag() -> str | None:
    tags = git("tag", "--list", "v[0-9]*", "--sort=-version:refname")
    return tags.splitlines()[0] if tags else None


def ensure_releasable() -> None:
    if git("branch", "--show-current") != "main":
        raise RuntimeError("Releases must be created from the main branch")
    if git("status", "--porcelain"):
        raise RuntimeError("Working tree must be clean before creating a release")

    latest = latest_version_tag()
    if latest and git("rev-list", f"{latest}..HEAD", "--count") == "0":
        raise RuntimeError(f"No commits have been added since {latest}")


def update_version(current: str, next_version: str) -> None:
    content = PYPROJECT.read_text(encoding="utf-8")
    updated, count = VERSION_RE.subn(f'version = "{next_version}"', content, count=1)
    if count != 1:
        raise RuntimeError(f"Unable to replace project version {current}")
    PYPROJECT.write_text(updated, encoding="utf-8")


def create_release(push: bool) -> str:
    ensure_releasable()
    current, next_version = next_patch_version()
    tag = f"v{next_version}"
    if git("tag", "--list", tag):
        raise RuntimeError(f"Tag already exists: {tag}")

    update_version(current, next_version)
    git("add", "pyproject.toml")
    git("commit", "-m", f"release: {tag}")
    git("tag", "-a", tag, "-m", f"Release {tag}")

    if push:
        git("push", "--atomic", "origin", "main", tag)

    return tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--push",
        action="store_true",
        help="push the release commit and tag to origin",
    )
    args = parser.parse_args()
    tag = create_release(push=args.push)
    print(f"Created {tag}{' and pushed it' if args.push else ''}")


if __name__ == "__main__":
    main()
