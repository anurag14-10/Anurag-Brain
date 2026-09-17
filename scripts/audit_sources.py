#!/usr/bin/env python3
"""Compare a brain against a snapshot of its sources. Read-only.

Every connected system holds part of the same truth and nothing measures where
they disagree, so gaps surface one at a time, by accident, during unrelated
work -- and each one turns out to have been there for weeks.

This is the arithmetic half of that: it joins entities to source records on
`external_refs {store, id}`, compares the fields a brain's `SOURCES.md` says
each source may be trusted for, and reports. It decides nothing. Which source
wins for which field was decided once, by a person, in that file; this only
applies it.

Findings are separated by what they ask of a person. Only a source declared
`authoritative` for a field produces a `fillable-gap` -- the one class anything
may write unattended. A `corroborating` source with a value where the brain has
none produces a `blank-here`, which is for reading: a source reliable enough
that a mismatch is worth investigating is not thereby the system that decides
what the field is, and one that fills blanks is authoritative for every
incomplete entity.

**No model call, and no credentials.** It reads a snapshot someone else
fetched, so the judgment is pre-declared rather than exercised at runtime, and
the fetching half can be a credentialed job or a session with connectors
without this script knowing the difference.

Snapshot shape:

    {"generated": "<iso>", "brain": "owner/repo", "lane": "api|mcp",
     "sources": {
       "<source id>": {
         "probe": "live|empty|unreachable",
         "covers": ["<folder>", ...],      # optional; omit if the fetch was whole
         "complete": true,                 # every record, not just pinned ones
         "records": [{"id": "<the system's own id>",
                      "modified": "<iso>",
                      "fields": {"<brain field name>": <value>}}]}}}

Records are keyed by the source's own id and carry BRAIN field names, because
the mapping from one to the other is `from:` in the trust declaration and
belongs to whoever fetched.

Two fields say what the fetch did NOT do, and both exist because absence reads
as agreement otherwise. `covers` names the folders it went looking for, so a
fetch limited to organizations does not report every pinned person as having
lost its counterpart. `complete` says the records are the source's whole set,
without which a record nothing points at cannot be distinguished from a record
that was simply not requested.

Usage:
    audit_sources.py <repo> <snapshot.json> [--json] [--out FILE]

Exit: 0 always. This reports; it never gates.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def load_validator():
    """Reuse the validator's wiki loader rather than writing a fifth one.

    Four scripts in this repo already parse entity frontmatter separately and
    disagree about READMEs and broken files. This one is the only version that
    records why a file failed to parse instead of silently returning {}.
    """
    spec = importlib.util.spec_from_file_location("validate_wiki", SCRIPTS / "validate_wiki.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_wiki"] = module
    spec.loader.exec_module(module)
    return module


vw = load_validator()

# How many rows of one kind are worth printing. Past this it is a number, not a
# list -- a report that scrolls is one nobody reads, which is the same failure
# as a warning that fires on everything.
MAX_ROWS = 25


def fingerprint(*parts: object) -> str:
    """A stable id for one finding, so a human verdict can silence it forever.

    A rejected finding that comes back next run is how a queue becomes noise
    somebody learns to scroll past.
    """
    joined = "␟".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def normalise(value) -> str:
    """Compare values the way a person would, not the way bytes do."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(sorted(normalise(v) for v in value))
    return " ".join(str(value).split()).casefold()


def refs_by_store(entity) -> dict[str, str]:
    """{store: id} for one entity, ignoring malformed entries."""
    out: dict[str, str] = {}
    for ref in entity.frontmatter.get("external_refs") or []:
        if isinstance(ref, dict) and ref.get("store") and ref.get("id"):
            out[str(ref["store"])] = str(ref["id"])
    return out


def verified_on(entity, store: str) -> str | None:
    for ref in entity.frontmatter.get("external_refs") or []:
        if isinstance(ref, dict) and str(ref.get("store")) == store:
            return str(ref["verified"]) if ref.get("verified") else None
    return None


