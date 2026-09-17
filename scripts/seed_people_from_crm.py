#!/usr/bin/env python3
"""Merge CRM contacts into people/ entries.

Takes contacts already fetched from the CRM as JSON and folds them into the
wiki: enriching people who already exist, creating those who do not, and
recording a spelling variant as an alias whenever the two differ.

Separating the fetch from the merge is deliberate. Fetching needs a live CRM
connection that only some runtimes have; merging is deterministic and belongs
in a script that can be read, tested and re-run. It also means a batch can be
re-applied safely -- the merge is idempotent.

Input JSON: a list of objects with
    name, email, phone, org      (org is a wiki organisation slug)

Usage:
    seed_people_from_crm.py <repo> <contacts.json> --source <id> [--dry-run]

`--source` is required and takes a source id from the brain's `SOURCES.md`.
It is not defaulted: that id is per-brain data -- "the `id` column is what
appears in an entity's `sources:` block" -- so guessing it writes a provenance
record naming a source the brain has never registered.

Nothing is invented. A missing role stays missing: the CRM does not hold job
titles in any usable form, and deriving one from an email domain is guessing.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

try:
    import yaml
except ImportError:
    print("needs PyYAML:  pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

TITLES = {"dr", "mr", "mrs", "ms", "prof", "shri", "smt"}


def tokens(name: str) -> list[str]:
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    return [t for t in re.sub(r"[^a-z ]", " ", n).split() if t and t not in TITLES]


def slugify(name: str) -> str:
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", n)).strip("-")


def same_person(a: str, b: str) -> bool:
    """Whether two names plausibly denote one person.

    An earlier version compared the first four letters of a forename and so
    failed on `dipa` against `deep` -- creating exactly the duplicate it was
    meant to prevent. Whole-name similarity with a surname anchor handles both
    spelling drift and names recorded at different completeness.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    sa, sb = set(ta), set(tb)
    # "<forename> <middle>" inside "<forename> <middle> <surname>". Two shared
    # tokens required,
    # so a shared forename alone never collapses two people together.
    if (sa <= sb or sb <= sa) and len(sa & sb) >= 2:
        return True
    if ta[-1] == tb[-1] and SequenceMatcher(None, ta[0], tb[0]).ratio() >= 0.75:
        return True
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.88


