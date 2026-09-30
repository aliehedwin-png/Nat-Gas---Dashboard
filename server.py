#!/usr/bin/env python3
"""Natural gas futures dashboard: static frontend + cached JSON API (stdlib only).

Env:
  EIA_API_KEY  free key from https://www.eia.gov/opendata/ (storage, spot, production, exports)
  NG_DEMO=1    serve synthetic data (for UI development / offline use)
  PORT         default 8000
"""
import json, math, os, random, statistics, sys, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
EIA_KEY = os.environ.get("EIA_API_KEY", "")
DEMO = os.environ.get("NG_DEMO") == "1"
UA = {"User-Agent": "Mozilla/5.0 (ng-dashboard)"}
_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------- Yahoo Finance (price, curve, related markets) ----------
def yahoo_chart(symbol, rng, interval):
    q = urllib.parse.quote(symbol)
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?range={rng}&interval={interval}")
    res = d["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    pts = [[t * 1000, round(c, 4)] for t, c in zip(ts, closes) if c is not None]
    return res["meta"], pts


def demo_series(n, start, vol, step_ms):
    now = int(time.time() * 1000)
    rnd = random.Random(int(start * 100) + n)
    v, out = start, []
    for i in range(n):
        v = max(0.5, v * (1 + rnd.gauss(0, vol)))
        out.append([now - (n - i) * step_ms, round(v, 3)])
    return out


def market(symbol, start, vol):
    def go():
        if DEMO:
            daily = demo_series(180, start, vol, 86400000)
            intra = demo_series(78, daily[-1][1], vol / 6, 300000)
            prev = daily[-2][1]
            last = intra[-1][1]
            return {"symbol": symbol, "price": last, "prev": prev, "daily": daily, "intraday": intra}
        meta, daily = yahoo_chart(symbol, "1y", "1d")
        _, intra = yahoo_chart(symbol, "1d", "5m")
        return {"symbol": symbol, "price": meta["regularMarketPrice"],
                "prev": meta.get("chartPreviousClose") or daily[-2][1],
                "daily": daily, "intraday": intra}
    return go


def api_market():
    out = {}
    for key, sym, start, vol in [("ng", "NG=F", 3.2, 0.03), ("wti", "CL=F", 68, 0.02), ("ttf", "TTF=F", 32, 0.025)]:
        try:
            out[key] = cached("mkt" + sym, 60, market(sym, start, vol))
        except Exception as e:
            out[key] = {"error": str(e)}
    return out


MONTH_CODES = "FGHJKMNQUVXZ"


def api_curve():
    def go():
        today = date.today()
        y, m = today.year, today.month
        rows = []
        for i in range(0, 13):
            mm = (m - 1 + i) % 12
            yy = y + (m - 1 + i) // 12
            sym = f"NG{MONTH_CODES[mm]}{str(yy)[2:]}.NYM"
            label = f"{yy}-{mm + 1:02d}"
            if DEMO:
                # winter premium shape
                seasonal = 0.6 * math.cos((mm - 0.5) / 12 * 2 * math.pi)
                rows.append({"label": label, "price": round(3.4 + seasonal + i * 0.02, 3)})
                continue
            try:
                meta, _ = yahoo_chart(sym, "5d", "1d")
                rows.append({"label": label, "price": round(meta["regularMarketPrice"], 3)})
            except Exception:
                pass  # contract not listed / no data
        return rows
    return cached("curve", 600, go)


# ---------- EIA ----------
def eia(route, series, freq, length, extra=""):
    if not EIA_KEY:
        raise RuntimeError("EIA_API_KEY not set")
    url = (f"https://api.eia.gov/v2/natural-gas/{route}/data/?api_key={EIA_KEY}&frequency={freq}"
           f"&data[0]=value&facets[series][]={series}&sort[0][column]=period&sort[0][direction]=desc"
           f"&length={length}{extra}")
    rows = http_json(url, 25)["response"]["data"]
    return [(r["period"], float(r["value"])) for r in rows if r.get("value") is not None][::-1]


def api_storage():
    def go():
        if DEMO:
            today = date.today()
            weeks = [today - timedelta(days=7 * i) for i in range(260)][::-1]
            def lvl(d):
                doy = d.timetuple().tm_yday
                return 2600 + 1200 * math.sin((doy - 100) / 365 * 2 * math.pi)
            rows = [(w.isoformat(), lvl(w) + (150 if w.year == today.year else 0)) for w in weeks]
        else:
            rows = eia("stor/wkly", "NW2_EPG0_SWO_R48_BCF", "weekly", 320)
        by_year = {}
        for p, v in rows:
            d = date.fromisoformat(p)
            by_year.setdefault(d.year, []).append((d, v))
        latest_d = date.fromisoformat(rows[-1][0])
        recent = []
        for p, v in rows[-52:]:
            d = date.fromisoformat(p)
            past = []
            for yr in range(d.year - 5, d.year):
                cand = [x for x in by_year.get(yr, []) if abs((x[0].replace(year=d.year) if not (x[0].month == 2 and x[0].day == 29) else x[0].replace(year=d.year, day=28)) - d).days <= 3]
                if cand:
                    past.append(cand[0][1])
            recent.append({"period": p, "value": v,
                           "avg5": round(statistics.mean(past), 1) if past else None,
                           "min5": min(past) if past else None, "max5": max(past) if past else None})
        last, prev = rows[-1][1], rows[-2][1]
        yago = [v for p, v in rows if abs((date.fromisoformat(p) - latest_d.replace(year=latest_d.year - 1)).days) <= 3]
        return {"weeks": recent, "latest": last, "change": last - prev,
                "vs_avg5": (last - recent[-1]["avg5"]) if recent[-1]["avg5"] else None,
                "vs_year_ago": (last - yago[0]) if yago else None, "asof": rows[-1][0]}
    return cached("storage", 3600, go)


def api_fundamentals():
    def go():
        out = {}
        specs = {
            "spot": ("pri/fut", "RNGWHHD", "daily", 260),
            "production": ("prod/sum", "N9070US2", "monthly", 36),
            "lng_exports": ("move/expc", "N9133US2", "monthly", 36),
            "mexico_exports": ("move/expc", "N9132MX2", "monthly", 36),
        }
        for k, (route, sid, freq, n) in specs.items():
            try:
                if DEMO:
                    base = {"spot": 3.3, "production": 3.5e6, "lng_exports": 1.1e5, "mexico_exports": 2.2e5}[k]
                    step = 1 if freq == "daily" else 30
                    out[k] = [[(date.today() - timedelta(days=step * (n - i))).isoformat(),
                               base * (1 + 0.1 * math.sin(i / 5))] for i in range(n)]
                else:
                    out[k] = eia(route, sid, freq, n)
            except Exception as e:
                out[k] = {"error": str(e)}
        return out
    return cached("fund", 3600, go)


# ---------- Weather (Open-Meteo): population-weighted HDD/CDD ----------
CITIES = [  # name, lat, lon, weight (rough gas-demand weighting)
    ("New York", 40.71, -74.01, 0.22), ("Chicago", 41.88, -87.63, 0.20),
    ("Boston", 42.36, -71.06, 0.10), ("Atlanta", 33.75, -84.39, 0.12),
    ("Houston", 29.76, -95.37, 0.14), ("Los Angeles", 34.05, -118.24, 0.08),
    ("Minneapolis", 44.98, -93.27, 0.08), ("Philadelphia", 39.95, -75.17, 0.06),
]


def api_weather():
    def go():
        days = {}
        for name, lat, lon, w in CITIES:
            if DEMO:
                base = date.today() - timedelta(days=7)
                for i in range(23):
                    d = (base + timedelta(days=i)).isoformat()
                    t = 55 + 25 * math.sin((base + timedelta(days=i)).timetuple().tm_yday / 365 * 2 * math.pi - 1.9) + random.Random(i + lat).gauss(0, 3)
                    days.setdefault(d, [0, 0])
                    days[d][0] += w * max(0, 65 - t); days[d][1] += w * max(0, t - 65)
                continue
            d = http_json("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                          "&daily=temperature_2m_mean&temperature_unit=fahrenheit"
                          "&past_days=7&forecast_days=16&timezone=auto" % (lat, lon))["daily"]
            for day, t in zip(d["time"], d["temperature_2m_mean"]):
                if t is None:
                    continue
                days.setdefault(day, [0, 0])
                days[day][0] += w * max(0, 65 - t)
                days[day][1] += w * max(0, t - 65)
        today = date.today().isoformat()
        rows = [{"date": k, "hdd": round(v[0], 1), "cdd": round(v[1], 1), "forecast": k > today}
                for k, v in sorted(days.items())]
        fc = [r for r in rows if r["forecast"]][:15]
        return {"days": rows, "hdd_15d": round(sum(r["hdd"] for r in fc), 1),
                "cdd_15d": round(sum(r["cdd"] for r in fc), 1)}
    return cached("weather", 3600, go)


ROUTES = {"/api/market": api_market, "/api/curve": api_curve, "/api/storage": api_storage,
          "/api/fundamentals": api_fundamentals, "/api/weather": api_weather}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **kw)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            return self.send_json({"demo": DEMO, "eia": bool(EIA_KEY)})
        if path in ROUTES:
            try:
                return self.send_json(ROUTES[path]())
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
        return super().do_GET()

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Serving on http://localhost:{port}  demo={DEMO}  eia_key={'yes' if EIA_KEY else 'NO'}", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
