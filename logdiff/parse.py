"""Turn raw text into comparable structure.

Three input kinds, auto-detected:
  log    - line-oriented app logs; each line is reduced to a *template* by
           masking volatile tokens (timestamps, ids, numbers, paths ...)
  trace  - stack traces (Python, JVM/.NET 'at ...', Node, Rust panics) mixed into
           logs are grouped into crash *signatures*
  agent  - JSONL tool-call history ({"tool": ..., "args": ..., ...})
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Order matters: more specific patterns first.
MASKS = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TIME>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<HEX>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<HASH>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-]+){2,}"), "<PATH>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|us|µs|ns|sec|secs|seconds)\b"), "<DUR>"),
    (re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:[KMGT]i?B|B|%)(?!\w)"), "<SIZE>"),
    (re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])"), "<N>"),
]
LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|ERR|FATAL|CRITICAL|PANIC)\b")
ERROR_LEVELS = {"ERROR", "ERR", "FATAL", "CRITICAL", "PANIC"}
DUR_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(ms|s|us|µs|ns|sec|secs|seconds)\b")
UNIT_MS = {"ms": 1, "s": 1000, "sec": 1000, "secs": 1000, "seconds": 1000,
           "us": 1e-3, "µs": 1e-3, "ns": 1e-6}

# stack-trace recognisers
PY_TB_START = re.compile(r"^Traceback \(most recent call last\):")
PY_FRAME = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)')
PY_EXC = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)\b|[A-Za-z_][\w.]*Error):?\s?(.*)$")
JVM_FRAME = re.compile(r"^\s+at\s+([\w$.<>/]+)\s*\(")
JVM_EXC = re.compile(r"^(?:Exception in thread \"[^\"]*\"\s+|Caused by:\s+)?([a-zA-Z_$][\w$]*(?:\.[\w$]+)+(?:Exception|Error)):?\s?(.*)$")
NODE_FRAME = re.compile(r"^\s+at\s+(?:async\s+)?([\w$.<>]+)\s+\(")
RUST_PANIC = re.compile(r"thread '([^']*)' panicked at (?:'([^']*)', )?([^:\s]+)")


def mask(line: str) -> str:
    for rx, repl in MASKS:
        line = rx.sub(repl, line)
    return re.sub(r"\s+", " ", line).strip()


def level_of(line: str) -> str:
    m = LEVEL_RE.search(line)
    if not m:
        return ""
    lv = m.group(1)
    return {"WARNING": "WARN", "ERR": "ERROR", "CRITICAL": "FATAL"}.get(lv, lv)


def durations_ms(line: str) -> list[float]:
    return [float(v) * UNIT_MS[u] for v, u in DUR_RE.findall(line)]


@dataclass
class Crash:
    exc_type: str
    message: str
    frames: list[str] = field(default_factory=list)  # innermost last
    line_no: int = 0

    @property
    def signature(self) -> str:
        top = " < ".join(reversed(self.frames[-3:])) if self.frames else "?"
        return f"{self.exc_type} @ {top}"


@dataclass
class LogDoc:
    kind: str
    lines: int = 0
    templates: dict = field(default_factory=dict)     # template -> count
    first_seen: dict = field(default_factory=dict)    # template -> example raw line
    levels: dict = field(default_factory=dict)        # template -> level
    latencies: dict = field(default_factory=dict)     # template -> [ms]
    crashes: list = field(default_factory=list)
    calls: list = field(default_factory=list)         # agent: list of dicts
    state: dict = field(default_factory=dict)         # agent: final merged state


def _norm_frame(path: str, func: str) -> str:
    base = re.split(r"[\\/]", path)[-1]
    return f"{base}:{func}"


def parse_text(text: str) -> LogDoc:
    doc = LogDoc(kind="log")
    lines = text.splitlines()
    doc.lines = len(lines)
    i = 0
    while i < len(lines):
        raw = lines[i]
        # ---- Python traceback block
        if PY_TB_START.search(raw):
            frames, j = [], i + 1
            while j < len(lines):
                m = PY_FRAME.match(lines[j])
                if m:
                    frames.append(_norm_frame(m.group(1), m.group(2)))
                    j += 1
                    continue
                if lines[j].startswith((" ", "\t")):  # source line / ^^^^ markers
                    j += 1
                    continue
                em = PY_EXC.match(lines[j].strip())
                if em:
                    doc.crashes.append(Crash(em.group(1), mask(em.group(2)), frames, i + 1))
                    j += 1
                break
            i = max(j, i + 1)
            continue
        # ---- JVM / .NET style exception header followed by 'at' frames
        jm = JVM_EXC.match(raw.strip())
        if jm and i + 1 < len(lines) and JVM_FRAME.match(lines[i + 1]):
            frames, j = [], i + 1
            while j < len(lines) and (JVM_FRAME.match(lines[j]) or lines[j].strip().startswith("...")):
                fm = JVM_FRAME.match(lines[j])
                if fm:
                    frames.append(fm.group(1))
                j += 1
            doc.crashes.append(Crash(jm.group(1), mask(jm.group(2)), list(reversed(frames)), i + 1))
            i = j
            continue
        # ---- Node: "TypeError: x" followed by '    at fn (file)'
        if i + 1 < len(lines) and NODE_FRAME.match(lines[i + 1]) and ":" in raw:
            etype, _, msg = raw.strip().partition(":")
            frames, j = [], i + 1
            while j < len(lines) and NODE_FRAME.match(lines[j]):
                frames.append(NODE_FRAME.match(lines[j]).group(1))
                j += 1
            doc.crashes.append(Crash(etype.split()[-1], mask(msg.strip()), list(reversed(frames)), i + 1))
            i = j
            continue
        rm = RUST_PANIC.search(raw)
        if rm:
            doc.crashes.append(Crash("panic", mask(rm.group(2) or ""), [rm.group(3)], i + 1))

        if raw.strip():
            t = mask(raw)
            doc.templates[t] = doc.templates.get(t, 0) + 1
            doc.first_seen.setdefault(t, raw.strip())
            doc.levels.setdefault(t, level_of(raw))
            ds = durations_ms(raw)
            if ds:
                doc.latencies.setdefault(t, []).extend(ds)
        i += 1
    if doc.crashes and len(doc.templates) < 3:
        doc.kind = "trace"
    return doc


def parse_agent(text: str) -> LogDoc:
    doc = LogDoc(kind="agent")
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        doc.lines += 1
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = ev.get("tool") or ev.get("name") or ev.get("type") or "?"
        call = {"i": len(doc.calls), "line": n, "tool": name, "args": ev.get("args", ev.get("input", {})),
                "ok": not (ev.get("error") or ev.get("is_error") or ev.get("status") == "error"),
                "error": ev.get("error"), "ms": ev.get("duration_ms", ev.get("latency_ms"))}
        doc.calls.append(call)
        if not call["ok"]:
            t = f"{name}: {mask(str(ev.get('error')))}"
            doc.templates[t] = doc.templates.get(t, 0) + 1
            doc.levels[t] = "ERROR"
            doc.first_seen.setdefault(t, f"{name}: {ev.get('error')}")
        if call["ms"] is not None:
            doc.latencies.setdefault(f"tool:{name}", []).append(float(call["ms"]))
        if isinstance(ev.get("state"), dict):
            doc.state.update(ev["state"])
    return doc


def detect_kind(text: str) -> str:
    sample = [ln for ln in text.splitlines()[:20] if ln.strip()]
    if sample and sum(ln.lstrip().startswith("{") for ln in sample) / len(sample) > 0.8:
        try:
            ev = json.loads(sample[0])
            if any(k in ev for k in ("tool", "name", "type")):
                return "agent"
        except json.JSONDecodeError:
            pass
    return "log"


def parse(text: str, kind: str = "auto") -> LogDoc:
    kind = detect_kind(text) if kind == "auto" else kind
    return parse_agent(text) if kind == "agent" else parse_text(text)
