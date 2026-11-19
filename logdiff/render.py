"""Colored terminal rendering. Honors NO_COLOR and non-TTY output."""
from __future__ import annotations

import json
import os
import sys


class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _w(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def red(self, s): return self._w("31", s)
    def green(self, s): return self._w("32", s)
    def yellow(self, s): return self._w("33", s)
    def cyan(self, s): return self._w("36", s)
    def dim(self, s): return self._w("2", s)
    def bold(self, s): return self._w("1", s)


def color_default(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def _short(v, n=90) -> str:
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
    return s if len(s) <= n else s[: n - 3] + "..."


def render(d: dict, color: bool, limit: int = 15) -> str:
    st = Style(color)
    out = [st.bold(f"logdiff  kind={d['kind']}  lines {d['lines'][0]} -> {d['lines'][1]}"), ""]

    def section(title, items, fmt, paint):
        if not items:
            return
        out.append(paint(st.bold(f"{title} ({len(items)})")))
        for it in items[:limit]:
            out.append("  " + fmt(it))
        if len(items) > limit:
            out.append(st.dim(f"  ... {len(items) - limit} more (use --json for all)"))
        out.append("")

    section("NEW ERRORS", d["new_errors"],
            lambda e: f"{st.red('+')} x{e['count']:<4} {_short(e['example'], 120)}", st.red)
    section("NEW CRASHES", d["new_crashes"],
            lambda c: f"{st.red('+')} x{c['count']:<4} {c['signature']}" +
            (st.dim(f"  \"{_short(c['message'], 60)}\"") if c["message"] else ""), st.red)
    section("MOVED CRASHES (same type, new location)", d["moved_crashes"],
            lambda m: f"{st.yellow('~')} {m['type']}: {st.dim(m['from'])} -> {m['to']}", st.yellow)
    section("LATENCY SHIFTS", d["latency_shifts"],
            lambda s: (st.red if s["ratio"] > 1 else st.green)(
                f"{s['pct']:>3} {s['before_ms']:>9.1f}ms -> {s['after_ms']:>9.1f}ms  x{s['ratio']:<5}")
            + "  " + _short(s["template"], 70), st.yellow)
    section("ERROR RATE CHANGES", d["error_count_changes"],
            lambda e: f"{st.yellow('~')} {e['before']} -> {e['after']}  {_short(e['template'], 90)}", st.yellow)
    seq = d.get("tool_sequence")
    if seq:
        out.append(st.bold(f"TOOL SEQUENCE  similarity={seq['similarity']:.0%}  "
                           f"calls {seq['calls'][0]} -> {seq['calls'][1]}"))
        for o in seq["changes"][:limit]:
            at = f"#{o['at'][0]}/{o['at'][1]}"
            if o["op"] == "insert":
                out.append(f"  {st.green('+')} {at:<8} {', '.join(o['after'])}")
            elif o["op"] == "delete":
                out.append(f"  {st.red('-')} {at:<8} {', '.join(o['before'])}")
            elif o["op"] == "replace":
                out.append(f"  {st.yellow('~')} {at:<8} {', '.join(o['before'])} -> {', '.join(o['after'])}")
            elif o["op"] == "args":
                out.append(f"  {st.cyan('a')} {at:<8} {o['tool']}: {st.dim(_short(o['before'], 50))} -> {_short(o['after'], 50)}")
            elif o["op"] == "now_fails":
                out.append(f"  {st.red('!')} {at:<8} {o['tool']} now fails: {_short(o['error'], 70)}")
        if len(seq["changes"]) > limit:
            out.append(st.dim(f"  ... {len(seq['changes']) - limit} more"))
        out.append("")
    section("STATE MUTATIONS", d.get("state_changes", []),
            lambda s: {"added": st.green("+"), "removed": st.red("-"), "changed": st.yellow("~")}[s["op"]]
            + f" {s['key']}: " + (_short(s.get("before"), 40) + " -> " if "before" in s else "")
            + (_short(s.get("after"), 40) if "after" in s else ""), st.cyan)
    section("NEW MESSAGES", d["new_messages"],
            lambda m: f"{st.green('+')} x{m['count']:<4} {m['level'] or '-':5} {_short(m['example'], 110)}", lambda s: s)
    section("VOLUME CHANGES", d["volume_changes"],
            lambda v: f"{st.yellow('~')} {v['before']:>5} -> {v['after']:<5} {_short(v['template'], 90)}", lambda s: s)
    section("RESOLVED", [{"t": e["template"]} for e in d["resolved_errors"]]
            + [{"t": c["signature"]} for c in d["resolved_crashes"]],
            lambda r: f"{st.green('-')} {_short(r['t'], 110)}", st.green)
    out.append(st.red(st.bold("REGRESSION")) if d["regression"] else st.green(st.bold("no regression detected")))
    return "\n".join(out)
