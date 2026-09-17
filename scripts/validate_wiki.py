#!/usr/bin/env python3
"""Deterministic hygiene checks for a company-brain wiki.

Everything here has a right answer that does not need a language model to
find: whether a slug resolves, whether a file carries the current schema
version, whether a tag exists in the controlled vocabulary, which entities
nothing links to. Reasoning through 300 files to answer those questions is
slower, costs tokens, and is less reliable than a script that reads them all
in under a second.

The rule this encodes: spend model tokens on judgment, never on arithmetic.

Usage:
    validate_wiki.py <repo>              report problems
    validate_wiki.py <repo> --stats      add a field-coverage profile
    validate_wiki.py <repo> --json       machine-readable, for CI
    validate_wiki.py <repo> --strict     treat warnings as failures

Exit status is 1 when errors are found (or warnings under --strict), so this
can gate CI on the wiki repo and catch drift from anything writing to it.

Requires PyYAML: entity frontmatter contains nested lists of mappings, which
is not worth hand-parsing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, not logic
    print(
        "validate_wiki.py needs PyYAML.  Install it with:  pip install pyyaml",
        file=sys.stderr,
    )
    raise SystemExit(2)

ENTITY_FOLDERS = {
    "organizations": "organization",
    "people": "person",
    "projects": "project",
    "products": "product",
}

# Which frontmatter fields point at other entities, and what they point at.
# A reference is either a bare slug or a mapping carrying one, so both shapes
# are accepted everywhere rather than being enforced per field -- the schema
# has changed shape once already and will again.
REFERENCE_FIELDS = {
    "organizations": {
        "people": "people",
        "related_organizations": "organizations",
    },
    "people": {
        # v3 renamed this and changed the key inside each entry; both shapes are
        # accepted so a part-migrated wiki still resolves.
        "affiliations": "organizations",
        "organizations": "organizations",
    },
    "projects": {
        "organizations": "organizations",
        "people": "people",
        "products_leveraged": "products",
        "reusable_components": "products",
    },
    "products": {
        "built_for": "organizations",
        "used_in": "projects",
    },
}

# Fields whose values must come from MASTERS.md, and which master governs each.
# This mapping is the one piece of organization-shaped knowledge in this script; it
# belongs in configuration alongside the vocabularies themselves, and moves
# there when MASTERS.md becomes adopter-editable.
VOCABULARY_FIELDS = {
    "organizations": {
        "broad_sector": "1a",
        # Each v3 axis draws from one specific ladder, whatever sector the
        # organisation itself sits in. A public sector undertaking is
        # government-owned *and* a listed company competing in a market, so it
        # legitimately carries a jurisdiction, a size and a reach at once.
        # Checking the ladder a value came from catches real errors; checking
        # whether a sector is "allowed" an axis would flag correct data.
        "reach": "1b/Samaaj",
        "size": "1b/Bazaar",
        "jurisdiction": "1b/Sarkaar",
        "scale_tier": "1b",   # v2 alias — any ladder, retained for compatibility
        "org_type": "1c",
    },
    "projects": {
        "vertical": "2",
        "services": "3",
        "solutions": "4",
        "tech_stack": "6",
        "regions": "7",
        "open_tags": "8",
        "delivered_via": "9",
        "showcase_publicly": "10",
        "engagement_stage": "11",
        # products_leveraged is deliberately absent here: it accepts either a
        # products/ slug or a Master 5 term, so it needs the dual check below
        # rather than the single-vocabulary one.
    },
    "products": {
        "funding_model": "12",
    },
}

# Relationship roles live on the edge, not on the entity, so they are checked
# separately from the flat fields above.
ROLE_MASTER = "13"

# Directories at a wiki root that are never entity folders, so their presence is
# not an undeclared entity type.
NON_ENTITY_DIRS = frozenset({
    "activities", "staging", "proposals", "reports", "scripts", "site",
    "skills", "node_modules", "__pycache__",
})

# Roles on a project's edges, by edge field name. The product only ever checked
# the organizations edge; a brain may declare more.
DEFAULT_ROLE_MASTERS = {"organizations": ROLE_MASTER}


TRUST_LEVELS = ("authoritative", "corroborating", "advisory", "unusable")

# Anything not declared is advisory: it may suggest and never write. Silence
# has to be the cautious answer, or forgetting to declare a field becomes a
# grant rather than an omission.
DEFAULT_TRUST = "advisory"


@dataclass
class SourceTrust:
    """What a brain has decided each source may be trusted for, field by field.

    `SOURCES.md` has always carried this as prose -- "Trust for:" and "Do not
    trust for:" per source, plus a "Where authority sits" table that says in
    so many words that it is documentation rather than automation. This is the
    same judgment in a form a script can act on, so that anything automated
    applies a decision a person already made instead of making one at runtime.

    Parsed from fenced yaml rather than from the prose. A term written without
    a bullet is invisible to every automated check, and loosening a parser to
    cope is how MASTERS.md taught this lesson -- so the machine-readable part
    is exact and sits beside the prose that explains it.
    """

    # source id -> folder -> field -> {"trust": level, "from": attribute}
    fields: dict[str, dict[str, dict[str, dict]]] = dc_field(default_factory=dict)
    never_automated: set[str] = dc_field(default_factory=set)   # "folder.field"
    declared_ids: set[str] = dc_field(default_factory=set)

    def level(self, source: str, folder: str, field: str) -> str:
        spec = self.fields.get(source, {}).get(folder, {}).get(field)
        return (spec or {}).get("trust", DEFAULT_TRUST)


def parse_source_trust(path: Path) -> tuple[SourceTrust, list[str]]:
    """Trust declarations from SOURCES.md, plus any malformed-block reasons.

    A block that will not parse is reported rather than skipped: a declaration
    nobody can read is not the same as no declaration, and treating it as the
    latter silently downgrades every field it covered to advisory.
    """
    trust = SourceTrust()
    problems: list[str] = []
    if not path.exists():
        return trust, problems

    text = path.read_text(encoding="utf-8")
    for raw in re.findall(r"```yaml\n(.*?)```", text, re.S):
        try:
            block = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            problems.append(f"a yaml block could not be parsed: {exc}")
            continue
        if not isinstance(block, dict):
            continue

        if "never_automated" in block:
            entries = block.get("never_automated") or []
            if isinstance(entries, list):
                trust.never_automated |= {str(e) for e in entries if isinstance(e, str)}

        source_id = block.get("id")
        if not source_id:
            continue
        source_id = str(source_id)
        trust.declared_ids.add(source_id)
        folders = block.get("fields")
        if not isinstance(folders, dict):
            continue
        per_folder: dict[str, dict[str, dict]] = {}
        for folder, fields in folders.items():
            if not isinstance(fields, dict):
                continue
            per_folder[str(folder)] = {
                str(name): (spec if isinstance(spec, dict) else {"trust": str(spec)})
                for name, spec in fields.items()
            }
        if per_folder:
            trust.fields[source_id] = per_folder
    return trust, problems


@dataclass
class Model:
    """The entity model in force.

    Everything below is DECLARED by a brain in `SCHEMA.yml` -- entity types and
    their folders, the master each field draws from, the master each edge role
    draws from. The constants above are the product's default, used only where a
    brain has declared nothing: a brain without a SCHEMA.yml has not said what
    its model is, and inventing an opinion about that would flag correct data.

    Where a brain HAS declared one it wins outright, with no merging. Merging
    would reintroduce the exact silence this replaces -- the product quietly
    checking fields against masters the brain never mentioned.
    """

    folders: dict[str, str]                    # folder -> expected `type` value
    vocab_fields: dict[str, dict[str, str]]    # folder -> {field -> master id}
    ref_fields: dict[str, dict[str, tuple[str, ...]]]  # folder -> {field -> candidate folders}
    role_masters: dict[str, str]               # edge field -> master id
    declared_fields: dict[str, set[str]]       # folder -> {field, ...}
    external_edges: set[tuple[str, str]]       # (folder, field) may point outside
    orphan_exempt: set[str]                    # folders the orphan rule skips
    orphan_needs_inbound: set[str]             # folders where outbound does not count
    rules: list[dict]                          # constraints the brain declared
    descriptions: dict[str, str]               # folder -> what the type MEANS
    source: str                                # "SCHEMA.yml" or "product default"

    @property
    def from_schema(self) -> bool:
        return self.source == "SCHEMA.yml"


def as_targets(value) -> tuple[str, ...]:
    """The folders a reference field may resolve in.

    A field names one folder in the common case and more than one in the real
    one: a product's `used_in` resolves against a project *or* an opportunity,
    because an opportunity that converts keeps its slug. A bare string stays
    meaning exactly what it meant, so every existing declaration is unchanged.
    """
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if isinstance(v, str) and v.strip())
    return ()


def default_model() -> Model:
    return Model(
        folders=dict(ENTITY_FOLDERS),
        vocab_fields={f: dict(v) for f, v in VOCABULARY_FIELDS.items()},
        ref_fields={f: {k: as_targets(v) for k, v in fields.items()}
                    for f, fields in REFERENCE_FIELDS.items()},
        role_masters=dict(DEFAULT_ROLE_MASTERS),
        declared_fields={},
        external_edges=set(EXTERNAL_REFERENCE_FIELDS),
        orphan_exempt=set(DEFAULT_ORPHAN_EXEMPT),
        orphan_needs_inbound=set(),
        rules=[],          # the product ships none, on purpose
        descriptions={},
        source="product default",
    )


def read_model(root: Path) -> tuple[Model, str | None]:
    """The brain's declared model, or the product default, plus any refusal.

    Returns (model, error). A malformed SCHEMA.yml yields the default model AND
    an error string: the run still says something useful, but it fails. Silently
    downgrading an unreadable declaration to the default is how `which_brain.py`
    once routed a customer's wiki into the wrong brain -- an unreadable signal is
    a refusal, never a pass.
    """
    path = root / "SCHEMA.yml"
    if not path.is_file():
        return default_model(), None
    try:
        schema = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return default_model(), f"SCHEMA.yml could not be parsed: {exc}"
    if not isinstance(schema, dict):
        return default_model(), "SCHEMA.yml does not contain a mapping"

    entities = schema.get("entities")
    if not isinstance(entities, dict) or not entities:
        return default_model(), None   # a schema that declares no entities

    folders: dict[str, str] = {}
    vocab_fields: dict[str, dict[str, str]] = {}
    ref_fields: dict[str, dict[str, tuple[str, ...]]] = {}
    descriptions: dict[str, str] = {}
    external_edges: set[tuple[str, str]] = set()
    orphan_exempt: set[str] = set()
    orphan_needs_inbound: set[str] = set()
    declared: dict[str, set[str]] = {}
    for type_name, spec in entities.items():
        if not isinstance(spec, dict):
            continue
        folder = spec.get("folder")
        if not folder:
            continue
        folders[str(folder)] = str(type_name)
        fields = spec.get("fields")
        if not isinstance(fields, dict):
            continue
        declared[str(folder)] = set(fields)
        # What the type MEANS, as opposed to what shape it is. A schema that
        # says `opportunity -> opportunities` and nothing more cannot tell a
        # reader that an opportunity is pre-contract and is never cited as
        # delivered work -- and increasingly the reader is a model writing the
        # next rule, which has no prose to fall back on.
        about = spec.get("description")
        if isinstance(about, str) and about.strip():
            descriptions[str(folder)] = " ".join(about.split())
        # An entity type nothing is expected to point at. A pipeline record or
        # a definition justifies itself; flagging every one is the warning that
        # fires on almost everything, which this project has paid for twice.
        if spec.get("orphan_exempt"):
            orphan_exempt.add(str(folder))
        # Whether naming things is enough to be connected, or something must
        # name YOU. A person who states where they work is part of the graph;
        # an organization that lists three related bodies and which no piece of
        # work refers to is usually one somebody created and then forgot. Both
        # readings are defensible, which is exactly why the brain decides.
        if spec.get("orphan_needs_inbound"):
            orphan_needs_inbound.add(str(folder))
        vocabs: dict[str, str] = {}
        refs: dict[str, tuple[str, ...]] = {}
        for name, fspec in fields.items():
            if not isinstance(fspec, dict):
                continue
            # `vocab` is a single value, `list_vocab` a list of them; both draw
            # from one master, and the check does not care which shape it is.
            master = fspec.get("vocab") or fspec.get("list_vocab")
            if master:
                vocabs[str(name)] = str(master)
            # Three spellings of "this points at another entity" -- a lone
            # slug, a list of slugs, a list of edge objects carrying a role.
            # iter_reference_slugs already handles all three value shapes, so
            # they collapse to one mapping. Each may name one folder or several.
            # `external_ref` is a fourth spelling, and the one a brain cannot
            # do without: an edge that counts towards connectivity but whose
            # target may legitimately not exist here. A person's employment
            # history is the case -- see EXTERNAL_REFERENCE_FIELDS below. Read
            # as an ordinary reference so the edge exists at all, and recorded
            # so the dangling check warns instead of failing.
            external = as_targets(fspec.get("external_ref")
                                  or fspec.get("list_external_ref"))
            targets = as_targets(fspec.get("ref") or fspec.get("list_ref")
                                 or fspec.get("edge")) or external
            if targets:
                refs[str(name)] = targets
            if external:
                external_edges.add((str(folder), str(name)))
        if vocabs:
            vocab_fields[str(folder)] = vocabs
        if refs:
            ref_fields[str(folder)] = refs

    if not folders:
        return default_model(), None

    roles = schema.get("role_masters")
    role_masters = ({str(k): str(v) for k, v in roles.items()}
                    if isinstance(roles, dict) else dict(DEFAULT_ROLE_MASTERS))

    # A brain that declares entities but no edges gets the product's table
    # rather than an empty graph: declaring no references is far more likely to
    # be an under-specified schema than a genuinely edgeless brain, and
    # silently resolving nothing would turn every dangling reference invisible.
    if not ref_fields:
        ref_fields = {f: {k: as_targets(v) for k, v in fields.items()}
                      for f, fields in REFERENCE_FIELDS.items()}

    # A brain that declares neither falls back to the product's table for the
    # same reason ref_fields does: silence here is far more likely to be an
    # under-specified schema than a brain that genuinely has no external edges
    # and no self-justifying entity type.
    return Model(folders=folders, vocab_fields=vocab_fields,
                 ref_fields=ref_fields, role_masters=role_masters,
                 declared_fields=declared,
                 external_edges=external_edges or set(EXTERNAL_REFERENCE_FIELDS),
                 orphan_exempt=orphan_exempt or set(DEFAULT_ORPHAN_EXEMPT),
                 orphan_needs_inbound=orphan_needs_inbound,
                 rules=[r for r in (schema.get("rules") or [])
                        if isinstance(r, dict)],
                 descriptions=descriptions,
                 source="SCHEMA.yml"), None

# Fields whose values may legitimately not be entities. Their edges still count
# towards what references what -- a product named here is genuinely in use --
# but an unresolved value is not a broken link, because a catalogue term was
# always a valid thing to write. check_vocabulary warns on these instead.
SOFT_REFERENCE_FIELDS = {"products_leveraged"}

# Edges that may point outside the wiki entirely, keyed by (folder, field).
#
# A person's employment history names the companies they have worked for, and
# the organization folder holds bodies *this* organization has a relationship
# with -- customer, funder, partner, end client. Those two sets overlap only by
# coincidence. Minting an entity for every past employer would fill the folder
# with companies nobody here has ever dealt with; the contacts graph alone spans
# over eight thousand of them.
#
# So an affiliation pointing at an organization with no entity is expected, not
# broken. It is still reported -- as a warning, because a mistyped employer slug
# looks exactly the same and only a person can tell them apart -- but it does
# not fail a build. When such a company does acquire a relationship, creating
# the entity under the slug already recorded makes every affiliation resolve
# with no edit to the person.
#
# Keyed on (folder, field) rather than field alone: `organizations` is also a
# field on projects, where a dangling reference is a genuine break.
EXTERNAL_REFERENCE_FIELDS = {
    ("people", "affiliations"),
    ("people", "organizations"),  # pre-v3 shape, still accepted above
}

# Folders the orphan rule skips where a brain declares nothing. A project is
# pointed at by the entries it produced, not the reverse.
DEFAULT_ORPHAN_EXEMPT = {"projects"}

# A rule may warn or fail the build, and nothing else. An unknown
# severity falls back to warn rather than being dropped: a rule nobody
# can grade is still a rule somebody meant.
RULE_SEVERITIES = ("warn", "error")

# How a value was obtained. An unrecognised method is worse than a missing one:
# it looks like recorded provenance while meaning nothing.
PROVENANCE_METHODS = {"queried", "stated", "derived", "document", "public"}

DISCLOSURE_LEVELS = {"public_named", "public_anonymised", "internal_only", "confidential"}
METRIC_KINDS = {"baseline", "outcome"}
CASE_STUDY_STATUSES = {"none", "drafted", "published"}

REQUIRED_FIELDS = ("name", "type", "schema_version")

# Long values are prose that happened to sit where a vocabulary term was
# expected; treating them as terms produces confident nonsense.
MAX_TERM_LENGTH = 80

MAX_ISSUES_PER_CODE = 40

WIKI_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warn"
    code: str
    path: str
    message: str


@dataclass
class Entity:
    slug: str
    folder: str
    path: Path
    frontmatter: dict
    body: str
    broken: str | None = None  # set when the file could not be parsed


@dataclass
class Wiki:
    root: Path
    entities: dict[tuple[str, str], Entity] = dc_field(default_factory=dict)
    schema_version: int | None = None
    declared_fields: dict[str, set[str]] = dc_field(default_factory=dict)
    vocabularies: dict[str, set[str]] = dc_field(default_factory=dict)
    source_ids: set[str] = dc_field(default_factory=set)
    external_ref_prefixes: set[str] = dc_field(default_factory=set)
    model: Model = dc_field(default_factory=default_model)
    model_error: str | None = None
    trust: SourceTrust = dc_field(default_factory=SourceTrust)
    trust_problems: list[str] = dc_field(default_factory=list)

    def by_folder(self, folder: str) -> list[Entity]:
        return [e for (f, _), e in sorted(self.entities.items()) if f == folder]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[str | None, str]:
    if not text.startswith("---"):
        return None, text
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not match:
        return None, text
    return match.group(1), match.group(2)


def load_wiki(root: Path) -> Wiki:
    wiki = Wiki(root=root)

    # The model first: it decides which folders are entity folders at all, so
    # everything below is driven by what the brain declared rather than by what
    # this product happens to know about.
    wiki.model, wiki.model_error = read_model(root)

    for folder in wiki.model.folders:
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            # A folder README or index is documentation, not an entity, and
            # build_rollups.py has always skipped them. Reading them as
            # entities reported a real file as a broken one, which is the kind
            # of error people learn to scroll past.
            if path.name in {"README.md", "index.md"}:
                continue
            text = path.read_text(encoding="utf-8")
            raw, body = split_frontmatter(text)
            slug = path.stem

            if raw is None:
                wiki.entities[(folder, slug)] = Entity(
                    slug, folder, path, {}, body, broken="no YAML frontmatter"
                )
                continue
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                wiki.entities[(folder, slug)] = Entity(
                    slug, folder, path, {}, body, broken=f"unparseable frontmatter: {exc}"
                )
                continue
            if not isinstance(data, dict):
                wiki.entities[(folder, slug)] = Entity(
                    slug, folder, path, {}, body, broken="frontmatter is not a mapping"
                )
                continue

            wiki.entities[(folder, slug)] = Entity(slug, folder, path, data, body)

    wiki.schema_version = read_schema_version(root / "CONVENTIONS.md")
    wiki.declared_fields = wiki.model.declared_fields
    wiki.vocabularies = parse_masters(root / "MASTERS.md")
    wiki.source_ids = parse_source_ids(root / "SOURCES.md")
    wiki.trust, wiki.trust_problems = parse_source_trust(root / "SOURCES.md")
    wiki.external_ref_prefixes = read_external_ref_prefixes(root)
    return wiki


def parse_source_ids(path: Path) -> set[str]:
    """Source identifiers, read from the headings that define them.

    Empty when SOURCES.md is absent, in which case store names are not checked
    -- an adopter part-way through setup should not be told their references
    are wrong.
    """
    if not path.exists():
        return set()
    return set(re.findall(r"^###\s+`([a-z0-9_]+)`", path.read_text(encoding="utf-8"), re.M))


def read_schema_version(path: Path) -> int | None:
    if not path.exists():
        return None
    match = re.search(
        r"^current schema version:\s*\**(\d+)", path.read_text(encoding="utf-8"),
        re.IGNORECASE | re.MULTILINE,
    )
    return int(match.group(1)) if match else None


def parse_masters(path: Path) -> dict[str, set[str]]:
    """Extract controlled vocabularies from the masters document.

    The document is written for people, so terms appear in several shapes:
    bullet lists, middot-separated runs, runs prefixed by a bold group label,
    and `Term — description` pairs. Prose paragraphs sit between them. The
    conservative reading below skips anything it cannot confidently interpret
    as a term, because a vocabulary check that invents violations is worse
    than no vocabulary check -- people stop reading the output.

    Longer term this belongs in a machine-readable form so parsing is exact
    rather than careful.
    """
    if not path.exists():
        return {}

    vocabularies: dict[str, set[str]] = defaultdict(set)
    current: str | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        heading = re.match(r"^###\s+(\d+[a-z])\.", stripped)
        if heading:
            current = heading.group(1)
            # A subsection like `5f` is also a valid value for fields that
            # name a catalog tier rather than an item inside it.
            vocabularies[heading.group(1)[0]].add(heading.group(1))
            continue
        heading = re.match(r"^##\s+(\d+)\.", stripped)
        if heading:
            current = heading.group(1)
            continue

        if current is None or not stripped:
            continue
        # Blockquotes are editorial notes; italic-only lines are asides.
        if stripped.startswith(">") or re.fullmatch(r"\*\(.*\)\*", stripped):
            continue
        if re.match(r"^\*\*(Note|On)\b", stripped):
            continue

        group = re.match(r"^\*\*Under ([A-Za-z]+):\*\*\s*(.+)$", stripped)
        if group and current:
            # `**Under Sarkaar:** National/Central · State · ...` — a ladder
            # scoped to one sector. Registered under both `1b/Sarkaar` and the
            # flat `1b`, so the legacy alias still validates against any ladder.
            for candidate in group.group(2).split("·"):
                term = clean_term(candidate)
                if term:
                    vocabularies[f"{current}/{group.group(1)}"].update(term_variants(term))
                    vocabularies[current].update(term_variants(term))
            continue

        if stripped.startswith("- "):
            candidates = [stripped[2:]]
        elif "·" in stripped:
            # A bold label before the run names the group, not a term.
            body = re.sub(r"^\*\*[^*]+:\*\*\s*", "", stripped)
            candidates = body.split("·")
        else:
            continue

        for candidate in candidates:
            term = clean_term(candidate)
            if term:
                vocabularies[current].update(term_variants(term))

    return dict(vocabularies)


def term_variants(term: str) -> set[str]:
    """A term and its form without a trailing parenthetical.

    Masters uses trailing parentheses two different ways, and only a human can
    tell them apart: `Bazaar (Enterprise/Market)` glosses a term whose real
    value is `Bazaar`, while `POC (Proof of Concept)` and `Public Sector
    Undertaking (PSU)` are written out in full wherever they are used. Rather
    than guess which is which -- and flag correct data either way -- accept
    both readings.
    """
    variants = {term}
    without_gloss = re.sub(r"\s*\([^()]*\)$", "", term).strip()
    if without_gloss:
        variants.add(without_gloss)

    # Some terms carry an inline category label -- `Headless CMS: Strapi` sits
    # inside a run of otherwise-bare technology names. The labelled thing is
    # what gets tagged, so register it on its own too.
    label, sep, labelled = term.partition(": ")
    if sep and label and labelled and len(labelled) <= MAX_TERM_LENGTH:
        variants.add(labelled.strip())

    return variants


def clean_term(raw: str) -> str | None:
    term = raw.strip()
    term = re.sub(r"^\*\*[^*]+:\*\*\s*", "", term)  # leftover group label
    term = re.sub(r"\*\(.*?\)\*", "", term)  # italic annotation
    term = term.strip().strip("*`").strip()
    # `Term — description` keeps only the term.
    term = re.split(r"\s+[—–]\s+", term, maxsplit=1)[0].strip()
    if not term or len(term) > MAX_TERM_LENGTH:
        return None
    # Sentence punctuation means this was prose, not a term.
    if term.endswith(".") or "; " in term:
        return None
    return term


# --------------------------------------------------------------------------
# Reference graph
# --------------------------------------------------------------------------


def iter_reference_slugs(value) -> list[str]:
    """Yield slugs from a reference field, whichever shape it takes."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        # `slug` everywhere except a person's affiliations, which say
        # `organization` because the entry describes a relationship, not a
        # bare pointer.
        slug = value.get("slug") or value.get("organization")
        return [slug] if isinstance(slug, str) and slug.strip() else []
    if isinstance(value, list):
        slugs: list[str] = []
        for item in value:
            slugs.extend(iter_reference_slugs(item))
        return slugs
    return []


