#!/usr/bin/env python3
"""Natural gas fundamentals dashboard: static frontend + cached JSON API (stdlib only).

Env:
  EIA_API_KEY  free key from https://www.eia.gov/opendata/ (storage, spot, production, exports, power burn)
  NG_DEMO=1    serve synthetic data (for UI development / offline use)
  PORT         default 8000

Optional manual feeds (data/ directory, CSV with header):
  data/lng_feedgas.csv   date,bcfd            daily LNG feedgas (no free API exists)
  data/rigs.csv          date,gas,oil         extra/backfilled Baker Hughes weekly counts
"""
import csv, io, json, math, os, random, re, statistics, sys, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
EIA_KEY = os.environ.get("EIA_API_KEY", "")
DEMO = os.environ.get("NG_DEMO") == "1"
UA = {"User-Agent": "Mozilla/5.0 (ng-dashboard)"}
_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        if hit:  # serve last good data rather than an error
            return hit[1]
        raise
    _cache[key] = (time.time(), val)
    return val


def http_get(url, timeout=25, headers=UA):
    for attempt in (1, 2, 3):  # retries: new TLS connections through proxies occasionally stall
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                return r.read()
        except (TimeoutError, OSError) as e:
            if attempt == 3 or "HTTP Error" in str(e):
                raise


def http_json(url, timeout=25):
    return json.loads(http_get(url, timeout))


def load_json(name, default):
    try:
        with open(os.path.join(DATA, name)) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f)


