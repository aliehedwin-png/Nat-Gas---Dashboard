"""Unit tests for ta_engine.py (stdlib unittest). Run:  python -m unittest discover -s tests -v"""
import math
import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ta_engine as ta

CFG = ta.config()


def utc(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


def zigzag(pivots, per_leg=12, wick=0.05):
    """Close series that moves linearly between pivot prices; returns bars dict with small wicks."""
    c = []
    for a, b in zip(pivots, pivots[1:]):
        for i in range(per_leg):
            c.append(a + (b - a) * i / per_leg)
    c.append(pivots[-1])
    o = [c[0]] + c[:-1]
    h = [max(x, y) + wick for x, y in zip(o, c)]
    l = [min(x, y) - wick for x, y in zip(o, c)]
    t = [1_700_000_000 + 3600 * i for i in range(len(c))]
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": [100] * len(c)}


class Indicators(unittest.TestCase):
    def test_sma_ema_known_values(self):
        x = [1, 2, 3, 4, 5]
        self.assertEqual(ta.sma(x, 3), [None, None, 2.0, 3.0, 4.0])
        e = ta.ema(x, 3)                       # seed = SMA(1,2,3)=2; k=0.5 -> 3.0, 4.0
        self.assertEqual(e, [None, None, 2.0, 3.0, 4.0])

    def test_rsi_extremes(self):
        up = list(range(1, 40))
        self.assertEqual(ta.rsi(up)[-1], 100.0)
        down = list(range(40, 1, -1))
        self.assertAlmostEqual(ta.rsi(down)[-1], 0.0, places=6)

    def test_atr_constant_range(self):
        h = [11.0] * 30
        l = [9.0] * 30
        c = [10.0] * 30
        self.assertAlmostEqual(ta.atr(h, l, c)[-1], 2.0)

    def test_bollinger_flat_series_has_zero_width(self):
        u, m, lo = ta.bollinger([5.0] * 40)
        self.assertAlmostEqual(u[-1], 5.0)
        self.assertAlmostEqual(lo[-1], 5.0)


class Structure(unittest.TestCase):
    def _swings(self, pivots):
        b = zigzag(pivots)
        a = ta.atr(b["h"], b["l"], b["c"])
        return ta.find_swings(b["h"], b["l"], a, 3, 1.0)

    def test_uptrend(self):
        seq = self._swings([10, 14, 12, 16, 14, 18, 16, 20, 18])
        self.assertEqual(ta.trend_state(seq), "up")
        self.assertTrue(all(seq[i][1] != seq[i + 1][1] for i in range(len(seq) - 1)), "swings must alternate")

    def test_downtrend(self):
        seq = self._swings([20, 16, 18, 14, 16, 12, 14, 10, 12])
        self.assertEqual(ta.trend_state(seq), "down")

    def test_range(self):
        seq = self._swings([10, 14, 11, 14, 11, 14, 11, 14, 11])
        self.assertEqual(ta.trend_state(seq), "range")

    def test_small_swings_are_filtered(self):
        # 40 very wide flat bars (large ATR), then wiggles far smaller than that ATR: they must not become swings
        n_wide = 40
        h = [13.0] * n_wide
        l = [7.0] * n_wide
        c = [10.0] * n_wide
        for i in range(40):
            mid = 10.0 + 0.1 * math.sin(i * 0.9)      # distinct peaks and troughs
            h.append(mid + 0.05)
            l.append(mid - 0.05)
            c.append(mid)
        a = ta.atr(h, l, c)
        seq = ta.find_swings(h, l, a, 3, 1.0)
        self.assertLessEqual(len(seq), 1, seq)
        # the same wiggles with a 0.05x ATR threshold do become swings (the filter is what removed them)
        self.assertGreater(len(ta.find_swings(h, l, a, 3, 0.05)), 3)

    def test_fibonacci_levels(self):
        f = ta.fib_levels([(0, "L", 10.0), (5, "H", 14.0)])
        self.assertEqual(f["dir"], "up")
        self.assertAlmostEqual(f["levels"]["0.382"], 14 - 0.382 * 4)
        self.assertAlmostEqual(f["levels"]["0.5"], 12.0)
        self.assertAlmostEqual(f["levels"]["0.618"], 14 - 0.618 * 4)
        g = ta.fib_levels([(0, "H", 14.0), (5, "L", 10.0)])
        self.assertEqual(g["dir"], "down")
        self.assertAlmostEqual(g["levels"]["0.5"], 12.0)
        self.assertAlmostEqual(g["levels"]["0.382"], 10 + 0.382 * 4)

    def test_confluence_zone_scoring(self):
        zones = ta.build_zones(price=103, atr_v=1.0, ma_vals={"EMA20": 100.0}, ma_cluster=None,
                               fib={"levels": {"0.5": 100.2}}, swing_prices=[("Swing low", 100.25)],
                               rounds=[105.0], bb={"BB lower": None}, width_atr=0.3)
        best = max(zones, key=lambda z: z["score"])
        self.assertEqual(best["score"], 3)
        self.assertEqual(best["cats"], ["fib", "ma", "swing"])
        self.assertAlmostEqual(best["lo"], 100.0)
        self.assertAlmostEqual(best["hi"], 100.25)
        self.assertEqual([z["score"] for z in zones if z["lo"] == 105.0], [1])

    def test_same_category_counts_once(self):
        zones = ta.build_zones(100, 1.0, {"EMA20": 100.0, "EMA50": 100.1, "SMA20": 100.2}, None, None, [], [], {}, 0.3)
        self.assertEqual(zones[0]["score"], 1)


class TimeHelpers(unittest.TestCase):
    def test_et_offset_around_dst(self):
        self.assertEqual(ta.et_offset_hours(utc(2026, 3, 8, 6, 59)), -5)
        self.assertEqual(ta.et_offset_hours(utc(2026, 3, 8, 7, 0)), -4)
        self.assertEqual(ta.et_offset_hours(utc(2026, 11, 1, 5, 59)), -4)
        self.assertEqual(ta.et_offset_hours(utc(2026, 11, 1, 6, 0)), -5)

    def test_et_to_ts(self):
        self.assertEqual(ta.et_to_ts(date(2026, 10, 8), 10, 30), utc(2026, 10, 8, 14, 30))   # EDT
        self.assertEqual(ta.et_to_ts(date(2026, 12, 10), 10, 30), utc(2026, 12, 10, 15, 30))  # EST

    def test_scheduled_events_weekday(self):
        ev = ta.scheduled_events("NG", utc(2026, 10, 6, 12), count=2)
        self.assertEqual(ev[0]["ts"], utc(2026, 10, 8, 14, 30))                 # Thursday 10:30 ET
        self.assertEqual(datetime.fromtimestamp(ev[0]["ts"], timezone.utc).weekday(), 3)
        cl = ta.scheduled_events("CL", utc(2026, 10, 6, 12), count=1)
        self.assertEqual(cl[0]["ts"], utc(2026, 10, 7, 14, 30))                 # Wednesday 10:30 ET
        self.assertEqual(datetime.fromtimestamp(cl[0]["ts"], timezone.utc).weekday(), 2)

    def test_event_window(self):
        ev = [{"name": "x", "ts": utc(2026, 10, 8, 14, 30)}]
        self.assertTrue(ta.in_event_window(utc(2026, 10, 8, 14, 44), ev, 15))
        self.assertTrue(ta.in_event_window(utc(2026, 10, 8, 14, 16), ev, 15))
        self.assertFalse(ta.in_event_window(utc(2026, 10, 8, 14, 46), ev, 15))

    def test_cme_session(self):
        self.assertFalse(ta.cme_open(utc(2026, 10, 10, 15)))        # Saturday
        self.assertFalse(ta.cme_open(utc(2026, 10, 11, 21, 59)))    # Sunday before 17:00 CT (22:00Z in CDT)
        self.assertTrue(ta.cme_open(utc(2026, 10, 11, 22, 1)))
        self.assertTrue(ta.cme_open(utc(2026, 10, 7, 20, 59)))      # Wednesday 15:59 CT
        self.assertFalse(ta.cme_open(utc(2026, 10, 7, 21, 30)))     # daily break 16:00-17:00 CT
        self.assertTrue(ta.cme_open(utc(2026, 10, 7, 22, 30)))
        self.assertFalse(ta.cme_open(utc(2026, 10, 9, 21, 30)))     # Friday after 16:00 CT

    def test_closed_only_drops_forming_bar(self):
        b = {"t": [0, 3600, 7200], "o": [1, 1, 1], "h": [1, 1, 1], "l": [1, 1, 1], "c": [1, 1, 1], "v": [1, 1, 1]}
        self.assertEqual(len(ta.closed_only(b, "1H", 7200 + 1800)["t"]), 2)   # third bar still forming at +30 min
        self.assertEqual(len(ta.closed_only(b, "1H", 7200 + 3600)["t"]), 3)


def uptrend_with_pullback():
    """1H bars: steady rise, pullback into 100-100.5, then a bullish rejection candle that closes at 101."""
    pivots = [90, 106, 100.6]
    b = zigzag(pivots, per_leg=40, wick=0.15)
    n = len(b["c"])
    # final rejection candle
    t = b["t"][-1] + 3600
    for k, v in (("t", t), ("o", 100.4), ("h", 101.2), ("l", 99.8), ("c", 101.0), ("v", 100)):
        b[k].append(v)
    return b


HTF_UP = {"trend": "up", "cluster": {"lo": 98.0, "hi": 99.0, "pos": "above"},
          "zones": [{"lo": 100.0, "hi": 100.5, "center": 100.25, "score": 3, "cats": ["fib", "ma", "swing"], "parts": ["Fib 50.0%", "EMA50", "Swing low"]},
                    {"lo": 103.9, "hi": 104.1, "center": 104.0, "score": 2, "cats": ["fib", "round"], "parts": ["x"]},
                    {"lo": 106.9, "hi": 107.1, "center": 107.0, "score": 2, "cats": ["ma", "bb"], "parts": ["y"]}]}


class Setups(unittest.TestCase):
    def test_pullback_long_in_uptrend(self):
        out = ta.scan_setups(uptrend_with_pullback(), "1H", HTF_UP, CFG)
        longs = [s for s in out if s["rule"] == "Pullback long in uptrend"]
        self.assertTrue(longs, "expected a pullback long")
        s = longs[0]
        self.assertEqual(s["dir"], "long")
        self.assertEqual(s["bars_ago"], 0)
        self.assertAlmostEqual(s["entry"], 101.0)
        self.assertLess(s["stop"], 99.8)                       # below the rejection low
        self.assertEqual(s["targets"][:2], [104.0, 107.0])    # next zones above, nearest first
        self.assertAlmostEqual(s["r1"], (104.0 - 101.0) / (101.0 - s["stop"]))
        self.assertGreater(s["r2"], s["r1"])

    def test_no_pullback_long_when_htf_trend_is_not_up(self):
        for trend in ("down", "range"):
            htf = dict(HTF_UP, trend=trend, cluster={"lo": 120.0, "hi": 125.0, "pos": "below"})
            htf["cluster"] = {"lo": 98.0, "hi": 99.0, "pos": "above"}
            out = ta.scan_setups(uptrend_with_pullback(), "1H", htf, CFG)
            self.assertFalse([s for s in out if s["rule"] == "Pullback long in uptrend"], trend)

    def test_needs_three_factor_zone(self):
        htf = dict(HTF_UP, zones=[dict(HTF_UP["zones"][0], score=2)] + HTF_UP["zones"][1:])
        out = ta.scan_setups(uptrend_with_pullback(), "1H", htf, CFG)
        self.assertFalse([s for s in out if s["rule"] == "Pullback long in uptrend"])

    def test_rally_short_into_resistance(self):
        pivots = [110, 94, 99.4]
        b = zigzag(pivots, per_leg=40, wick=0.15)
        t = b["t"][-1] + 3600
        for k, v in (("t", t), ("o", 99.6), ("h", 100.2), ("l", 98.9), ("c", 99.0), ("v", 100)):
            b[k].append(v)
        htf = {"trend": "down", "cluster": {"lo": 101.0, "hi": 103.0, "pos": "below"},
               "zones": [{"lo": 99.5, "hi": 100.0, "center": 99.75, "score": 3, "cats": ["fib", "ma", "swing"], "parts": ["a"]},
                         {"lo": 95.9, "hi": 96.1, "center": 96.0, "score": 2, "cats": ["fib", "round"], "parts": ["b"]},
                         {"lo": 92.9, "hi": 93.1, "center": 93.0, "score": 2, "cats": ["ma", "bb"], "parts": ["c"]}]}
        out = ta.scan_setups(b, "1H", htf, CFG)
        shorts = [s for s in out if s["rule"] == "Rally short into resistance"]
        self.assertTrue(shorts, "expected a rally short")
        s = shorts[0]
        self.assertEqual(s["dir"], "short")
        self.assertGreater(s["stop"], 100.2)
        self.assertEqual(s["targets"][:2], [96.0, 93.0])
        self.assertGreater(s["r1"], 0)

    def test_failed_breakdown_long(self):
        # range 100-110 builds a confirmed swing low at 100, then a poke to 99 that closes back above within 2 bars
        pivots = [105, 100, 108, 102, 110, 104, 109]
        b = zigzag(pivots, per_leg=14, wick=0.1)
        for o_, h_, l_, c_ in ((108.4, 108.5, 99.0, 99.4), (99.5, 101.6, 99.3, 101.4)):
            b["t"].append(b["t"][-1] + 3600)
            b["o"].append(o_); b["h"].append(h_); b["l"].append(l_); b["c"].append(c_); b["v"].append(100)
        htf = {"trend": "range", "cluster": {"lo": 104, "hi": 106, "pos": "below"}, "zones": []}
        out = ta.scan_setups(b, "1H", htf, CFG)
        fb = [s for s in out if s["rule"] == "Failed breakdown"]
        self.assertTrue(fb, "expected a failed breakdown long: %s" % [(s["rule"], s["bars_ago"]) for s in out])
        self.assertEqual(fb[0]["dir"], "long")
        self.assertLess(fb[0]["stop"], 99.0)                 # beyond the poke low

    def test_stopped_setup_is_marked(self):
        b = uptrend_with_pullback()
        for o_, h_, l_, c_ in ((101.0, 101.1, 97.0, 97.5),):  # next bar trades through the stop
            b["t"].append(b["t"][-1] + 3600)
            b["o"].append(o_); b["h"].append(h_); b["l"].append(l_); b["c"].append(c_); b["v"].append(100)
        out = ta.scan_setups(b, "1H", HTF_UP, CFG)
        longs = [s for s in out if s["rule"] == "Pullback long in uptrend" and s["bars_ago"] == 1]
        self.assertTrue(longs)
        self.assertEqual(longs[0]["status"], "stopped")


class MarketLevel(unittest.TestCase):
    def test_bias_labels(self):
        self.assertEqual(ta.bias_of({"trend": "up", "cluster": {"pos": "above"}}), "bullish")
        self.assertEqual(ta.bias_of({"trend": "down", "cluster": {"pos": "below"}}), "bearish")
        self.assertEqual(ta.bias_of({"trend": "up", "cluster": {"pos": "below"}}), "mixed")
        self.assertEqual(ta.bias_of({"error": "x"}), "unknown")

    def test_real_sample_files_analyze_without_error(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        for mk in ("NG", "CL"):
            p = ta.find_bars(mk, "1D", [os.path.join(root, "sample_bars")])
            if not p:
                continue
            res = ta.analyze_market(mk, [os.path.join(root, "sample_bars")], CFG, now_ts=utc(2026, 10, 7, 1))
            d = res["tfs"]["1D"]
            self.assertNotIn("error", d)
            self.assertGreater(d["atr"], 0)
            self.assertIn(d["trend"], ("up", "down", "range"))
            self.assertTrue(d["zones"])


if __name__ == "__main__":
    unittest.main()