def build_reference_graph(wiki: Wiki) -> tuple[list[tuple], dict[tuple, set[tuple]], dict[tuple, set[tuple]]]:
    """Resolve every frontmatter reference into a typed edge.

    Returns the edge list and an inbound index. The wiki stores relationships
    as slugs scattered across four folders with no derived graph, so questions
    like "what points at this organization?" otherwise require re-deriving
    this every time anyone asks.
    """
    edges: list[tuple] = []
    inbound: dict[tuple, set[tuple]] = defaultdict(set)
    outbound: dict[tuple, set[tuple]] = defaultdict(set)

    for (folder, slug), entity in wiki.entities.items():
        for field, candidates in wiki.model.ref_fields.get(folder, {}).items():
            for target_slug in iter_reference_slugs(entity.frontmatter.get(field)):
                source = (folder, slug)
                # First candidate that actually holds the slug wins. Where a
                # field names several folders, a reference is resolved if ANY
                # of them has it -- an opportunity that converted to a project
                # keeps its slug, and pointers written before the move must not
                # break after it.
                resolved = next((c for c in candidates
                                 if (c, target_slug) in wiki.entities), None)
                target = (resolved or candidates[0], target_slug)
                edges.append((source, field, target, candidates))
                if resolved:
                    inbound[target].add(source)
                    outbound[source].add(target)

    return edges, inbound, outbound


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_model(wiki: Wiki) -> list[Issue]:
    """That the model in force is the one the brain meant.

    Fixing the hardcoded folder lists without this leaves the next mismatch
    exactly as invisible as the last one: a folder the validator cannot see is
    reported as nothing at all, which reads identically to a folder with no
    problems.
    """
    issues: list[Issue] = []

    if wiki.model_error:
        issues.append(Issue("error", "unreadable-schema", "SCHEMA.yml", wiki.model_error))

    if not wiki.model.from_schema:
        return issues   # nothing was declared, so there is nothing to disagree with

    for folder in sorted(wiki.model.folders):
        if not (wiki.root / folder).is_dir():
            issues.append(Issue(
                "warn", "missing-declared-folder", "SCHEMA.yml",
                f"declares entity folder `{folder}/`, which does not exist",
            ))

    # A folder holding entity files that the model does not name is validated by
    # nothing -- the case that made this whole item necessary.
    for child in sorted(wiki.root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in wiki.model.folders or child.name in NON_ENTITY_DIRS:
            continue
        if any(f.name.lower() != "readme.md" for f in child.glob("*.md")):
            issues.append(Issue(
                "warn", "undeclared-folder", child.name,
                f"holds .md files but `{child.name}` is not declared in SCHEMA.yml, "
                "so nothing here is validated",
            ))

    # A field pointed at a master that resolves to no terms is a check that
    # passes because it never ran.
    if wiki.vocabularies:
        for folder, fields in sorted(wiki.model.vocab_fields.items()):
            for field, master in sorted(fields.items()):
                if not wiki.vocabularies.get(master):
                    issues.append(Issue(
                        "warn", "unresolvable-vocab", "SCHEMA.yml",
                        f"`{folder}.{field}` draws from master `{master}`, "
                        "which MASTERS.md does not define -- the check cannot run",
                    ))
        for edge_field, master in sorted(wiki.model.role_masters.items()):
            if not wiki.vocabularies.get(master):
                issues.append(Issue(
                    "warn", "unresolvable-vocab", "SCHEMA.yml",
                    f"role master `{master}` for the `{edge_field}` edge is not "
                    "defined in MASTERS.md -- the check cannot run",
                ))
    return issues


def check_source_trust(wiki: Wiki) -> list[Issue]:
    """That the trust declarations are ones a machine can act on safely.

    Every failure here is a configuration error that would otherwise show up as
    silence: a field quietly left advisory, a check quietly not run, or -- the
    one that matters -- a machine quietly arbitrating between two declared
    authorities, which is the arbitration this whole design refuses.
    """
    issues: list[Issue] = []
    rel = "SOURCES.md"

    for problem in wiki.trust_problems:
        issues.append(Issue("error", "unreadable-trust-block", rel, problem))

    # A block declaring a source the registry never registered cannot be acted
    # on: the id is what appears in an entity's `sources:` block, so a trust
    # rule keyed to an unknown one is a rule for nothing.
    for source_id in sorted(wiki.trust.declared_ids - wiki.source_ids):
        issues.append(Issue(
            "error", "trust-block-without-source", rel,
            f"a trust block declares `{source_id}`, which has no `### \u0060{source_id}\u0060` entry",
        ))

    authorities: dict[str, list[str]] = defaultdict(list)
    for source_id, folders in sorted(wiki.trust.fields.items()):
        for folder, fields in sorted(folders.items()):
            for field, spec in sorted(fields.items()):
                level = spec.get("trust", DEFAULT_TRUST)
                key = f"{folder}.{field}"

                if level not in TRUST_LEVELS:
                    issues.append(Issue(
                        "error", "unknown-trust-level", rel,
                        f"`{source_id}` claims `{level}` for {key}; "
                        f"expected one of {', '.join(TRUST_LEVELS)}",
                    ))
                    continue

                if level == "authoritative":
                    authorities[key].append(source_id)

                # Absence is not permission. A field no system may fill is not
                # a field a system may fill, whatever a source claims.
                if level not in ("advisory", "unusable") and key in wiki.trust.never_automated:
                    issues.append(Issue(
                        "error", "authority-over-manual-field", rel,
                        f"`{source_id}` claims `{level}` for {key}, which is "
                        "listed under never_automated",
                    ))

                # A source that may write a value has to say which of its own
                # attributes carries it, or nothing can read it.
                if level in ("authoritative", "corroborating") and not spec.get("from"):
                    issues.append(Issue(
                        "warn", "unmapped-trust-field", rel,
                        f"`{source_id}` is {level} for {key} but names no `from:` "
                        "attribute, so the value cannot be fetched",
                    ))

                # The defect that produced this check: a source declared
                # authoritative for a field drawing from a controlled list,
                # with nothing translating its vocabulary into that list. The
                # comparison then disagrees on every record, and the fill
                # writes a value the vocabulary check will reject.
                if (level in ("authoritative", "corroborating")
                        and wiki.model.vocab_fields.get(folder, {}).get(field)
                        and not isinstance(spec.get("values"), dict)):
                    issues.append(Issue(
                        "warn", "untranslated-vocabulary", rel,
                        f"`{source_id}` is {level} for {key}, which draws from "
                        f"master {wiki.model.vocab_fields[folder][field]}, but "
                        "declares no `values:` mapping from its own vocabulary",
                    ))

                declared = wiki.model.declared_fields.get(folder)
                if declared and field not in declared:
                    issues.append(Issue(
                        "warn", "undeclared-trust-field", rel,
                        f"`{source_id}` declares trust for {key}, which SCHEMA.yml "
                        "does not list for that entity type",
                    ))

    for key, sources in sorted(authorities.items()):
        if len(sources) > 1:
            issues.append(Issue(
                "error", "competing-authority", rel,
                f"{key} is claimed `authoritative` by {', '.join(sorted(sources))}; "
                "a machine must not pick between declared authorities",
            ))
    return issues



# --------------------------------------------------------------------------
# Declared rules
# --------------------------------------------------------------------------
#
# The product ships NO rules. It ships the means to declare them.
#
# Every rule worth writing so far has been about a field that exists only
# because some brain's SCHEMA.yml declared it -- `converted_to`, `engagements`,
# `review_interval`. A rule about a field is a claim that the field exists, so
# a rule hardcoded here is one organization's model compiled into a product
# sold to organizations with their own. That is the mistake the v1->v2
# migration exists to undo, and it is easy to make twice, because rules of this
# kind read as generic hygiene rather than as somebody's schema.
#
# So rules travel with the schema that declares their fields. Which removes the
# judgement entirely: never ask whether a rule is generic, ask which schema
# declares the field, and put the rule beside it.
#
# Declarative on purpose. A plugin loading Python from a brain would let a data
# repository run code inside a tool that runs on laptops and in CI, which the
# product could neither test nor diagnose. Every shape below was taken from a
# rule somebody had already written by hand, not invented here.


def rule_values(value) -> list:
    """A field's values, list-shaped or not, so a rule reads the same either way."""
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def rule_present(value) -> bool:
    """Present means it says something; blank, empty list and absent do not.

    The wiki is full of fields left blank deliberately -- blank beats plausible
    -- and treating an intentional blank as an answer would fire the rule on
    exactly the entries doing the right thing.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def rule_applies(when, fm: dict) -> bool:
    """Whether a rule's condition holds. No condition means it always applies."""
    if not isinstance(when, dict):
        return True
    value = fm.get(when.get("field"))
    if "present" in when:
        return rule_present(value) is bool(when["present"])
    if "equals" in when:
        return str(when["equals"]) in [str(v) for v in rule_values(value)]
    if "in" in when:
        wanted = {str(v) for v in rule_values(when["in"])}
        return bool(wanted & {str(v) for v in rule_values(value)})
    return True


def rule_scopes(entity: "Entity", rule: dict) -> list[tuple[str, dict]]:
    """The mappings a rule applies to: the entity, or each entry of a list field.

    `each:` is what lets a rule reach inside a list of mappings. Half the rules
    written by hand are about list entries rather than about the entity, and
    without this every one of them would need its own Python.

    Returns the scopes and, separately, any entry that could not be one.
    """
    each = rule.get("each")
    if not each:
        return [("", entity.frontmatter)], []
    scoped, malformed = [], []
    for i, item in enumerate(entity.frontmatter.get(each) or []):
        if isinstance(item, dict):
            scoped.append((f"{each}[{i}].", item))
        else:
            # Naming a field in `each:` says its entries are mappings. One that
            # is not gets skipped by every rule aimed at it -- so the entry with
            # the most wrong with it is the one nothing reports, which is the
            # silence this file exists to refuse.
            malformed.append(f"{each}[{i}] is not a mapping, so no rule aimed "
                             "inside it can be applied")
    return scoped, malformed


def check_declared_rules(wiki: "Wiki", entity: "Entity", rel: str) -> list[Issue]:
    """Constraints a brain declared in SCHEMA.yml, applied to one entity."""
    issues: list[Issue] = []
    unreadable: list[str] = []
    for rule in wiki.model.rules:
        applies = rule.get("applies_to")
        if applies and entity.folder not in rule_values(applies):
            continue
        severity = rule.get("severity", "warn")
        if severity not in RULE_SEVERITIES:
            severity = "warn"
        code = str(rule.get("id") or "declared-rule")
        scopes, malformed = rule_scopes(entity, rule)
        # Reported once per field however many rules point inside it, because
        # the same broken entry described five times is how a report stops
        # being read.
        for problem in malformed:
            if problem not in unreadable:
                unreadable.append(problem)
        for prefix, fm in scopes:
            if not rule_applies(rule.get("when"), fm):
                continue
            for problem in apply_rule(rule, fm, entity):
                issues.append(Issue(severity, code, rel,
                                    f"{prefix}{problem}" if prefix else problem))

    issues.extend(Issue("error", "malformed-list-entry", rel, problem)
                  for problem in unreadable)
    return issues


def apply_rule(rule: dict, fm: dict, entity: "Entity") -> list[str]:
    """What one rule finds wrong with one mapping, as messages."""
    said = rule.get("message")
    out: list[str] = []

    for field in rule_values(rule.get("require")):
        if not rule_present(fm.get(field)):
            out.append(said or f"`{field}` is required here but is blank")

    for field in rule_values(rule.get("forbid")):
        if rule_present(fm.get(field)):
            out.append(said or f"`{field}` must not be set here")

    one_of = rule.get("one_of")
    if isinstance(one_of, dict):
        allowed = {str(v) for v in rule_values(one_of.get("values"))}
        for v in rule_values(fm.get(one_of.get("field"))):
            if str(v) not in allowed:
                out.append(said or f"`{one_of.get('field')}` is {v!r}, not one of "
                                   f"{', '.join(sorted(allowed))}")

    pattern = rule.get("pattern")
    if isinstance(pattern, dict):
        field, expr = pattern.get("field"), str(pattern.get("matches"))
        try:
            compiled = re.compile(expr)
        except re.error as exc:
            # A rule that will not compile is a refusal, never a pass: staying
            # silent would read exactly like a rule that found nothing wrong.
            out.append(f"rule pattern `{expr}` will not compile ({exc})")
        else:
            for v in rule_values(fm.get(field)):
                # A blank is not a badly-shaped value, it is an absent one --
                # and absent is `require`'s question, not this one's. Without
                # this, a shape rule fires on every entry that correctly left
                # the field alone: 25 of them, the first time this ran.
                if not rule_present(v):
                    continue
                if not compiled.fullmatch(str(v)):
                    out.append(said or
                               f"`{field}` is {v!r}, which is not shaped like {expr}")

    # An entity pointing at itself. The slug comes from the entity rather than
    # the mapping, so this is the one shape `each:` cannot express.
    for field in rule_values(rule.get("not_self")):
        for v in rule_values(fm.get(field)):
            if str(v).rsplit("/", 1)[-1] == entity.slug:
                out.append(said or f"`{field}` points at this entry itself")

    unique = rule.get("unique")
    if isinstance(unique, dict):
        seen: set[str] = set()
        for item in rule_values(entity.frontmatter.get(unique.get("field"))):
            key = item.get(unique.get("by")) if isinstance(item, dict) else item
            if key is None:
                continue
            if str(key) in seen:
                out.append(said or f"`{unique.get('by')}` {str(key)!r} appears "
                                   "more than once")
            seen.add(str(key))

    stale = rule.get("stale_after")
    if isinstance(stale, dict):
        out.extend(rule_staleness(stale, fm, said))

    return out


def rule_staleness(stale: dict, fm: dict, said: str | None) -> list[str]:
    """A date plus an interval, measured against today.

    Kept a primitive rather than left to each brain, because "is this overdue"
    is arithmetic and the one thing this project has learned twice over is to
    spend tokens on judgement and never on arithmetic.
    """
    raw = fm.get(stale.get("date"))
    if not rule_present(raw):
        return []
    # No interval named means the date IS the deadline -- an expiry rather than
    # a review cadence. Both are "is this date behind us", and splitting them
    # into two primitives would have each brain pick the wrong one.
    field = stale.get("interval_months")
    months = fm.get(field) if field else 0
    if field and not rule_present(months):
        return []
    try:
        when = date.fromisoformat(str(raw)[:10])
        interval = int(months or 0)
    except (ValueError, TypeError):
        return []          # shape is `pattern:`'s job, not this one's
    if interval < 0:
        return []
    due_month = when.month - 1 + interval
    try:
        due = when.replace(year=when.year + due_month // 12,
                           month=due_month % 12 + 1)
    except ValueError:                       # 31st landing in a shorter month
        due = when.replace(year=when.year + due_month // 12,
                           month=due_month % 12 + 1, day=28)
    if due >= date.today():
        return []
    if not interval:
        return [said or f"`{stale.get('date')}` {when.isoformat()} is in the past"]
    return [said or f"last {stale.get('date')} {when.isoformat()}, every "
                    f"{interval} month(s) — due {due.isoformat()}"]


def check_rule_declarations(wiki: Wiki) -> list[Issue]:
    """That the rules a brain declared are ones this can actually apply.

    Every way a rule can be wrong is silent: it matches nothing and reports
    nothing, which is indistinguishable from a rule that found no problems.
    That is the failure this whole file exists to refuse, so the declarations
    are checked as strictly as the data.
    """
    issues: list[Issue] = []
    rel = "SCHEMA.yml"
    actions = ("require", "forbid", "one_of", "pattern", "not_self", "unique",
               "stale_after")
    seen: set[str] = set()

    for i, rule in enumerate(wiki.model.rules):
        rule_id = str(rule.get("id") or "")
        where = f"rule `{rule_id}`" if rule_id else f"rule {i}"

        if not rule_id:
            issues.append(Issue("error", "rule-without-id", rel,
                                f"{where} has no `id` — the id is the code a "
                                "reader greps for when the warning fires"))
        elif rule_id in seen:
            issues.append(Issue("error", "duplicate-rule-id", rel,
                                f"{where} is declared more than once"))
        seen.add(rule_id)

        if not any(a in rule for a in actions):
            issues.append(Issue("error", "rule-without-effect", rel,
                                f"{where} declares no constraint — one of "
                                f"{', '.join(actions)} is required"))

        severity = rule.get("severity", "warn")
        if severity not in RULE_SEVERITIES:
            issues.append(Issue("error", "unknown-rule-severity", rel,
                                f"{where} claims severity `{severity}`; expected "
                                f"{' or '.join(RULE_SEVERITIES)}"))

        pattern = rule.get("pattern")
        if isinstance(pattern, dict):
            try:
                re.compile(str(pattern.get("matches")))
            except re.error as exc:
                issues.append(Issue("error", "unreadable-rule-pattern", rel,
                                    f"{where} pattern will not compile: {exc}"))

        # A rule naming a folder the brain does not have matches nothing, and
        # a typo here looks exactly like a rule with nothing to report.
        for folder in rule_values(rule.get("applies_to")):
            if wiki.model.from_schema and str(folder) not in wiki.model.folders:
                issues.append(Issue("warn", "rule-for-undeclared-folder", rel,
                                    f"{where} applies_to `{folder}`, which "
                                    "SCHEMA.yml does not declare"))

        # Likewise a field nothing declares: the rule is either guarding a
        # field that was renamed, or guarding a typo.
        declared: set[str] = set()
        for folder in (rule_values(rule.get("applies_to"))
                       or list(wiki.model.folders)):
            declared |= wiki.model.declared_fields.get(str(folder), set())
        if declared and not rule.get("each"):
            named = [rule.get("when", {}).get("field")] if isinstance(
                rule.get("when"), dict) else []
            named += rule_values(rule.get("require")) + rule_values(rule.get("forbid"))
            named += rule_values(rule.get("not_self"))
            for field in [f for f in named if f]:
                if str(field) not in declared:
                    issues.append(Issue("warn", "rule-for-undeclared-field", rel,
                                        f"{where} names `{field}`, which "
                                        "SCHEMA.yml does not declare"))
    return issues


def check_wiki(wiki: Wiki) -> list[Issue]:
    issues: list[Issue] = (check_model(wiki) + check_source_trust(wiki)
                          + check_rule_declarations(wiki))
    edges, inbound, outbound = build_reference_graph(wiki)

    for (folder, slug), entity in sorted(wiki.entities.items()):
        rel = str(entity.path.relative_to(wiki.root))

        if entity.broken:
            issues.append(Issue("error", "unreadable-frontmatter", rel, entity.broken))
            continue

        fm = entity.frontmatter

        # A tombstone: the entity is gone but the file could not be removed,
        # which happens on surfaces where the delete operation is gated. It is
        # exempt from the entity checks -- it no longer describes an entity --
        # but it must say what replaced it, or it is just a broken file.
        if fm.get("deprecated"):
            if not fm.get("superseded_by"):
                issues.append(Issue(
                    "warn", "tombstone-without-successor", rel,
                    "marked deprecated but does not say what supersedes it",
                ))
            continue

        for required in REQUIRED_FIELDS:
            if fm.get(required) in (None, ""):
                issues.append(
                    Issue("error", "missing-required-field", rel, f"`{required}` is missing or empty")
                )

        expected_type = wiki.model.folders[folder]
        actual_type = fm.get("type")
        if actual_type and actual_type != expected_type:
            issues.append(
                Issue(
                    "error",
                    "type-folder-mismatch",
                    rel,
                    f"type is `{actual_type}` but the file sits in {folder}/ (expects `{expected_type}`)",
                )
            )

        issues.extend(check_schema_version(wiki, entity, rel))
        issues.extend(check_declared_fields(wiki, entity, rel))
        issues.extend(check_provenance(entity, rel))
        issues.extend(check_affiliations(entity, rel))
        issues.extend(check_disclosure(entity, rel))
        issues.extend(check_delivery_shape(entity, rel))
        issues.extend(check_external_refs(wiki, entity, rel))
        issues.extend(check_vocabulary(wiki, entity, rel))
        issues.extend(check_body_links(wiki, entity, rel))
        issues.extend(check_declared_rules(wiki, entity, rel))

    issues.extend(check_dangling_references(wiki, edges))
    issues.extend(check_orphans(wiki, inbound, outbound))
    return issues


def check_schema_version(wiki: Wiki, entity: Entity, rel: str) -> list[Issue]:
    if wiki.schema_version is None:
        return []
    version = entity.frontmatter.get("schema_version")
    if not isinstance(version, int):
        return []
    if version < wiki.schema_version:
        return [
            Issue(
                "warn",
                "stale-schema-version",
                rel,
                f"at schema v{version}; the wiki is on v{wiki.schema_version}",
            )
        ]
    if version > wiki.schema_version:
        return [
            Issue(
                "error",
                "schema-version-ahead",
                rel,
                f"claims schema v{version}, ahead of CONVENTIONS.md (v{wiki.schema_version})",
            )
        ]
    return []


def check_declared_fields(wiki: Wiki, entity: Entity, rel: str) -> list[Issue]:
    """Fields on a file that the brain's own SCHEMA.yml does not declare.

    Only runs where a brain has a SCHEMA.yml; a brain without one has not said
    what its fields are, and inventing an opinion about that would flag correct
    data. Where one exists, a field nobody declared is usually a leftover from
    an older shape or a hand-edit — small, quiet, and invisible until something
    tries to render it.

    A warning rather than an error on purpose: an undeclared field is a
    question about the model, not a broken file, and the answer is as often
    "declare it" as "delete it".
    """
    declared = wiki.declared_fields.get(entity.folder)
    if not declared:
        return []
    return [
        Issue("warn", "undeclared-field", rel,
              f"`{field}` is not declared for {entity.folder} in SCHEMA.yml")
        for field in entity.frontmatter
        if field not in declared
    ]


def read_declared_fields(root: Path) -> dict[str, set[str]]:
    """{folder: {field, …}} from SCHEMA.yml, or {} where the brain has none."""
    path = root / "SCHEMA.yml"
    if not path.is_file():
        return {}
    try:
        schema = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    out: dict[str, set[str]] = {}
    for spec in (schema.get("entities") or {}).values():
        if isinstance(spec, dict) and spec.get("folder") and isinstance(spec.get("fields"), dict):
            out[spec["folder"]] = set(spec["fields"])
    return out


def read_external_ref_prefixes(root: Path) -> set[str]:
    """Reference prefixes SCHEMA.yml declares as resolving in another brain.

    A brain built to extend another writes references shaped
    `<prefix>:<folder>/<slug>` (optionally `#<fragment>`) into its ref fields,
    and declares each prefix under `external_refs_schema` so the string is a
    reference with a stated resolution rather than a private convention --
    docs/schema-format-extensions.md has the format. This validator cannot
    resolve them, because the target lives in a repository it cannot see; that
    makes them another repo's problem to check, not this one's error.

    Empty where SCHEMA.yml is absent or declares none, in which case a
    prefixed value is treated like any other slug -- "looks external" is not
    the same as "is declared external", and a mistyped slug with a colon in it
    must not slip past the dangling check.
    """
    path = root / "SCHEMA.yml"
    if not path.is_file():
        return set()
    try:
        schema = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set()
    declared = schema.get("external_refs_schema")
    return {str(prefix) for prefix in declared} if isinstance(declared, dict) else set()


def check_provenance(entity: Entity, rel: str) -> list[Issue]:
    """`method` says how a value was obtained; an unknown one records nothing."""
    issues: list[Issue] = []
    for entry in entity.frontmatter.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        method = entry.get("method")
        if method is not None and method not in PROVENANCE_METHODS:
            issues.append(Issue(
                "warn", "unknown-provenance-method", rel,
                f"`method: {method}` is not one of {', '.join(sorted(PROVENANCE_METHODS))}",
            ))
        # `source` is the default model's spelling; a brain declaring its own
        # model may spell the same field `system` (SCHEMA.yml: `{system, method,
        # date}`). Insisting on one produced 76 warnings against a wiki whose
        # provenance was complete, which teaches people to ignore the validator.
        if not (entry.get("source") or entry.get("system")):
            issues.append(Issue("warn", "provenance-without-source", rel,
                                "a sources entry names no source (no `source:` or `system:`)"))
    return issues


def check_affiliations(entity: Entity, rel: str) -> list[Issue]:
    """A person's affiliations must not contradict themselves.

    Getting these wrong is quiet and expensive: a stale `current` makes every
    project that person touched describe them by the wrong organization.
    """
    if entity.folder != "people":
        return []
    issues: list[Issue] = []
    for entry in entity.frontmatter.get("affiliations") or []:
        if not isinstance(entry, dict):
            continue
        status, ended = entry.get("status"), entry.get("to")
        org = entry.get("organization", "?")
        if status not in (None, "current", "past"):
            issues.append(Issue("warn", "invalid-affiliation-status", rel,
                                f"`{org}` has status `{status}`; expected current or past"))
        if status == "current" and ended:
            issues.append(Issue("warn", "affiliation-contradiction", rel,
                                f"`{org}` is marked current but has an end date of {ended}"))
        if status == "past" and not ended:
            issues.append(Issue("warn", "past-affiliation-undated", rel,
                                f"`{org}` is marked past with no end date — when did it end?"))
    return issues


def check_external_refs(wiki: Wiki, entity: Entity, rel: str) -> list[Issue]:
    """Pointers into other systems.

    A reference naming a store nobody has registered cannot be followed and
    cannot be refreshed -- it looks like a link while being a dead string. The
    store must be a source id from SOURCES.md, which is the same registry that
    says how to reach that system.
    """
    issues: list[Issue] = []
    known = wiki.source_ids
    for ref in entity.frontmatter.get("external_refs") or []:
        if not isinstance(ref, dict):
            issues.append(Issue("warn", "malformed-external-ref", rel,
                                "an external_refs entry is not a mapping"))
            continue
        store, record = ref.get("store"), ref.get("id")
        if not store or not record:
            issues.append(Issue("warn", "incomplete-external-ref", rel,
                                f"external ref needs both a store and an id (got {store!r}, {record!r})"))
            continue
        if known and store not in known:
            issues.append(Issue("warn", "unknown-external-store", rel,
                                f"`{store}` is not a source registered in SOURCES.md"))
        if not ref.get("verified"):
            issues.append(Issue("warn", "external-ref-undated", rel,
                                f"reference to {store} has no verified date"))
    return issues


def check_disclosure(entity: Entity, rel: str) -> list[Issue]:
    """Permission, metrics and quotes — the fields that reach the outside world.

    An absent block is fine and means not cleared. A malformed one is not: it
    looks like a decision while carrying none, and these are the fields that
    decide what gets said publicly about a real client.
    """
    issues: list[Issue] = []
    fm = entity.frontmatter

    for block, field, allowed, label in (
        (fm.get("disclosure"), "level", DISCLOSURE_LEVELS, "disclosure"),
        (fm.get("case_study"), "status", CASE_STUDY_STATUSES, "case_study"),
    ):
        if isinstance(block, dict):
            value = block.get(field)
            if value is not None and value not in allowed:
                issues.append(Issue("warn", f"invalid-{label}-{field}", rel,
                                    f"`{value}` is not one of {', '.join(sorted(allowed))}"))

    disclosure = fm.get("disclosure")
    if isinstance(disclosure, dict) and disclosure.get("level"):
        if not disclosure.get("cleared_by"):
            issues.append(Issue("warn", "clearance-unattributed", rel,
                                "disclosure names a level but nobody cleared it"))

    attribution = fm.get("customer_attribution")
    if isinstance(attribution, dict) and attribution.get("may_name") and not attribution.get("cleared_by"):
        issues.append(Issue("warn", "clearance-unattributed", rel,
                            "customer_attribution permits naming but nobody cleared it"))

    for metric in fm.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        if metric.get("kind") not in (None, *METRIC_KINDS):
            issues.append(Issue("warn", "invalid-metric-kind", rel,
                                f"`{metric.get('kind')}` is not baseline or outcome"))
        if metric.get("value") is not None and not metric.get("as_of"):
            issues.append(Issue("warn", "metric-undated", rel,
                                f"`{metric.get('metric', '?')}` has a value but no as_of date"))

    for quote in fm.get("quotes") or []:
        if isinstance(quote, dict) and quote.get("cleared_for_public") and not quote.get("attribution"):
            issues.append(Issue("warn", "quote-cleared-unattributed", rel,
                                "a quote is cleared for public use but has no attribution"))
    return issues


def check_delivery_shape(entity: Entity, rel: str) -> list[Issue]:
    """Whether the roles on a project agree with how it was delivered.

    Work fronted by a partner has a distinct shape: the fronting organization
    holds the contract, and the organization the work was actually for holds
    none. Recording the latter as `customer` overstates the relationship, and
    makes "who are our customers" return organizations never contracted.
    """
    if entity.folder != "projects":
        return []

    roles = {
        org.get("role")
        for org in entity.frontmatter.get("organizations") or []
        if isinstance(org, dict)
    }
    delivered_via = entity.frontmatter.get("delivered_via")
    issues: list[Issue] = []

    # `delivered_via` records who held the client relationship, not whether a
    # partner touched the work. Contracting directly while a partner
    # participates in delivery is a normal, common shape — only an end_client
    # implies somebody else fronted the engagement.
    if "end_client" in roles and delivered_via == "Direct":
        issues.append(Issue(
            "warn", "delivery-shape-mismatch", rel,
            "names an end_client but is marked delivered_via Direct — "
            "an end_client means a partner held the client relationship",
        ))
    if delivered_via == "Partner" and not ({"end_client", "delivery_partner"} & roles):
        issues.append(Issue(
            "warn", "partner-delivery-unattributed", rel,
            "delivered_via is Partner but no partner is named",
        ))
    if "end_client" in roles and "customer" not in roles:
        issues.append(Issue(
            "warn", "end-client-without-customer", rel,
            "an end_client is named but nobody is recorded as the contracting customer",
        ))
    return issues


def roles_on(edge: dict) -> list[str]:
    """Roles on one edge, in either spelling.

    v9 renamed `role:` to `roles: []`; both are still in the wild and the
    retire-over-two-versions rule says read both until nothing writes the old
    one.
    """
    out: list[str] = []
    single = edge.get("role")
    if isinstance(single, str) and single.strip():
        out.append(single.strip())
    many = edge.get("roles")
    if isinstance(many, list):
        out += [r.strip() for r in many if isinstance(r, str) and r.strip()]
    return out


def check_vocabulary(wiki: Wiki, entity: Entity, rel: str) -> list[Issue]:
    if not wiki.vocabularies:
        return []

    issues: list[Issue] = []
    fields = dict(wiki.model.vocab_fields.get(entity.folder, {}))

    for field, master in fields.items():
        allowed = wiki.vocabularies.get(master)
        if not allowed:
            continue
        value = entity.frontmatter.get(field)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, str) or not item.strip():
                continue  # blank is honest, and explicitly allowed
            if item.strip() not in allowed:
                issues.append(
                    Issue(
                        "warn",
                        "vocabulary-violation",
                        rel,
                        f"`{field}: {item}` is not in MASTERS.md master {master}",
                    )
                )

    if entity.folder == "projects":
        # Master 5 catalogues far more accelerators than have their own entity
        # file, so a value is valid as either a slug or a catalogue term.
        # Requiring an entity for each would tax recording the fact at all.
        # Master 5's terms live under its subsections (5a-5h); the bare "5" key
        # holds only the subsection ids. Union them.
        catalogue = {
            term
            for key, terms in wiki.vocabularies.items()
            if key == "5" or key.startswith("5")
            for term in terms
        }
        for item in entity.frontmatter.get("products_leveraged") or []:
            if not isinstance(item, str) or not item.strip():
                continue
            value = item.strip()
            if ("products", value) in wiki.entities:
                continue
            if catalogue and value not in catalogue:
                issues.append(Issue(
                    "warn", "unknown-product-leveraged", rel,
                    f"`{value}` is neither a products/ entry nor a Master 5 term",
                ))

        # One master per edge field, as the brain declares them. The product
        # only ever checked the organizations edge; a brain that also declares
        # one for people gets people[].role checked too.
        for edge_field, master in wiki.model.role_masters.items():
            allowed = wiki.vocabularies.get(master)
            if not allowed:
                continue
            for edge in entity.frontmatter.get(edge_field) or []:
                if not isinstance(edge, dict):
                    continue
                for role in roles_on(edge):
                    if role not in allowed:
                        issues.append(
                            Issue(
                                "warn",
                                "vocabulary-violation",
                                rel,
                                f"{edge_field} role `{role}` is not in MASTERS.md master {master}",
                            )
                        )
    return issues


