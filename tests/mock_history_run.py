"""Runs history_export.main() against a fake ib_async (not a substitute for a real TWS test)."""
import os, sys, types, tempfile
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import history_export as h
class Bar:
    def __init__(s, d): s.date, s.open, s.high, s.low, s.close, s.volume = d, 3.0, 3.1, 2.9, 3.05, 100
class ContFuture:
    def __init__(s, **k): s.k = k
calls = []
class IB:
    def connect(s, host, p, clientId, readonly, timeout):
        assert readonly and clientId == 18
        if p != 7496: raise ConnectionRefusedError("refused")
    def qualifyContracts(s, c): pass
    def reqHistoricalData(s, c, **k):
        calls.append((c.k["symbol"], k["durationStr"]))
        if k["durationStr"] in ("20 Y", "15 Y"): return []
        return [Bar(date(2016, 10, 7) + timedelta(days=i)) for i in range(2500)]
    def sleep(s, x): pass
    def disconnect(s): pass
sys.modules["ib_async"] = types.SimpleNamespace(IB=IB, ContFuture=ContFuture)
with tempfile.TemporaryDirectory() as d:
    h.OUT = d
    rc = h.main(types.SimpleNamespace(host="x", port=0, client_id=18))
    assert rc == 0 and sorted(os.listdir(d)) == ["CL_daily_contfut.csv", "NG_daily_contfut.csv"], os.listdir(d)
    rows = open(os.path.join(d, "NG_daily_contfut.csv")).read().splitlines()
    assert rows[1] == "2016-10-07,3.0,3.1,2.9,3.05,100.0" and len(rows) == 2501, rows[:2]
print("mock history ok")
