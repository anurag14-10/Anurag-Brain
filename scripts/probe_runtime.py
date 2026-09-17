#!/usr/bin/env python3
"""Report what this runtime actually provides. Facts, not documentation.

Different Claude surfaces give the skill different powers — a shell with git
and the network, a server-side sandbox with neither, or no execution at all —
and the documentation lags what any given org's settings allow. This probe
answers the question empirically, in one run, so capability claims in the
adapter references can cite observations instead of assumptions.

Run it anywhere the skill is installed:

    python3 scripts/probe_runtime.py

Pure standard library, read-only apart from one marker file in the system
temp directory (used to detect whether the environment persists between
runs). Never exits non-zero: an environment that cannot run it has answered
the question a different way.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def can_reach(host: str, port: int = 443, timeout: float = 4.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "reachable"
    except OSError as exc:
        return f"NO ({exc.__class__.__name__})"


def which(cmd: str) -> str:
    path = shutil.which(cmd)
    if not path:
        return "absent"
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True,
                             timeout=10).stdout.strip().splitlines()
        return f"{path} ({out[0] if out else 'version unknown'})"
    except Exception:
        return path


def importable(module: str) -> str:
    try:
        mod = importlib.import_module(module)
        return f"yes ({getattr(mod, '__version__', 'version unknown')})"
    except ImportError:
        return "NO"


def persistence_marker() -> str:
    marker = Path(tempfile.gettempdir()) / "brain-skill-probe-marker"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if marker.exists():
        previous = marker.read_text().strip()
        marker.write_text(now)
        return f"marker from a previous run found ({previous}) — environment persisted"
    try:
        marker.write_text(now)
        return "no earlier marker — first run in this environment (run again to test persistence)"
    except OSError as exc:
        return f"could not write marker: {exc}"


def main() -> int:
    here = Path(__file__).resolve()
    skill_dir = here.parent.parent

    lines = [
        "# Runtime probe",
        "",
        f"- when: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- python: {sys.version.split()[0]} at {sys.executable}",
        f"- platform: {platform.platform()}",
        f"- cwd: {os.getcwd()}",
        f"- script location: {here}",
        f"- skill dir contents: {sorted(p.name for p in skill_dir.iterdir())[:12]}",
        "",
        "## Dependencies",
        f"- PyYAML importable: {importable('yaml')}",
        f"- pip present: {which('pip3') if shutil.which('pip3') else which('pip')}",
        "",
        "## Tools",
        f"- git: {which('git')}",
        f"- gh: {which('gh')}",
        "",
        "## Network (TCP 443 connect test)",
        f"- pypi.org: {can_reach('pypi.org')}",
        f"- files.pythonhosted.org: {can_reach('files.pythonhosted.org')}",
        f"- github.com: {can_reach('github.com')}",
        f"- api.github.com: {can_reach('api.github.com')}",
        "",
        "## Filesystem",
        f"- temp dir writable: {os.access(tempfile.gettempdir(), os.W_OK)}",
        f"- home: {os.environ.get('HOME', '(unset)')}",
        f"- OUTPUT_DIR env: {os.environ.get('OUTPUT_DIR', '(unset)')}",
        f"- persistence: {persistence_marker()}",
        "",
        "## Environment hints",
    ]
    for key in sorted(os.environ):
        if any(s in key for s in ("CLAUDE", "ANTHROPIC", "SANDBOX", "CONTAINER")):
            lines.append(f"- {key}={os.environ[key][:80]}")
    if lines[-1] == "## Environment hints":
        lines.append("- (none matching CLAUDE/ANTHROPIC/SANDBOX/CONTAINER)")

    lines += [
        "",
        "## What this means for the skill",
        "- PyYAML yes → the bundled screens/validators can run here on provided text.",
        "- git + github.com yes → the full adapter-git path works here.",
        "- git or github.com NO → writes must go through the connector; keep them small.",
        "- pypi reachable but PyYAML NO → `pip install pyyaml` should succeed once.",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # a probe that crashes has still answered something
        print(f"probe crashed: {exc.__class__.__name__}: {exc}")
        raise SystemExit(0)
