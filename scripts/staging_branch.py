#!/usr/bin/env python3
"""Decide which branch a sensed candidate is staged on.

Staging keeps one queue per writer, for three reasons that all still hold: no
two writers collide, the wiki's CI does not run on unreviewed content, and
something the author decides against never reaches a shared branch.

What "a writer" means depends on the surface, and getting it wrong is not
cosmetic:

  - **A personal surface** -- Claude Code, Codex, Gemini CLI, Claude Desktop --
    has one person behind it. The queue is theirs, named for their GitHub login.
  - **A shared channel** -- Claude Tag in Slack -- has no individual behind it
    at all. It runs as the organization's identity, and the meaningful unit is
    the channel: #platform-eng notices things about platform work, and the
    people who review them are the people in that channel. A queue named for
    the bot would collect every channel's candidates in one pile, which is the
    one arrangement guaranteed to be reviewed by nobody.

Resolution order, first that answers:

  1. `--channel` -- the caller knows it is in a shared channel and says so
  2. `--login` -- an explicit override
  3. the GitHub login of whoever will push (`gh api user`, then git config)
  4. refuse

Step 4 is deliberate. A branch name invented for an unidentifiable writer
either collides with somebody else's queue or hides in one nobody opens, and
both are worse than saying so. Refusing means: keep the candidate in the
conversation, and say once that staging is unavailable and why.

Usage:
    staging_branch.py                          personal surface
    staging_branch.py --channel "#platform-eng"   shared channel
    staging_branch.py --pattern "staging/{scope}" --channel eng-platform
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

DEFAULT_PATTERN = "staging/{scope}"
SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def slugify(value: str) -> str:
    """A branch-safe form of a channel name.

    Slack channel names are already close to safe, but a display name can carry
    spaces, emoji or a leading #, and git refs reject a good deal of that.
    """
    value = value.strip().lstrip("#").lower()
    value = SLUG_RE.sub("-", value).strip("-.")
    return value


def git_login() -> str | None:
    """Whoever would push from here, if that can be established."""
    for command in (
        ["gh", "api", "user", "--jq", ".login"],
        ["git", "config", "--get", "github.user"],
    ):
        try:
            out = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def decide(channel: str | None, login: str | None, pattern: str) -> dict:
    if channel:
        scope = slugify(channel)
        if not scope:
            return {"decision": "refuse", "exit": 4, "branch": None,
                    "reason": f"channel name {channel!r} leaves nothing usable in a branch name"}
        return {"decision": "channel", "exit": 0,
                "branch": pattern.format(scope=f"slack-{scope}", user=f"slack-{scope}"),
                "reason": f"shared channel — the queue belongs to {channel}, not to whoever typed"}

    resolved = login or git_login()
    if resolved:
        scope = slugify(resolved)
        return {"decision": "personal", "exit": 0,
                "branch": pattern.format(scope=scope, user=scope),
                "reason": f"personal surface — the queue belongs to {resolved}"}

    return {"decision": "refuse", "exit": 4, "branch": None,
            "reason": ("cannot establish who this queue belongs to: no channel was given and no "
                       "GitHub login could be read. Do not invent one — an invented name either "
                       "collides with somebody's queue or hides in one nobody opens. Hold the "
                       "candidates in the conversation and say once that staging is unavailable.")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--channel", help="the shared channel this session is in, e.g. '#platform-eng'")
    ap.add_argument("--login", help="override the GitHub login")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN,
                    help="branch pattern; {scope} is the writer, {user} is its retained alias")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = decide(args.channel, args.login, args.pattern)
    if args.json:
        import json
        print(json.dumps(result, indent=2))
    else:
        head = {"channel": "BRANCH", "personal": "BRANCH", "refuse": "REFUSE"}[result["decision"]]
        print(f"{head}: {result['branch'] or '-'}")
        print(f"  {result['reason']}")
    return result["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
