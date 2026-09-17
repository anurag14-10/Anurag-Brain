#!/usr/bin/env python3
"""Manage the queue of sensed candidates waiting for a human decision.

Things the skill noticed but nobody asked it to save land in `staging/` rather
than in the wiki. This script is the queue's bookkeeping: what is pending, how
old it is, what was decided, and how often the sensing turns out to be right.

It deliberately does **not** write to the wiki. Promoting a candidate means
running the real pipeline -- resolve, corroborate, reconcile, commit -- and that
needs judgment this script has no business having. So the agent decides and
writes; this script only records the decision and does the arithmetic.

That split matters for a second reason: every decision recorded here is a
labelled example of whether the sensor was right, and `--stats` is the only
measurement of that which exists. Guesses would poison it.

Usage:
    review_staged.py <repo>                     everything still open, oldest first
    review_staged.py <repo> --all               include decided items
    review_staged.py <repo> --approve ID [ID…]  cleared for the pipeline
    review_staged.py <repo> --reject  ID [ID…]  not worth logging
    review_staged.py <repo> --promoted ID [ID…] committed to the wiki
    review_staged.py <repo> --quarantine ID     needs a human, do not auto-handle
    review_staged.py <repo> --screen            refuse credentials; exit 1 on a hit
    review_staged.py <repo> --stats             promote rate by entity type
    review_staged.py <repo> --json

Requires PyYAML.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("review_staged.py needs PyYAML.  Install it with:  pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

STAGING_DIR = "staging"

# `unreviewed` is not one of the wiki's corroborated/asserted/contested values,
# and must never be confused with them. A staged item has no standing at all
# until somebody looks at it.
# Three groups, because "decided" and "finished" are not the same thing. An
# approved item has been judged but not yet written to the wiki, and it is the
# easiest thing here to lose: the reviewer believes it landed. So it stays in
# the default listing until something marks it promoted.
NEEDS_DECISION = ("unreviewed", "quarantined")
NEEDS_WRITING = ("approved",)
OPEN = NEEDS_DECISION + NEEDS_WRITING
CLOSED = ("promoted", "rejected")
STATUSES = OPEN + CLOSED

OPERATIONS = ("create", "update", "merge")

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n(.*))?\Z", re.DOTALL)

# Credential patterns only. The softer prohibitions in SKILL.md -- health,
# compensation, personal disputes -- are judgment calls a regex gets wrong in
# both directions, and self-triage is what catches those. A credential is
# different: it is mechanically recognisable, and it is already a leak the
# moment it is written down, promoted or not.
SECRET_PATTERNS = (
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # `sk-proj-…` is the current OpenAI project-key shape, and the hyphens in it
    # meant an alnum-only tail matched nothing at all.
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    # postgres://user:pw@host — a password nobody typed the word "password" near.
    ("url-credentials", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:[^\s/@]{4,}@")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    # `is`/`was` as well as `:`/`=`: staged reasoning is prose, and "the server
    # password is hunter2hunter2" carried a live credential past the screen.
    ("assigned-secret", re.compile(
        r"(?i)\b(?:password|passwd|passphrase|secret|api[_\-]?key|access[_\-]?token|"
        r"client[_\-]?secret|private[_\-]?key)\b"
        r"\s*(?:[:=]|\bis\b|\bwas\b)\s*\S{8,}")),
)


@dataclass
class Item:
    """One sensed candidate, plus where it lives so a decision can be written back."""

    path: Path
    index: int
    data: dict

    @property
    def id(self) -> str:
        return str(self.data.get("id") or f"{self.path.stem}#{self.index}")

    @property
    def status(self) -> str:
        return str(self.data.get("status") or "unreviewed")

    @property
    def target(self) -> str:
        return str(self.data.get("target") or "?")

    @property
    def folder(self) -> str:
        """Entity type, taken from the target path. Drives the per-type stats."""
        target = self.target
        return target.split("/", 1)[0] if "/" in target else "unknown"

    @property
    def operation(self) -> str:
        return str(self.data.get("operation") or "create")

    @property
    def staged_on(self) -> str:
        return str(self.data.get("staged") or self.path.stem)

    def age_days(self, today: date) -> int | None:
        """Days since staging. Surfaced because a queue nobody works is the
        failure mode this whole mechanism has to avoid."""
        try:
            return (today - date.fromisoformat(self.staged_on)).days
        except ValueError:
            return None

    def searchable_text(self) -> str:
        return yaml.safe_dump(self.data, sort_keys=True, allow_unicode=True)

    def as_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "operation": self.operation,
                "target": self.target, "staged": self.staged_on,
                "reasoning": self.data.get("reasoning"),
                "diff": self.data.get("diff")}


@dataclass
class Staging:
    files: dict[Path, dict] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def by_id(self, wanted: str) -> Item | None:
        for item in self.items:
            if item.id == wanted:
                return item
        return None


def split_frontmatter(text: str) -> tuple[str, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return "", text
    return match.group(1), match.group(2) or ""


def load_staging(repo: Path) -> Staging:
    """Read every staging file. Missing or empty is normal, not an error --
    most sessions notice nothing worth keeping."""
    staging = Staging()
    root = repo / STAGING_DIR
    if not root.is_dir():
        return staging

    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        raw, _ = split_frontmatter(text)
        if not raw.strip():
            # Content with no frontmatter is almost always a hand-written note
            # in the wrong shape. Skipping it silently is how someone's captured
            # thought disappears without anyone noticing.
            if text.strip():
                staging.problems.append(
                    f"{path.name}: has content but no `---` frontmatter block, so "
                    f"nothing in it is staged")
            continue
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            staging.problems.append(f"{path.name}: unreadable frontmatter ({exc})")
            continue
        if not isinstance(parsed, dict):
            staging.problems.append(f"{path.name}: frontmatter is not a mapping")
            continue

        entries = parsed.get("items")
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            staging.problems.append(f"{path.name}: `items` is not a list")
            continue

        staging.files[path] = parsed
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                staging.problems.append(f"{path.name}: item {index} is not a mapping")
                continue
            item = Item(path=path, index=index, data=entry)
            if item.status not in STATUSES:
                staging.problems.append(
                    f"{path.name}: {item.id} has status `{item.status}`; "
                    f"expected one of {', '.join(STATUSES)}")
            if item.operation not in OPERATIONS:
                staging.problems.append(
                    f"{path.name}: {item.id} has operation `{item.operation}`")
            staging.items.append(item)

    duplicates = _duplicate_ids(staging.items)
    for dup in duplicates:
        staging.problems.append(f"id `{dup}` appears more than once; decisions would be ambiguous")
    return staging


def _duplicate_ids(items: list[Item]) -> list[str]:
    seen, dupes = set(), []
    for item in items:
        if item.id in seen and item.id not in dupes:
            dupes.append(item.id)
        seen.add(item.id)
    return dupes


def write_back(staging: Staging, touched: set[Path]) -> None:
    """Rewrite only the files whose items changed.

    The body is left empty on purpose. A generated human-readable rendering
    alongside the data would be a second source of truth for the same facts,
    and keeping the two in step is exactly the drift `build_backlinks.py`
    exists to prevent elsewhere.
    """
    for path in sorted(touched):
        parsed = staging.files[path]
        rendered = yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True, width=88)
        path.write_text(f"---\n{rendered}---\n", encoding="utf-8")


def set_status(staging: Staging, ids: list[str], status: str,
               today: date) -> tuple[set[Path], list[str]]:
    touched: set[Path] = set()
    missing: list[str] = []
    for wanted in ids:
        item = staging.by_id(wanted)
        if item is None:
            missing.append(wanted)
            continue
        item.data["status"] = status
        # Keep the decision date, because the gap between staging and decision
        # is what tells you whether the queue is actually being worked.
        item.data["reviewed"] = today.isoformat()
        touched.add(item.path)
    return touched, missing


def screen(staging: Staging) -> list[tuple[str, str, str]]:
    """Find credentials in staged content.

    Runs over the raw text of each item rather than named fields, because a
    token pasted into `evidence` is exactly as leaked as one in `diff`.
    """
    hits: list[tuple[str, str, str]] = []
    for item in staging.items:
        text = item.searchable_text()
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append((item.id, str(item.path.name), name))
    return hits


def stats(staging: Staging) -> dict:
    """Promote rate per entity type -- the sensing precision.

    Only decided items count in the denominator. Including pending ones would
    make the sensor look worse the faster it works, which is backwards.
    """
    buckets: dict[str, dict[str, int]] = {}
    for item in staging.items:
        bucket = buckets.setdefault(item.folder, dict.fromkeys(STATUSES, 0))
        bucket[item.status] = bucket.get(item.status, 0) + 1

    out = {}
    for folder in sorted(buckets):
        counts = buckets[folder]
        kept = counts.get("approved", 0) + counts.get("promoted", 0)
        decided = kept + counts.get("rejected", 0)
        out[folder] = {
            "pending": sum(counts.get(s, 0) for s in NEEDS_DECISION),
            "awaiting_write": counts.get("approved", 0),
            "kept": kept,
            "rejected": counts.get("rejected", 0),
            "decided": decided,
            # None rather than 0.0: no decisions yet is not a precision of zero.
            "precision": round(kept / decided, 3) if decided else None,
        }
    return out


def render_items(items: list[Item], today: date) -> str:
    if not items:
        return "Nothing staged.\n"
    lines = []
    for item in items:
        age = item.age_days(today)
        age_note = f", {age}d old" if age is not None and age > 0 else ""
        note = item.status
        if item.status == "approved":
            # Approved means somebody said yes and the wiki does not know yet.
            note = "approved — NOT YET WRITTEN"
        lines.append(f"[{item.id}] {item.operation} {item.target} ({note}{age_note})")
        if reasoning := item.data.get("reasoning"):
            lines.append(f"    why: {reasoning}")
        diff = item.data.get("diff")
        if isinstance(diff, dict):
            for kind in ("added", "modified", "removed"):
                block = diff.get(kind)
                if isinstance(block, dict) and block:
                    pairs = ", ".join(f"{k}={v!r}" for k, v in block.items())
                    lines.append(f"    {kind}: {pairs}")
        if evidence := item.data.get("evidence"):
            lines.append(f"    heard: {str(evidence).strip()[:160]}")
        lines.append("")
    return "\n".join(lines)


def render_stats(data: dict) -> str:
    if not data:
        return "Nothing staged, so nothing to measure.\n"
    width = max(len(k) for k in data)
    lines = [f"{'type'.ljust(width)}  pending  unwritten  kept  rejected  precision"]
    for folder, row in data.items():
        precision = "-" if row["precision"] is None else f"{row['precision']:.0%}"
        lines.append(
            f"{folder.ljust(width)}  {row['pending']:>7}  {row['awaiting_write']:>9}  "
            f"{row['kept']:>4}  {row['rejected']:>8}  {precision:>9}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--all", action="store_true", help="include already-decided items")
    parser.add_argument("--approve", nargs="+", metavar="ID")
    parser.add_argument("--reject", nargs="+", metavar="ID")
    parser.add_argument("--promoted", nargs="+", metavar="ID")
    parser.add_argument("--quarantine", nargs="+", metavar="ID")
    parser.add_argument("--screen", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--today", help="ISO date, for reproducible output in tests")
    args = parser.parse_args()

    if not args.repo.is_dir():
        print(f"not a directory: {args.repo}", file=sys.stderr)
        return 2

    try:
        today = date.fromisoformat(args.today) if args.today else date.today()
    except ValueError:
        print(f"not an ISO date: {args.today}", file=sys.stderr)
        return 2

    staging = load_staging(args.repo)
    for problem in staging.problems:
        print(f"warning: {problem}", file=sys.stderr)

    transitions = (("approved", args.approve), ("rejected", args.reject),
                   ("promoted", args.promoted), ("quarantined", args.quarantine))
    touched: set[Path] = set()
    missing: list[str] = []
    for status, ids in transitions:
        if not ids:
            continue
        changed, absent = set_status(staging, ids, status, today)
        touched |= changed
        missing += absent

    if missing:
        # Refuse the whole batch rather than applying it partly: a half-applied
        # review is worse than none, because the reviewer believes it landed.
        print(f"no staged item with id: {', '.join(missing)}", file=sys.stderr)
        return 1

    if touched:
        write_back(staging, touched)

    if args.screen:
        hits = screen(staging)
        if args.json:
            print(json.dumps([{"id": i, "file": f, "pattern": p} for i, f, p in hits], indent=2))
        elif hits:
            for item_id, filename, pattern in hits:
                print(f"{filename}: {item_id} looks like a credential ({pattern})")
            print("\nRemove these before promoting. A credential in a staging file is "
                  "already committed history.", file=sys.stderr)
        else:
            print("No credentials found in staged items.")
        return 1 if hits else 0

    if args.stats:
        data = stats(staging)
        print(json.dumps(data, indent=2) if args.json else render_stats(data), end="")
        return 0

    shown = staging.items if args.all else [i for i in staging.items if i.status in OPEN]
    shown = sorted(shown, key=lambda i: (i.staged_on, i.id))

    if args.json:
        print(json.dumps([i.as_dict() for i in shown], indent=2))
    else:
        if touched:
            print(f"Recorded {len(touched)} file(s) worth of decisions.\n")
        print(render_items(shown, today), end="")

    # An open queue is not a failure -- it is the normal state.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