def load_people(repo: Path) -> dict[str, tuple[str, Path]]:
    out = {}
    for path in sorted((repo / "people").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        data = {}
        if text.startswith("---"):
            try:
                data = yaml.safe_load(text.split("---", 2)[1]) or {}
            except yaml.YAMLError:
                pass
        out[path.stem] = (data.get("name") or path.stem, path)
    return out


AFFIL_BLOCK = "  - organization: {org}\n"


def enrich(path: Path, org: str, email: str, phone: str, crm_name: str, wiki_name: str) -> str | None:
    text = path.read_text(encoding="utf-8")
    if email and email in text:
        return None  # already recorded; re-running must not duplicate

    match = re.search(rf"(  - organization: {re.escape(org)}\n(?:    \w+:.*\n)*)", text, re.M)
    if not match:
        return "no-affiliation"

    block = match.group(1)
    additions = ""
    if email and "    email:" not in block:
        additions += f'    email: "{email}"\n'
    if phone and "    phone:" not in block:
        additions += f'    phone: "{phone}"\n'
    if additions:
        text = text.replace(block, block.rstrip("\n") + "\n" + additions, 1)

    if tokens(crm_name) != tokens(wiki_name):
        if re.search(r"^aliases:", text, flags=re.M):
            if crm_name not in text:
                text = re.sub(r"^aliases: \[(.*)\]$",
                              lambda m: f'aliases: [{m.group(1) + ", " if m.group(1) else ""}"{crm_name}"]',
                              text, count=1, flags=re.M)
        else:
            text = text.replace("type: person\n", f'type: person\naliases: ["{crm_name}"]\n', 1)

    path.write_text(text, encoding="utf-8")
    return "enriched"


TEMPLATE = """---
name: "{name}"
type: person
schema_version: 5
affiliations:
  - organization: {org}
    role: ""
    email: "{email}"
    phone: "{phone}"
    status: current
    from: ""
    to: ""
first_logged: "{today}"
last_updated: "{today}"
sources:
  - source: {source}
    confirms: [{confirms}]
    method: queried
    ref: "contact record, via the {org} account"
    verified: {today}
contested: []
---

# {name}

Contact at [{org}](../organizations/{org}.md), recorded from `{source}`.

## Notes

- `role` left blank: this source does not hold job titles in any usable form.
  Fill from conversation or another source that does.
"""


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    source_id = next((a.split("=", 1)[1] for a in sys.argv[1:]
                      if a.startswith("--source=")), None)
    if source_id is None and "--source" in sys.argv:
        i = sys.argv.index("--source")
        source_id = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if source_id in args:
            args.remove(source_id)
    if len(args) != 2 or not source_id:
        print(__doc__)
        return 1

    repo, contacts_file = Path(args[0]), Path(args[1])
    contacts = json.loads(contacts_file.read_text())
    # Today, not a literal. This was a hardcoded date, so every person the
    # seeder created carried it as first_logged, last_updated and verified --
    # and staleness in this system is computed from `verified`, so a frozen
    # date silently reports a batch as freshly confirmed forever.
    today = date.today().isoformat()

    orgs = {p.stem for p in (repo / "organizations").glob("*.md")}
    people = load_people(repo)

    created, enriched, aliased, skipped = [], [], [], []

    for row in contacts:
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip()
        phone = (row.get("phone") or "").strip()
        org = (row.get("org") or "").strip()
        if not name or not org:
            skipped.append(f"{name or '?'} — incomplete row")
            continue
        if org not in orgs:
            skipped.append(f"{name} — no organisation entry for {org}")
            continue

        hit = next((slug for slug, (wiki_name, _) in people.items() if same_person(wiki_name, name)), None)

        # A single-token name is not enough to create a person on. CRM records
        # people as "<name> (<org>)" and "<name> (<initials>) - <org>", which reduce
        # to a bare forename once the parenthetical is stripped -- and both
        # already existed in the wiki under their full names. Creating from one
        # token manufactures a duplicate that no later matcher can resolve,
        # because there is nothing to match on. Enriching an existing person is
        # still fine: the match was made on more than the name alone.
        if not hit and len(tokens(name)) < 2:
            skipped.append(f"{name} — only one name part, too little to create a person")
            continue

        if hit:
            wiki_name, path = people[hit]
            if dry_run:
                enriched.append(f"{name} -> {hit}")
                continue
            result = enrich(path, org, email, phone, name, wiki_name)
            if result == "no-affiliation":
                skipped.append(f"{name} — exists as {hit} but has no {org} affiliation")
            elif result == "enriched":
                enriched.append(f"{name} -> {hit}")
                if tokens(name) != tokens(wiki_name):
                    aliased.append(f"{wiki_name}  <-  CRM '{name}'")
        else:
            slug = slugify(name)
            if slug in people:
                skipped.append(f"{name} — slug {slug} taken by a different person")
                continue
            confirms = "name" + (", email" if email else "") + (", phone" if phone else "")
            if not dry_run:
                (repo / "people" / f"{slug}.md").write_text(
                    TEMPLATE.format(name=name, org=org, email=email, phone=phone,
                                    today=today, confirms=confirms,
                                    source=source_id), encoding="utf-8")
                people[slug] = (name, repo / "people" / f"{slug}.md")
            created.append(slug)

    verb = "would " if dry_run else ""
    print(f"{verb}enrich {len(enriched)} · {verb}create {len(created)} · skip {len(skipped)}")
    if aliased:
        print("\naliases recorded (same person, different spelling in CRM):")
        for line in aliased:
            print(f"  {line}")
    if skipped:
        print("\nskipped:")
        for line in skipped:
            print(f"  {line}")
    print("\nRun find_duplicates.py afterwards — name matching catches most, not all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