def audit(wiki, snapshot: dict) -> dict:
    """Findings, plus the coverage numbers that are counts rather than rows."""
    findings: list[dict] = []
    coverage: list[dict] = []
    skipped: list[dict] = []
    partial: list[dict] = []

    sources = snapshot.get("sources") or {}
    for source_id, payload in sorted(sources.items()):
        probe = (payload or {}).get("probe")

        # A source that answered nothing is UNAVAILABLE, not empty. Treating it
        # as empty would report every pinned entity as having lost its
        # counterpart -- hundreds of findings that say only that a probe
        # failed. An unreadable signal is a refusal, never a pass.
        if probe != "live":
            skipped.append({"source": source_id, "probe": probe or "absent",
                            "why": "did not report live; contributes no findings"})
            continue

        records = {str(r["id"]): r for r in (payload.get("records") or [])
                   if isinstance(r, dict) and r.get("id")}

        # Which folders this fetch actually went looking for. A fetch that asked
        # only about organizations cannot say anything about a pinned person,
        # and reporting one as having lost its counterpart says "the source
        # dropped it" when the truth is "nobody asked" -- the same confusion
        # `probe` refuses at the level of a whole source. Six of the seven
        # missing-counterpart rows in the first real run were this.
        #
        # Absent means the fetch covered everything, which is what a full nightly
        # job does and what every snapshot written before this meant.
        covers = payload.get("covers")
        if isinstance(covers, list):
            covers = {str(f) for f in covers}
            partial.append({"source": source_id, "covers": sorted(covers)})
        else:
            covers = set(wiki.model.folders)

        seen_ids: set[str] = set()
        trusted_folders = wiki.trust.fields.get(source_id, {})
        unmapped: Counter = Counter()

        for folder in sorted(wiki.model.folders):
            pinned = unpinned = 0
            for entity in wiki.by_folder(folder):
                if entity.broken:
                    continue
                rel = str(entity.path.relative_to(wiki.root))
                ref_id = refs_by_store(entity).get(source_id)
                if not ref_id:
                    unpinned += 1
                    continue
                pinned += 1
                record = records.get(ref_id)

                if record is None:
                    if folder in covers:
                        findings.append({
                            "kind": "missing-counterpart", "source": source_id,
                            "path": rel, "field": None,
                            "detail": f"pinned to {source_id} id {ref_id}, which "
                                      "the snapshot does not contain",
                            "fingerprint": fingerprint("missing", source_id, rel,
                                                       ref_id),
                        })
                    continue
                seen_ids.add(ref_id)

                their = record.get("fields") or {}
                for field, spec in sorted(trusted_folders.get(folder, {}).items()):
                    level = (spec or {}).get("trust", vw.DEFAULT_TRUST)
                    key = f"{folder}.{field}"

                    # Declared as structurally unable to answer, or as nobody's
                    # to fill. Either way a value from here is not a finding,
                    # and saying so once is what keeps the report readable.
                    if level == "unusable" or key in wiki.trust.never_automated:
                        continue

                    ours, theirs = entity.frontmatter.get(field), their.get(field)
                    if normalise(theirs) == "":
                        continue

                    # A source answers in its own vocabulary. Where the brain
                    # declared a translation, apply it -- and where the
                    # translation has no entry for what arrived, say so rather
                    # than comparing the raw value: an unmapped value compared
                    # directly reads as a disagreement on every run, and filled
                    # into a controlled field it is simply wrong.
                    mapping = (spec or {}).get("values")
                    if isinstance(mapping, dict):
                        if theirs in mapping:
                            theirs = mapping[theirs]
                        else:
                            # One row per missing entry, not per entity: a
                            # vocabulary gap is one decision about the mapping,
                            # and repeating it 50 times is how a report stops
                            # being read.
                            unmapped[(source_id, folder, field, str(theirs))] += 1
                            continue
                    if normalise(ours) == "":
                        # Filling a gap and confirming a value are different
                        # permissions. Only an authoritative source may ever
                        # have its value written; a corroborating one saying
                        # something where the wiki says nothing is worth
                        # reading and is never worth acting on unattended.
                        gap = level == "authoritative"
                        findings.append({
                            "kind": "fillable-gap" if gap else "blank-here",
                            "source": source_id, "path": rel,
                            "field": field, "trust": level,
                            "detail": f"`{field}` is blank here; {source_id} has "
                                      f"{theirs!r}",
                            "fingerprint": fingerprint("blank", source_id, rel, field, theirs),
                        })
                    elif normalise(ours) != normalise(theirs):
                        findings.append({
                            "kind": "field-disagreement", "source": source_id,
                            "path": rel, "field": field, "trust": level,
                            "detail": f"`{field}`: here {entity.frontmatter.get(field)!r}, "
                                      f"{source_id} {theirs!r}",
                            "fingerprint": fingerprint("disagree", source_id, rel, field,
                                                       ours, theirs),
                        })

                modified = record.get("modified")
                verified = verified_on(entity, source_id)
                if modified and verified and str(modified)[:10] > verified[:10]:
                    findings.append({
                        "kind": "stale", "source": source_id, "path": rel, "field": None,
                        "detail": f"{source_id} changed {str(modified)[:10]}; last "
                                  f"verified here {verified}",
                        "fingerprint": fingerprint("stale", source_id, rel, modified),
                    })

            if pinned or unpinned:
                coverage.append({"source": source_id, "folder": folder,
                                 "pinned": pinned, "unpinned": unpinned})

        for (src, folder, field, value), count in sorted(unmapped.items()):
            findings.append({
                "kind": "unmapped-source-value", "source": src,
                "path": None, "field": field, "trust": None, "count": count,
                "detail": f"`{folder}.{field}`: {src} answered {value!r} on "
                          f"{count} record(s), which its `values:` mapping does "
                          "not translate",
                "fingerprint": fingerprint("unmapped", src, folder, field, value),
            })

        # Records nothing points at. Only meaningful for a source that returned
        # its whole set; a snapshot of one page would report the rest as
        # orphans, so the fetcher says so rather than this guessing.
        if payload.get("complete"):
            for record_id in sorted(set(records) - seen_ids):
                findings.append({
                    "kind": "orphan-counterpart", "source": source_id,
                    "path": None, "field": None,
                    "detail": f"{source_id} id {record_id} has no entity pinned to it",
                    "fingerprint": fingerprint("orphan", source_id, record_id),
                })

    return {"generated": date.today().isoformat(),
            "brain": snapshot.get("brain"),
            "snapshot_generated": snapshot.get("generated"),
            "lane": snapshot.get("lane"),
            "skipped_sources": skipped,
            "partial_sources": partial,
            "coverage": coverage,
            "findings": findings}


