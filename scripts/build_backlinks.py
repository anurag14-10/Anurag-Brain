#!/usr/bin/env python3
"""Write a navigable link section into every entity page.

The frontmatter graph is deliberately one-directional: a project names its
organizations, and organizations do not name their projects, so two files can
never disagree about the same relationship. That is right for the data.

It is wrong for reading. An organization page with no link to its engagements
is a dead end, in Obsidian and on GitHub alike -- and the connections between
pages are a large part of what a wiki is for.

So the links are *generated* from the graph rather than authored into it. There
is no duplicate state to drift, because nobody maintains this by hand: the
bookkeeping is the machine's job, which is the whole argument for keeping a
wiki this way.

Links are relative markdown (`../people/x.md`) rather than `[[wikilinks]]`,
because that form resolves in Obsidian *and* renders on GitHub. Wikilinks would
break the latter.

Usage:
    build_backlinks.py <repo>            write the sections
    build_backlinks.py <repo> --check    exit 1 if any are out of date
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    print("needs PyYAML:  pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

BEGIN = "<!-- BEGIN GENERATED LINKS -->"
END = "<!-- END GENERATED LINKS -->"
FOLDERS = ("organizations", "people", "projects", "products")


def load(repo: Path) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for folder in FOLDERS:
        entries = {}
        directory = repo / folder
        if directory.is_dir():
            for path in sorted(directory.glob("*.md")):
                text = path.read_text(encoding="utf-8")
                data = {}
                if text.startswith("---"):
                    try:
                        data = yaml.safe_load(text.split("---", 2)[1]) or {}
                    except yaml.YAMLError:
                        data = {}
                entries[path.stem] = data if isinstance(data, dict) else {}
        out[folder] = entries
    return out


def name_of(folder: str, slug: str, wiki) -> str:
    return (wiki[folder].get(slug) or {}).get("name") or slug.replace("-", " ")


def ref(folder: str, slug: str, wiki, note: str = "") -> str:
    if slug not in wiki[folder]:
        return ""
    label = name_of(folder, slug, wiki)
    return f"- [{label}](../{folder}/{slug}.md)" + (f" — {note}" if note else "")


def slugs(value) -> list[tuple[str, str]]:
    """(slug, note) pairs from a relationship field of any supported shape."""
    out = []
    for item in value or []:
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), ""))
        elif isinstance(item, dict):
            slug = item.get("slug") or item.get("organization")
            if isinstance(slug, str) and slug.strip():
                out.append((slug.strip(), str(item.get("role") or "")))
    return out


def build_index(wiki):
    """Reverse edges, so each entity knows what points at it."""
    proj_orgs = defaultdict(list)      # org  -> [(project, role)]
    proj_people = defaultdict(list)    # person -> [(project, role)]
    proj_products = defaultdict(list)  # product -> [project]
    org_people = defaultdict(list)     # org -> [(person, role)]

    for slug, data in wiki["projects"].items():
        for org, role in slugs(data.get("organizations")):
            proj_orgs[org].append((slug, role))
        for person, role in slugs(data.get("people")):
            proj_people[person].append((slug, role))
        for item, _ in slugs(data.get("products_leveraged")) + slugs(data.get("reusable_components")):
            proj_products[item].append(slug)

    for slug, data in wiki["people"].items():
        for org, role in slugs(data.get("affiliations")):
            org_people[org].append((slug, role))

    for slug, data in wiki["products"].items():
        built = data.get("built_for")
        if isinstance(built, str) and built.strip():
            proj_orgs.setdefault(built.strip(), [])
    return proj_orgs, proj_people, proj_products, org_people


def section(title: str, rows: list[str]) -> list[str]:
    rows = [r for r in rows if r]
    return [f"\n**{title}**\n", *rows] if rows else []


def render(folder: str, slug: str, wiki, index) -> str:
    proj_orgs, proj_people, proj_products, org_people = index
    data = wiki[folder][slug]
    out: list[str] = []

    if folder == "organizations":
        out += section("Engagements", [
            ref("projects", p, wiki, role) for p, role in sorted(proj_orgs.get(slug, []))
        ])
        out += section("People", [
            ref("people", p, wiki, role) for p, role in sorted(org_people.get(slug, []))
        ])
        out += section("Related organizations", [
            ref("organizations", o, wiki, n) for o, n in
            sorted((r.get("slug"), r.get("relationship", "")) for r in (data.get("related_organizations") or [])
                   if isinstance(r, dict) and r.get("slug"))
        ])
        out += section("Products built for this organization", [
            ref("products", p, wiki) for p, d in sorted(wiki["products"].items())
            if d.get("built_for") == slug
        ])

    elif folder == "people":
        out += section("Organizations", [
            ref("organizations", o, wiki, role) for o, role in slugs(data.get("affiliations"))
        ])
        out += section("Engagements", [
            ref("projects", p, wiki, role) for p, role in sorted(proj_people.get(slug, []))
        ])

    elif folder == "projects":
        out += section("Organizations", [
            ref("organizations", o, wiki, role) for o, role in slugs(data.get("organizations"))
        ])
        out += section("People", [
            ref("people", p, wiki, role) for p, role in slugs(data.get("people"))
        ])
        out += section("Products", [
            ref("products", p, wiki) for p, _ in
            slugs(data.get("products_leveraged")) + slugs(data.get("reusable_components"))
        ])

    elif folder == "products":
        built = data.get("built_for")
        out += section("Built for", [ref("organizations", built, wiki)] if isinstance(built, str) else [])
        out += section("Used in", [
            ref("projects", p, wiki) for p in sorted(set(proj_products.get(slug, [])))
        ])

    if not out:
        return ""
    return "\n".join([
        "", "---", "", "## Related", "",
        "<!-- Generated by scripts/build_backlinks.py — edits between the markers will be overwritten. -->",
        BEGIN, *out, "", END, "",
    ])


def merge(text: str, block: str) -> str:
    """Return what the file should contain, without touching the file.

    Kept separate from `apply` so that `--check` can compare instead of
    writing. It used to write each stale file and then write the original back,
    which meant a read-only-sounding flag mutated the wiki twice per file and
    left it corrupted if the process died in between.
    """
    existing = re.search(
        r"\n---\n\n## Related\n\n<!-- Generated by[^\n]*\n" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n",
        text, re.S)

    if existing:
        return text[: existing.start()] + (block if block else "\n")
    if not block:
        return text
    return text.rstrip("\n") + "\n" + block


def apply(path: Path, block: str) -> bool:
    text = path.read_text(encoding="utf-8")
    updated = merge(text, block)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def would_change(path: Path, block: str) -> bool:
    """Whether `apply` would rewrite this file. Reads only."""
    text = path.read_text(encoding="utf-8")
    return merge(text, block) != text


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    check = "--check" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 1
    repo = Path(args[0])

    wiki = load(repo)
    index = build_index(wiki)

    changed, linked = [], 0
    for folder in FOLDERS:
        for slug in wiki[folder]:
            block = render(folder, slug, wiki, index)
            if block:
                linked += 1
            path = repo / folder / f"{slug}.md"
            if check:
                if would_change(path, block):
                    changed.append(f"{folder}/{slug}.md")
            elif apply(path, block):
                changed.append(f"{folder}/{slug}.md")

    if check:
        if changed:
            print(f"link sections are out of date in {len(changed)} file(s)", file=sys.stderr)
            print("run:  python3 scripts/build_backlinks.py .", file=sys.stderr)
            return 1
        print("link sections are current")
        return 0

    total = sum(len(wiki[f]) for f in FOLDERS)
    print(f"updated {len(changed)} of {total} entity pages")
    print(f"{linked} pages now carry a Related section")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