def check_body_links(wiki: Wiki, entity: Entity, rel: str) -> list[Issue]:
    """Relative links in prose are how a human navigates; broken ones rot quietly."""
    issues: list[Issue] = []
    seen: set[str] = set()
    for match in WIKI_LINK_RE.finditer(entity.body):
        href = match.group(1).split("#")[0].strip()
        if not href or href.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if not href.endswith(".md") or href in seen:
            continue
        seen.add(href)
        target = (entity.path.parent / href).resolve()
        if not target.exists():
            issues.append(Issue("warn", "dangling-body-link", rel, f"link to `{href}` resolves to nothing"))
    return issues


def check_dangling_references(wiki: Wiki, edges: list[tuple]) -> list[Issue]:
    issues: list[Issue] = []
    # One warning per unresolved employer, not per stint. Somebody with seven
    # roles at the same company is one thing to look at, not seven.
    reported_external: set[tuple[str, str]] = set()
    for (folder, slug), field, target, candidates in edges:
        if field in SOFT_REFERENCE_FIELDS:
            continue
        if target not in wiki.entities:
            # A declared external prefix resolves in another repository, which
            # this validator cannot see. Skip rather than warn: on a brain
            # built to extend another, these are most of the references, and a
            # warning that fires on almost everything is not a warning.
            value = target[1]
            if ":" in value and value.split(":", 1)[0] in wiki.external_ref_prefixes:
                continue
            source_path = str(wiki.entities[(folder, slug)].path.relative_to(wiki.root))
            if (folder, field) in wiki.model.external_edges:
                if (source_path, target[1]) in reported_external:
                    continue
                reported_external.add((source_path, target[1]))
                issues.append(
                    Issue(
                        "warn",
                        "external-affiliation",
                        source_path,
                        f"`{field}` names {target[1]}, which has no entity — "
                        "expected for a past employer, a typo otherwise",
                    )
                )
                continue
            # Name every folder that was tried. "does not exist in projects"
            # sends somebody looking in one place when the field legitimately
            # resolves in two.
            where = (f"{candidates[0]}/{target[1]}.md" if len(candidates) == 1
                     else f"{target[1]}.md in any of "
                          + ", ".join(f"{c}/" for c in candidates))
            issues.append(
                Issue(
                    "error",
                    "dangling-reference",
                    source_path,
                    f"`{field}` points at {where}, which does not exist",
                )
            )
    return issues


