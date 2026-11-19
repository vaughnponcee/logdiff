import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from logdiff.cli import main
from logdiff.diff import diff, pct
from logdiff.parse import detect_kind, durations_ms, level_of, mask, parse
from logdiff.render import render

FX = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return (FX / name).read_text(encoding="utf-8")


class MaskTests(unittest.TestCase):
    def test_volatile_tokens_masked(self):
        a = mask("2026-11-18T14:00:01.123Z INFO GET /orders/42 200 13ms req=1b4e28ba-2fa1-41d2-883f-0016d3cca427")
        b = mask("2026-11-19T09:12:59.999Z INFO GET /orders/7 200 250ms req=6fa459ea-ee8a-4ca4-894e-db77e160355e")
        self.assertEqual(a, b)
        self.assertIn("<UUID>", a)
        self.assertIn("<DUR>", a)

    def test_sizes_ips_hex_paths(self):
        m = mask("mem=238MB ptr=0x7ffe3a10 from 10.0.3.7:8443 file C:\\srv\\app\\x.py 95%")
        for tok in ("<SIZE>", "<HEX>", "<IP>", "<PATH>"):
            self.assertIn(tok, m)

    def test_identifiers_with_digits_survive(self):
        self.assertIn("utf8", mask("decode utf8 failed"))
        self.assertIn("v2", mask("api v2 call"))

    def test_levels(self):
        self.assertEqual(level_of("2026 WARNING disk"), "WARN")
        self.assertEqual(level_of("[ERR] boom"), "ERROR")
        self.assertEqual(level_of("CRITICAL thing"), "FATAL")
        self.assertEqual(level_of("plain line"), "")

    def test_durations(self):
        self.assertEqual(durations_ms("took 1.5s then 20ms and 300us"), [1500.0, 20.0, 0.3])

    def test_pct(self):
        self.assertEqual(pct([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(pct([], 95), 0.0)


class AppLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = diff(parse(load("app_before.log")), parse(load("app_after.log")))

    def test_new_error_found(self):
        tpl = [e["template"] for e in self.d["new_errors"]]
        self.assertEqual(len(tpl), 1)
        self.assertIn("inventory reservation failed", tpl[0])
        self.assertEqual(self.d["new_errors"][0]["count"], 8)

    def test_unchanged_error_not_reported_as_new(self):
        self.assertFalse(any("payments timeout" in e["template"] for e in self.d["new_errors"]))

    def test_resolved(self):
        self.assertTrue(any("legacy exporter" in e["template"] for e in self.d["resolved_errors"]))

    def test_latency_shift(self):
        db = [s for s in self.d["latency_shifts"] if "db query" in s["template"]]
        self.assertTrue(db)
        self.assertTrue(all(2.0 < s["ratio"] < 3.5 for s in db), db)
        # http latency did not change and must not be reported
        self.assertFalse(any("http GET" in s["template"] for s in self.d["latency_shifts"]))

    def test_volume_changes(self):
        tpls = {v["template"]: v for v in self.d["volume_changes"]}
        retry = next(v for t, v in tpls.items() if "retry" in t)
        self.assertGreater(retry["after"], 2 * retry["before"])
        self.assertTrue(any("cache miss" in t for t in tpls))

    def test_only_true_new_messages(self):
        self.assertEqual(len(self.d["new_messages"]), 1)
        self.assertIn("fast_checkout", self.d["new_messages"][0]["example"])

    def test_regression_flag(self):
        self.assertTrue(self.d["regression"])

    def test_identical_inputs_are_clean(self):
        d = diff(parse(load("app_before.log")), parse(load("app_before.log")))
        for k in ("new_errors", "new_crashes", "latency_shifts", "new_messages", "volume_changes"):
            self.assertEqual(d[k], [], k)
        self.assertFalse(d["regression"])


class CrashTests(unittest.TestCase):
    def test_python_and_jvm(self):
        a, b = parse(load("crash_before.txt")), parse(load("crash_after.txt"))
        self.assertEqual(len(a.crashes), 1)
        self.assertEqual(a.crashes[0].frames, ["worker.py:run", "worker.py:process", "models.py:price"])
        self.assertEqual(len(b.crashes), 3)
        d = diff(a, b)
        types = {c["type"] for c in d["new_crashes"]}
        self.assertEqual(types, {"KeyError", "TypeError", "java.lang.IllegalStateException"})
        jvm = next(c for c in d["new_crashes"] if c["type"].startswith("java"))
        self.assertEqual(jvm["frames"][-1], "com.shop.db.Pool.acquire")  # innermost last
        self.assertEqual(len(d["moved_crashes"]), 1)
        self.assertEqual(d["moved_crashes"][0]["type"], "TypeError")

    def test_node_and_rust(self):
        node = "TypeError: Cannot read properties of undefined (reading 'id')\n" \
               "    at getUser (/app/src/users.js:14:22)\n    at async handler (/app/src/api.js:8:5)\n"
        rust = "thread 'main' panicked at 'index out of bounds', src/main.rs:10:5\n"
        self.assertEqual(parse(node).crashes[0].frames, ["handler", "getUser"])
        self.assertEqual(parse(rust).crashes[0].exc_type, "panic")

    def test_same_crash_different_line_numbers_is_same_signature(self):
        a = load("crash_before.txt")
        b = a.replace("line 120", "line 133").replace("batch=482", "batch=999")
        d = diff(parse(a), parse(b))
        self.assertEqual(d["new_crashes"], [])
        self.assertFalse(d["regression"])


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a, cls.b = load("agent_before.jsonl"), load("agent_after.jsonl")
        cls.d = diff(parse(cls.a), parse(cls.b))

    def test_detect(self):
        self.assertEqual(detect_kind(self.a), "agent")
        self.assertEqual(detect_kind(load("app_before.log")), "log")

    def test_sequence(self):
        seq = self.d["tool_sequence"]
        self.assertEqual(seq["calls"], [6, 8])
        ops = [o["op"] for o in seq["changes"]]
        self.assertIn("insert", ops)
        self.assertIn("args", ops)
        self.assertIn("now_fails", ops)
        inserted = [t for o in seq["changes"] if o["op"] == "insert" for t in o["after"]]
        self.assertEqual(sorted(inserted), ["read_file", "write_file"])

    def test_state(self):
        st = {s["key"]: s for s in self.d["state_changes"]}
        self.assertEqual(st["touched_secrets"]["op"], "added")
        self.assertEqual(st["status"]["after"], "gave_up")
        self.assertEqual(st["tests_passed"]["before"], True)

    def test_tool_latency(self):
        self.assertTrue(any(s["template"] == "tool:run_tests" and s["ratio"] > 7
                            for s in self.d["latency_shifts"]))


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(list(args))
        return code, buf.getvalue()

    def test_exit_codes(self):
        code, _ = self.run_cli(str(FX / "app_before.log"), str(FX / "app_after.log"), "--color", "never")
        self.assertEqual(code, 1)
        code, out = self.run_cli(str(FX / "app_before.log"), str(FX / "app_before.log"), "--color", "never")
        self.assertEqual(code, 0)
        self.assertIn("no regression", out)
        self.assertEqual(main(["/no/such/file", "/nope"]), 2)

    def test_json_export(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "d.json"
            self.run_cli(str(FX / "agent_before.jsonl"), str(FX / "agent_after.jsonl"),
                         "--json", str(out), "--no-color")
            data = json.loads(out.read_text())
            self.assertIn("tool_sequence", data)
        _, out = self.run_cli(str(FX / "crash_before.txt"), str(FX / "crash_after.txt"), "--json", "-")
        self.assertEqual(len(json.loads(out)["new_crashes"]), 3)

    def test_color_switch(self):
        d = diff(parse(load("app_before.log")), parse(load("app_after.log")))
        self.assertIn("\033[31m", render(d, True))
        self.assertNotIn("\033[", render(d, False))
        os.environ["NO_COLOR"] = "1"
        try:
            _, out = self.run_cli(str(FX / "app_before.log"), str(FX / "app_after.log"))
            self.assertNotIn("\033[", out)
        finally:
            del os.environ["NO_COLOR"]

    def test_no_network_imports(self):
        src = "".join(p.read_text() for p in (Path(__file__).parent.parent / "logdiff").glob("*.py"))
        for mod in ("socket", "urllib", "http.client", "requests"):
            self.assertNotIn(f"import {mod}", src)


class PerfTests(unittest.TestCase):
    def test_100k_lines_under_budget(self):
        big = load("app_before.log") * 80  # ~96k lines
        t = time.perf_counter()
        diff(parse(big), parse(big))
        self.assertLess(time.perf_counter() - t, 30)


if __name__ == "__main__":
    unittest.main()
