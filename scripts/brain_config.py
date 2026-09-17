#!/usr/bin/env python3
"""The installed copy's configuration: one file, one format, read whole.

An install is configured by `config/brain.yml` beside SKILL.md. That path is
the same however the skill arrived -- a plugin cache, an install.sh copy under
~/.gemini/skills, an unpacked artifact -- because it is resolved relative to
the skill directory rather than from any runtime's environment variable.

This module owns the format and nothing else: no filesystem policy, no
precedence between layers, no argument parsing. `which_brain.py` decides which
layer wins; `set_wiki_config.py` decides what to write. Both come here to read
and render it, so there is exactly one parser to keep honest.

Two list styles parse. Layer 1 (config/brain.yml), hand-edited in a derived
distribution repo, uses block style because it reads better; layer 2 (persisted)
and layer 3 (SKILL.md) use flow style. One parser reading all three beats separate
parsers drifting apart.

Deliberately dependency-free, like which_brain.py and set_wiki_config.py: this
runs before an adopter's environment has anything installed.
"""

from __future__ import annotations

import re
from pathlib import Path

# Bumped only when a field changes shape. A vocabulary or a new optional field
# is not a schema change -- the same call the wiki's schema makes. A bump means
# every derived repo's sync PR stops until its file is updated, so a bump that
# did not change shape trains people past the label.
CONFIG_SCHEMA = 1

CONFIG_DIRNAME = "config"
CONFIG_FILENAME = "brain.yml"

READ = "read"
READ_WRITE = "read_write"

UNSET = {"", "unset", "none", "[]", "~", "null"}

_FLOW_RE = re.compile(r"^brains:\s*\[([^\]]*)\]", re.MULTILINE)
_BLOCK_RE = re.compile(r"^brains:\s*$\n((?:\s*-\s*.*\n?)*)", re.MULTILINE)
_SCALAR_RE = r"^%s:\s*(\S.*?)\s*$"
_PAIR_ITEM_RE = re.compile(r"\{\s*primary:\s*([^,}]+?)\s*,\s*extends:\s*([^,}]+?)\s*\}")
_PAIRS_BLOCK_RE = re.compile(r"^pairs:\s*$\n((?:\s*-\s*[^\n]*(?:\n(?!\s*-)[^\n]*)*\n?)*)", re.MULTILINE)
_PAIRS_FLOW_RE = re.compile(r"^pairs:\s*\[(.*)\]\s*$", re.MULTILINE)


def config_path(skill_dir: Path) -> Path:
    """Where an installed copy's configuration lives, relative to the skill."""
    return Path(skill_dir) / CONFIG_DIRNAME / CONFIG_FILENAME


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""


def _scalar(text: str, field: str) -> str | None:
    m = re.search(_SCALAR_RE % re.escape(field), text, re.MULTILINE)
    if not m:
        return None
    value = _strip_comment(m.group(1)).strip().strip('"').strip("'")
    return value or None


def _entries(text: str) -> list[str]:
    """Brain entries from either list style, comments and blanks removed."""
    m = _FLOW_RE.search(text)
    if m:
        return [e.strip() for e in m.group(1).split(",") if e.strip()]
    m = _BLOCK_RE.search(text)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = _strip_comment(line).strip()
        if line.startswith("-"):
            entry = line[1:].strip()
            if entry:
                out.append(entry)
    return out


def parse_brains(text: str) -> dict[str, str]:
    """The access set: every brain this install may touch, and how.

    One list carries read and write because two lists drift and the failure is
    silent. No suffix means read_write; an unrecognised suffix resolves to
    read, because least privilege is the safe direction to be wrong in and the
    alternative is granting a write on a typo.
    """
    brains: dict[str, str] = {}
    for entry in _entries(text):
        repo, _, access = entry.partition(":")
        repo = repo.strip()
        if not repo:
            continue
        access = access.strip() or READ_WRITE
        brains[repo] = READ_WRITE if access == READ_WRITE else READ
    return brains


