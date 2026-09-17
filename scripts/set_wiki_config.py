#!/usr/bin/env python3
"""Write configuration across three layers: config/brain.yml, machine-level, and SKILL.md.

A copy of this skill ships pointed at no wiki at all
(a wiki named at build time). Before it can be installed for anyone else, the
configuration layers have to be written -- and hand-editing is exactly the kind
of mechanical step this project's own rule says to script rather than reason through.

`config/brain.yml` (layer 1) is hand-edited by operators; machine-level config
(layer 2) is written by persist(); SKILL.md's block (layer 3) is the fallback.
Which layer is written depends on which was authored (declares a schema).

Deliberately dependency-free (no PyYAML) -- this script is meant to run
before an adopter's environment has anything installed.

The registry (`brains`) is the complete set of repositories an install may
write to. Anything absent from it is not a target -- which is how a
customer-owned wiki stays out of a company install. `<owner/repo>` is the
default brain and is added to the registry automatically if missing.

Exit status:
    0  written -- or nothing needed writing
    1  the config block is not shaped the way this script expects
    2  usage: a bad argument, or two that contradict
    4  written, but one or more layers were SKIPPED because they exist and
       cannot be read. Every readable layer was still written and the skipped
       file is named on stderr, left byte-for-byte alone. Non-zero because a
       caller under `set -eu` otherwise reports a successful install while
       every subsequent read refuses -- which_brain.py's own refusal is exit
       3, so 3 is not reused here. `unset` reads no layer and never returns
       this: it is the recovery command.

Usage:
    set_wiki_config.py <owner/repo>
    set_wiki_config.py <owner/repo> --config-file /path/to/config/brain.yml
    set_wiki_config.py <owner/repo> --brains a/b,c/d
    set_wiki_config.py <owner/repo> --brains a/b,c/d --pair a/b=c/d
    set_wiki_config.py <owner/repo> --branch main --prefix "brain:"
    set_wiki_config.py <owner/repo> --folders organizations,people,projects,products,activities
    set_wiki_config.py <owner/repo> --source-access read_write
    set_wiki_config.py <owner/repo> --skill-md /path/to/SKILL.md   # for testing
    set_wiki_config.py unset                       # clear it: no default, empty registry
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_config  # noqa: E402  -- a sibling command, not an installed package
# For its absent-vs-authored-empty notion, and for REPORTING which layer the
# read side will elect. Nothing WRITTEN depends on that election: each layer is
# still its own fallback, and consuming an elected result is exactly how an
# explicit narrowing got silently re-widened once already.
import which_brain  # noqa: E402  -- likewise a sibling command

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
ACCESS_LEVELS = ("read", "read_write")

# The shipped state: no default brain, empty registry. `build.py` uses this to
# neutralise a copy before packaging, because a machine that has run
# install.sh has a configured source tree and an artifact must never carry
# whoever built it.
UNSET = "unset"

FIELD_PATTERNS = {
    "repo": re.compile(r"^(repo:\s*)(\S+)", re.MULTILINE),
    "branch": re.compile(r"^(branch:\s*)(\S+)", re.MULTILINE),
    "commit_prefix": re.compile(r'^(commit_prefix:\s*)"([^"]*)"', re.MULTILINE),
    "source_access": re.compile(r"^(source_access:\s*)(\S+)", re.MULTILINE),
    "folders": re.compile(r"^(folders:\s*)\[([^\]]*)\]", re.MULTILINE),
    "brains": re.compile(r"^(brains:\s*)\[([^\]]*)\]", re.MULTILINE),
    "pairs": re.compile(r"^(pairs:\s*)\[([^\]]*)\]", re.MULTILINE),
}


class ConfigError(Exception):
    """The block, or a field in it, isn't shaped the way this script expects."""


