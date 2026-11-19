"""logdiff BEFORE AFTER [--json out.json] [--kind auto|log|agent] [--no-color]

Exit codes: 0 = no regression, 1 = regression (new errors/crashes, latency
up, tool now failing), 2 = usage error. No network access, ever.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .diff import diff
from .parse import parse
from .render import color_default, render


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="logdiff", description="Diff two logs/traces/agent histories")
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--kind", choices=["auto", "log", "agent"], default="auto")
    ap.add_argument("--json", metavar="FILE", help="write full diff as JSON ('-' for stdout)")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--limit", type=int, default=15, help="max items per section")
    ap.add_argument("--ratio", type=float, default=1.5, help="latency change ratio to report")
    a = ap.parse_args(argv)

    try:
        ta = Path(a.before).read_text(encoding="utf-8", errors="replace")
        tb = Path(a.after).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"logdiff: {e}", file=sys.stderr)
        return 2
    result = diff(parse(ta, a.kind), parse(tb, a.kind), latency_ratio=a.ratio)

    if a.json == "-":
        print(json.dumps(result, indent=2, default=str))
    else:
        color = {"always": True, "never": False}.get(a.color, color_default())
        if a.no_color:
            color = False
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if color and sys.platform == "win32":
            import os
            os.system("")  # enables ANSI escape processing on Windows consoles
        print(render(result, color, a.limit))
        if a.json:
            Path(a.json).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 1 if result["regression"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