def render(result: dict) -> str:
    lines = [f"# Source audit — {result['generated']}", ""]
    if result.get("brain"):
        lines += [f"Brain: `{result['brain']}` · snapshot "
                  f"{result.get('snapshot_generated') or 'undated'} "
                  f"({result.get('lane') or 'lane unstated'})", ""]

    for entry in result["skipped_sources"]:
        lines.append(f"> **`{entry['source']}` contributed nothing** — probe "
                     f"{entry['probe']}. Findings from it are absent because it "
                     "was not asked, not because it agreed.")
    if result["skipped_sources"]:
        lines.append("")

    by_kind: dict[str, list[dict]] = defaultdict(list)
    for finding in result["findings"]:
        by_kind[finding["kind"]].append(finding)

    # Rows, because these are rare and each one is a decision.
    lines += ["## Needs a person", ""]
    acted = False
    for kind in ("field-disagreement", "unmapped-source-value", "stale",
                 "missing-counterpart"):
        rows = by_kind.get(kind) or []
        if not rows:
            continue
        acted = True
        lines.append(f"### {kind} ({len(rows)})")
        for row in rows[:MAX_ROWS]:
            where = row["path"] or row["source"]
            lines.append(f"- `{where}` — {row['detail']}")
        if len(rows) > MAX_ROWS:
            lines.append(f"- … and {len(rows) - MAX_ROWS} more")
        lines.append("")
    if not acted:
        lines += ["(nothing)", ""]

    # Numbers, because one row per unpinned entity is a report nobody reads.
    lines += ["## Coverage", "",
              "What is pinned to a source by id. Anything unpinned is matched by "
              "name on every ingest, or not at all.", ""]
    for entry in result.get("partial_sources") or []:
        lines.append(f"> **`{entry['source']}` was fetched for "
                     f"{', '.join(entry['covers'])} only.** Pinned entries in "
                     "any other folder were not looked for, so nothing here "
                     "says whether they still exist at the source.")
    if result.get("partial_sources"):
        lines.append("")
    for entry in result["coverage"]:
        total = entry["pinned"] + entry["unpinned"]
        pct = (100 * entry["pinned"] // total) if total else 0
        lines.append(f"- `{entry['source']}` · {entry['folder']}: "
                     f"{entry['pinned']}/{total} pinned ({pct}%)")
    if not result["coverage"]:
        lines.append("(no source reported live)")
    lines.append("")

    gaps = by_kind.get("fillable-gap") or []
    if gaps:
        lines += [f"## Gaps an authoritative source can fill ({len(gaps)})", "",
                  "The wiki says nothing and a source declared `authoritative` "
                  "for the field does. This is the only class anything may write "
                  "without a person; every other class is a suggestion.", ""]
        for row in gaps[:MAX_ROWS]:
            lines.append(f"- `{row['path']}` — {row['detail']}")
        if len(gaps) > MAX_ROWS:
            lines.append(f"- … and {len(gaps) - MAX_ROWS} more")
        lines.append("")

    blanks = by_kind.get("blank-here") or []
    if blanks:
        lines += [f"## Blank here, present at a corroborating source ({len(blanks)})",
                  "",
                  "Read these; do not write them. A corroborating source confirms "
                  "a value the wiki already holds — offering one where the wiki "
                  "holds none is outside what it was trusted for.", ""]
        for row in blanks[:MAX_ROWS]:
            lines.append(f"- `{row['path']}` — {row['detail']}")
        if len(blanks) > MAX_ROWS:
            lines.append(f"- … and {len(blanks) - MAX_ROWS} more")
        lines.append("")

    orphans = by_kind.get("orphan-counterpart") or []
    if orphans:
        lines += [f"## At the source, not here ({len(orphans)})", ""]
        for row in orphans[:MAX_ROWS]:
            lines.append(f"- {row['detail']}")
        if len(orphans) > MAX_ROWS:
            lines.append(f"- … and {len(orphans) - MAX_ROWS} more")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo")
    parser.add_argument("snapshot")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="write the report here instead of stdout")
    args = parser.parse_args()

    root = Path(args.repo)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read snapshot: {exc}", file=sys.stderr)
        return 2

    wiki = vw.load_wiki(root)
    result = audit(wiki, snapshot)

    output = json.dumps(result, indent=2) if args.json else render(result)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