class UnreadableLayer(Exception):
    """A layer this run would write exists, but will not open or decode.

    The refusal is scoped to that layer's *write*, not to the run. An
    unreadable signal is never a silent pass -- but the two ways of being
    loud are not equally correct here:

      * the corrupt file is left byte-for-byte alone and every other layer
        is still written. Overwriting it would destroy exactly the fields
        the command line omitted, because each layer's fallback is that same
        layer's own contents -- and refusing to write the readable layers
        would punish them for one layer's corruption.
      * the status code still has to say a layer was skipped, so this
        returns 4. A caller that cannot see the skip reports success while
        every later read refuses. That means exit 4 is a real non-zero: a
        caller invoking this under `set -eu` -- `install.sh` does,
        before its runtime loop -- must tolerate it rather than abort on it.

    Nothing is silently elected in the meantime: `which_brain.load_config`
    refuses an unreadable layer with exit 3 on the read side, which is where
    that refusal belongs.
    """

    def __init__(self, path, detail: str):
        self.path, self.detail = Path(path), detail
        super().__init__(
            f"error: {path} exists but could not be read ({detail}) -- that layer "
            f"was NOT written. A field omitted on the command line falls back to "
            f"this layer's own contents, so overwriting it would be a guess. "
            f"Repair or delete the file, pass --brains together with --pair or "
            f"--unset-pairs to write it without reading it, or run `unset`")


def extract_block(text: str) -> tuple[str, int, int]:
    match = re.search(r"```yaml\n(.*?)\n```", text, re.DOTALL)
    if not match:
        raise ConfigError("no ```yaml config block found in SKILL.md")
    return match.group(1), match.start(1), match.end(1)


def patch_field(block: str, field: str, value: str) -> str:
    pattern = FIELD_PATTERNS[field]
    m = pattern.search(block)
    if not m:
        raise ConfigError(f"'{field}:' not found in the config block")
    if field in ("folders", "brains", "pairs"):
        replacement = f"{m.group(1)}[{value}]"
    elif field == "commit_prefix":
        replacement = f'{m.group(1)}"{value}"'
    else:
        replacement = f"{m.group(1)}{value}"
    return block[: m.start()] + replacement + block[m.end() :]


def persisted_path() -> Path:
    """Layer 2: a person's own answer, kept where a plugin update cannot reach."""
    home = os.environ.get("CLAUDE_BRAIN_STATE") or os.path.join(
        os.path.expanduser("~"), ".claude", "company-brain-capture")
    return Path(home) / "config"