def parse_pairs(text: str) -> list[tuple[str, str]]:
    """Parse pairs from both flow and block-mapping styles.

    A hand-edited file in a derived distribution repo is likelier to use
    block-mapping style, so both flow and block styles parse identically.

    Flow style (machine-written by dump()):
        pairs:
          - {primary: a/b, extends: c/d}

    Block style (hand-edited):
        pairs:
          - primary: a/b
            extends: c/d
    """
    body = ""
    m = _PAIRS_BLOCK_RE.search(text)
    if m:
        body = m.group(1)
    else:
        # `_PAIRS_FLOW_RE` anchors the closing `]` to end-of-line, so a
        # trailing inline comment -- the exact shape SKILL.md ships,
        # `pairs:  [...]  # ... — see Step 0a` -- broke the match. Strip the
        # comment off the `pairs:` line itself before matching flow style;
        # `_strip_comment` already does this for every scalar field.
        line = next((ln for ln in text.splitlines() if ln.startswith("pairs:")), "")
        m = _PAIRS_FLOW_RE.match(_strip_comment(line))
        if m:
            body = m.group(1)

    if not body:
        return []

    pairs = []

    # Try flow-style matches first (curly braces)
    for p, e in _PAIR_ITEM_RE.findall(body):
        pairs.append((p.strip(), e.strip()))

    # If no flow-style pairs found, try block-mapping style
    if not pairs:
        # Split by lines starting with '-' to get individual items
        lines = body.split('\n')
        i = 0
        while i < len(lines):
            line = _strip_comment(lines[i]).strip()
            # Look for a line that starts with '-' (list item marker)
            if line.startswith('-'):
                # Collect this item and all its continuation lines
                item_text = line[1:].strip()  # Remove '-' and get the content
                i += 1
                # Collect continuation lines (lines that don't start with '-')
                while i < len(lines):
                    next_line = _strip_comment(lines[i])
                    if not next_line.strip():  # Skip empty lines
                        i += 1
                        continue
                    if next_line.lstrip().startswith('-'):  # Stop at next item
                        break
                    if len(next_line) - len(next_line.lstrip()) > 0:  # Continuation (indented)
                        item_text += '\n' + next_line.strip()
                        i += 1
                    else:
                        break

                # Parse the collected item text to extract primary and extends
                primary = None
                extends = None
                for pair_line in item_text.split('\n'):
                    pair_line = _strip_comment(pair_line).strip()
                    if ':' in pair_line:
                        key, _, value = pair_line.partition(':')
                        key = key.strip().lower()
                        value = value.strip()
                        if key == 'primary':
                            primary = value
                        elif key == 'extends':
                            extends = value

                if primary and extends:
                    pairs.append((primary, extends))
            else:
                i += 1

    return pairs


def parse(text: str) -> dict:
    """Every field, or its absence. A field nobody wrote acquires no value."""
    repo = _scalar(text, "repo")
    if repo is not None and repo.lower() in UNSET:
        repo = None
    schema = _scalar(text, "config_schema")
    try:
        # Absent reads as 0, never as current: a file written before schemas
        # existed must not claim to be up to date, or a migration tail goes
        # unnoticed exactly the way v7 and v8 did in the wiki.
        schema_n = int(schema) if schema is not None else 0
    except ValueError:
        schema_n = 0
    landing = _scalar(text, "release_landing")
    return {
        "config_schema": schema_n,
        "brains": parse_brains(text),
        "repo": repo,
        "pairs": parse_pairs(text),
        "release_landing": landing,
    }


def dump(brains: dict[str, str], repo: str | None,
         pairs: list[tuple[str, str]] | None = None,
         release_landing: str | None = None) -> str:
    """Render the file. Block-list style, because a human edits this one."""
    lines = [
        "# This install's configuration. Owned by whoever installed the skill;",
        "# a product release never writes it. See SKILL.md, `## Configuration`.",
        f"config_schema: {CONFIG_SCHEMA}",
        "brains:" if brains else "brains: []",
    ]
    for name in sorted(brains):
        suffix = "" if brains[name] == READ_WRITE else f":{brains[name]}"
        lines.append(f"  - {name}{suffix}")
    lines.append(f"repo: {repo}" if repo else "repo: unset")
    if pairs:
        lines.append("pairs:")
        for primary, extends in pairs:
            lines.append(f"  - {{primary: {primary}, extends: {extends}}}")
    else:
        lines.append("pairs: []")
    if release_landing:
        lines.append(f"release_landing: {release_landing}")
    return "\n".join(lines) + "\n"
