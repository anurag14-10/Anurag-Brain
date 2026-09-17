#!/usr/bin/env python3
"""One report of the wiki's open human judgment work. Read-only.

The wiki's largest known gaps are not tooling problems — they are queues of
decisions only a person can make, and nothing surfaces them today, so they
compete with everything else for attention and lose. This script makes each
queue visible, ordered so a person can work it top down:

  1. Disclosure backlog     projects with no named clearance — the single
                            biggest unlock for sales and marketing
  2. Stale affiliations     people whose every affiliation reads `current`,
                            oldest update first — the wiki's largest known
                            inaccuracy is that v2 could not say otherwise
  3. Duplicate worklist     probable same-real-world-thing pairs, via
                            find_duplicates.py, plus edges whose note says
                            "duplicate" — confirm or merge, once each
  4. external_refs coverage how much of the wiki is pinned to a system of
                            record by id, per folder per store — what is
                            not pinned still gets matched by name

Usage:
    quality_report.py <wiki-root> [--out FILE]

Requires PyYAML. Never writes to the wiki; --out writes the report file only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    print("quality_report.py needs PyYAML (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def entities(root: Path, folder: str) -> dict[str, dict]:
    directory = root / folder
    if not directory.is_dir():
        return {}
    return {p.stem: frontmatter(p) for p in sorted(directory.glob("*.md"))
            if p.name.lower() != "readme.md"}


# ------------------------------------------------------------- 1. disclosure


def disclosure_backlog(projects: dict[str, dict]) -> list[str]:
    lines = []
    rows = []
    for slug, meta in projects.items():
        disclosure = meta.get("disclosure") or {}
        if not isinstance(disclosure, dict):
            disclosure = {}
        if disclosure.get("cleared_by"):
            continue
        orgs = []
        for edge in meta.get("organizations") or []:
            if isinstance(edge, dict) and edge.get("slug"):
                orgs.append(edge["slug"])
        level = disclosure.get("level") or "(no level)"
        rows.append((", ".join(orgs) or "(no organization)", slug, level))
    # Grouped by client so one call clears several projects at once.
    for org, slug, level in sorted(rows):
        lines.append(f"- `{slug}` — {org} — level: {level}")
    return lines


# ------------------------------------------------------- 2. stale affiliation


def stale_affiliations(people: dict[str, dict]) -> list[str]:
    rows = []
    for slug, meta in people.items():
        affs = [a for a in (meta.get("affiliations") or []) if isinstance(a, dict)]
        if not affs:
            continue
        current = [a for a in affs if a.get("status") == "current"]
        if len(current) != len(affs):
            continue  # somebody has already judged this person's history
        rows.append((str(meta.get("last_updated") or ""), len(affs), slug))
    rows.sort()
    return [f"- `{slug}` — {n} affiliation(s), all `current`, last touched {upd or 'never'}"
            for upd, n, slug in rows]


# --------------------------------------------------------------- 3. duplicates


def duplicate_worklist(root: Path, organizations: dict[str, dict]) -> list[str]:
    lines = []
    seen: set[tuple[str, str]] = set()
    for slug, meta in organizations.items():
        for edge in meta.get("related_organizations") or []:
            if not isinstance(edge, dict):
                continue
            note = str(edge.get("note") or edge.get("role") or "").lower()
            other = edge.get("slug")
            if other and ("duplicate" in note or "same " in note):
                pair = tuple(sorted((slug, other)))
                if pair not in seen:
                    seen.add(pair)
                    lines.append(f"- `{pair[0]}` ↔ `{pair[1]}` — noted: {note}")

    finder = Path(__file__).resolve().parent / "find_duplicates.py"
    try:
        result = subprocess.run(
            [sys.executable, str(finder), str(root), "--json"],
            capture_output=True, text=True, timeout=120,
        )
        for hit in json.loads(result.stdout or "[]"):
            a, b = hit.get("left"), hit.get("right")
            if not a or not b:
                continue
            pair = tuple(sorted((str(a), str(b))))
            if pair not in seen:
                seen.add(pair)
                folder = hit.get("folder", "")
                ack = " (already acknowledged on an edge)" if hit.get("acknowledged") else ""
                why = hit.get("reason") or f"score {hit.get('score')}"
                lines.append(f"- `{pair[0]}` ↔ `{pair[1]}` [{folder}] — {why}{ack}")
    except Exception:
        lines.append("- (find_duplicates.py could not run here — pairs above "
                     "are only the ones noted on edges)")
    return lines


# ----------------------------------------------------------------- 4. coverage


def refs_coverage(root: Path, folders: list[str]) -> list[str]:
    lines = []
    for folder in folders:
        data = entities(root, folder)
        if not data:
            continue
        # Entities pinned, not references held. One entity may legitimately
        # carry several ids in the same store -- a brand standard pointing at
        # four Drive files is one entity pinned, not four -- and counting refs
        # reported `4/1` against a folder holding a single file. The heading
        # asks how much of the wiki is pinned, which is a question about
        # entities.
        by_store: dict[str, set[str]] = {}
        for slug, meta in data.items():
            for ref in meta.get("external_refs") or []:
                if isinstance(ref, dict) and ref.get("store") and ref.get("id"):
                    by_store.setdefault(str(ref["store"]), set()).add(slug)
        total = len(data)
        stores = ", ".join(f"{store}: {len(slugs)}/{total}"
                           for store, slugs in sorted(by_store.items())) \
            or f"none of {total} pinned"
        lines.append(f"- {folder}: {stores}")
    return lines


# ----------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root")
    parser.add_argument("--out", default=None, help="write the report here instead of stdout")
    args = parser.parse_args()

    root = Path(args.root)
    if not (root / "CONVENTIONS.md").exists():
        print(f"{root} does not look like a wiki (no CONVENTIONS.md)", file=sys.stderr)
        return 2

    projects = entities(root, "projects")
    people = entities(root, "people")
    organizations = entities(root, "organizations")

    backlog = disclosure_backlog(projects)
    stale = stale_affiliations(people)
    dupes = duplicate_worklist(root, organizations)
    folders = [d.name for d in sorted(root.iterdir())
               if d.is_dir() and not d.name.startswith(".")
               and any(d.glob("*.md"))
               # staging and proposals are not entities; reports is where this
               # script's own output lands in CI -- a report that counts itself
               # grows a self-referential line on every run after the first.
               and d.name not in ("staging", "proposals", "reports")]
    coverage = refs_coverage(root, folders)

    report = "\n".join([
        f"# Wiki quality report — {date.today().isoformat()}",
        "",
        "Read-only snapshot of the queues that need a person. Work top down;",
        "every line is one decision.",
        "",
        f"## 1. Disclosure backlog ({len(backlog)} projects, grouped by client)",
        "",
        "A project with no named clearance cannot be cited in a bid or a case",
        "study. `cleared_by` is the field that closes a line.",
        "",
        *(backlog or ["(none — every project has a named clearance)"]),
        "",
        f"## 2. Affiliations never judged ({len(stale)} people, oldest first)",
        "",
        "Every affiliation on these people reads `current` — the v2 default,",
        "not a decision. Confirming one costs seconds; the list shrinks as",
        "people are touched.",
        "",
        *(stale or ["(none)"]),
        "",
        f"## 3. Duplicate worklist ({len(dupes)} pairs)",
        "",
        "Confirm-or-merge, once per pair. A merged pair stops seeding new",
        "inconsistencies; a confirmed distinct pair should say so on the edge.",
        "",
        *(dupes or ["(none found)"]),
        "",
        "## 4. external_refs coverage",
        "",
        "What is pinned to a system of record by id. Anything unpinned is",
        "still matched by name on every ingest.",
        "",
        *coverage,
        "",
    ])

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
