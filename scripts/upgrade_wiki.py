#!/usr/bin/env python3
"""Bring a wiki's vendored copies of this skill's files up to date.

A brain vendors things the skill owns: the validators CI runs, the visualiser
page and its builder. Each was copied at some past version that nothing records,
so fixing the skill leaves every connected wiki running the old copy -- silently,
which is the part that costs. `build_backlinks.py --check` wrote to the wiki for
weeks after the fix existed, because the fix was in the skill and CI ran the copy.

What this does NOT do, deliberately:

  - It never touches entity files. Data is the wiki's own; this is code.
  - It never overwrites a file the brain declares as its own. A schema-driven
    brain legitimately has its own validator and rollup builder, and replacing
    those with the generic ones is a regression wearing an upgrade's clothes.
    Declare them in SCHEMA.yml:

        vendored:
          owns: [scripts/validate_wiki.py, scripts/build_rollups.py]

  - It writes no commit. A code-only change is easy to review and belongs in a
    commit of its own, with a human deciding when.

Usage:
    upgrade_wiki.py <repo>            copy over what is stale
    upgrade_wiki.py <repo> --check    report only, exit 1 if anything is stale
    upgrade_wiki.py <repo> --all      include files the brain does not have yet
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent

# What a wiki may vendor, and where it lands. Everything here is owned by the
# skill unless a brain says otherwise.
VENDORED = [
    ("scripts/validate_wiki.py", "scripts/validate_wiki.py"),
    ("scripts/build_rollups.py", "scripts/build_rollups.py"),
    ("scripts/build_backlinks.py", "scripts/build_backlinks.py"),
    ("scripts/find_duplicates.py", "scripts/find_duplicates.py"),
    ("scripts/build_site.py", "scripts/build_site.py"),
    ("site/index.html", "site/index.html"),
    ("site/style.css", "site/style.css"),
    ("site/app.js", "site/app.js"),
]


def owned_by_brain(repo: Path) -> set[str]:
    """Paths the brain has claimed, which an upgrade must leave alone."""
    schema = repo / "SCHEMA.yml"
    if not schema.is_file():
        return set()
    try:
        import yaml
    except ImportError:
        return set()
    try:
        data = yaml.safe_load(schema.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set()
    vendored = data.get("vendored") or {}
    return set(vendored.get("owns") or [])


def survey(repo: Path, include_missing: bool) -> tuple[list, list, list]:
    owned = owned_by_brain(repo)
    stale, current, skipped = [], [], []
    for source_rel, target_rel in VENDORED:
        source, target = SKILL / source_rel, repo / target_rel
        if not source.is_file():
            continue
        if target_rel in owned:
            skipped.append((target_rel, "the brain owns this file"))
            continue
        if not target.exists():
            if include_missing:
                stale.append((target_rel, "missing"))
            else:
                skipped.append((target_rel, "not vendored here"))
            continue
        if filecmp.cmp(source, target, shallow=False):
            current.append(target_rel)
        else:
            stale.append((target_rel, "differs"))
    return stale, current, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", type=Path)
    ap.add_argument("--check", action="store_true", help="report only; exit 1 if stale")
    ap.add_argument("--all", action="store_true",
                    help="also copy files this wiki does not vendor yet")
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not (repo / "CONVENTIONS.md").is_file():
        print(f"{repo} does not look like a wiki (no CONVENTIONS.md)", file=sys.stderr)
        return 2

    stale, current, skipped = survey(repo, include_missing=args.all)

    for rel, why in skipped:
        print(f"  skipped  {rel:32} {why}")
    for rel in current:
        print(f"  current  {rel}")
    for rel, why in stale:
        print(f"  STALE    {rel:32} {why}")

    if not stale:
        print(f"\n{repo.name} is up to date with the skill.")
        return 0

    if args.check:
        print(f"\n{len(stale)} vendored file(s) behind the skill.", file=sys.stderr)
        print(f"run:  python3 {Path(__file__).name} {repo}", file=sys.stderr)
        return 1

    for rel, _ in stale:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL / rel, target)
    print(f"\nupdated {len(stale)} file(s). Nothing is committed — review the diff,")
    print("then commit them on their own, without data changes mixed in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