def check_orphans(wiki: Wiki, inbound: dict[tuple, set[tuple]], outbound: dict[tuple, set[tuple]]) -> list[Issue]:
    """Entities nothing points at.

    Not automatically wrong -- an organization can be logged deliberately
    before any project with it exists -- but it is the shape a half-finished
    entry takes, so it is worth surfacing rather than discovering months later.
    Projects are excluded: they are the root of the graph and are not expected
    to have anything pointing at them.
    """
    issues: list[Issue] = []
    # A folder nothing can point at is a catalogue, not a graph node: "nothing
    # references this" is its normal state rather than a half-finished entry.
    # Derived from the model, because the alternative -- flagging every
    # certification and every glossary term -- is a warning that fires on
    # almost everything, which this project has already paid for twice.
    referenceable = {t for fields in wiki.model.ref_fields.values()
                     for targets in fields.values() for t in targets}
    for (folder, slug), entity in sorted(wiki.entities.items()):
        if folder in wiki.model.orphan_exempt or entity.broken:
            continue
        if folder not in referenceable:
            continue
        # An entity that states its own connections is part of the graph, even
        # if nothing points back at it. This was a people-only exception --
        # since v3 put affiliations on the person, that edge IS the
        # relationship, and requiring a return link would store it twice and
        # let the two disagree. The rule was always general, though: an orphan
        # is something nothing connects in EITHER direction, and an entry that
        # names its own edges is not the half-finished thing this check looks
        # for. Generalising it removes a special case rather than adding one.
        #
        # It also matters more since folders beyond the default four are
        # checked: an opportunity is a pipeline record that legitimately has
        # nothing pointing at it, and flagging every one of them is how a
        # warning stops being read.
        if folder not in wiki.model.orphan_needs_inbound and outbound.get((folder, slug)):
            continue
        if not inbound.get((folder, slug)):
            issues.append(
                Issue(
                    "warn",
                    "orphan-entity",
                    str(entity.path.relative_to(wiki.root)),
                    "nothing in the wiki references this entity",
                )
            )
    return issues


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def field_coverage(wiki: Wiki) -> dict[str, dict[str, str]]:
    """How completely each entity type is filled in.

    Blank fields are legitimate -- the schema prefers a gap to a guess -- so
    this is a profile to read, not a list of faults.
    """
    profile: dict[str, dict[str, str]] = {}
    for folder in wiki.model.folders:
        entities = [e for e in wiki.by_folder(folder) if not e.broken]
        if not entities:
            continue
        counts: Counter = Counter()
        keys: set[str] = set()
        for entity in entities:
            keys.update(entity.frontmatter.keys())
            for key, value in entity.frontmatter.items():
                if value in (None, "", [], {}):
                    counts[key] += 1
        total = len(entities)
        profile[folder] = {
            key: f"{counts.get(key, 0)}/{total} blank ({round(100 * counts.get(key, 0) / total)}%)"
            for key in sorted(keys)
            if counts.get(key)
        }
    return profile


