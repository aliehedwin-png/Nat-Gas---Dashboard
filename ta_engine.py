"""Technical analysis engine for CL and NG futures (stdlib only).

Implements the "CL/NG Technical Analysis Platform" build spec, v1 slice:
  indicators (EMA 20/50/100/200, SMA 20/50, RSI 14, MACD 12/26/9, Bollinger 20/2, ATR 14),
  swing detection (fractal + minimum size), trend state, Fibonacci retracements, confluence zones,
  the four setup rules, the event filter and the alert types.

It reads bars only; it never places orders. Bars come from data/bars/<MKT>_<TF>.json (written by ibkr_feed.py)
or, if absent, from sample_bars/. File format (columns, as IBKR returns them):
  {"market","contract","exchange","tf","source","fetched":ISO,"time":[ISO..],"open":[..],"high":[..],"low":[..],"close":[..],"volume":[..]}
Every tunable number is in DEFAULTS and can be overridden in data/ta_config.json.
"""
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone

TF_SECONDS = {"15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}
TFS = ["15m", "1H", "4H", "1D"]

DEFAULTS = {
    "markets": {
        "NG": {"name": "Natural gas (NYMEX NG)", "decimals": 3, "round_step": 0.25, "event": "EIA natural gas storage (Thu 10:30 ET)"},
        "CL": {"name": "Crude oil (NYMEX CL)", "decimals": 2, "round_step": 1.0, "event": "EIA petroleum status (Wed 10:30 ET)"},
    },
    "swing_n": {"15m": 5, "1H": 4, "4H": 3, "1D": 3},
    "swing_min_atr": 1.0,
    "confluence_atr": 0.3,          # zone width: levels within 0.3 x ATR of each other
    "zone_min_score_setup": 3,      # setups need a confluence zone of 3 or more
    "zone_min_score_target": 2,
    "min_r": 1.5,                   # below this a setup is logged but not alerted
    "event_window_min": 15,         # no alerts 15 minutes either side of a scheduled event
    "signal_tfs": ["15m", "1H"],
    "htf": "4H",                    # higher timeframe giving trend and confluence zones
    "lookback_bars": {"15m": 6, "1H": 4},   # how many recent closed bars may hold a signal
    "fail_break_atr": 0.25,
    "fail_bars": 3,
    "retest_bars": 12,
    "vol_spike_atr": 2.0,
    "chart_bars": 140,
}


# ---------------------------------------------------------------- config / loading
def config(data_dir=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    if data_dir:
        try:
            with open(os.path.join(data_dir, "ta_config.json")) as f:
                over = json.load(f)
            for k, v in over.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    for kk, vv in v.items():
                        if isinstance(vv, dict) and isinstance(cfg[k].get(kk), dict):
                            cfg[k][kk].update(vv)
                        else:
                            cfg[k][kk] = vv
                else:
                    cfg[k] = v
        except (OSError, ValueError):
            pass
    return cfg


def iso_to_ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def load_bars_file(path):
    """Return {'t','o','h','l','c','v','meta'} from a bars file (columns or rows). High/low are repaired to enclose open/close."""
    with open(path) as f:
        d = json.load(f)
    if "time" in d:
        t = [x if isinstance(x, (int, float)) else iso_to_ts(x) for x in d["time"]]
        o, h, l, c = d["open"], d["high"], d["low"], d["close"]
        v = d.get("volume") or [0] * len(t)
    else:  # rows: [ts,o,h,l,c,v]
        rows = d["bars"]
        t = [r[0] for r in rows]
        o, h, l, c = [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows]
        v = [r[5] if len(r) > 5 else 0 for r in rows]
    h = [max(a, b, cc) for a, b, cc in zip(h, o, c)]
    l = [min(a, b, cc) for a, b, cc in zip(l, o, c)]
    meta = {k: d.get(k) for k in ("market", "contract", "exchange", "tf", "source", "fetched")}
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "meta": meta}


def find_bars(market, tf, dirs):
    for d in dirs:
        p = os.path.join(d, f"{market}_{tf}.json")
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- indicators
def sma(x, n):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    """EMA seeded with the SMA of the first n values (as TradingView's ta.ema). None until defined."""
    out = [None] * len(x)
    vals = [(i, v) for i, v in enumerate(x) if v is not None]
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    start = vals[n - 1][0]
    prev = sum(v for _, v in vals[:n]) / n
    out[start] = prev
    for i, v in vals[n:]:
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(x, n):
    """Wilder's smoothing (RMA), SMA-seeded."""
    out = [None] * len(x)
    if len(x) < n:
        return out
    prev = sum(x[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(x)):
        prev = (prev * (n - 1) + x[i]) / n
        out[i] = prev
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    gains = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    losses = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(c, fast=12, slow=26, sig=9):
    ef, es = ema(c, fast), ema(c, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    signal = ema(line, sig)
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, signal)]
    return line, signal, hist


def bollinger(c, n=20, k=2.0):
    mid = sma(c, n)
    up, lo = [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)  # population standard deviation
        up[i], lo[i] = m + k * sd, m - k * sd
    return up, mid, lo


def atr(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return rma(tr, n)


# ---------------------------------------------------------------- structure
def find_swings(h, l, atr_s, n, min_atr):
    """Fractal swings (a bar higher/lower than n bars either side), alternating, each at least min_atr x ATR from the previous."""
    cands = []
    for i in range(n, len(h) - n):
        if h[i] > max(h[i - n:i]) and h[i] >= max(h[i + 1:i + n + 1]):
            cands.append((i, "H", h[i]))
        if l[i] < min(l[i - n:i]) and l[i] <= min(l[i + 1:i + n + 1]):
            cands.append((i, "L", l[i]))
    cands.sort(key=lambda x: (x[0], x[1]))
    seq = []
    for c in cands:
        if not seq:
            seq.append(c)
            continue
        last = seq[-1]
        if c[1] == last[1]:
            if (c[1] == "H" and c[2] > last[2]) or (c[1] == "L" and c[2] < last[2]):
                seq[-1] = c
            continue
        a = atr_s[c[0]] or next((x for x in reversed(atr_s[:c[0]]) if x), None)
        if a is None or abs(c[2] - last[2]) >= min_atr * a:
            seq.append(c)
    return seq


def trend_state(seq):
    highs = [s for s in seq if s[1] == "H"][-2:]
    lows = [s for s in seq if s[1] == "L"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "range"
    if highs[1][2] > highs[0][2] and lows[1][2] > lows[0][2]:
        return "up"
    if highs[1][2] < highs[0][2] and lows[1][2] < lows[0][2]:
        return "down"
    return "range"


def fib_levels(seq):
    if len(seq) < 2:
        return None
    a, b = seq[-2], seq[-1]
    up = b[2] > a[2]
    span = abs(b[2] - a[2])
    levels = {}
    for r in (0.382, 0.5, 0.618):
        levels[str(r)] = b[2] - r * span if up else b[2] + r * span
    return {"from": a[2], "to": b[2], "dir": "up" if up else "down", "levels": levels}


def round_levels(price, step, atr_v, span=6):
    if not step:
        return []
    lo, hi = price - span * atr_v, price + span * atr_v
    out, x = [], math.ceil(lo / step) * step
    while x <= hi:
        out.append(round(x, 6))
        x += step
    return out


def build_zones(price, atr_v, ma_vals, ma_cluster, fib, swing_prices, rounds, bb, width_atr):
    """Cluster all candidate levels within width_atr x ATR of each other; score = number of distinct categories."""
    cands = []
    for name, v in ma_vals.items():
        if v is not None:
            cands.append((v, "ma", name))
    if ma_cluster:
        cands += [(ma_cluster[0], "ma", "cluster low"), (ma_cluster[1], "ma", "cluster high")]
    if fib:
        for r, v in fib["levels"].items():
            cands.append((v, "fib", f"Fib {float(r) * 100:.1f}%"))
    for lbl, v in swing_prices:
        cands.append((v, "swing", lbl))
    for v in rounds:
        cands.append((v, "round", f"{v:g}"))
    for lbl, v in bb.items():
        if v is not None:
            cands.append((v, "bb", lbl))
    cands.sort(key=lambda x: x[0])
    zones, cur = [], []
    for c in cands:
        if cur and c[0] - cur[0][0] > width_atr * atr_v:
            zones.append(cur)
            cur = []
        cur.append(c)
    if cur:
        zones.append(cur)
    out = []
    for z in zones:
        cats = sorted({c[1] for c in z})
        lo, hi = min(c[0] for c in z), max(c[0] for c in z)
        out.append({"lo": lo, "hi": hi, "center": (lo + hi) / 2, "score": len(cats), "cats": cats,
                    "parts": sorted({c[2] for c in z}), "dist_atr": (((lo + hi) / 2) - price) / atr_v if atr_v else None})
    return out


def pos_vs(price, lo, hi):
    return "above" if price > hi else "below" if price < lo else "inside"


# ---------------------------------------------------------------- timeframe analysis
def analyze_tf(bars, tf, mcfg, cfg):
    n = len(bars["c"])
    if n < 40:
        return {"tf": tf, "error": f"only {n} closed bars; need at least 40"}
    t, o, h, l, c = bars["t"], bars["o"], bars["h"], bars["l"], bars["c"]
    atr_s = atr(h, l, c)
    atr_v = next((x for x in reversed(atr_s) if x), None)
    e = {k: ema(c, k) for k in (20, 50, 100, 200)}
    s = {k: sma(c, k) for k in (20, 50)}
    rs = rsi(c)
    ml, ms, mh = macd(c)
    bu, bm, bl = bollinger(c)
    price = c[-1]
    lastv = lambda arr: arr[-1]
    mas = {"EMA20": lastv(e[20]), "EMA50": lastv(e[50]), "EMA100": lastv(e[100]), "EMA200": lastv(e[200]),
           "SMA20": lastv(s[20]), "SMA50": lastv(s[50])}
    present = [v for v in mas.values() if v is not None]
    cluster = (min(present), max(present)) if present else None
    slopes = {}
    for nm, arr in (("EMA20", e[20]), ("EMA50", e[50]), ("EMA100", e[100]), ("EMA200", e[200]), ("SMA20", s[20]), ("SMA50", s[50])):
        slopes[nm] = ((arr[-1] - arr[-6]) / atr_v) if arr[-1] is not None and arr[-6] is not None and atr_v else None
    seq = find_swings(h, l, atr_s, cfg["swing_n"][tf], cfg["swing_min_atr"])
    trend = trend_state(seq)
    fib = fib_levels(seq) if tf in ("4H", "1D") else None
    recent = seq[-8:]
    swing_prices = [(("Swing high" if s_[1] == "H" else "Swing low"), s_[2]) for s_ in recent]
    rounds = round_levels(price, mcfg["round_step"], atr_v)
    bbv = {"BB upper": bu[-1], "BB mid": bm[-1], "BB lower": bl[-1]}
    zones = build_zones(price, atr_v, mas, cluster, fib, swing_prices, rounds, bbv, cfg["confluence_atr"])
    # indicator states
    rv = rs[-1]
    rzone = None if rv is None else "overbought" if rv > 70 else "oversold" if rv < 30 else "neutral"
    diverg = None
    hs = [x for x in seq if x[1] == "H"][-2:]
    ls = [x for x in seq if x[1] == "L"][-2:]
    if len(hs) == 2 and rs[hs[1][0]] is not None and rs[hs[0][0]] is not None and hs[1][2] > hs[0][2] and rs[hs[1][0]] < rs[hs[0][0]]:
        diverg = "bearish divergence (higher high, lower RSI)"
    if len(ls) == 2 and rs[ls[1][0]] is not None and rs[ls[0][0]] is not None and ls[1][2] < ls[0][2] and rs[ls[1][0]] > rs[ls[0][0]]:
        diverg = "bullish divergence (lower low, higher RSI)"
    macd_state = None
    if ml[-1] is not None and ms[-1] is not None:
        cross = None
        if ml[-2] is not None and ms[-2] is not None:
            if ml[-2] <= ms[-2] and ml[-1] > ms[-1]:
                cross = "bullish cross"
            elif ml[-2] >= ms[-2] and ml[-1] < ms[-1]:
                cross = "bearish cross"
        contracting = mh[-1] is not None and mh[-2] is not None and abs(mh[-1]) < abs(mh[-2])
        macd_state = {"line": ml[-1], "signal": ms[-1], "hist": mh[-1], "cross": cross, "contracting": contracting,
                      "side": "above signal" if ml[-1] > ms[-1] else "below signal"}
    bb_state = None
    if bu[-1] is not None:
        bb_state = {"upper": bu[-1], "mid": bm[-1], "lower": bl[-1],
                    "pctb": (price - bl[-1]) / (bu[-1] - bl[-1]) if bu[-1] != bl[-1] else None,
                    "touch": "upper band" if h[-1] >= bu[-1] else "lower band" if l[-1] <= bl[-1] else None,
                    "close_back_inside": (bool(bu[-2] is not None and ((h[-2] >= bu[-2] and c[-1] < bu[-1] and c[-2] >= bu[-2]) or (l[-2] <= bl[-2] and c[-1] > bl[-1] and c[-2] <= bl[-2]))))}
    k = cfg["chart_bars"]
    sl = slice(max(0, n - k), n)
    series = {"t": t[sl], "o": o[sl], "h": h[sl], "l": l[sl], "c": c[sl],
              "ema20": e[20][sl], "ema50": e[50][sl], "ema100": e[100][sl], "ema200": e[200][sl],
              "bbu": bu[sl], "bbl": bl[sl]}
    offset = sl.start
    return {
        "tf": tf, "n": n, "last": {"t": t[-1], "o": o[-1], "h": h[-1], "l": l[-1], "c": c[-1], "v": bars["v"][-1]},
        "atr": atr_v, "ma": mas, "slopes": slopes,
        "cluster": {"lo": cluster[0], "hi": cluster[1], "pos": pos_vs(price, *cluster)} if cluster else None,
        "rsi": {"value": rv, "zone": rzone, "divergence": diverg}, "macd": macd_state, "bb": bb_state,
        "trend": trend,
        "swings": [{"i": s_[0] - offset, "t": t[s_[0]], "type": s_[1], "price": s_[2]} for s_ in seq[-10:] if s_[0] >= offset],
        "swing_all": [{"i": s_[0], "type": s_[1], "price": s_[2], "t": t[s_[0]]} for s_ in seq[-12:]],
        "fib": fib, "zones": zones, "series": series,
        "warm": {"ema200": n >= 3 * 200 * 0.5},   # >= 300 bars: seed influence below ~5%
    }


# ---------------------------------------------------------------- time helpers (no tz database needed)
def _nth_weekday(year, month, weekday, nth):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=nth - 1)


def et_offset_hours(ts):
    """US Eastern offset from UTC (-4 in daylight time, -5 otherwise) without needing a tz database (Windows has none)."""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    start = datetime.combine(_nth_weekday(dt.year, 3, 6, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)   # 2:00 EST = 07:00 UTC
    end = datetime.combine(_nth_weekday(dt.year, 11, 6, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)    # 2:00 EDT = 06:00 UTC
    return -4 if start <= dt < end else -5


def et_to_ts(d, hour, minute):
    """Epoch seconds of a US Eastern wall time on date d."""
    for off in (-4, -5):
        ts = int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc).timestamp()) - off * 3600
        if et_offset_hours(ts) == off:
            return ts
    return ts


def cme_open(ts):
    """CME energy futures: Sun 17:00 CT to Fri 16:00 CT, daily break 16:00-17:00 CT (Central = Eastern - 1h)."""
    off = et_offset_hours(ts) - 1
    dt = datetime.fromtimestamp(ts + off * 3600, timezone.utc)
    wd, hm = dt.weekday(), dt.hour * 60 + dt.minute
    if wd == 5:
        return False
    if wd == 6:
        return hm >= 17 * 60
    if wd == 4:
        return hm < 16 * 60
    return not (16 * 60 <= hm < 17 * 60)


def scheduled_events(market, now_ts, custom=None, count=4):
    """Upcoming scheduled events (recurring EIA releases plus any in data/events.json)."""
    out = []
    d0 = datetime.fromtimestamp(now_ts, timezone.utc).date() - timedelta(days=1)
    wd, name = (3, "EIA natural gas storage report") if market == "NG" else (2, "EIA petroleum status report")
    for i in range(0, 21):
        d = d0 + timedelta(days=i)
        if d.weekday() == wd:
            out.append({"name": name, "ts": et_to_ts(d, 10, 30)})
    for ev in custom or []:
        try:
            out.append({"name": ev["name"], "ts": iso_to_ts(ev["time"])})
        except (KeyError, ValueError):
            pass
    out.sort(key=lambda e: e["ts"])
    return [e for e in out if e["ts"] >= now_ts - 3600][:count]


def in_event_window(ts, events, minutes):
    for e in events:
        if abs(ts - e["ts"]) <= minutes * 60:
            return e
    return None


# ---------------------------------------------------------------- setups
def _targets(entry, direction, zones, swings, min_score):
    """Two targets from the next confluence zones beyond entry; swing levels fill in when zones run out."""
    if direction == "long":
        cand = sorted([z["center"] for z in zones if z["lo"] > entry and z["score"] >= min_score])
        cand += sorted(s_["price"] for s_ in swings if s_["type"] == "H" and s_["price"] > entry)
    else:
        cand = sorted([z["center"] for z in zones if z["hi"] < entry and z["score"] >= min_score], reverse=True)
        cand += sorted((s_["price"] for s_ in swings if s_["type"] == "L" and s_["price"] < entry), reverse=True)
    out = []
    for v in cand:
        if all(abs(v - u) > 1e-9 for u in out):
            out.append(v)
        if len(out) == 2:
            break
    out.sort(reverse=(direction == "short"))
    return out


def _make(rule, direction, tf, k, bars, entry, stop, zones, swings, cfg, label, zone=None):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    tg = _targets(entry, direction, zones, swings, cfg["zone_min_score_target"])
    r1 = abs(tg[0] - entry) / risk if tg else None
    r2 = abs(tg[1] - entry) / risk if len(tg) > 1 else None
    return {"rule": rule, "dir": direction, "tf": tf, "t": bars["t"][k], "entry": entry, "stop": stop,
            "targets": tg, "r1": r1, "r2": r2, "label": label,
            "zone": {"lo": zone["lo"], "hi": zone["hi"], "score": zone["score"], "parts": zone["parts"]} if zone else None}


def scan_setups(sig_bars, tf, htf, cfg):
    """Evaluate the four v1 setup rules on the most recent closed bars of a signal timeframe (15m or 1H)."""
    n = len(sig_bars["c"])
    if n < 60 or htf.get("error") or "zones" not in htf:   # an empty zone list is fine: rules 3 and 4 do not need zones
        return []
    o, h, l, c, t = sig_bars["o"], sig_bars["h"], sig_bars["l"], sig_bars["c"], sig_bars["t"]
    atr_s = atr(h, l, c)
    a = atr_s[-1]
    if not a:
        return []
    zones = [z for z in htf["zones"]]
    big = [z for z in zones if z["score"] >= cfg["zone_min_score_setup"]]
    htf_trend = htf["trend"]
    cl = htf.get("cluster")
    seq = find_swings(h, l, atr_s, cfg["swing_n"][tf], cfg["swing_min_atr"])
    swings = [{"i": s_[0], "type": s_[1], "price": s_[2]} for s_ in seq]
    found = []
    lb = cfg["lookback_bars"][tf]
    for k in range(n - 1, max(n - 1 - lb, 20), -1):
        rng = h[k] - l[k]
        if rng <= 0:
            continue
        body_up, body_dn = c[k] > o[k], c[k] < o[k]
        lw = (min(o[k], c[k]) - l[k]) / rng
        uw = (h[k] - max(o[k], c[k])) / rng
        recent_lo, recent_hi = min(l[max(0, k - 6):k + 1]), max(h[max(0, k - 6):k + 1])
        # 1. pullback long in an uptrend
        if htf_trend == "up":
            for z in big:
                if z["hi"] <= c[k] and recent_lo <= z["hi"] and recent_lo >= z["lo"] - 1.5 * a and body_up and lw >= 0.4 and c[k] > z["hi"]:
                    stop = min(z["lo"], l[k]) - 0.5 * a
                    s_ = _make("Pullback long in uptrend", "long", tf, k, sig_bars, c[k], stop, zones, swings, cfg,
                               f"4H uptrend; pulled back into a {z['score']}-factor zone and rejected it", z)
                    if s_:
                        found.append(s_)
        # 2. rally short into resistance (price below the 4H MA cluster)
        if cl and (htf_trend == "down" or cl["pos"] == "below"):
            for z in big:
                if z["lo"] >= c[k] and z["center"] <= cl["hi"] + cfg["confluence_atr"] * a and recent_hi >= z["lo"] and recent_hi <= z["hi"] + 1.5 * a and body_dn and uw >= 0.4 and c[k] < z["lo"]:
                    stop = max(z["hi"], h[k]) + 0.5 * a
                    s_ = _make("Rally short into resistance", "short", tf, k, sig_bars, c[k], stop, zones, swings, cfg,
                               f"Price below the 4H MA cluster; rallied into a {z['score']}-factor zone and rejected it", z)
                    if s_:
                        found.append(s_)
        # 3. failed breakdown / breakout of a swing level
        for sw in swings[-8:]:
            if sw["i"] >= k - 1:
                continue
            S = sw["price"]
            if sw["type"] == "L":
                for j in range(max(sw["i"] + 1, k - cfg["fail_bars"]), k):
                    if l[j] < S - cfg["fail_break_atr"] * a and c[k] > S and min(c[j:k + 1]) < S + 1e-12:
                        stop = min(l[j:k + 1]) - 0.25 * a
                        s_ = _make("Failed breakdown", "long", tf, k, sig_bars, c[k], stop, zones, swings, cfg,
                                   f"Broke the swing low {S:g} then closed back above it within {k - j} bars")
                        if s_:
                            found.append(s_)
                        break
            else:
                for j in range(max(sw["i"] + 1, k - cfg["fail_bars"]), k):
                    if h[j] > S + cfg["fail_break_atr"] * a and c[k] < S:
                        stop = max(h[j:k + 1]) + 0.25 * a
                        s_ = _make("Failed breakout", "short", tf, k, sig_bars, c[k], stop, zones, swings, cfg,
                                   f"Broke the swing high {S:g} then closed back below it within {k - j} bars")
                        if s_:
                            found.append(s_)
                        break
        # 4. break and retest
        for sw in swings[-8:]:
            S = sw["price"]
            if sw["i"] >= k - 2:
                continue
            for j in range(max(sw["i"] + 1, k - cfg["retest_bars"]), k - 1):
                if sw["type"] == "H" and c[j] > S + 0.1 * a and all(c[x] > S for x in range(j, k + 1)) and l[k] <= S + 0.3 * a and c[k] > S and body_up:
                    s_ = _make("Break and retest", "long", tf, k, sig_bars, c[k], min(l[k], S) - 0.5 * a, zones, swings, cfg,
                               f"Closed above the swing high {S:g}, retested it and it held")
                    if s_:
                        found.append(s_)
                    break
                if sw["type"] == "L" and c[j] < S - 0.1 * a and all(c[x] < S for x in range(j, k + 1)) and h[k] >= S - 0.3 * a and c[k] < S and body_dn:
                    s_ = _make("Break and retest", "short", tf, k, sig_bars, c[k], max(h[k], S) + 0.5 * a, zones, swings, cfg,
                               f"Closed below the swing low {S:g}, retested it and it held")
                    if s_:
                        found.append(s_)
                    break
    # keep the most recent signal per (rule, direction, zone/level)
    uniq = {}
    for s_ in found:
        key = (s_["rule"], s_["dir"], s_["tf"], round((s_["zone"] or {}).get("lo", s_["stop"]), 3))
        if key not in uniq or s_["t"] > uniq[key]["t"]:
            uniq[key] = s_
    out = list(uniq.values())
    for s_ in out:
        # invalidated (stop hit) or already at target since the signal?
        k = max(i for i, tt in enumerate(t) if tt == s_["t"])
        later_h, later_l = h[k + 1:], l[k + 1:]
        s_["status"] = "active"
        if later_h:
            if s_["dir"] == "long" and min(later_l) <= s_["stop"] or s_["dir"] == "short" and max(later_h) >= s_["stop"]:
                s_["status"] = "stopped"
            elif s_["targets"] and (s_["dir"] == "long" and max(later_h) >= s_["targets"][0] or s_["dir"] == "short" and min(later_l) <= s_["targets"][0]):
                s_["status"] = "target reached"
        s_["bars_ago"] = n - 1 - k
        s_["id"] = f"{s_['tf']}|{s_['rule']}|{s_['dir']}|{s_['t']}"
    return sorted(out, key=lambda s_: -s_["t"])


# ---------------------------------------------------------------- market-level analysis
def bias_of(a):
    """Bias label from the 4H structure: trend plus price vs MA cluster."""
    if not a or a.get("error") or not a.get("cluster"):
        return "unknown"
    pos, tr = a["cluster"]["pos"], a["trend"]
    if tr == "up" and pos == "above":
        return "bullish"
    if tr == "down" and pos == "below":
        return "bearish"
    if pos == "above" and tr == "range":
        return "mildly bullish"
    if pos == "below" and tr == "range":
        return "mildly bearish"
    return "mixed"


def _truncate(bars, k):
    return {x: (bars[x][:k] if isinstance(bars[x], list) else bars[x]) for x in bars}


def closed_only(bars, tf, asof):
    """Drop a still-forming last bar: keep bars whose end time is <= asof."""
    sec = TF_SECONDS[tf]
    n = len(bars["t"])
    shut = not cme_open(asof)
    while n > 0 and bars["t"][n - 1] + sec > asof + 1 and not (shut and bars["t"][n - 1] < asof):
        # futures sessions end at the 16:00 CT break, before start + tf: when the market is shut, every bar that has started is complete
        n -= 1
    return _truncate(bars, n)


def analyze_market(market, bar_dirs, cfg, now_ts=None, custom_events=None):
    now_ts = now_ts or int(time.time())
    mcfg = cfg["markets"][market]
    res = {"market": market, "name": mcfg["name"], "decimals": mcfg["decimals"], "tfs": {}, "meta": {}, "now": now_ts}
    raw, closed = {}, {}
    for tf in TFS:
        p = find_bars(market, tf, bar_dirs)
        if not p:
            res["tfs"][tf] = {"tf": tf, "error": "no bars file (run the IBKR feed or add data/bars/%s_%s.json)" % (market, tf)}
            continue
        try:
            b = load_bars_file(p)
        except (OSError, ValueError, KeyError) as e:
            res["tfs"][tf] = {"tf": tf, "error": f"could not read {os.path.basename(p)}: {e}"}
            continue
        raw[tf] = b
        fetched = iso_to_ts(b["meta"]["fetched"]) if b["meta"].get("fetched") else int(os.path.getmtime(p))
        closed[tf] = closed_only(b, tf, fetched)
        res["meta"][tf] = {"file": os.path.basename(p), "dir": os.path.basename(os.path.dirname(p)), "fetched": fetched, "contract": b["meta"].get("contract"),
                           "source": b["meta"].get("source"), "bars": len(b["t"])}
        res["tfs"][tf] = analyze_tf(closed[tf], tf, mcfg, cfg)
    htf = res["tfs"].get(cfg["htf"], {})
    events = scheduled_events(market, now_ts, custom_events)
    res["events"] = events
    res["event_window"] = bool(in_event_window(now_ts, events, cfg["event_window_min"]))
    res["event_day"] = any(abs(e["ts"] - now_ts) < 12 * 3600 for e in events)
    # setups
    setups = []
    for tf in cfg["signal_tfs"]:
        if tf in closed and not htf.get("error") and htf:
            for s_ in scan_setups(closed[tf], tf, htf, cfg):
                ev = in_event_window(s_["t"] + TF_SECONDS[tf], events, cfg["event_window_min"])
                s_["event_blocked"] = ev["name"] if ev else None
                s_["alert"] = bool(s_["r1"] and s_["r1"] >= cfg["min_r"] and not ev and s_["status"] == "active")
                setups.append(s_)
    res["setups"] = sorted(setups, key=lambda s_: -s_["t"])
    # bias and alert events
    res["bias"] = bias_of(htf)
    prev = None
    if "4H" in closed and len(closed["4H"]["t"]) > 60:
        prev = analyze_tf(_truncate(closed["4H"], len(closed["4H"]["t"]) - 1), "4H", mcfg, cfg)
    res["bias_prev"] = bias_of(prev) if prev else None
    alerts = []
    if res["bias_prev"] and res["bias"] != res["bias_prev"] and "unknown" not in (res["bias"], res["bias_prev"]):
        t4 = closed["4H"]["t"][-1] + TF_SECONDS["4H"]
        alerts.append({"id": f"bias|{t4}|{res['bias']}", "type": "Bias change", "priority": "medium", "tf": "4H", "t": t4,
                       "text": f"4H bias changed from {res['bias_prev']} to {res['bias']}"})
    for s_ in res["setups"]:
        if s_["alert"] and s_["bars_ago"] <= 1:
            alerts.append({"id": s_["id"], "type": "Setup triggered", "priority": "high", "tf": s_["tf"], "t": s_["t"],
                           "text": f"{s_['rule']} ({s_['dir']}) on {s_['tf']}: entry {s_['entry']:.{mcfg['decimals']}f}, stop {s_['stop']:.{mcfg['decimals']}f}, "
                                   f"{s_['r1']:.1f}R to the first target"})
    for tf in ("1H", "4H"):
        a = res["tfs"].get(tf)
        if not a or a.get("error") or len(closed[tf]["c"]) < 3:
            continue
        cc = closed[tf]["c"]
        lvls = [(sw["price"], "swing " + ("high" if sw["type"] == "H" else "low")) for sw in a["swing_all"][-6:]]
        lvls += [((z["lo"] + z["hi"]) / 2, f"{z['score']}-factor zone") for z in a["zones"] if z["score"] >= cfg["zone_min_score_setup"]]
        for v, nm in lvls:
            if (cc[-2] - v) * (cc[-1] - v) < 0:
                tt = closed[tf]["t"][-1] + TF_SECONDS[tf]
                alerts.append({"id": f"lvl|{tf}|{tt}|{round(v, 3)}", "type": "Level break", "priority": "high", "tf": tf, "t": tt,
                               "text": f"{tf} closed {'above' if cc[-1] > v else 'below'} the {nm} at {v:.{mcfg['decimals']}f}"})
    if "15m" in closed and res["tfs"].get("15m") and not res["tfs"]["15m"].get("error"):
        a15 = res["tfs"]["15m"]
        rng = closed["15m"]["h"][-1] - closed["15m"]["l"][-1]
        if a15["atr"] and rng > cfg["vol_spike_atr"] * a15["atr"]:
            tt = closed["15m"]["t"][-1] + TF_SECONDS["15m"]
            alerts.append({"id": f"vol|{tt}", "type": "Volatility spike", "priority": "medium", "tf": "15m", "t": tt,
                           "text": f"15m range {rng:.{mcfg['decimals']}f} is {rng / a15['atr']:.1f}x ATR, usually a headline"})
    for a_ in alerts:
        a_["blocked"] = bool(in_event_window(a_["t"], events, cfg["event_window_min"]) and a_["type"] == "Setup triggered")
    res["alerts"] = alerts
    last_t = max((res["tfs"][tf]["last"]["t"] + TF_SECONDS[tf] for tf in TFS if res["tfs"].get(tf) and not res["tfs"][tf].get("error")), default=None)
    res["last_bar_end"] = last_t
    res["market_open"] = cme_open(now_ts)
    res.setdefault("contract", None)
    res["no_bars"] = not res["meta"]
    if res["meta"]:
        contracts = {m["contract"] for m in res["meta"].values() if m.get("contract")}
        res["contract"] = sorted(contracts)[0] if len(contracts) == 1 else "/".join(sorted(contracts))
        res["contract_mismatch"] = len(contracts) > 1
        res["fetched"] = max(m["fetched"] for m in res["meta"].values())
        res["source"] = sorted({m["source"] for m in res["meta"].values() if m.get("source")})
    return res
