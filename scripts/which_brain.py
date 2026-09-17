#!/usr/bin/env python3
"""Decide which brain a capture belongs to, from the working directory.

Step 0a of the skill describes this in prose, but two of its rules have exactly
one right answer and no judgement in them:

  - the working directory is inside a registered wiki  -> that wiki is the target
  - the working directory is inside an UNREGISTERED wiki -> write nothing

The second rule is the one that matters, and leaving it to a model to remember
is the wrong place for it: the failure is silent. Somebody stands in a customer's
wiki, says "log this", and the content lands in the company brain instead, where
it validates cleanly and nobody ever notices. So it is a script.

The distinction that makes this safe: a wiki is a repository with a
CONVENTIONS.md at its root. An ordinary project repository is not a wiki and
carries no signal either way -- working on application code must not block a
capture.

Exit status:
    0  decided; the target is printed
    3  write refused: the brain is out of scope for this install, or configured
       read-only, or is a wiki this cannot identify
    4  no brain configured yet -- ask, then persist (see Step 0a)

Access is one set, configured per install; reading and writing are subsets of
it. `--may-read` answers the read question, and it is a real question: a
credential that reaches every repository in an organization does not put every
wiki in scope.

Deliberately dependency-free, like set_wiki_config.py: this runs before an
adopter's environment has anything installed.

Usage:
    which_brain.py                     decide for the current directory
    which_brain.py --cwd /path/to/repo
    which_brain.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_config  # noqa: E402  -- a sibling command, not an installed package

# The keys `load_config` recognises a layer by. Presence of any one of these,
# textually, is what elects a layer now -- see `_declares_config`.
_CONFIG_KEY_RE = re.compile(
    r"(?m)^(?:config_schema|brains|repo|pairs|release_landing):")


class UnreadableConfigLayer(Exception):
    """A config layer exists but could not be read or decoded as UTF-8.

    Distinguished on purpose from `_read_text` returning `None`, which means
    there is genuinely no file at this path -- the ordinary, silent case of a
    lower layer simply not being configured. This one means the file *is*
    there and the read failed, and this file's own rule (see
    `normalise_remote` below) is that a signal this cannot read is a refusal,
    never a silent fall-through to a different brain.
    """

    def __init__(self, path: Path, detail: str):
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {detail}")


def _declares_config(text: str | None) -> bool:
    """Whether a layer's raw text is authored configuration at all.

    A layer is elected on the presence of a recognised key, never on whether
    `brains` or `repo` produced values (2026-09-06 ruling): `brains: []` /
    `repo: unset` is a deliberate clearing, and treating it as absence is how
    clearing an org file stopped meaning anything -- a stale lower layer took
    back over. A layer whose text is empty, or names none of
    `config_schema`, `brains`, `repo`, `pairs`, `release_landing`, is
    genuinely absent and is skipped.
    """
    return bool(text) and bool(_CONFIG_KEY_RE.search(text))


def _read_text(path: Path) -> str | None:
    """The file's text, or `None` if there is genuinely no file here.

    Existence is decided by trying to read, not by asking `Path.is_file()`
    first: under a mode-000 parent directory `is_file()` itself raises
    `PermissionError` rather than returning `False`, so trusting it as a
    pre-check is exactly how "cannot tell" became "no file here". Anything
    that exists but will not open, or will not decode as UTF-8, raises
    `UnreadableConfigLayer` rather than returning `None` -- an unreadable
    layer-1 file used to be indistinguishable from an absent one, which is
    how a persisted config for a different brain silently took over.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise UnreadableConfigLayer(path, str(exc)) from exc


def persisted_path() -> Path:
    """Where layer 2 lives, whether or not anything has been written there yet.

    Split out of `persisted_config` so `source_path` (below) can name this
    file without re-reading it -- reporting *which* layer decided is only
    useful if it also says *which file*, and this is the one path that is
    not simply "next to SKILL.md".
    """
    # One location, not a search path. CLAUDE_BRAIN_STATE means "use this
    # state", and falling back to the machine's config when the named
    # directory holds none is how a caller who asked for isolation gets the
    # machine anyway — which is exactly how a test suite came to assert
    # somebody's real brain.
    state = os.environ.get("CLAUDE_BRAIN_STATE")
    base = Path(state) if state else Path(
        os.path.join(os.path.expanduser("~"), ".claude", "company-brain-capture"))
    return base / "config"


