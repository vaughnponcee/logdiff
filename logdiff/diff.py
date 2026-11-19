"""Compare two parsed documents and report only what changed."""
from __future__ import annotations

import difflib

from .parse import ERROR_LEVELS, LogDoc


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def diff(a: LogDoc, b: LogDoc, *, min_ratio: float = 2.0, latency_ratio: float = 1.5,
         latency_floor_ms: float = 5.0) -> dict:
    out: dict = {"kind": b.kind, "lines": [a.lines, b.lines]}

    # --- errors -----------------------------------------------------------
    def errs(doc):
        return {t: c for t, c in doc.templates.items() if doc.levels.get(t) in ERROR_LEVELS}

    ea, eb = errs(a), errs(b)
    out["new_errors"] = [{"template": t, "count": c, "example": b.first_seen[t]}
                         for t, c in sorted(eb.items(), key=lambda x: -x[1]) if t not in ea]
    out["resolved_errors"] = [{"template": t, "count": c} for t, c in ea.items() if t not in eb]
    out["error_count_changes"] = [
        {"template": t, "before": ea[t], "after": eb[t]}
        for t in ea.keys() & eb.keys()
        if max(ea[t], eb[t]) / max(1, min(ea[t], eb[t])) >= min_ratio]

    # --- crashes (grouped by signature) ---------------------------------------
    def sigs(doc):
        d: dict = {}
        for c in doc.crashes:
            d.setdefault(c.signature, []).append(c)
        return d

    ca, cb = sigs(a), sigs(b)
    out["new_crashes"] = [{"signature": s, "count": len(v), "type": v[0].exc_type,
                           "message": v[0].message, "frames": v[0].frames, "line": v[0].line_no}
                          for s, v in cb.items() if s not in ca]
    out["resolved_crashes"] = [{"signature": s, "count": len(v)} for s, v in ca.items() if s not in cb]
    # same exception type, different place: often the same bug moved
    moved = []
    types_a = {v[0].exc_type: s for s, v in ca.items()}
    for item in out["new_crashes"]:
        if item["type"] in types_a and types_a[item["type"]] not in cb:
            moved.append({"type": item["type"], "from": types_a[item["type"]], "to": item["signature"]})
    out["moved_crashes"] = moved

    # --- non-error template volume (warnings / chatter that appeared) ------------
    ta, tb = a.templates, b.templates
    new_t = [t for t in tb if t not in ta and t not in eb]
    out["new_messages"] = [{"template": t, "count": tb[t], "level": b.levels.get(t, ""),
                            "example": b.first_seen[t]}
                           for t in sorted(new_t, key=lambda t: -tb[t])]
    out["gone_messages"] = [{"template": t, "count": ta[t]} for t in ta if t not in tb and t not in ea]
    out["volume_changes"] = sorted(
        [{"template": t, "before": ta[t], "after": tb[t]}
         for t in ta.keys() & tb.keys() if t not in eb
         and max(ta[t], tb[t]) >= 5 and max(ta[t], tb[t]) / max(1, min(ta[t], tb[t])) >= min_ratio],
        key=lambda x: -abs(x["after"] - x["before"]))

    # --- latency ---------------------------------------------------------------
    shifts = []
    for t in a.latencies.keys() & b.latencies.keys():
        la, lb = a.latencies[t], b.latencies[t]
        for p in (50, 95):
            va, vb = pct(la, p), pct(lb, p)
            if max(va, vb) < latency_floor_ms:
                continue
            ratio = (vb + 1e-9) / (va + 1e-9)
            if ratio >= latency_ratio or ratio <= 1 / latency_ratio:
                shifts.append({"template": t, "pct": f"p{p}", "before_ms": round(va, 2),
                               "after_ms": round(vb, 2), "ratio": round(ratio, 2),
                               "n": [len(la), len(lb)]})
    out["latency_shifts"] = sorted(shifts, key=lambda s: -abs(s["after_ms"] - s["before_ms"]))

    # --- agent tool sequences --------------------------------------------------
    if a.calls or b.calls:
        sa = [c["tool"] for c in a.calls]
        sb = [c["tool"] for c in b.calls]
        ops = []
        sm = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                ops.append({"op": tag, "before": sa[i1:i2], "after": sb[j1:j2],
                            "at": [i1, j1]})
            else:  # same tool, same position: did its arguments change?
                for k in range(i2 - i1):
                    ca_, cb_ = a.calls[i1 + k], b.calls[j1 + k]
                    if ca_["args"] != cb_["args"]:
                        ops.append({"op": "args", "tool": ca_["tool"], "at": [i1 + k, j1 + k],
                                    "before": ca_["args"], "after": cb_["args"]})
                    if ca_["ok"] and not cb_["ok"]:
                        ops.append({"op": "now_fails", "tool": cb_["tool"], "at": [i1 + k, j1 + k],
                                    "error": cb_["error"]})
        out["tool_sequence"] = {"similarity": round(sm.ratio(), 3), "calls": [len(sa), len(sb)],
                                "changes": ops}
        sa_keys, sb_keys = set(a.state), set(b.state)
        out["state_changes"] = (
            [{"key": k, "op": "added", "after": b.state[k]} for k in sorted(sb_keys - sa_keys)]
            + [{"key": k, "op": "removed", "before": a.state[k]} for k in sorted(sa_keys - sb_keys)]
            + [{"key": k, "op": "changed", "before": a.state[k], "after": b.state[k]}
               for k in sorted(sa_keys & sb_keys) if a.state[k] != b.state[k]])

    out["regression"] = bool(out["new_errors"] or out["new_crashes"]
                             or any(s["ratio"] >= latency_ratio for s in out["latency_shifts"])
                             or any(o["op"] == "now_fails"
                                    for o in out.get("tool_sequence", {}).get("changes", [])))
    return out
