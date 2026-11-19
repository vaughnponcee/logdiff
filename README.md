# logdiff

`logdiff` compares two runs and shows what actually changed. It works on application logs, stack traces and agent tool-call histories. It reports:

- new errors
- new or moved crashes
- latency shifts
- changed tool sequences
- unexpected state mutations

It never shows line-by-line noise from timestamps, request IDs or PIDs.

It runs 100% locally. There are no network imports (a test enforces this), no telemetry and no dependencies. It needs only the Python 3.10+ standard library.

```bash
python -m logdiff before.log after.log                  # colored report
python -m logdiff before.log after.log --json diff.json # also write JSON
python -m logdiff a.jsonl b.jsonl --json -              # JSON to stdout only
```

Exit code: `0` means no regression, `1` means a regression, `2` means a usage or IO error. You can use it as a CI gate.

## What it looks like

These are the committed fixtures: two deploys of an order service, a pair of crash dumps, and two coding-agent runs.

```
$ python -m logdiff tests/fixtures/app_before.log tests/fixtures/app_after.log
logdiff  kind=log  lines 1201 -> 1285

NEW ERRORS (1)
  + x8    2026-11-18T14:00:17.815Z ERROR inventory reservation failed sku=SKU-136 reason=stale_version

LATENCY SHIFTS (2)
  p95      33.9ms ->      88.0ms  x2.6    <TS> INFO db query orders.by_customer took <DUR> rows=<N>
  p50      19.1ms ->      50.1ms  x2.63   <TS> INFO db query orders.by_customer took <DUR> rows=<N>

NEW MESSAGES (1)
  + x1    WARN  2026-11-18T14:02:37.972Z WARN  feature flag 'fast_checkout' enabled for 100% of traffic

VOLUME CHANGES (3)
  ~    55 -> 142   <TS> WARN payments retry attempt=<N> upstream=<IP>
  ~    16 -> 62    <TS> DEBUG cache miss key=cust:<N>
  ~    72 -> 36    <TS> DEBUG cache hit key=cust:<N>

RESOLVED (1)
  - <TS> ERROR legacy exporter failed: disk quota exceeded

REGRESSION
```

```
$ python -m logdiff tests/fixtures/crash_before.txt tests/fixtures/crash_after.txt
NEW CRASHES (3)
  + x1    KeyError @ worker.py:process < worker.py:run  "'eu-north-<N>'"
  + x1    TypeError @ pricing.py:apply_discount < worker.py:run  "unsupported operand type(s) for *: ..."
  + x1    java.lang.IllegalStateException @ com.shop.db.Pool.acquire < com.shop.orders.OrderRepo.find < ...

MOVED CRASHES (same type, new location) (1)
  ~ TypeError: TypeError @ models.py:price < worker.py:process < worker.py:run -> TypeError @ pricing.py:apply_discount < worker.py:run
```

```
$ python -m logdiff tests/fixtures/agent_before.jsonl tests/fixtures/agent_after.jsonl
TOOL SEQUENCE  similarity=86%  calls 6 -> 8
  a #1/1     grep: {"pattern": "def total"} -> {"pattern": "total"}
  + #3/3     read_file
  a #3/4     edit_file: {"lines": "17-19", "path": "src/pricing.py"} -> {"lines": "40-58", "path": "src/discounts.py"}
  a #4/5     run_tests: {"cmd": "pytest -q tests/test_pricing.py"} -> {"cmd": "pytest -q"}
  ! #4/5     run_tests now fails: 3 failed, 211 passed
  + #5/6     write_file

STATE MUTATIONS (5)
  + touched_secrets: true
  ~ status: done -> gave_up
  ~ tests_passed: true -> false
  ...
```

Run `python tests/fixtures/make_fixtures.py` to regenerate the fixtures. The generator is seeded, so the output is identical each time.

## How it works

**1. Templating.** Each log line is reduced to a template by masking volatile tokens in order: ISO timestamps, clock times, UUIDs, hex, long hashes, IPs (with ports), paths, durations, sizes and percentages, then bare numbers. Two lines that differ only in those tokens become the same template. The two runs are then compared as *template → count* maps, not as sequences of lines. That is why a reordered or interleaved log does not produce a wall of diff.

**2. Errors** are templates whose level is ERROR/FATAL/CRITICAL/PANIC:
- *new*: the template is absent before;
- *resolved*: the template is absent after;
- *rate change*: the count changed ≥ 2×.

**3. Crashes.** Stack traces are recognized and grouped into a signature: `ExceptionType @ innermost < caller < caller`, using the top three frames by file and function only. Line numbers are dropped, so an unrelated edit that shifts a line does not create a "new" crash; this is tested. Supported formats:
- Python tracebacks, including 3.11+ `~~~^^^` markers
- JVM/.NET `at` frames with `Caused by:`
- Node `at fn (file)` frames
- Rust `panicked at`

A crash with the same type but a different signature, whose old signature disappeared, is reported as **moved**. That usually means the same bug surfacing somewhere else.

**4. Latency.** Durations (`ms`, `s`, `us`, `ns`) are pulled out of every line *before* masking and are stored per template. p50 and p95 are compared, and a shift is reported when the ratio is ≥ `--ratio` (1.5× by default) and either side is ≥ 5 ms. Comparing per template means that a slow DB query is not averaged away by fast HTTP lines.

**5. Agent traces** (JSONL, one event per line, with a `tool` or `name` key). The tool-name sequences are aligned with `difflib.SequenceMatcher` to find inserts, deletes and replacements. For aligned calls it also reports changed `args` and calls that now fail (`error`, `is_error`, or `status: "error"`). `state` objects are merged in order, and the final states are diffed into added, removed and changed keys. `duration_ms` feeds into the latency check as `tool:<name>`.

## Trade-offs

- **Count-based, not order-based.** A change in *when* something happens is invisible for plain logs. Agent traces are the exception: there, order is the point, so they are sequence-aligned.
- **Masking can over-merge.** `user 42 deleted` and `user 7 deleted` become one template, which is what you want. `retry 1` and `retry 3` also merge, which is sometimes not what you want; their counts still show up under volume changes.
- **Format detection is heuristic.** If more than 80% of the first 20 lines are JSON objects with a `tool`, `name` or `type` key, the input is treated as an agent trace. Force it with `--kind log` or `--kind agent`.
- **Throughput is about 25k lines per second** (11 MB, 120k + 128k lines, in 10 s on one core). Regex masking dominates the cost. It is fine for CI artifacts and local runs, but it is not meant for streaming gigabytes.

## Layout

```
logdiff/   parse.py (masking, crash + agent parsing) · diff.py · render.py · cli.py
tests/     test_logdiff.py (26 tests) · fixtures/ (+ make_fixtures.py)
```

```bash
python -m unittest discover -s tests -t .
```

Color is on when stdout is a TTY. Set `NO_COLOR=1` or pass `--color never` to turn it off, or `--color always` to force it.

## License

MIT