def persisted_config() -> str | None:
    """A config a person set, which survives replacing the skill's own files.

    Read before SKILL.md and preferred over it. A plugin update rewrites the
    skill directory, so a brain configured there is configured until the next
    release — which is how an install that had been writing to a wiki came back
    refusing it. Nothing here is required: an artifact uploaded to a chat
    surface has no filesystem to read, and falls back to its baked block.
    """
    return _read_text(persisted_path())


def source_path(source: str, skill_md: Path) -> Path | None:
    """The on-disk file behind a `load_config` source name, or `None` for
    `"none"` -- there is nothing to point at when nothing is configured.

    A bare `"config-file"` tells an operator which *layer* won but not which
    *file* -- and on a machine with several installs, each with its own
    `config/brain.yml`, that is the whole question a person debugging
    precedence is asking. Mirrors `set_wiki_config.elected_path`, which
    solves the identical problem for that script's own report; kept as two
    small functions rather than one shared import because neither script
    depends on the other by design (see this module's own docstring)."""
    return {
        "config-file": brain_config.config_path(skill_md.parent),
        "persisted": persisted_path(),
        "skill-md": skill_md,
    }.get(source)


def _skill_md_block(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    block = re.search(r"```yaml\n(.*?)\n```", text, re.DOTALL)
    if not block:
        raise SystemExit("no ```yaml config block in SKILL.md")
    return block.group(1)


def load_config(skill_md: Path) -> dict:
    """The winning layer's configuration, whole, and which layer that was.

    Three layers, in order:

      1. config/brain.yml beside SKILL.md -- what the installer owns. Resolved
         relative to the skill directory, so it is the same path whether the
         skill arrived as a plugin, an install.sh copy under ~/.gemini/skills,
         or an unpacked artifact. No runtime's environment variable appears
         here on purpose.
      2. ~/.claude/company-brain-capture/config -- a person's own answer to
         Step 0a, kept outside the skill directory so a plugin update cannot
         erase it.
      3. SKILL.md's own block -- the alias layer, and all a chat-surface
         artifact has. Retired over two versions once nothing reads it.

    **A layer is taken whole.** The layer that supplies `brains` supplies every
    field. Merging them field by field is what left a configured machine taking
    its access set from the persisted copy and its pair declaration from
    SKILL.md, with nothing reporting the split.

    **A layer is elected lazily**, and on whether it was *authored*, not on
    whether it produced values. `config/brain.yml` and the persisted copy are
    only read when a higher layer did not already win, so a well-configured
    layer 1 or 2 never depends on SKILL.md existing or being well-formed --
    the two layers that exist to replace it. And an empty-but-authored layer
    (`brains: []` / `repo: unset`, or one naming only `pairs:`) still wins: see
    `_declares_config`.

    An unreadable layer -- exists but will not open or decode -- raises
    `UnreadableConfigLayer` instead of being read as absent, and does so
    before any lower layer is even looked at: an unreadable signal is a
    refusal, never a silent fall-through to a different brain.
    """
    layers = [
        ("config-file", lambda: _read_text(brain_config.config_path(skill_md.parent))),
        ("persisted", persisted_config),
        ("skill-md", lambda: _skill_md_block(skill_md)),
    ]
    for source, get_text in layers:
        text = get_text()
        if not _declares_config(text):
            continue
        cfg = brain_config.parse(text)
        # A default has to be writable to be a default: it is where a capture
        # goes when nothing else decides, and a capture is a write.
        if cfg["repo"] and cfg["repo"] not in cfg["brains"]:
            cfg["brains"][cfg["repo"]] = brain_config.READ_WRITE
        cfg["source"] = source
        return cfg
    return {"config_schema": 0, "brains": {}, "repo": None, "pairs": [],
            "release_landing": None, "source": "none"}


def read_config(skill_md: Path) -> tuple[dict[str, str], str | None]:
    """The access set and the default brain. A view over `load_config`."""
    cfg = load_config(skill_md)
    return cfg["brains"], cfg["repo"]


def writable(brains: dict[str, str]) -> list[str]:
    return sorted(r for r, access in brains.items() if access == brain_config.READ_WRITE)


def git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def normalise_remote(url: str) -> str | None:
    """git@github.com:owner/repo.git and https://…/owner/repo -> owner/repo

    Trailing slashes are stripped before and after `.git`, because `git clone
    https://github.com/o/r/` stores the slash and an unparsed remote used to
    mean `repo is None` -- which skipped the refusal below entirely and sent a
    customer wiki's content to the default brain. A remote this cannot read is
    a refusal, never a silent pass.
    """
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")].rstrip("/")
    m = re.search(r"[:/]([^/:]+)/([^/]+)$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def registered_as(repo: str, registry) -> str | None:
    """The registry's own spelling of `repo`, or None if it is not in there.

    GitHub treats owner and repo names case-insensitively, so a clone made from
    `Owner/Wiki` is the same repository as `owner/wiki`. An
    exact string compare refused to write to a wiki that was in fact registered.
    The registry's spelling is what gets returned, so everything downstream
    writes one spelling regardless of how the clone was made.
    """
    folded = {entry.casefold(): entry for entry in registry}
    return folded.get(repo.casefold())


def _refuse_unreadable_config(exc: UnreadableConfigLayer) -> dict:
    """The Critical-finding refusal: named exit 3, and explicit that this is a
    refusal, not a choice of some other brain.

    No layer was elected -- the read failed before `load_config` could return
    one -- so `source` is `None` rather than one of the four elected values;
    `source_path` still names the file that could not be read, since that is
    exactly what `exc` already knows and what an operator needs first.
    """
    return {
        "decision": "refuse", "brain": None, "exit": 3, "cwd_repo": None,
        "source": None, "source_path": str(exc.path),
        "reason": (
            f"{exc.path} exists but could not be read ({exc.detail}). Refusing "
            f"rather than falling through to a different, differently-scoped "
            f"brain -- an unreadable config layer is a refusal, never a silent "
            f"choice of something else."
        ),
    }


def _tag(result: dict, source: str, path: Path | None) -> dict:
    """Stamp a decision with the layer that produced it, so `--json` carries
    the single most useful diagnostic this three-layer design has (USAGE.md
    already promises it; before this, `decide()`/`may_read()` computed the
    layer via `load_config` and then threw it away). `source_path` is the
    on-disk file behind that layer, or `None` for `"none"` -- there is
    nothing to point at when nothing is configured."""
    result["source"] = source
    result["source_path"] = str(path) if path is not None else None
    return result


def decide(cwd: Path, skill_md: Path) -> dict:
    try:
        cfg = load_config(skill_md)
    except UnreadableConfigLayer as exc:
        return _refuse_unreadable_config(exc)
    brains, default = cfg["brains"], cfg["repo"]
    source, path = cfg["source"], source_path(cfg["source"], skill_md)

    root = git(cwd, "rev-parse", "--show-toplevel")
    repo = None
    is_wiki = False
    if root:
        remote = git(cwd, "remote", "get-url", "origin")
        repo = normalise_remote(remote) if remote else None
        # A wiki declares itself by carrying the spec the skill reads.
        is_wiki = (Path(root) / "CONVENTIONS.md").is_file()

    if not brains:
        # Before anything about the working directory. An unconfigured install
        # standing in a wiki used to report that wiki as out of scope, which
        # reads as a deliberate exclusion rather than as "nobody has said which
        # brain this install serves".
        return _tag({"decision": "unconfigured", "brain": None, "exit": 4,
                "reason": "no brain is configured — ask which one, verify it is reachable, "
                          "then persist it with set_wiki_config.py"}, source, path)

    if is_wiki and not repo:
        # A wiki whose repository cannot be named is not thereby permission to
        # write somewhere else. Absence is not permission.
        return _tag({"decision": "refuse", "brain": None, "exit": 3, "cwd_repo": None,
                "reason": (
                    "the working directory is a wiki (it has a CONVENTIONS.md) whose "
                    "repository cannot be identified -- no `origin` remote, or one this "
                    "cannot parse. Write nothing until it is known which wiki this is: "
                    "`git remote -v` here, and name the target explicitly if it is right."
                )}, source, path)

    if is_wiki and repo:
        known = registered_as(repo, brains)
        if known and brains[known] == brain_config.READ_WRITE:
            return _tag({"decision": "target", "brain": known, "exit": 0,
                    "reason": f"the working directory is {known}, which this install may write to"},
                    source, path)
        if known:
            return _tag({"decision": "read_only", "brain": known, "exit": 3, "cwd_repo": known,
                    "reason": (
                        f"{known} is configured read-only for this install. Read it if the "
                        f"question is about it; write nothing, here or elsewhere on its behalf. "
                        f"Writable: {writable(brains) or 'none'}"
                    )}, source, path)
        return _tag({"decision": "refuse", "brain": None, "exit": 3, "cwd_repo": repo,
                "reason": (
                    f"the working directory is the {repo} wiki, which this install has no "
                    f"access to. Write nothing and read nothing from it. Do not fall back to "
                    f"another brain: content meant for this wiki does not belong in one with a "
                    f"different audience. Configured: {sorted(brains) or 'none'}"
                )}, source, path)

    writables = writable(brains)
    if not writables:
        return _tag({"decision": "read_only", "brain": None, "exit": 3,
                "reason": ("every configured brain is read-only for this install, so there is "
                           "nowhere to write a capture. Say so rather than choosing one.")},
                source, path)

    if len(writables) == 1:
        return _tag({"decision": "target", "brain": writables[0], "exit": 0,
                "reason": "only one brain is writable"}, source, path)

    return _tag({"decision": "classify", "brain": default, "exit": 0, "candidates": writables,
            "reason": ("no working-directory signal -- classify against each writable brain's "
                       f"`## Scope` in its CONVENTIONS.md; default {default} if genuinely "
                       "ambiguous")}, source, path)


def may_read(repo: str, skill_md: Path) -> dict:
    """Whether this install may read a repository as a brain.

    Access is the set an organization configured; reading and writing are
    subsets of it. So this asks only about membership -- a read-only brain is
    readable, and a brain nobody configured is not, whatever a broader
    credential happens to permit.

    That last part is the point. An organization-wide grant can make every
    repository reachable, and reachable is not the same as in scope: content in
    one wiki was written for that wiki's audience, and answering from it
    elsewhere moves it to another. Nothing is written, so no write guard fires,
    and the answer has still crossed an audience.

    A repository is only a brain if it carries CONVENTIONS.md; ordinary
    repositories are not governed here.
    """
    try:
        cfg = load_config(skill_md)
    except UnreadableConfigLayer as exc:
        return _refuse_unreadable_config(exc)
    brains = cfg["brains"]
    source, path = cfg["source"], source_path(cfg["source"], skill_md)
    known = registered_as(repo, brains)
    if known:
        return _tag({"decision": "read", "brain": known, "exit": 0, "access": brains[known],
                "reason": f"{known} is configured for this install ({brains[known]})"},
                source, path)
    return _tag({"decision": "refuse", "brain": None, "exit": 3, "cwd_repo": repo,
            "reason": (
                f"{repo} is not configured for this install, so it is not a source. Do not "
                f"search it and do not answer from it, whatever a broader credential allows. "
                f"Say which brain you did consult and that an answer may exist in one you "
                f"cannot reach. Configured: {sorted(brains) or 'none'}"
            )}, source, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", type=Path, default=Path.cwd())
    ap.add_argument("--may-read", metavar="OWNER/REPO",
                    help="may this install read that repository as a brain?")
    ap.add_argument("--skill-md", type=Path,
                    default=Path(__file__).resolve().parent.parent / "SKILL.md")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.may_read:
        result = may_read(args.may_read, args.skill_md)
    else:
        result = decide(args.cwd.resolve(), args.skill_md)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        head = {"target": "TARGET", "refuse": "REFUSE", "read": "READ",
                "read_only": "READ-ONLY", "unconfigured": "UNCONFIGURED",
                "classify": "CLASSIFY"}[result["decision"]]
        print(f"{head}: {result['brain'] or '-'}")
        print(f"  {result['reason']}")
        # "none" is reported like any other layer -- omitting it here is how
        # "nothing is configured anywhere" used to look identical to a
        # skipped line, on the one command USAGE.md says answers this.
        where = f" ({result['source_path']})" if result.get("source_path") else ""
        print(f"  source: {result['source']}{where}")
    return result["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
