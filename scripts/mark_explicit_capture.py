#!/usr/bin/env python3
"""Tell the background sensing pass that this turn already captured.

Run from inside the conversation, after a successful explicit capture
(Step 7). The post-turn sensing hook checks for this sentinel and skips the
turn — a turn where the user just decided what to keep must not have the
same content re-staged behind their back.

The conversation cannot see the hook's per-turn id, so the sentinel keys on
$CLAUDE_SESSION_ID where the environment provides it and on a hash of the
working directory where it does not. The sensing side checks both keys and
consumes whichever it finds; a sentinel older than ten minutes guards
nothing and is ignored there.

Usage:
    mark_explicit_capture.py [--cwd DIR] [--data-dir DIR]

Pure standard library. Exit 0 on success; this script writing nowhere is
worth surfacing, so failures exit 1 with a one-line reason.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import paths  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    data = Path(args.data_dir) if args.data_dir else paths.data_dir()
    key = paths.sentinel_keys(os.environ.get("CLAUDE_SESSION_ID"), args.cwd)[0]
    sentinel = data / "sentinels" / key
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"could not write the capture sentinel: {exc}", file=sys.stderr)
        return 1
    print(f"marked: {sentinel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