def read_layer(path: Path) -> str | None:
    """One layer's own raw text, or None when it has nothing authored in it.

    Absent is empty; unreadable is not. A file that exists and will not open
    or decode raises `UnreadableLayer` rather than reading as "" -- reading it
    as empty is how a writer overwrites an authored layer with a guess.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise UnreadableLayer(path, str(exc)) from exc


def persist(brains: str, repo: str, pairs: list[tuple[str, str]] | None = None) -> Path | None:
    """Write the access set where a plugin update cannot reach it.

    Every field, not some -- `config_schema`, `brains`, `repo` and `pairs` are
    all written every time this runs, because a field this leaves out is not
    merely inconsistent, it is gone: `which_brain.py` takes whichever layer
    wins whole, so a field missing here is a field no lower layer gets a
    chance to supply either.

    Kept in this file's own flat/flow format rather than routed through
    `brain_config.dump` -- that renderer always writes `brains:` as a block
    list, which does not match the format this file has been written in
    since before `brain_config.py` existed. `brain_config.parse` reads either
    style, so nothing downstream cares; this only preserves what a machine's
    already-persisted file looks like after an in-place upgrade. A test feeds
    this format through `brain_config.parse` and through
    `which_brain.load_config` so the two renderers cannot silently drift.
    """
    target = persisted_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if repo == UNSET:
            if target.exists():
                target.unlink()
            return None
        pairs_field = ", ".join(
            "{primary: %s, extends: %s}" % (p, e) for p, e in (pairs or []))
        target.write_text(
            "# Written by set_wiki_config.py. Read in preference to the config\n"
            "# block in SKILL.md, so a plugin update cannot erase it.\n"
            f"config_schema: {brain_config.CONFIG_SCHEMA}\n"
            f"brains: [{brains}]\n"
            f"repo:   {repo}\n"
            f"pairs:  [{pairs_field}]\n",
            encoding="utf-8",
        )
        return target
    except OSError:
        return None


def _as_brains(entries: list[str], repo: str | None) -> dict[str, str]:
    """The access set a list of registry entries resolves to.

    Resolved the way the READ side resolves it, `load_config`'s derived
    default included, so the two are comparable. Comparison only -- nothing
    this script writes comes from here, because minting that derived default
    as an authored entry is a bug this loop has already paid for once.
    """
    brains = brain_config.parse_brains("brains: [%s]" % ", ".join(entries))
    if repo and repo not in brains:
        brains[repo] = brain_config.READ_WRITE
    return brains


def _render_brains(brains: dict[str, str]) -> str:
    return ", ".join(
        name if access == brain_config.READ_WRITE else f"{name}:{access}"
        for name, access in sorted(brains.items())) or "(empty)"


def _render_pairs(pairs) -> str:
    return ", ".join(
        "{primary: %s, extends: %s}" % (p, e) for p, e in pairs) or "(empty)"


def elected_path(source: str, skill_md: Path) -> Path | None:
    """The file behind a `load_config` source name, so the report can name it."""
    return {
        "config-file": brain_config.config_path(skill_md.parent),
        "persisted": persisted_path(),
        "skill-md": skill_md,
    }.get(source)


def report_effective(skill_md: Path, repo: str | None, wrote) -> None:
    """Print the layer the READ side will elect, and flag any divergence.

    Three separate defects in this loop had one shape: the writer reported a
    change `which_brain.load_config` would not honour. The last of them is not
    a bug in either side -- an explicit narrowing is authoritative in every
    layer this run writes, and a wider layer 1 this run was not asked to write
    legitimately still wins the read. What was wrong was that nobody said so:
    the script printed `brains: a/b`, exited 0, and the install kept the wider
    set.

    So the read side is asked directly, and only to REPORT. Nothing written
    depends on it. A layer that makes `load_config` raise --
    `UnreadableConfigLayer`, or `SystemExit` on a block it cannot find --
    degrades this to silence rather than failing an otherwise-successful run.

    `wrote` is (path, brains entries, pairs) per layer this run wrote, in
    precedence order.
    """
    try:
        cfg = which_brain.load_config(skill_md)
    except (Exception, SystemExit) as exc:
        # Degrading to silence was its own defect. A corrupt layer this run
        # does not write is never read by the writer, so nothing is skipped
        # and the exit is 0 -- correct -- but the read side still refuses it
        # with exit 3. The operator saw a clean run, an empty stderr and no
        # report, and a skill that then answered nothing. Say so: a note,
        # not an error. It changes no exit code and never raises.
        print(f"note: cannot report which configuration layer the read side "
              f"will elect ({type(exc).__name__}: {exc})", file=sys.stderr)
        return

    where = elected_path(cfg.get("source", ""), skill_md)
    print(f"effective config: {cfg.get('source')}" + (f" — {where}" if where else ""))
    print(f"  brains: {_render_brains(cfg['brains'])}")
    print(f"  repo:   {cfg['repo'] or '(unset)'}")
    print(f"  pairs:  {_render_pairs(cfg['pairs'])}")

    if not wrote:
        return
    try:
        written = {Path(path).resolve(): (_as_brains(entries, repo), sorted(pairs))
                   for path, entries, pairs in wrote}
        resolved = where.resolve() if where else None
    except OSError:
        return
    mine = written.get(resolved)
    same_layer = mine is not None
    if mine is None:
        # The elected layer is one this run left alone. Compare against the
        # highest layer it did write -- what would have won without it.
        mine = next(iter(written.values()))
    theirs = (cfg["brains"], sorted(cfg["pairs"]))
    if mine == theirs and cfg["repo"] == repo:
        return

    print("warning: what wins is not what this run wrote")
    if not same_layer:
        print(f"  {where} is the layer load_config elects, "
              f"and this run did not write it")
    print(f"  it says:       brains: {_render_brains(theirs[0])} | "
          f"repo: {cfg['repo'] or '(unset)'} | pairs: {_render_pairs(theirs[1])}")
    print(f"  this run wrote brains: {_render_brains(mine[0])} | "
          f"repo: {repo or '(unset)'} | pairs: {_render_pairs(mine[1])}")
    if same_layer:
        print(f"  that is the layer this run wrote, so inspect {where} by hand")
    else:
        print(f"  to make this run's values take effect, write that layer too "
              f"(--config-file {where}), or remove the shadowing layer")


def default_skill_md() -> Path:
    return Path(__file__).resolve().parent.parent / "SKILL.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/repo of the wiki, e.g. acme-corp/company-brain")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--prefix", dest="commit_prefix", default=None)
    parser.add_argument(
        "--brains", default=None,
        help="comma-separated access set: every brain this install may touch, each "
             "as owner/repo[:read|read_write]. No suffix means read_write. Reading and "
             "writing are subsets of this set; the default brain is added if absent",
    )
    parser.add_argument(
        "--pair", action="append", default=None, metavar="PRIMARY=EXTENDS",
        help="declare two registered brains as designed together, e.g. "
             "acme/wiki=acme/commercial-wiki. Repeatable. Both members must be in "
             "the access set; references run extends -> primary, never back",
    )
    parser.add_argument(
        "--unset-pairs", action="store_true",
        help="clear every pair declaration and nothing else -- repo and brains are "
             "left exactly as they are. `unset` clears pairs too, but also the "
             "default brain and the whole access set; this is the escape hatch for "
             "a re-run that narrows --brains past a pair member, which is refused "
             "otherwise (a pair naming a brain outside the access set is inert)",
    )
    parser.add_argument(
        "--folders", default=None,
        help="comma-separated, e.g. organizations,people,projects,products,activities",
    )
    parser.add_argument(
        "--source-access", dest="source_access", choices=["read", "read_write"], default=None,
    )
    parser.add_argument(
        "--no-persist", action="store_true",
        help="suppress the machine-level config write; other layers still write if elected")
    parser.add_argument(
        "--skill-md", default=None,
        help="path to SKILL.md; defaults to the copy next to this script",
    )
    parser.add_argument(
        "--config-file", default=None, metavar="PATH",
        help="also write the install's config/brain.yml at PATH. This is the "
             "file an installed copy reads first; install.sh and build.py pass "
             "it, and a derived distribution repo owns its own copy")
    args = parser.parse_args(argv)

    if args.repo != UNSET and not REPO_RE.match(args.repo):
        print(f"error: repo '{args.repo}' doesn't look like owner/repo", file=sys.stderr)
        return 2

    if args.repo == UNSET and args.brains:
        print("error: `unset` clears the access set; --brains contradicts it", file=sys.stderr)
        return 2

    if args.repo == UNSET and args.pair:
        print("error: `unset` clears the pair declarations; --pair contradicts it", file=sys.stderr)
        return 2

    if args.unset_pairs and args.pair:
        print("error: --unset-pairs clears the pair declarations; --pair contradicts it",
              file=sys.stderr)
        return 2

    # A pair is validated for shape here and for registry membership later,
    # once the final access set is known -- a pair naming a brain the install
    # cannot touch would declare a relationship nothing can act on.
    pairs = []
    for entry in (args.pair or []):
        primary, sep, extends = entry.partition("=")
        primary, extends = primary.strip(), extends.strip()
        if not sep or not REPO_RE.match(primary) or not REPO_RE.match(extends):
            print(f"error: '{entry}' doesn't look like PRIMARY=EXTENDS "
                  "(two owner/repo names joined by '=')", file=sys.stderr)
            return 2
        if primary == extends:
            print(f"error: a brain cannot pair with itself: '{entry}'", file=sys.stderr)
            return 2
        pairs.append((primary, extends))

    # Validate each entry before touching the file: a half-written access set is
    # worse than an unchanged one, and a typo in an access level silently means
    # `read` at runtime.
    if args.brains:
        for entry in (e.strip() for e in args.brains.split(",")):
            if not entry:
                continue
            repo, _, access = entry.partition(":")
            if not REPO_RE.match(repo.strip()):
                print(f"error: '{repo.strip()}' doesn't look like owner/repo", file=sys.stderr)
                return 2
            if access and access.strip() not in ACCESS_LEVELS:
                print(f"error: access '{access.strip()}' for {repo.strip()} is not one of "
                      f"{', '.join(ACCESS_LEVELS)}", file=sys.stderr)
                return 2
        # The default is where a capture goes when nothing else decides, and a
        # capture is a write. Declaring it read-only is a contradiction worth
        # refusing rather than resolving.
        for entry in (e.strip() for e in args.brains.split(",")):
            repo, _, access = entry.partition(":")
            if repo.strip() == args.repo and access.strip() == "read":
                print(f"error: {args.repo} is the default brain, so it cannot be read-only. "
                      f"Name a writable default, or give this one read_write.", file=sys.stderr)
                return 2

    skill_md = Path(args.skill_md) if args.skill_md else default_skill_md()
    if not skill_md.exists():
        print(f"error: no SKILL.md at {skill_md}", file=sys.stderr)
        return 2

    text = skill_md.read_text(encoding="utf-8")

    try:
        block, start, end = extract_block(text)

        if args.repo == UNSET:
            block = patch_field(block, "repo", UNSET)
            block = patch_field(block, "brains", "")
            block = patch_field(block, "pairs", "")
            skill_md.write_text(text[:start] + block + text[end:], encoding="utf-8")
            if not args.no_persist:
                persist("", UNSET)
            if args.config_file:
                target = Path(args.config_file)
                if target.exists():
                    target.unlink()
                print(f"  cleared {target}")
            print(f"cleared {skill_md}")
            print("  repo: unset")
            print("  brains: (empty)")
            print("  pairs: (empty)")
            report_effective(skill_md, None, [])
            return 0

        updates = {"repo": args.repo}

        # ---- Each layer is its own fallback -----------------------------
        #
        # A field not given on the command line falls back to *that same
        # layer's* current contents. Never to another layer's, and never to
        # a cross-layer elected result. `which_brain.load_config` is
        # deliberately not called from here, because the read side and the
        # write side are asking different questions:
        #
        #   * load_config INJECTS the default brain into `brains` as a
        #     derived convenience (`cfg["brains"][cfg["repo"]] = READ_WRITE`).
        #     Consuming that and writing it back mints an *authored* registry
        #     entry nobody wrote -- so retargeting the default without
        #     --brains turned the old default into a standing read_write
        #     grant, even though a default deliberately absent from the set
        #     is a supported state.
        #   * this writer writes layers 2 and 3 always but layer 1 only when
        #     --config-file is passed, so the layer that wins the read was
        #     the one layer the writer left alone. An explicit narrowing was
        #     then silently re-widened by the next plain re-run, which copied
        #     layer 1's wider set down into layer 2.
        #
        # Reading each layer's own raw text through `brain_config.parse*`
        # dissolves both: the injection lives in load_config, not in the
        # parser, and a plain re-run becomes a no-op per layer instead of a
        # cross-layer copy.
        #
        # A layer is only read when something actually falls back to it. With
        # --brains and (--pair or --unset-pairs) every field is explicit, so
        # no layer is consulted at all -- which is why `--brains X
        # --unset-pairs` still works when some layer is unreadable, and why
        # an unreadable layer this run does not write can never affect it.
        needs_fallback = args.brains is None or (
            args.pair is None and not args.unset_pairs)

        def own_text(raw: str | None) -> str | None:
            """This layer's own authored text, or None if it authored none.

            `read_layer` distinguishes "absent" from "present"; this is the
            other half of the same distinction, and it is borrowed from the
            read side rather than invented a second time: a file that exists
            but names none of `config_schema`/`brains`/`repo`/`pairs`/
            `release_landing` is not a layer, which is exactly the call
            `which_brain._declares_config` makes when electing one.

            The distinction matters because a higher layer is taken WHOLE. A
            layer that authors emptiness (`brains: []`) is honoured as
            authored -- an emptied layer 1 masking layer 2 is intended. A
            layer that authors nothing is brand new, and see `seed` below.
            """
            return raw if which_brain._declares_config(raw) else None

        def authored(raw: str | None) -> tuple[list[str], list[tuple[str, str]]]:
            """One layer's (brains entries, pairs) after this run's arguments.

            Explicit --brains / --pair / --unset-pairs win outright, in every
            layer this run writes: an explicit narrowing must narrow. What is
            omitted comes from `raw` -- this layer's own text (or, for a layer
            that does not exist yet, its seed), nothing else.
            """
            text_ = raw or ""
            if args.brains is not None:
                entries = [b.strip() for b in args.brains.split(",") if b.strip()]
            else:
                entries = [
                    name if access == brain_config.READ_WRITE else f"{name}:{access}"
                    for name, access in brain_config.parse_brains(text_).items()
                ]
            # An empty access set adopts the default brain: a config naming a
            # repo it is not allowed to write to would be inert. Added without
            # a suffix, which means read_write -- the default has to be
            # writable. Nothing else is ever added implicitly: a repo missing
            # from `brains` is not a target, and that absence is deliberate.
            if not any(e.partition(":")[0].strip() == args.repo for e in entries):
                entries.append(args.repo)
            if args.unset_pairs:
                declared: list[tuple[str, str]] = []
            elif args.pair is not None:
                declared = pairs
            else:
                declared = brain_config.parse_pairs(text_)
            return entries, declared

        # Layer 3: SKILL.md's own block, which is also the text being patched.
        #
        # `existing` / `existing_pairs` stay block-derived and stay separate
        # from the fallback: they answer "does the block need patching",
        # which is a different question from "what does an omitted field fall
        # back to". `patch_field` can only rewrite flow style, so an
        # unchanged block-style value must not be forced through it.
        existing = []
        m = FIELD_PATTERNS["brains"].search(block)
        if m:
            existing = [b.strip() for b in m.group(2).split(",") if b.strip()]
        existing_pairs = brain_config.parse_pairs(block)
        block_own = own_text(block)
        listed, block_pairs = authored(block_own)

        # Layers 1 and 2, each read from itself. An unreadable one is refused
        # -- but only its own write is refused; see `UnreadableLayer`.
        #
        # `seed` is the one place a lower layer's text crosses into a higher
        # one, and it is not a fallback: it applies only to a layer that does
        # not exist yet. A higher layer is taken WHOLE by the read side, so
        # CREATING one out of nothing narrows the effective config to whatever
        # this run's arguments happened to name -- a plain re-run with no
        # layer 2 present minted one holding only the default brain and
        # shadowed a fully configured block, while reporting "no changes".
        # Seeding a brand-new layer from the highest authored layer below it
        # makes creating it a no-op for what `load_config` elects. Layers that
        # already exist are untouched by this: each still reads only itself,
        # so an explicit narrowing is never re-widened.
        #
        # Layer 2's seed is layer 3 -- layer 1 is HIGHER, so it is never a
        # seed for anything, which is what keeps a wider untouched layer 1
        # from being copied downwards. Layer 1's seed is layer 2 when this run
        # read it, else layer 3: a layer this run does not write is never
        # read, so under --no-persist layer 2 cannot seed anything. Any
        # divergence that leaves is reported, not silently resolved.
        skipped: list[UnreadableLayer] = []
        state_path = persisted_path()
        layer2 = layer1 = landing1 = None
        seed = block_own if needs_fallback else None
        if not args.no_persist:
            try:
                own2 = own_text(read_layer(state_path)) if needs_fallback else None
                layer2 = authored(own2 if own2 is not None else seed)
                if own2 is not None:
                    seed = own2
            except UnreadableLayer as exc:
                skipped.append(exc)
        if args.config_file:
            try:
                own1 = (own_text(read_layer(Path(args.config_file)))
                        if needs_fallback else None)
                layer1 = authored(own1 if own1 is not None else seed)
                # `release_landing` has no flag and never will -- it is
                # hand-edited, and it tells a derived distribution repo's
                # sync workflow whether a release merges itself or waits for
                # a human. So its only source is this same file's own current
                # contents: the per-layer self-fallback every other field
                # already uses. Never the seed, because resurrecting it from
                # a lower layer would undo a deliberately cleared layer 1 --
                # and no lower layer authors this field anyway. Dumping
                # without it dropped it on every rewrite.
                landing1 = brain_config.parse(own1)["release_landing"] if own1 else None
            except UnreadableLayer as exc:
                skipped.append(exc)

        if args.branch:
            updates["branch"] = args.branch
        if args.commit_prefix:
            updates["commit_prefix"] = args.commit_prefix
        if args.source_access:
            updates["source_access"] = args.source_access
        if args.folders:
            updates["folders"] = ", ".join(f.strip() for f in args.folders.split(","))

        # Both members of a pair must be in the access set (SKILL.md says so,
        # and a pair pointing outside it is inert). Checked per layer, against
        # that layer's own registry as it will stand after this run -- and
        # checked for every layer before any of them is written, because a
        # refused run must leave nothing written anywhere.
        for entries, declared, where in [
            (listed, block_pairs, skill_md),
            *([(layer2[0], layer2[1], state_path)] if layer2 else []),
            *([(layer1[0], layer1[1], Path(args.config_file))] if layer1 else []),
        ]:
            names = {e.partition(":")[0].strip() for e in entries}
            names.add(args.repo)
            for primary, extends in declared:
                for member in (primary, extends):
                    if member not in names:
                        print(f"error: pair member '{member}' is not in the access set "
                              f"({', '.join(sorted(names))}) of {where} -- add it via "
                              f"--brains first, or clear the pair with --unset-pairs",
                              file=sys.stderr)
                        return 2

        if m and listed != existing:
            updates["brains"] = ", ".join(listed)
        elif args.brains is not None:
            updates["brains"] = ", ".join(listed or [args.repo])
        if block_pairs != existing_pairs:
            updates["pairs"] = ", ".join(
                "{primary: %s, extends: %s}" % (p, e) for p, e in block_pairs)

        new_block = block
        for field, value in updates.items():
            new_block = patch_field(new_block, field, value)
    except ConfigError as exc:
        print(f"error: {exc} -- refusing to guess, fix the file or this script", file=sys.stderr)
        return 1

    for exc in skipped:
        print(str(exc), file=sys.stderr)

    # What each layer this run writes will hold, in precedence order, for the
    # divergence report at the end.
    wrote = []
    if layer1 is not None:
        wrote.append((Path(args.config_file), layer1[0], layer1[1]))
    if layer2 is not None:
        wrote.append((state_path, layer2[0], layer2[1]))
    wrote.append((skill_md, listed, block_pairs))

    written = None
    if layer2 is not None:
        written = persist(", ".join(layer2[0]), args.repo, layer2[1])
    if layer1 is not None:
        target = Path(args.config_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            brain_config.dump(
                brain_config.parse_brains("brains: [%s]" % ", ".join(layer1[0])),
                args.repo, pairs=layer1[1], release_landing=landing1),
            encoding="utf-8")
        print(f"  wrote {target}")

    if new_block == block:
        print("no changes -- config block already matches")
        if written:
            print(f"  persisted to {written}")
    else:
        skill_md.write_text(text[:start] + new_block + text[end:], encoding="utf-8")
        print(f"updated {skill_md}")
        for field, value in updates.items():
            print(f"  {field}: {value}")
        if written:
            print(f"  persisted to {written} — survives a plugin update")
    for exc in skipped:
        print(f"  {exc.path}: left alone, unreadable")

    report_effective(skill_md, args.repo, wrote)

    # A skipped layer has to be visible in the exit code. install.sh runs this
    # under `set -eu`, which reads nothing but the status: on exit 0 it printed
    # success and moved on, while every later read refused with exit 3 -- the
    # install reported done and the skill was inert until somebody repaired a
    # file nobody had been told to look at.
    return 4 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
