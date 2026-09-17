#!/usr/bin/env python3
"""Build the static data file that the visualiser site reads.

The site in site/ is plain HTML/CSS/JS with no build step and no runtime
dependencies. All it needs is site/data.json, which this script produces by
reading SCHEMA.yml and every entity file in the wiki.

Like the validator, this script knows nothing about hackathons, teams or
ideas. It reads the entity model from SCHEMA.yml and renders whatever that
file says exists. Adding an entity type to the schema makes it appear here.

The wiki is private and the site it feeds is public, so fields that should
not leave the repo are dropped under --public. That list lives in
REDACTED_FIELDS below; the redaction happens here rather than in the workflow
so a local build can be checked against exactly what would be published.

Usage:
    build_site.py <repo>                       write <repo>/site/data.json
    build_site.py <repo> --out path/data.json  write somewhere else
    build_site.py <repo> --public              drop fields nobody should
                                               publish (work emails)
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("build_site.py needs PyYAML:  pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

NESTED_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Fields stripped from a --public build, whatever entity they are on. A brain
# declares its own in SCHEMA.yml:
#
#   publish:
#     redact: [email, phone, rate_card]
#
# `email` is a floor rather than a default: every brain seen so far carries work
# addresses somewhere, and a public page is not the place for them. A brain that
# genuinely wants them published can say so with `publish: {redact_only: [...]}`.
REDACTED_FLOOR = {"email"}


def scrub_patterns(schema: dict) -> list[re.Pattern]:
    """Extra regexes a brain wants removed from every string it publishes.

        publish:
          scrub: ['157535000\\d+']    # CRM record ids, wherever they were typed

    Field-level redaction cannot reach a value somebody wrote into a sentence,
    and on a real wiki they do: a CRM account id turned up inside a project's
    body prose, having survived every rule about which *fields* may be
    published. Patterns are the only handle on that, and declaring them is a
    brain's own call — nobody else knows what its internal identifiers look like.
    """
    publish = schema.get("publish") or {}
    return [re.compile(p) for p in (publish.get("scrub") or [])]


def redacted_fields(schema: dict) -> set[str]:
    publish = schema.get("publish") or {}
    if publish.get("redact_only") is not None:
        return set(publish["redact_only"])
    return REDACTED_FLOOR | set(publish.get("redact") or [])


# ---------------------------------------------------------------------------
# Reading the wiki
# ---------------------------------------------------------------------------

def read_frontmatter(path: Path) -> tuple[dict, str]:
    """Return (frontmatter, body). Malformed files yield ({}, raw text)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, parts[2]
    if not isinstance(data, dict):
        data = {}
    return data, parts[2]


def redact(fm: dict, body: str, fields: set[str], patterns: list = ()) -> tuple[dict, str]:
    """Strip unpublishable fields, at any depth, and scrub addresses from prose.

    Top-level stripping alone was not enough on a real wiki. Work addresses on
    this schema live at `affiliations[].email` -- inside a list, inside a
    mapping -- and a public build that removed only the top-level `email` key
    published sixty-six of them. So the walk is recursive, and a declared field
    is removed wherever it appears.

    Free text is the other half. A `sources[].ref` is written by hand and can
    quote anything, including an address, so every string in a public build is
    scrubbed regardless of which field holds it. That cannot catch every kind
    of identifier a person might type into prose -- which is why a brain whose
    provenance notes carry internal record ids should redact `sources` itself
    rather than trusting this to find them.
    """

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in fields}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            return clean_text(value)
        return value

    def clean_text(text: str) -> str:
        text = EMAIL_RE.sub("[redacted]", text)
        for pattern in patterns:
            text = pattern.sub("[redacted]", text)
        return text

    clean = scrub(fm)
    return clean, clean_text(body)


def load_entities(repo: Path, schema: dict, public: bool = False) -> dict[str, list[dict]]:
    redact_these = redacted_fields(schema)
    scrub_these = scrub_patterns(schema)
    """Every entity file in the wiki, grouped by entity type."""
    out: dict[str, list[dict]] = {}
    for name, spec in schema.get("entities", {}).items():
        folder = repo / spec["folder"]
        records: list[dict] = []
        if folder.is_dir():
            nested = spec.get("layout") == "nested"
            pattern = "*/*.md" if nested else "*.md"
            for path in sorted(folder.glob(pattern)):
                if path.name == "README.md":
                    continue
                fm, body = read_frontmatter(path)
                if public:
                    fm, body = redact(fm, body, redact_these, scrub_these)
                rel = path.relative_to(repo).as_posix()
                if nested:
                    ident = f"{path.parent.name}/{path.stem}"
                    match = NESTED_NAME_RE.match(path.stem)
                    fm.setdefault("date", match.group(1) if match else None)
                else:
                    ident = path.stem
                title, title_field = entity_title(name, ident, fm)
                records.append(
                    {
                        "id": ident,
                        "type": name,
                        "path": rel,
                        "title": title,
                        "title_field": title_field,
                        "fm": fm,
                        "body": render_markdown(body),
                    }
                )
        out[name] = records
    return out


