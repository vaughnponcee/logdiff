"""Regenerate the fixture logs. Deterministic (seeded).

before/after pairs model a realistic regression between two deploys of an
order service: a cache change slows the DB path, a new KeyError crash
appears, retries spike, and the agent trace takes a different route.
"""
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent


def app_log(seed: int, after: bool) -> str:
    rng = random.Random(seed)
    out = []
    t = 0.0
    for i in range(1200):
        t += rng.expovariate(4)
        ts = f"2026-11-18T14:{int(t // 60) % 60:02d}:{t % 60:06.3f}Z"
        rid = "%08x-%04x-4%03x-a%03x-%012x" % tuple(rng.getrandbits(b) for b in (32, 16, 12, 12, 48))
        db_ms = rng.lognormvariate(3.0, 0.35) * (2.6 if after else 1.0)
        r = rng.random()
        if r < 0.55:
            out.append(f"{ts} INFO  http GET /orders/{rng.randint(1, 9999)} 200 {rng.randint(3, 40)}ms req={rid}")
        elif r < 0.80:
            out.append(f"{ts} INFO  db query orders.by_customer took {db_ms:.1f}ms rows={rng.randint(0, 50)}")
        elif r < 0.88:
            out.append(f"{ts} DEBUG cache {'miss' if rng.random() < (0.6 if after else 0.1) else 'hit'} key=cust:{rng.randint(1, 500)}")
        elif r < 0.93:
            n = 3 if after and rng.random() < 0.5 else 1
            for k in range(n):
                out.append(f"{ts} WARN  payments retry attempt={k + 1} upstream=10.0.3.{rng.randint(2, 40)}:8443")
        elif r < 0.96:
            out.append(f"{ts} ERROR payments timeout after 5000ms order={rng.randint(1, 9999)}")
        else:
            out.append(f"{ts} INFO  worker heartbeat pid={rng.randint(1000, 9000)} mem={rng.randint(200, 260)}MB")
        if after and i % 150 == 75:
            out.append(f"{ts} ERROR inventory reservation failed sku=SKU-{rng.randint(100, 999)} reason=stale_version")
        if after and i == 600:
            out.append(f"{ts} WARN  feature flag 'fast_checkout' enabled for 100% of traffic")
    if not after:
        out.append("2026-11-18T14:59:59.000Z ERROR legacy exporter failed: disk quota exceeded")
    return "\n".join(out) + "\n"


PY_CRASH_A = """\
2026-11-18T14:02:11.120Z INFO  worker starting batch=482
Traceback (most recent call last):
  File "/srv/app/worker.py", line 88, in run
    self.process(batch)
  File "/srv/app/worker.py", line 120, in process
    total = sum(item.price for item in batch.items)
  File "/srv/app/models.py", line 41, in price
    return self._cents / 100
TypeError: unsupported operand type(s) for /: 'NoneType' and 'int'
2026-11-18T14:02:11.300Z INFO  worker restarting
"""

PY_CRASH_B = """\
2026-11-18T16:40:03.004Z INFO  worker starting batch=517
Traceback (most recent call last):
  File "/srv/app/worker.py", line 88, in run
    self.process(batch)
  File "/srv/app/worker.py", line 131, in process
    region = self.regions[batch.customer.region_code]
             ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^
KeyError: 'eu-north-2'
2026-11-18T16:40:03.010Z INFO  worker restarting
Traceback (most recent call last):
  File "/srv/app/worker.py", line 88, in run
    self.process(batch)
  File "/srv/app/pricing.py", line 17, in apply_discount
    return price * (1 - pct / 100)
TypeError: unsupported operand type(s) for *: 'NoneType' and 'float'
2026-11-18T16:40:03.020Z INFO  worker restarting
"""

JVM_CRASH_B = """\
Exception in thread "main" java.lang.IllegalStateException: pool exhausted (max=32)
\tat com.shop.db.Pool.acquire(Pool.java:77)
\tat com.shop.orders.OrderRepo.find(OrderRepo.java:41)
\tat com.shop.api.OrdersController.get(OrdersController.java:23)
\t... 14 more
"""


def agent(after: bool) -> str:
    ev = [
        {"tool": "read_file", "args": {"path": "src/cart.py"}, "duration_ms": 12, "state": {"files_read": 1}},
        {"tool": "grep", "args": {"pattern": "def total"}, "duration_ms": 30},
        {"tool": "read_file", "args": {"path": "src/pricing.py"}, "duration_ms": 9, "state": {"files_read": 2}},
        {"tool": "edit_file", "args": {"path": "src/pricing.py", "lines": "17-19"}, "duration_ms": 22,
         "state": {"files_modified": ["src/pricing.py"]}},
        {"tool": "run_tests", "args": {"cmd": "pytest -q tests/test_pricing.py"}, "duration_ms": 4100,
         "state": {"tests_passed": True}},
        {"tool": "finish", "args": {}, "duration_ms": 1, "state": {"status": "done"}},
    ]
    if after:
        ev[1]["args"] = {"pattern": "total"}  # broader search
        ev.insert(3, {"tool": "read_file", "args": {"path": "src/discounts.py"}, "duration_ms": 10,
                      "state": {"files_read": 3}})
        ev[4] = {"tool": "edit_file", "args": {"path": "src/discounts.py", "lines": "40-58"}, "duration_ms": 25,
                 "state": {"files_modified": ["src/pricing.py", "src/discounts.py"]}}
        ev[5] = {"tool": "run_tests", "args": {"cmd": "pytest -q"}, "duration_ms": 31800,
                 "error": "3 failed, 211 passed", "state": {"tests_passed": False}}
        ev.insert(6, {"tool": "write_file", "args": {"path": ".env"}, "duration_ms": 3,
                      "state": {"touched_secrets": True}})
        ev[-1]["state"] = {"status": "gave_up"}
    return "\n".join(json.dumps(e) for e in ev) + "\n"


if __name__ == "__main__":
    (HERE / "app_before.log").write_text(app_log(1, False), encoding="utf-8")
    (HERE / "app_after.log").write_text(app_log(1, True), encoding="utf-8")
    (HERE / "crash_before.txt").write_text(PY_CRASH_A, encoding="utf-8")
    (HERE / "crash_after.txt").write_text(PY_CRASH_B + JVM_CRASH_B, encoding="utf-8")
    (HERE / "agent_before.jsonl").write_text(agent(False), encoding="utf-8")
    (HERE / "agent_after.jsonl").write_text(agent(True), encoding="utf-8")
    print("fixtures written to", HERE)
