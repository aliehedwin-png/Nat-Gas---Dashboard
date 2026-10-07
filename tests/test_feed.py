import os, sys, tempfile, unittest, json
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ibkr_feed as f
import ta_engine as t


class Feed(unittest.TestCase):
    C = [("NGX6", date(2026, 10, 28)), ("NGZ6", date(2026, 11, 25))]

    def test_front_by_default(self):
        self.assertEqual(f.pick_contract(self.C, {"NGX6": [9, 9], "NGZ6": [1, 1]}, date(2026, 10, 6)), "NGX6")

    def test_roll_on_volume_two_sessions(self):
        self.assertEqual(f.pick_contract(self.C, {"NGX6": [9, 5, 5], "NGZ6": [1, 6, 6]}, date(2026, 10, 6)), "NGZ6")
        self.assertEqual(f.pick_contract(self.C, {"NGX6": [9, 9, 5], "NGZ6": [1, 1, 6]}, date(2026, 10, 6)), "NGX6")

    def test_roll_near_expiry(self):
        self.assertEqual(f.pick_contract(self.C, {}, date(2026, 10, 22)), "NGZ6")

    def test_expired_skipped(self):
        self.assertEqual(f.pick_contract(self.C, {}, date(2026, 10, 29)), "NGZ6")

    def test_payload_roundtrip_and_validity(self):
        bars = [(1791320400 + 900 * i, 3.0 + i / 100, 3.05 + i / 100, 2.98 + i / 100, 3.02 + i / 100, 10 + i) for i in range(5)]
        self.assertTrue(f.valid(bars))
        self.assertFalse(f.valid([(1, 3.0, 2.9, 2.8, 3.0, 1)]))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "NG_15m.json")
            f.write_atomic(p, f.to_payload("NG", "NGX6", "15m", bars, 1791324000))
            b = t.load_bars_file(p)
            self.assertEqual(b["t"], [x[0] for x in bars])
            self.assertEqual(b["c"], [x[4] for x in bars])
            self.assertEqual(b["meta"]["contract"], "NGX6")
            self.assertFalse(os.path.exists(p + ".tmp"))


class Closed(unittest.TestCase):
    def test_market_shut_means_session_bar_is_complete(self):
        # daily bar opened Mon 2026-10-05 22:00Z (17:00 CT); market shut at 21:00Z Tue; fetched 21:20Z
        bars = {"t": [t.iso_to_ts("2026-10-04T22:00:00Z"), t.iso_to_ts("2026-10-05T22:00:00Z")], "o": [1, 1], "h": [1, 1], "l": [1, 1], "c": [1, 1], "v": [1, 1], "meta": {}}
        self.assertEqual(len(t.closed_only(bars, "1D", t.iso_to_ts("2026-10-06T21:20:00Z"))["t"]), 2)
        self.assertEqual(len(t.closed_only(bars, "1D", t.iso_to_ts("2026-10-06T20:30:00Z"))["t"]), 1)  # still open: forming bar dropped


class Schedule(unittest.TestCase):
    def test_due(self):
        T = 1791400000 // 900 * 900                     # a 15m boundary
        self.assertFalse(f.due(T - 30, T - 60, 60, 900))       # 30 s since the last download, no close in between
        self.assertTrue(f.due(T - 30, T - 95, 60, 900))        # refresh interval passed
        self.assertFalse(f.due(T + 5, T - 40, 60, 900))        # bar closed, but wait CLOSE_DELAY for the final trades
        self.assertTrue(f.due(T + 10, T - 40, 60, 900))        # 10 s after the close: fetch now, not at the next interval
        self.assertFalse(f.due(T + 15, T + 10, 60, 900))       # already fetched after this close
        H = T // 3600 * 3600 + 3600                            # hourly check for 4H / Daily closes
        self.assertTrue(f.due(H + 12, H - 300, 600, 3600))
        self.assertFalse(f.due(H - 100, H - 400, 600, 3600))

    def test_daily_dates_stamped_at_session_open(self):
        from datetime import date as D
        class B:
            date, open, high, low, close, volume = D(2026, 10, 6), 1, 2, 0.5, 1.5, 10
        class IB:
            def reqHistoricalData(self, *a, **k): return [B()]
        self.assertEqual(f.iso(f.fetch_bars(IB(), None, "1 day", "1 Y")[0][0]), "2026-10-05T22:00:00Z")


if __name__ == "__main__":
    unittest.main()