def render(wiki: Wiki, issues: list[Issue], stats: bool) -> str:
    lines: list[str] = []
    entities = [e for e in wiki.entities.values() if not e.broken]
    lines.append(
        f"{len(wiki.entities)} entities across {len(wiki.model.folders)} folders"
        + f" ({wiki.model.source})"
        + (f" · schema v{wiki.schema_version}" if wiki.schema_version else "")
        + (f" · {len(wiki.vocabularies)} vocabularies loaded" if wiki.vocabularies else "")
    )
    for folder in wiki.model.folders:
        count = len(wiki.by_folder(folder))
        if count:
            lines.append(f"  {folder:<16} {count}")

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warn"]

    if not issues:
        lines.append("\nNo problems found.")
    else:
        lines.append(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")

    grouped: dict[tuple[str, str], list[Issue]] = defaultdict(list)
    for issue in issues:
        grouped[(issue.severity, issue.code)].append(issue)

    for severity in ("error", "warn"):
        for (sev, code), group in sorted(grouped.items()):
            if sev != severity:
                continue
            lines.append(f"\n{sev.upper()}  {code}  ({len(group)})")
            for issue in group[:MAX_ISSUES_PER_CODE]:
                lines.append(f"  {issue.path}: {issue.message}")
            if len(group) > MAX_ISSUES_PER_CODE:
                lines.append(f"  ... and {len(group) - MAX_ISSUES_PER_CODE} more")

    if stats:
        # What the model says each type MEANS, listed first: a folder whose
        # meaning is undeclared is the one a reader -- or a model writing the
        # next rule -- will guess at.
        if wiki.model.from_schema:
            lines.append("\nWhat each entity type means (from SCHEMA.yml)")
            for folder in sorted(wiki.model.folders):
                about = wiki.model.descriptions.get(folder)
                lines.append(f"\n  {folder}")
                lines.append(f"    {about}" if about
                             else "    (no description declared — a reader has "
                                  "only the folder name to go on)")

        lines.append("\nField coverage (blank counts — gaps are allowed, not faults)")
        for folder, fields in field_coverage(wiki).items():
            lines.append(f"\n  {folder} ({len([e for e in wiki.by_folder(folder) if not e.broken])} files)")
            for name, summary in sorted(fields.items(), key=lambda kv: -int(kv[1].split('/')[0])):
                lines.append(f"    {name:<24} {summary}")
        _ = entities
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path, help="path to the wiki repository")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--stats", action="store_true", help="include a field-coverage profile")
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    args = parser.parse_args()

    if not args.repo.is_dir():
        print(f"not a directory: {args.repo}", file=sys.stderr)
        return 2

    wiki = load_wiki(args.repo)
    if not wiki.entities:
        print(f"no entity files found under {args.repo}", file=sys.stderr)
        return 2

    issues = check_wiki(wiki)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warn"]

    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": wiki.schema_version,
                    "model": {"source": wiki.model.source,
                              "folders": sorted(wiki.model.folders)},
                    "counts": {f: len(wiki.by_folder(f)) for f in wiki.model.folders},
                    "errors": len(errors),
                    "warnings": len(warnings),
                    "issues": [vars(i) for i in issues],
                    **({"coverage": field_coverage(wiki)} if args.stats else {}),
                },
                indent=2,
            )
        )
    else:
        print(render(wiki, issues, args.stats))

    if errors:
        return 1
    if warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
