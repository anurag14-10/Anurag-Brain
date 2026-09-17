#!/usr/bin/env python3
"""Write one activity-log entry file — the shell shortcut for Step 7.5.

Same protocol as every other surface: a logging event creates one small
file under `activities/entries/<YYYY-MM>/` and never rewrites the monthly
digest. This script exists so a shell surface can do that in one line and
get the filename conventions right:

    echo "- 2026-09-02: [project] Acme LMS — kickoff confirmed" | \\
        log_activity.py <wiki-root> --scope <your-github-login>

    log_activity.py <wiki-root> --scope <your-github-login> --digest   # also regenerate

The entry text must follow the ledger format (`- YYYY-MM-DD: ...`); the
month directory comes from the date in the first line, so a late-logged
event lands in the month it happened. Commit the entry file in the same
commit as the entity files it describes.

Pure standard library.
"""

from __future__ import annotations

import argparse
import getpass
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DATE_RE = re.compile(r"^- (\d{4}-\d{2})-\d{2}:")


def branch_safe(scope: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", scope.lower()).strip("-") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="wiki repository root")
    parser.add_argument("--scope", default=None,
                        help="the writer, as in staging_branch.py — a GitHub "
                             "login or channel; defaults to the local username")
    parser.add_argument("--text", default=None,
                        help="the entry; read from stdin when omitted")
    parser.add_argument("--digest", action="store_true",
                        help="also regenerate the month's digest locally")
    args = parser.parse_args()

    root = Path(args.root)
    if not (root / "CONVENTIONS.md").exists():
        print(f"{root} does not look like a wiki (no CONVENTIONS.md)", file=sys.stderr)
        return 2

    text = (args.text if args.text is not None else sys.stdin.read()).strip()
    match = DATE_RE.match(text)
    if not match:
        print("an entry starts `- YYYY-MM-DD: ` — the ledger format is the "
              "contract, and a digest built from malformed entries lies to "
              "its readers", file=sys.stderr)
        return 1

    month = match.group(1)
    scope = branch_safe(args.scope or getpass.getuser())
    entries_dir = root / "activities" / "entries" / month
    entries_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    target = entries_dir / f"{stamp}-{scope}-a1.md"
    suffix = 1
    while target.exists():
        suffix += 1
        target = entries_dir / f"{stamp}-{scope}-a{suffix}.md"
    target.write_text(text + "\n", encoding="utf-8")
    print(target.relative_to(root))

    if args.digest:
        builder = Path(__file__).resolve().parent / "build_activity_log.py"
        result = subprocess.run([sys.executable, str(builder), str(root)],
                                capture_output=True, text=True)
        print(result.stdout.strip())
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