def entity_title(kind: str, ident: str, fm: dict) -> tuple[str, str | None]:
    """The display title, and the field it came from.

    The page needs the second half: the field that became the title must not
    also be printed as the body, which is what made every feed item repeat
    itself.
    """
    for key in ("title", "name", "decision", "learning", "what_changed"):
        value = fm.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
    tail = ident.split("/")[-1]
    match = NESTED_NAME_RE.match(tail)
    if match:
        tail = match.group(2)
    return tail.replace("-", " ").title(), None


# ---------------------------------------------------------------------------
# Markdown. A deliberately small subset — enough for the prose these files
# actually contain, with no third-party renderer to keep the site dependency
# free. Everything is HTML-escaped before any tag is emitted.
# ---------------------------------------------------------------------------

INLINE_CODE_RE = re.compile(r"`([^`]+)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
ITALIC_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
BARE_URL_RE = re.compile(r"(?<![\"(>])\bhttps?://[^\s<>)\]]+")
# Filled from SCHEMA.yml by set_entity_folders() before anything is rendered.
# The folder names used to be a literal list here, which meant adding an entity
# type to the schema silently stopped its links from resolving.
FOLDER_TO_TYPE: dict[str, str] = {}
ENTITY_LINK_RE: re.Pattern | None = None


def set_entity_folders(schema: dict) -> None:
    global FOLDER_TO_TYPE, ENTITY_LINK_RE
    FOLDER_TO_TYPE = {
        spec["folder"]: name for name, spec in (schema.get("entities") or {}).items()
    }
    folders = "|".join(re.escape(f) for f in sorted(FOLDER_TO_TYPE, key=len, reverse=True))
    ENTITY_LINK_RE = re.compile(rf"^(?:\.\./)*({folders})/(.+?)\.md$") if folders else None


def rewrite_target(target: str) -> str:
    """Turn a link to another entity file into an in-app route."""
    if ENTITY_LINK_RE is None:
        return target
    match = ENTITY_LINK_RE.match(target.strip())
    if not match:
        return target
    return f"#/{FOLDER_TO_TYPE[match.group(1)]}/{match.group(2)}"


def render_inline(text: str) -> str:
    text = html.escape(text, quote=False)
    codes: list[str] = []

    def stash_code(match: re.Match) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = INLINE_CODE_RE.sub(stash_code, text)

    def link(match: re.Match) -> str:
        target = rewrite_target(match.group(2))
        external = target.startswith("http")
        attrs = ' target="_blank" rel="noopener noreferrer"' if external else ""
        return f'<a href="{html.escape(target, quote=True)}"{attrs}>{match.group(1)}</a>'

    text = LINK_RE.sub(link, text)
    # <https://example.com> — markdown's autolink form. Escaping has already
    # turned the brackets into entities by this point.
    text = re.sub(
        r"&lt;(https?://[^\s<>]+?)&gt;",
        lambda m: f'<a href="{html.escape(m.group(1), quote=True)}" target="_blank"'
        f' rel="noopener noreferrer">{m.group(1)}</a>',
        text,
    )
    text = BARE_URL_RE.sub(
        lambda m: f'<a href="{html.escape(m.group(0), quote=True)}" target="_blank"'
        f' rel="noopener noreferrer">{m.group(0)}</a>',
        text,
    )
    text = BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = ITALIC_RE.sub(r"<em>\1</em>", text)
    for index, code in enumerate(codes):
        text = text.replace(f"\x00{index}\x00", f"<code>{html.escape(code)}</code>")
    return text


def render_markdown(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None
    pending_blank = False
    in_code = False
    code: list[str] = []
    table: list[list[str]] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(f"<p>{render_inline(' '.join(para))}</p>")
            para = []

    def flush_list() -> None:
        nonlocal list_tag, pending_blank
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None
        pending_blank = False

    def flush_table() -> None:
        nonlocal table
        if not table:
            return
        rows = [r for r in table if not all(set(c.strip()) <= set("-: ") for c in r)]
        if rows:
            head, *body = rows
            cells = "".join(f"<th>{render_inline(c)}</th>" for c in head)
            html_rows = [f"<thead><tr>{cells}</tr></thead>"]
            body_rows = "".join(
                "<tr>" + "".join(f"<td>{render_inline(c)}</td>" for c in row) + "</tr>"
                for row in body
            )
            if body_rows:
                html_rows.append(f"<tbody>{body_rows}</tbody>")
            out.append("<table>" + "".join(html_rows) + "</table>")
        table = []

    def flush_all() -> None:
        flush_para()
        flush_list()
        flush_table()

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
                code = []
                in_code = False
            else:
                flush_all()
                in_code = True
            continue
        if in_code:
            code.append(raw)
            continue

        stripped = line.strip()
        if not stripped:
            # Inside a list, hold the blank: markdown allows loose lists, and
            # closing on the first blank line restarted the numbering at 1 for
            # every item.
            if list_tag:
                pending_blank = True
            else:
                flush_all()
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            flush_para()
            flush_list()
            table.append([c.strip() for c in stripped.strip("|").split("|")])
            continue
        flush_table()

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_all()
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_all()
            out.append("<hr>")
            continue

        if stripped.startswith("> "):
            flush_all()
            out.append(f"<blockquote>{render_inline(stripped[2:])}</blockquote>")
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or ordered:
            wanted = "ul" if bullet else "ol"
            flush_para()
            if list_tag != wanted:
                flush_list()
                out.append(f"<{wanted}>")
                list_tag = wanted
            pending_blank = False
            item = (bullet or ordered).group(1)
            out.append(f"<li>{render_inline(item)}</li>")
            continue

        # An indented line under a list item continues that item.
        if list_tag and not pending_blank and re.match(r"^\s{2,}\S", line) and out and out[-1].endswith("</li>"):
            out[-1] = out[-1][: -len("</li>")] + " " + render_inline(stripped) + "</li>"
            continue

        if list_tag:
            flush_list()
            pending_blank = False
        para.append(stripped)

    if in_code and code:
        out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
    flush_all()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Repo activity. Commits are the heartbeat of the page — they show the wiki
# moving even when a given team's entity file has not changed.
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def load_commits(repo: Path, limit: int = 250) -> list[dict]:
    raw = git(repo, "log", f"-{limit}", "--date=iso-strict", "--pretty=%H%x1f%an%x1f%ad%x1f%s")
    commits = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, author, date, subject = parts
        commits.append(
            {"sha": sha[:7], "author": author, "date": date, "subject": subject}
        )
    return commits


# ---------------------------------------------------------------------------
# What the page needs to know about the model, derived from SCHEMA.yml rather
# than restated in the page. Everything below is a rule, not a list of names:
# add an entity type or a reference field to the schema and the page picks it
# up — a tab, a stat card, links in both directions.
# ---------------------------------------------------------------------------

def ref_target(fspec: dict) -> str | None:
    """The folder a reference field points at, if it is a reference field."""
    return fspec.get("ref") or fspec.get("list_ref")


def entity_meta(schema: dict) -> dict:
    folder_to_type = {
        spec["folder"]: name for name, spec in (schema.get("entities") or {}).items()
    }
    meta: dict[str, dict] = {}
    for name, spec in (schema.get("entities") or {}).items():
        fields = spec.get("fields") or {}
        parent_type = spec.get("parent_entity")

        # A field holding a date that is not bookkeeping is the date this record
        # is *about*: `date` on an update, `decided_on` on a decision.
        date_field = next(
            (f for f, fs in fields.items() if fs.get("type") == "date"), None
        )
        # The person a record belongs to is its first single reference that is
        # not the parent it is filed under. No field name is assumed.
        actor_field = next(
            (
                f
                for f, fs in fields.items()
                if fs.get("ref")
                and not fs.get("generated")
                and folder_to_type.get(fs["ref"]) != parent_type
            ),
            None,
        )
        meta[name] = {
            "folder": spec["folder"],
            "layout": spec.get("layout", "flat"),
            "parent_field": spec.get("parent_field"),
            "parent_type": parent_type,
            # Flat entities are things you browse; nested ones are records in a
            # log, and belong in the activity feed rather than a tab of their own.
            "list_view": spec.get("layout", "flat") != "nested",
            "display": spec.get("display") or {},
            "date_field": date_field,
            "actor_field": actor_field,
            "fields": {
                f: {
                    "vocab": fs.get("vocab") or fs.get("list_vocab"),
                    "list": bool(fs.get("list_ref") or fs.get("list_vocab")),
                    "ref_type": folder_to_type.get(ref_target(fs)) if ref_target(fs) else None,
                    "generated": bool(fs.get("generated") or fs.get("derived_from_edges")),
                    "bookkeeping": bool(fs.get("bookkeeping")),
                    "type": fs.get("type"),
                }
                for f, fs in fields.items()
            },
        }
    return meta


def build_graph(schema: dict, entities: dict[str, list[dict]]) -> dict:
    """Every edge the frontmatter declares, in both directions.

    The page cannot show what links to an entity without this: a reference is
    stored on one side only, deliberately, so the other side has to be derived.
    Which is exactly why relationships were missing from the page — each view
    was hand-written per entity type, so an edge nobody had thought to code was
    invisible even though the data held it.
    """
    folder_to_type = {
        spec["folder"]: name for name, spec in (schema.get("entities") or {}).items()
    }
    known = {f"{e['type']}:{e['id']}" for group in entities.values() for e in group}
    graph: dict[str, dict[str, list]] = {}

    def slot(node: str) -> dict:
        return graph.setdefault(node, {"out": [], "in": []})

    for name, spec in (schema.get("entities") or {}).items():
        fields = spec.get("fields") or {}
        for entity in entities.get(name, []):
            node = f"{name}:{entity['id']}"
            slot(node)
            for field, fspec in fields.items():
                target_folder = ref_target(fspec)
                if not target_folder:
                    continue
                target_type = folder_to_type.get(target_folder)
                if not target_type:
                    continue
                value = entity["fm"].get(field)
                values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
                for raw in values:
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    other = f"{target_type}:{raw}"
                    if other not in known:
                        continue          # dangling; the validator reports it
                    slot(node)["out"].append({"field": field, "type": target_type, "id": raw})
                    slot(other)["in"].append({"field": field, "type": name, "id": entity["id"]})
    return graph


class NoModel(Exception):
    """The brain does not declare a machine-readable entity model."""


def read_schema(repo: Path) -> dict:
    path = repo / "SCHEMA.yml"
    if not path.exists():
        raise NoModel(
            f"{repo}/SCHEMA.yml not found.\n\n"
            "The page renders whatever the brain declares, so a brain needs a\n"
            "machine-readable model before it can have one. SCHEMA.yml lists the\n"
            "entity types, their folders, their fields, and which fields are\n"
            "references to other entities.\n\n"
            "Deliberately not inferred from the files: a guessed model produces a\n"
            "page that is subtly wrong in ways nobody can correct, and the same\n"
            "declaration also drives the validator and the rollups.\n\n"
            "A brain that declares one is the worked example to copy the shape from."
        )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def brand(repo: Path, slug: str, schema: dict) -> dict:
    """What this brain is called, for the page's title and header.

    The page used to carry one brain's name in its markup, so every other brain
    rendered under somebody else's title -- a grants brain announcing itself as
    a hackathon. Declared wins, then the README's own heading, then the
    repository name; there is no default worth guessing.

        site:
          title: Company Brain
          subtitle: What this brain covers
    """
    declared = schema.get("site") or {}
    title = (declared.get("title") or "").strip()
    subtitle = (declared.get("subtitle") or "").strip()

    if not title:
        readme = repo / "README.md"
        if readme.is_file():
            for line in readme.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
    if not title:
        title = (slug.split("/")[-1] if slug else repo.resolve().name)
    return {"title": title, "subtitle": subtitle}


def jsonable(value):
    """Dates survive the trip to the page as ISO strings.

    An unquoted `2026-08-10` in frontmatter is a `datetime.date` once PyYAML
    has read it, and json cannot serialise one -- so a brain whose dates happen
    to be unquoted produced no site at all, with a traceback rather than a
    message. Both spellings are legitimate YAML and both appear in real wikis,
    frequently in the same file, since editors that rewrite frontmatter tend to
    drop the quotes.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable: {value!r}")


def build(repo: Path, public: bool = False) -> dict:
    schema = read_schema(repo)
    set_entity_folders(schema)
    entities = load_entities(repo, schema, public=public)
    remote = git(repo, "config", "--get", "remote.origin.url")
    slug = ""
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
    if match:
        slug = match.group(1)
    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "public": public,
        "commit": git(repo, "rev-parse", "--short", "HEAD"),
        "repo": slug,
        "brand": brand(repo, slug, schema),
        "schema_version": schema.get("schema_version"),
        "vocabularies": schema.get("vocabularies", {}),
        "entity_order": list(schema.get("entities", {}).keys()),
        "entity_folders": {
            name: spec["folder"] for name, spec in schema.get("entities", {}).items()
        },
        "entity_meta": entity_meta(schema),
        "graph": build_graph(schema, entities),
        "entities": entities,
        "commits": load_commits(repo),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--out", default=None, help="output path for data.json")
    parser.add_argument(
        "--public",
        action="store_true",
        help="drop fields the brain declares unpublishable (publish.redact in SCHEMA.yml)",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    try:
        data = build(repo, public=args.public)
    except NoModel as exc:
        print(f"cannot build a site for this brain:\n\n{exc}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else repo / "site" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=jsonable),
                   encoding="utf-8")

    counts = ", ".join(f"{k} {len(v)}" for k, v in data["entities"].items())
    mode = "public build, redacted" if args.public else "full build"
    print(f"wrote {out} — {counts}, {len(data['commits'])} commits ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
