"""Runs ibkr_feed.run() against a fake ib_async to check the plumbing (not a substitute for a real TWS test)."""
import os, sys, types, tempfile, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ibkr_feed as f

class Con:
    def __init__(s, sym, ltd, local): s.symbol, s.lastTradeDateOrContractMonth, s.localSymbol = sym, ltd, local
class CD:
    def __init__(s, c): s.contract = c
class Bar:
    def __init__(s, d, o, h, l, c, v): s.date, s.open, s.high, s.low, s.close, s.volume = d, o, h, l, c, v
class Future:
    def __init__(s, **k): s.k = k
class IB:
    def connect(s, h, p, clientId, readonly, timeout):
        assert readonly is True
    def reqContractDetails(s, fut):
        m = fut.k["symbol"]; return [CD(Con(m, "20261028", m + "X6")), CD(Con(m, "20261125", m + "Z6"))]
    def reqHistoricalData(s, c, **k):
        base = datetime(2026, 10, 5, tzinfo=timezone.utc)
        if k["barSizeSetting"] == "1 day":      # real ib_async returns a plain date for daily bars
            return [Bar((base - timedelta(days=80 - i)).date(), 3.0, 3.1, 2.9, 3.05, 100) for i in range(80)]
        return [Bar(base + timedelta(minutes=15 * i), 3.0, 3.1, 2.9, 3.05, 100) for i in range(80)]
    def sleep(s, x): pass
    def disconnect(s): pass
    def isConnected(s): return True
sys.modules["ib_async"] = types.SimpleNamespace(IB=IB, Future=Future)
with tempfile.TemporaryDirectory() as d:
    f.OUT = d
    f.run(types.SimpleNamespace(host="x", port=7497, client_id=1, once=True, parent=0))
    names = sorted(os.listdir(d)); print(names)
    assert len(names) == 8, names
    j = json.load(open(os.path.join(d, "NG_4H.json"))); assert j["contract"] == "NGX6" and len(j["time"]) == 80 and j["source"] == "ibkr-feed"
    j = json.load(open(os.path.join(d, "CL_1D.json"))); assert len(j["time"]) == 80 and j["time"][-1] == "2026-10-03T22:00:00Z", j["time"][-1]
print("mock feed ok")
