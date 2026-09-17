#!/usr/bin/env python3
"""Find entities that look like the same real-world thing recorded twice.

Near-duplicates are the main way this wiki degrades: an organization gets
logged under a slug nobody would guess, someone later logs it again, and the
two drift apart carrying half the history each. Catching them is string
comparison, not judgment, so it belongs in a script -- but *deciding* whether
a candidate pair is genuinely one entity does need a human, because plenty of
similar names are legitimately distinct organizations.

So this reports candidates and ranks them. It never merges anything.

Usage:
    find_duplicates.py <repo>                    candidates across all folders
    find_duplicates.py <repo> --folder organizations
    find_duplicates.py <repo> --threshold 0.90   stricter matching
    find_duplicates.py <repo> --json

Requires PyYAML.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("find_duplicates.py needs PyYAML.  Install it with:  pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

FOLDERS = ("organizations", "people", "projects", "products")

# Words that describe an organization's legal form rather than its identity.
# "Pratham Education Foundation" and "Pratham Education" are the same body;
# stripping these is what lets the comparison see that.
NOISE_WORDS = {
    "foundation", "trust", "society", "institute", "institution", "association",
    "limited", "ltd", "pvt", "private", "inc", "incorporated", "llc", "llp",
    "corporation", "corp", "company", "co", "technologies", "technology",
    "solutions", "services", "systems", "group", "holdings", "ventures",
    "the", "and", "of", "for", "to", "a", "an",
}

# Words that say *where* or *how widely* an organization operates rather than
# who it is. These generate most real duplicates in this wiki: one body
# registers separately in each geography, and the arms get logged as unrelated
# organizations. Removing them exposes the shared identity underneath --
# "Pratham USA" and "Pratham International" both reduce to "pratham".
QUALIFIER_WORDS = {
    "usa", "us", "uk", "india", "indian", "america", "american", "global",
    "globally", "worldwide", "international", "national", "regional", "local",
    "asia", "africa", "europe", "european", "canada", "australia",
}

DEFAULT_THRESHOLD = 0.85

# Below this, a shared-token signal is too weak to be worth a human's time.
MIN_SHARED_TOKENS = 1

# A word shared by more names than this is domain vocabulary, not an identity.
MAX_DISTINCTIVE_FREQUENCY = 3


@dataclass
class Candidate:
    folder: str
    left: str
    right: str
    score: float
    reason: str
    acknowledged: bool

    def as_dict(self) -> dict:
        return {
            "folder": self.folder,
            "left": self.left,
            "right": self.right,
            "score": round(self.score, 3),
            "reason": self.reason,
            "acknowledged": self.acknowledged,
        }


def load_entities(repo: Path, folder: str) -> dict[str, dict]:
    directory = repo / folder
    if not directory.is_dir():
        return {}
    entities: dict[str, dict] = {}
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            entities[path.stem] = {}
            continue
        match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        try:
            data = yaml.safe_load(match.group(1)) if match else None
        except yaml.YAMLError:
            data = None
        entities[path.stem] = data if isinstance(data, dict) else {}
    return entities


def tokenize(name: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return [t for t in cleaned.split() if t and t not in NOISE_WORDS]


def acronym(name: str) -> str:
    words = [w for w in re.sub(r"[^A-Za-z\s]", " ", name).split() if w.lower() not in NOISE_WORDS]
    return "".join(w[0].upper() for w in words if w)


def acknowledged_pairs(entities: dict[str, dict]) -> set[frozenset[str]]:
    """Pairs already linked via related_organizations.

    Someone has looked at these two and recorded how they relate. That is not
    proof they are distinct, but it does mean the resemblance has been seen
    and handled, so it should not be reported as a fresh discovery.
    """
    pairs: set[frozenset[str]] = set()
    for slug, data in entities.items():
        for entry in data.get("related_organizations") or []:
            other = entry.get("slug") if isinstance(entry, dict) else entry
            if isinstance(other, str) and other and other != slug:
                pairs.add(frozenset({slug, other}))
    return pairs


def distinctive(tokens: set[str], frequency: dict[str, int]) -> bool:
    """Whether a set of words is specific enough to identify one organization.

    A shared word only implies a shared identity if it is rare here. "Pratham"
    appears in three names and means something; "digital" appears across the
    corpus and means nothing. Measuring this from the data rather than from a
    hand-written stopword list means it keeps working as the wiki grows and
    picks up whatever vocabulary that particular organization is full of.
    """
    return any(frequency.get(token, 0) <= MAX_DISTINCTIVE_FREQUENCY for token in tokens)


def compare(left_name: str, right_name: str, frequency: dict[str, int]) -> tuple[float, str] | None:
    left_tokens, right_tokens = tokenize(left_name), tokenize(right_name)
    if not left_tokens or not right_tokens:
        return None

    left_set, right_set = set(left_tokens), set(right_tokens)
    shared = left_set & right_set

    # Strip scope qualifiers to compare the identity underneath. Two names
    # that agree once "USA" and "International" are set aside are usually the
    # same body's separately-registered arms.
    left_core, right_core = left_set - QUALIFIER_WORDS, right_set - QUALIFIER_WORDS
    if left_core and right_core and distinctive(left_core & right_core, frequency):
        qualifiers = (left_set | right_set) & QUALIFIER_WORDS
        if left_core == right_core and qualifiers:
            return 0.97, (
                f"identical apart from scope wording ({', '.join(sorted(qualifiers))}) — "
                "likely the same body registered in more than one place"
            )
        if left_core < right_core or right_core < left_core:
            smaller, larger = sorted((left_core, right_core), key=len)
            return 0.95, f"one name's core sits inside the other (extra: {' '.join(sorted(larger - smaller))})"

    # One full name entirely inside the other, qualifiers included.
    if left_set < right_set or right_set < left_set:
        smaller, larger = sorted((left_set, right_set), key=len)
        extra = " ".join(sorted(larger - smaller))
        return 0.95, f"one name contains the other (differs by: {extra})"

    similarity = SequenceMatcher(None, " ".join(left_tokens), " ".join(right_tokens)).ratio()

    if shared and len(shared) >= MIN_SHARED_TOKENS:
        overlap = len(shared) / min(len(left_set), len(right_set))
        if overlap >= 0.5:
            score = max(similarity, 0.5 + 0.4 * overlap)
            return score, f"shares {len(shared)} significant word(s): {', '.join(sorted(shared))}"

    if similarity >= 0.8:
        return similarity, "names are spelled almost identically"

    return None


def find_candidates(repo: Path, folders: tuple[str, ...], threshold: float) -> list[Candidate]:
    candidates: list[Candidate] = []

    for folder in folders:
        entities = load_entities(repo, folder)
        if len(entities) < 2:
            continue
        known = acknowledged_pairs(entities)
        names = {slug: (data.get("name") or slug.replace("-", " ")) for slug, data in entities.items()}

        frequency: dict[str, int] = {}
        for name in names.values():
            for token in set(tokenize(str(name))):
                frequency[token] = frequency.get(token, 0) + 1

        for left, right in combinations(sorted(entities), 2):
            result = compare(str(names[left]), str(names[right]), frequency)
            if result is None:
                continue
            score, reason = result
            if score < threshold:
                continue
            candidates.append(
                Candidate(folder, left, right, score, reason, frozenset({left, right}) in known)
            )

        # Same initials, different names: worth a glance, never a merge on its
        # own. Real organizations do share acronyms, and this wiki already
        # holds two different bodies that both go by IESA.
        by_acronym: dict[str, list[str]] = {}
        for slug, name in names.items():
            initials = acronym(str(name))
            if len(initials) >= 3:
                by_acronym.setdefault(initials, []).append(slug)
        for initials, slugs in by_acronym.items():
            if len(slugs) < 2:
                continue
            for left, right in combinations(sorted(slugs), 2):
                if any(c.left == left and c.right == right for c in candidates):
                    continue
                candidates.append(
                    Candidate(
                        folder, left, right, threshold,
                        f"both reduce to the acronym {initials} — often coincidence, check before acting",
                        frozenset({left, right}) in known,
                    )
                )

    candidates.sort(key=lambda c: (-c.score, c.folder, c.left))
    return candidates


def render(candidates: list[Candidate], threshold: float) -> str:
    if not candidates:
        return f"No candidate duplicates at or above {threshold:.2f}."

    fresh = [c for c in candidates if not c.acknowledged]
    seen = [c for c in candidates if c.acknowledged]

    lines = [
        f"{len(candidates)} candidate pair(s) at or above {threshold:.2f}.",
        "These are suggestions. Confirm before merging — similar names are often different bodies.",
    ]

    if fresh:
        lines.append(f"\nUnreviewed ({len(fresh)})")
        for c in fresh:
            lines.append(f"  {c.score:.2f}  {c.folder}/{c.left}  <->  {c.right}")
            lines.append(f"        {c.reason}")

    if seen:
        lines.append(f"\nAlready linked via related_organizations ({len(seen)}) — resemblance already handled")
        for c in seen:
            lines.append(f"  {c.score:.2f}  {c.folder}/{c.left}  <->  {c.right}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--folder", choices=FOLDERS, help="restrict to one entity type")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.repo.is_dir():
        print(f"not a directory: {args.repo}", file=sys.stderr)
        return 2

    folders = (args.folder,) if args.folder else FOLDERS
    candidates = find_candidates(args.repo, folders, args.threshold)

    if args.json:
        print(json.dumps([c.as_dict() for c in candidates], indent=2))
    else:
        print(render(candidates, args.threshold))

    # Candidates are advisory, so this does not fail a build on its own.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