def load_csv(name):
    try:
        with open(os.path.join(DATA, name), newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return None


# ---------- EIA ----------
def eia_raw(path, facets, freq, length):
    if not EIA_KEY:
        raise RuntimeError("EIA_API_KEY not set")
    q = [("api_key", EIA_KEY), ("frequency", freq), ("data[0]", "value"),
         ("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", str(length))]
    for k, vals in facets.items():
        q += [(f"facets[{k}][]", v) for v in vals]
    rows = http_json(f"https://api.eia.gov/v2/{path}/data/?" + urllib.parse.urlencode(q))["response"]["data"]
    return [(r["period"], float(r["value"])) for r in rows if r.get("value") is not None][::-1]


def eia(route, series, freq, length):
    return eia_raw("natural-gas/" + route, {"series": [series]}, freq, length)


def api_storage():
    def go():
        if DEMO:
            today = date.today()
            weeks = [today - timedelta(days=7 * i) for i in range(260)][::-1]
            def lvl(d):
                return 2600 + 1200 * math.sin((d.timetuple().tm_yday - 100) / 365 * 2 * math.pi)
            rows = [(w.isoformat(), lvl(w) + (150 if w.year == today.year else 0)) for w in weeks]
        else:
            rows = eia("stor/wkly", "NW2_EPG0_SWO_R48_BCF", "weekly", 320)
        by_year = {}
        for p, v in rows:
            d = date.fromisoformat(p)
            by_year.setdefault(d.year, []).append((d, v))
        latest_d = date.fromisoformat(rows[-1][0])

        def shift(d, years):
            try:
                return d.replace(year=d.year + years)
            except ValueError:
                return d.replace(year=d.year + years, day=28)

        recent = []
        for p, v in rows[-52:]:
            d = date.fromisoformat(p)
            past = []
            for yr in range(d.year - 5, d.year):
                cand = [x for x in by_year.get(yr, []) if abs(shift(x[0], d.year - yr) - d).days <= 3]
                if cand:
                    past.append(cand[0][1])
            recent.append({"period": p, "value": v,
                           "avg5": round(statistics.mean(past), 1) if past else None,
                           "min5": min(past) if past else None, "max5": max(past) if past else None})
        last, prev = rows[-1][1], rows[-2][1]
        yago = [v for p, v in rows if abs((date.fromisoformat(p) - shift(latest_d, -1)).days) <= 3]
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


# ---------- Power burn (EIA-930 gas generation -> Bcf/d) ----------
HEAT_RATE = 7.6  # MMBtu per MWh, fleet-average assumption
BTU_PER_CF = 1.037  # MMBtu per Mcf


def api_power():
    def go():
        if DEMO:
            rows = []
            for i in range(430):
                d = date.today() - timedelta(days=430 - i)
                doy = d.timetuple().tm_yday
                rows.append((d.isoformat(), 38 + 14 * math.exp(-((doy - 205) / 40) ** 2) + random.Random(i).gauss(0, 1.2)))
        else:
            mwh = eia_raw("electricity/rto/daily-fuel-type-data",
                          {"respondent": ["US48"], "fueltype": ["NG"], "timezone": ["Eastern"]}, "daily", 430)
            rows = [(p, v * HEAT_RATE / BTU_PER_CF / 1e6) for p, v in mwh]  # MWh -> Bcf
        by = dict(rows)
        recent = rows[-90:]
        ly = []
        for p, v in recent:
            d = date.fromisoformat(p)
            k = (d - timedelta(days=364)).isoformat()
            if k in by:
                ly.append([p, by[k]])
        last7 = statistics.mean(v for _, v in rows[-7:])
        ly7 = [by[k] for k in ((date.fromisoformat(p) - timedelta(days=364)).isoformat() for p, _ in rows[-7:]) if k in by]
        return {"days": [[p, round(v, 2)] for p, v in recent], "last_year": [[p, round(v, 2)] for p, v in ly],
                "latest": round(rows[-1][1], 1), "avg7": round(last7, 1),
                "vs_ly7": round(last7 - statistics.mean(ly7), 1) if ly7 else None, "asof": rows[-1][0],
                "note": f"Estimated from EIA-930 US48 gas generation at {HEAT_RATE} MMBtu/MWh"}
    return cached("power", 3600, go)


# ---------- CFTC positioning (Disaggregated, futures only, NYMEX Henry Hub) ----------
def api_cftc():
    def go():
        if DEMO:
            rows = []
            for i in range(260):
                d = date.today() - timedelta(days=7 * (260 - i))
                mm = -60000 + 90000 * math.sin(i / 20) + random.Random(i).gauss(0, 8000)
                rows.append({"date": d.isoformat(), "mm_long": 200000 + mm / 2, "mm_short": 200000 - mm / 2,
                             "mm_net": mm, "prod_net": -mm * 0.5, "swap_net": -mm * 0.3, "oi": 1.5e6})
        else:
            q = urllib.parse.urlencode({"$where": "cftc_contract_market_code='023651'",
                                        "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "260"})
            raw = http_json("https://publicreporting.cftc.gov/resource/72hh-3qpy.json?" + q)
            f = lambda r, *keys: next((float(r[k]) for k in keys if k in r and r[k] not in (None, "")), 0.0)
            rows = []
            for r in raw[::-1]:
                ml, ms = f(r, "m_money_positions_long_all"), f(r, "m_money_positions_short_all")
                rows.append({"date": r["report_date_as_yyyy_mm_dd"][:10], "mm_long": ml, "mm_short": ms,
                             "mm_net": ml - ms,
                             "prod_net": f(r, "prod_merc_positions_long") - f(r, "prod_merc_positions_short"),
                             "swap_net": f(r, "swap_positions_long_all") - f(r, "swap__positions_short_all", "swap_positions_short_all"),
                             "oi": f(r, "open_interest_all")})
        if not rows:
            raise RuntimeError("CFTC returned no rows for contract 023651")
        nets = [r["mm_net"] for r in rows[-156:]]
        cur = rows[-1]["mm_net"]
        pct = 100 * sum(1 for n in nets if n <= cur) / len(nets)
        return {"weeks": rows[-156:], "latest": rows[-1], "change": cur - rows[-2]["mm_net"], "pctile_3y": round(pct),
                "asof": rows[-1]["date"]}
    return cached("cftc", 6 * 3600, go)


# ---------- Baker Hughes rig counts (best-effort scrape of weekly xlsx) ----------
def xlsx_sheet_rows(blob, sheet_name):
    """Stream rows (lists of cell strings) of one worksheet from an xlsx blob."""
    M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    z = zipfile.ZipFile(io.BytesIO(blob))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    target = next(rels[s.get(R + "id")] for s in ET.fromstring(z.read("xl/workbook.xml")).iter(M + "sheet")
                  if s.get("name") == sheet_name)
    shared = ["".join(t.text or "" for t in si.iter(M + "t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(M + "si")]
    for _, row in ET.iterparse(z.open("xl/" + target.lstrip("/").replace("xl/", "")), events=("end",)):
        if row.tag == M + "row":
            vals = []
            for c in row.findall(M + "c"):
                v = c.find(M + "v")
                vals.append("" if v is None else shared[int(v.text)] if c.get("t") == "s" else v.text)
            row.clear()
            yield vals


# rigcount.bakerhughes.com stalls/403s unknown and browser-like User-Agents but serves curl's
BH_HEADERS = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}


def scrape_rigs():
    """Return {iso_date: {"gas": n, "oil": n}} of US weekly rig counts from Baker Hughes' report workbook."""
    page = http_get("https://rigcount.bakerhughes.com/na-rig-count", 25, BH_HEADERS).decode("utf8", "ignore")
    best = None
    for href, title in re.findall(r'<a href="([^"]+)"[^>]*title="(\d\d-\d\d-\d{4}[^"]*Rig[ _]Count[ _]Report[^"]*\.xlsx)"', page, re.I):
        mm, dd, yy = title[:10].split("-")
        if best is None or (yy, mm, dd) > best[0]:
            best = ((yy, mm, dd), href)
    if not best:
        raise RuntimeError("Baker Hughes weekly report link not found (page layout changed?)")
    blob = http_get(urllib.parse.urljoin("https://rigcount.bakerhughes.com/", best[1]), 90, BH_HEADERS)
    hist, cols = {}, None
    for r in xlsx_sheet_rows(blob, "NAM Weekly"):
        if cols is None:
            if "DrillFor" in r and "US_PublishDate" in r:
                cols = {k: r.index(k) for k in ("Country", "DrillFor", "US_PublishDate", "Rig Count Value")}
            continue
        if len(r) <= max(cols.values()) or r[cols["Country"]].upper() != "UNITED STATES":
            continue
        kind = r[cols["DrillFor"]].lower()
        if kind not in ("gas", "oil"):
            continue
        d = (date(1899, 12, 30) + timedelta(days=int(float(r[cols["US_PublishDate"]])))).isoformat()
        h = hist.setdefault(d, {"gas": 0.0, "oil": 0.0})
        h[kind] += float(r[cols["Rig Count Value"]])
    if not hist:
        raise RuntimeError("No US rows parsed from Baker Hughes NAM Weekly sheet")
    return hist


def api_rigs():
    def go():
        if DEMO:
            hist = {(date.today() - timedelta(days=7 * i)).isoformat(): {"gas": 100 + round(8 * math.sin(i / 5)), "oil": 410}
                    for i in range(60)}
        else:
            hist = load_json("rigs.json", {})
            for r in load_csv("rigs.csv") or []:
                hist[r["date"]] = {"gas": float(r["gas"]), "oil": float(r.get("oil") or 0)}
            err = None
            try:
                hist.update(scrape_rigs())
                save_json("rigs.json", hist)
            except Exception as e:
                err = str(e)
            if not hist:
                raise RuntimeError(err or "no rig data")
        ds = sorted(hist)
        last = hist[ds[-1]]["gas"]
        prev = hist[ds[-2]]["gas"] if len(ds) > 1 else None
        yago = [hist[d]["gas"] for d in ds if abs((date.fromisoformat(d) - (date.fromisoformat(ds[-1]) - timedelta(days=364))).days) <= 3]
        return {"weeks": [[d, hist[d]["gas"], hist[d].get("oil")] for d in ds[-104:]], "latest": last,
                "wow": last - prev if prev is not None else None,
                "yoy": last - yago[0] if yago else None, "asof": ds[-1], "points": len(ds)}
    return cached("rigs", 6 * 3600, go)


# ---------- LNG feedgas (CSV drop-in; falls back to EIA monthly exports as a proxy) ----------
def api_lng():
    def go():
        rows = load_csv("lng_feedgas.csv")
        if DEMO:
            return {"source": "demo", "days": [[(date.today() - timedelta(days=120 - i)).isoformat(), round(14 + 1.5 * math.sin(i / 9), 2)]
                                                for i in range(120)]}
        if rows:
            days = [[r["date"], float(r["bcfd"])] for r in rows if r.get("bcfd")][-180:]
            return {"source": "csv", "days": days}
        mo = eia("move/expc", "N9133US2", "monthly", 36)  # MMcf per month
        days = [[p + "-15", v / 1000 / 30.4] for p, v in mo]
        return {"source": "eia_proxy", "days": [[a, round(b, 2)] for a, b in days],
                "note": "No data/lng_feedgas.csv found: showing EIA monthly LNG exports (Bcf/d) as a proxy."}
    return cached("lng", 3600, go)


# ---------- Weather: GFS vs ECMWF, population-weighted HDD/CDD, forecast revisions ----------
CITIES = [  # name, lat, lon, weight (rough gas-demand weighting)
    ("New York", 40.71, -74.01, 0.22), ("Chicago", 41.88, -87.63, 0.20),
    ("Boston", 42.36, -71.06, 0.10), ("Atlanta", 33.75, -84.39, 0.12),
    ("Houston", 29.76, -95.37, 0.14), ("Los Angeles", 34.05, -118.24, 0.08),
    ("Minneapolis", 44.98, -93.27, 0.08), ("Philadelphia", 39.95, -75.17, 0.06),
]
MODELS = {"gfs": "gfs_seamless", "ecmwf": "ecmwf_ifs025"}


def wx_snapshot_history(now_totals):
    hist = load_json("wx_history.json", [])
    now = time.time()
    if DEMO:
        r = random.Random(1)
        hist = [{"ts": now - h * 3600, **{k: v + r.gauss(0, 4) for k, v in now_totals.items()}} for h in range(72, 0, -6)]
    if not hist or now - hist[-1]["ts"] > 50 * 60:
        hist.append({"ts": now, **now_totals})
        hist = hist[-720:]
        if not DEMO:
            save_json("wx_history.json", hist)
    return hist


def api_weather():
    def go():
        today = date.today().isoformat()
        days = {}  # date -> {model: [hdd, cdd]}
        live = None
        if not DEMO:  # one multi-location request = one TLS connection through the proxy
            live = http_json("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                             "&daily=temperature_2m_mean&temperature_unit=fahrenheit&past_days=7&forecast_days=15"
                             "&models=%s&timezone=auto" % (",".join(str(c[1]) for c in CITIES),
                                                          ",".join(str(c[2]) for c in CITIES),
                                                          ",".join(MODELS.values())), 20)
            live = [x["daily"] for x in live]
        for ci, (name, lat, lon, w) in enumerate(CITIES):
            if DEMO:
                for m in MODELS:
                    for i in range(22):
                        d0 = date.today() - timedelta(days=7) + timedelta(days=i)
                        t = 58 + 22 * math.sin(d0.timetuple().tm_yday / 365 * 2 * math.pi - 1.9) + random.Random(i * 7 + lat).gauss(0, 2) + (1.5 if m == "gfs" else 0)
                        s_ = days.setdefault(d0.isoformat(), {}).setdefault(m, [0, 0])
                        s_[0] += w * max(0, 65 - t); s_[1] += w * max(0, t - 65)
                continue
            d = live[ci]
            for m, mid in MODELS.items():
                for day, t in zip(d["time"], d.get("temperature_2m_mean_" + mid, [])):
                    if t is None:
                        continue
                    s_ = days.setdefault(day, {}).setdefault(m, [0, 0])
                    s_[0] += w * max(0, 65 - t)
                    s_[1] += w * max(0, t - 65)
        rows = []
        for k, v in sorted(days.items()):
            rows.append({"date": k, "forecast": k > today,
                         **{m: {"hdd": round(v[m][0], 1), "cdd": round(v[m][1], 1)} for m in MODELS if m in v}})
        fc = [r for r in rows if r["forecast"]][:15]
        tot = {}
        for m in MODELS:
            tot[m + "_hdd"] = round(sum(r.get(m, {}).get("hdd", 0) for r in fc), 1)
            tot[m + "_cdd"] = round(sum(r.get(m, {}).get("cdd", 0) for r in fc), 1)
        hist = wx_snapshot_history(tot)
        now = time.time()

        def rev(hours):
            old = [h for h in hist if h["ts"] <= now - hours * 3600]
            if not old:
                return None
            o = old[-1]
            return {k: round(tot[k] - o[k], 1) for k in tot}
        return {"days": rows, "totals": tot, "rev_6h": rev(6), "rev_24h": rev(24),
                "history": [{"ts": h["ts"] * 1000, **{k: h[k] for k in tot if k in h}} for h in hist[-240:]],
                "models": {"gfs": "GFS", "ecmwf": "ECMWF IFS"}}
    return cached("weather", 1800, go)


ROUTES = {"/api/storage": api_storage, "/api/fundamentals": api_fundamentals, "/api/power": api_power,
          "/api/cftc": api_cftc, "/api/rigs": api_rigs, "/api/lng": api_lng, "/api/weather": api_weather}


CACHE_KEYS = {"/api/storage": "storage", "/api/fundamentals": "fund", "/api/power": "power",
              "/api/cftc": "cftc", "/api/rigs": "rigs", "/api/lng": "lng", "/api/weather": "weather"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **kw)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            return self.send_json({"demo": DEMO, "eia": bool(EIA_KEY)})
        if path in ROUTES:
            if "fresh" in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query):
                _cache.pop(CACHE_KEYS.get(path), None)  # bypass cache to catch a new release
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
