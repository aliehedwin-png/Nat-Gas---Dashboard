#!/usr/bin/env python3
"""Natural gas fundamentals dashboard: static frontend + cached JSON API (stdlib only).

Env:
  EIA_API_KEY  free key from https://www.eia.gov/opendata/ (storage, spot, production, exports, power, STEO);
               may also be set in a .env file next to server.py (gitignored)
  NG_DEMO=1    serve synthetic data (for UI development / offline use)
  PORT         default 8000

Optional manual feeds / config (data/ directory):
  data/lng_feedgas.csv     date,bcfd          daily LNG feedgas (no free API exists)
  data/rigs.csv            date,gas,oil       extra/backfilled Baker Hughes weekly counts
  data/score_config.json   {"weights": {"wx_vs_normal": 20, "cftc": 0}}   see scoring.py
"""
import calendar, csv, html, io, json, math, os, random, re, statistics, sys, threading, time
import urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import scoring

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")


VERSION = "2026-10-01.4"
KEY_SOURCE = "none"


def load_dotenv(path):
    """Minimal .env reader (KEY=VALUE per line); real environment variables win, blank values are ignored."""
    try:
        with open(path, encoding="utf-8-sig") as f:  # utf-8-sig tolerates a Windows Notepad BOM
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'").strip()
                    if v and not os.environ.get(k):
                        os.environ[k] = v
                        return os.path.basename(path)
    except OSError:
        pass
    return None


if os.environ.get("EIA_API_KEY"):
    KEY_SOURCE = "environment variable"
for _name in (".env", ".env.txt"):  # .env.txt: Notepad often appends .txt
    if not os.environ.get("EIA_API_KEY") and load_dotenv(os.path.join(ROOT, _name)):
        KEY_SOURCE = _name
KEY_FILE = os.path.join(DATA, "eia_key.txt")  # written by the in-page "Save key" box
if not os.environ.get("EIA_API_KEY"):
    try:
        with open(KEY_FILE, encoding="utf-8-sig") as _f:
            _k = _f.read().strip()
        if _k:
            os.environ["EIA_API_KEY"] = _k
            KEY_SOURCE = "saved from the dashboard"
    except OSError:
        pass
EIA_KEY = os.environ.get("EIA_API_KEY", "")
DEMO = os.environ.get("NG_DEMO") == "1"
UA = {"User-Agent": "Mozilla/5.0 (ng-dashboard)"}
# rigcount.bakerhughes.com stalls/403s unknown and browser-like User-Agents but serves curl's
BH_HEADERS = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}

_cache = {}   # key -> (timestamp, value); persisted to data/cache.json so restarts don't re-spend API quota
_meta = {}    # key -> {"stale": bool, "error": str}
_lock = threading.Lock()


def save_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    tmp = os.path.join(DATA, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, os.path.join(DATA, name))


def load_json(name, default):
    try:
        with open(os.path.join(DATA, name)) as f:
            return json.load(f)
    except Exception:
        return default


def load_csv(name):
    try:
        with open(os.path.join(DATA, name), newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return None


if not DEMO:
    _cache.update({k: tuple(v) for k, v in load_json("cache.json", {}).items()})


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        if hit:  # serve last good data rather than an error, but flag it
            _meta[key] = {"stale": True, "error": str(e)}
            return hit[1]
        raise
    _meta[key] = {"stale": False, "error": ""}
    with _lock:
        _cache[key] = (time.time(), val)
        if not DEMO:
            try:
                save_json("cache.json", {k: list(v) for k, v in _cache.items()})
            except Exception:
                pass
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


# ---------- EIA ----------
def eia_fetch(path, facets, freq, length, cols=("value",), start=None):
    if not EIA_KEY:
        raise RuntimeError("EIA_API_KEY not set")
    q = [("api_key", EIA_KEY), ("frequency", freq), ("sort[0][column]", "period"),
         ("sort[0][direction]", "desc"), ("length", str(length))]
    q += [(f"data[{i}]", c) for i, c in enumerate(cols)]
    if start:
        q.append(("start", start))
    for k, vals in facets.items():
        q += [(f"facets[{k}][]", v) for v in vals]
    return http_json(f"https://api.eia.gov/v2/{path}/data/?" + urllib.parse.urlencode(q), 30)["response"]["data"]


def eia_raw(path, facets, freq, length):
    rows = eia_fetch(path, facets, freq, length)
    return [(r["period"], float(r["value"])) for r in rows if r.get("value") is not None][::-1]


def eia(route, series, freq, length):
    return eia_raw("natural-gas/" + route, {"series": [series]}, freq, length)


def shift_year(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


# ---------- Storage (total, regions, projection) ----------
def storage_stats(rows, keep=52):
    """rows: ascending [(iso_date, Bcf)] of weekly working gas."""
    pts = [(date.fromisoformat(p), v) for p, v in rows]

    def near(target):
        for i, (d, _) in enumerate(pts):
            if abs((d - target).days) <= 3:
                return i
        return None

    def past_vals(d):
        out = []
        for k in range(1, 6):
            j = near(shift_year(d, -k))
            if j is not None:
                out.append(pts[j][1])
        return out

    weeks = []
    for d, v in pts[-keep:]:
        past = past_vals(d)
        weeks.append({"period": d.isoformat(), "value": v,
                      "avg5": round(statistics.mean(past), 1) if past else None,
                      "min5": min(past) if past else None, "max5": max(past) if past else None})
    d, last = pts[-1]
    change = last - pts[-2][1]
    chgs = []
    for k in range(1, 6):
        j = near(shift_year(d, -k))
        if j:
            chgs.append(pts[j][1] - pts[j - 1][1])
    avg5 = weeks[-1]["avg5"]
    jy = near(shift_year(d, -1))
    # end-of-season projection: add the 5-yr average remaining build/draw to today's level
    if 4 <= d.month <= 10:
        tgt = date(d.year, 10, 31)
    elif d.month >= 11:
        tgt = date(d.year + 1, 3, 31)
    else:
        tgt = date(d.year, 3, 31)
    deltas, ends = [], []
    for k in range(1, 6):
        j, t = near(shift_year(d, -k)), near(shift_year(tgt, -k))
        if j is not None and t is not None:
            deltas.append(pts[t][1] - pts[j][1])
            ends.append(pts[t][1])
    proj = None
    if deltas:
        proj = {"date": tgt.isoformat(), "level": round(last + statistics.mean(deltas), 0),
                "avg5_end": round(statistics.mean(ends), 0), "min5_end": min(ends), "max5_end": max(ends),
                "kind": "end of injection season" if tgt.month == 10 else "end of withdrawal season"}
    return {"weeks": weeks, "latest": last, "change": change,
            "chg_avg5": round(statistics.mean(chgs), 1) if chgs else None,
            "avg5": avg5, "vs_avg5": (last - avg5) if avg5 else None,
            "pct_vs_avg5": ((last - avg5) / avg5 * 100) if avg5 else None,
            "vs_year_ago": (last - pts[jy][1]) if jy is not None else None,
            "asof": d.isoformat(), "projection": proj}


REGION_SERIES = {  # key: (display name, EIA series)
    "east": ("East", "NW2_EPG0_SWO_R31_BCF"), "midwest": ("Midwest", "NW2_EPG0_SWO_R32_BCF"),
    "south_central": ("South Central", "NW2_EPG0_SWO_R33_BCF"), "mountain": ("Mountain", "NW2_EPG0_SWO_R34_BCF"),
    "pacific": ("Pacific", "NW2_EPG0_SWO_R35_BCF"), "salt": ("  of which Salt", "NW2_EPG0_SSO_R33_BCF"),
    "nonsalt": ("  of which Non-salt", "NW2_EPG0_SNO_R33_BCF"),
}


def demo_weekly(scale=1.0, this_year_bump=150):
    today = date.today()
    weeks = [today - timedelta(days=7 * i) for i in range(330)][::-1]
    return [(w.isoformat(), scale * (2600 + 1200 * math.sin((w.timetuple().tm_yday - 100) / 365 * 2 * math.pi))
             + (this_year_bump * scale if w.year == today.year else 0)) for w in weeks]


def api_storage_regions():
    def go():
        regs = []
        if DEMO:
            grouped = {k: demo_weekly(s, 100 * s) for k, s in zip(REGION_SERIES, (.24, .28, .31, .07, .09, .07, .24))}
        else:
            raw = eia_fetch("natural-gas/stor/wkly", {"series": [s for _, s in REGION_SERIES.values()]},
                            "weekly", 330 * len(REGION_SERIES))
            by = {}
            for r in raw:
                if r.get("value") is not None:
                    by.setdefault(r["series"], []).append((r["period"], float(r["value"])))
            grouped = {k: sorted(by.get(s, [])) for k, (_, s) in REGION_SERIES.items()}
        for k, (name, _) in REGION_SERIES.items():
            if len(grouped[k]) > 60:
                s = storage_stats(grouped[k], keep=1)
                regs.append({"key": k, "name": name.strip(), "sub": name.startswith(" "), "latest": s["latest"],
                             "change": s["change"], "vs_avg5": s["vs_avg5"], "pct_vs_avg5": s["pct_vs_avg5"],
                             "vs_year_ago": s["vs_year_ago"]})
        return regs
    return cached("storage_regions", 3600, go)


def api_storage():
    def go():
        rows = demo_weekly() if DEMO else eia("stor/wkly", "NW2_EPG0_SWO_R48_BCF", "weekly", 330)
        return storage_stats(rows)
    out = dict(cached("storage", 3600, go))
    try:  # regions are cached separately so a failure here never poisons the headline numbers
        out["regions"] = api_storage_regions()
    except Exception as e:
        out["regions"] = {"error": str(e)}
    return out


def api_fundamentals():
    specs = {
        "spot": ("pri/fut", "RNGWHHD", "daily", 260),
        "production": ("prod/sum", "N9070US2", "monthly", 36),  # N9070US2 = U.S. DRY natural gas production (MMcf)
        "lng_exports": ("move/expc", "N9133US2", "monthly", 36),
        "mexico_exports": ("move/expc", "N9132MX2", "monthly", 36),
    }

    def one(k):
        route, sid, freq, n = specs[k]
        if DEMO:
            base = {"spot": 3.3, "production": 3.5e6, "lng_exports": 1.1e5, "mexico_exports": 2.2e5}[k]
            step = 1 if freq == "daily" else 30
            return [[(date.today() - timedelta(days=step * (n - i))).isoformat(),
                     base * (1 + 0.1 * math.sin(i / 5) + i * .002)] for i in range(n)]
        return eia(route, sid, freq, n)
    out = {}
    for k in specs:  # cached per series so one failure (e.g. a rate limit) is retried next request, not cached for an hour
        try:
            out[k] = cached("fund_" + k, 3600, lambda k=k: one(k))
        except Exception as e:
            out[k] = {"error": str(e)}
    return out


def api_steo():
    """EIA Short-Term Energy Outlook: marketed production, power-sector consumption, working gas inventory."""
    def go():
        ids = {"prod": "NGMPPUS", "power": "NGEPCON", "inv": "NGWGPUS"}
        today = date.today()
        if DEMO:
            series = {}
            for k, base in (("prod", 108), ("power", 36), ("inv", 3300)):
                series[k] = [[f"{today.year - 1 + (i // 12)}-{i % 12 + 1:02d}",
                              round(base * (1 + .03 * math.sin(i / 3) + .004 * i), 1)] for i in range(36)]
        else:
            raw = eia_fetch("steo", {"seriesId": list(ids.values())}, "monthly", 3 * 40, start=f"{today.year - 1}-01")
            series = {}
            for k, sid in ids.items():
                series[k] = sorted([r["period"], float(r["value"])] for r in raw
                                   if r["seriesId"] == sid and r.get("value") is not None)
        return {"series": series, "current_month": today.strftime("%Y-%m"),
                "units": {"prod": "Bcf/d", "power": "Bcf/d", "inv": "Bcf"}}
    return cached("steo", 6 * 3600, go)


# ---------- Power burn + generation mix + nuclear outages (EIA-930 / EIA outages) ----------
HEAT_RATE = 7.6     # MMBtu per MWh, fleet-average assumption
BTU_PER_CF = 1.037  # MMBtu per Mcf


def mw_to_bcfd(mw):
    """Continuous MW of generation -> Bcf/d of gas at the assumed heat rate."""
    return mw * 24 * HEAT_RATE / BTU_PER_CF / 1e6


def roll7(pairs):
    out = []
    for i in range(6, len(pairs)):
        out.append([pairs[i][0], statistics.mean(v for _, v in pairs[i - 6:i + 1])])
    return out


def api_nuclear():
    def go():
        if DEMO:
            rows = [((date.today() - timedelta(days=430 - i)).isoformat(),
                     9000 + 6000 * math.exp(-((i % 365 - 90) / 40) ** 2) + random.Random(i).gauss(0, 300), 97000)
                    for i in range(430)]
        else:
            raw = eia_fetch("nuclear-outages/us-nuclear-outages", {}, "daily", 430, cols=("outage", "capacity", "percentOutage"))
            rows = [(r["period"], float(r["outage"]), float(r["capacity"])) for r in raw if r.get("outage") is not None][::-1]
        by = {p: (o, c) for p, o, c in rows}
        rec = rows[-90:]
        ly = []
        for p, o, c in rec:
            k = (date.fromisoformat(p) - timedelta(days=364)).isoformat()
            if k in by:
                ly.append([p, round(by[k][0] / by[k][1] * 100, 2)])
        last7 = statistics.mean(o for _, o, _ in rows[-7:])
        ly7 = [by[k][0] for k in ((date.fromisoformat(p) - timedelta(days=364)).isoformat() for p, _, _ in rows[-7:]) if k in by]
        delta = last7 - statistics.mean(ly7) if ly7 else None
        p, o, c = rows[-1]
        return {"days": [[p_, round(o_ / c_ * 100, 2)] for p_, o_, c_ in rec], "last_year": ly,
                "latest_pct": round(o / c * 100, 2), "latest_mw": o, "asof": p,
                "delta_mw": round(delta) if delta is not None else None,
                "delta_bcfd": round(mw_to_bcfd(delta), 2) if delta is not None else None}
    return cached("nuclear", 3600, go)


def api_power():
    def go():
        if DEMO:
            by_fuel = {f: {} for f in ("NG", "WND", "SUN", "WAT", "NUC")}
            for i in range(430):
                d = date.today() - timedelta(days=430 - i)
                doy = d.timetuple().tm_yday
                r = random.Random(i)
                by_fuel["NG"][d.isoformat()] = (38 + 14 * math.exp(-((doy - 205) / 40) ** 2) + r.gauss(0, 1.2)) * 1.037 / HEAT_RATE * 1e6 / 1
                by_fuel["WND"][d.isoformat()] = 1.8e6 + 4e5 * math.sin(doy / 20) + r.gauss(0, 1e5) + (2e5 if d.year == date.today().year else 0)
                by_fuel["SUN"][d.isoformat()] = 4e5 + 5e5 * math.sin((doy - 80) / 365 * math.pi) ** 2 + (2e5 if d.year == date.today().year else 0)
                by_fuel["WAT"][d.isoformat()] = 7e5 + r.gauss(0, 3e4)
                by_fuel["NUC"][d.isoformat()] = 1.9e6
        else:
            raw = eia_fetch("electricity/rto/daily-fuel-type-data",
                            {"respondent": ["US48"], "fueltype": ["NG", "WND", "SUN", "WAT", "NUC"], "timezone": ["Eastern"]},
                            "daily", 430 * 5)
            by_fuel = {}
            for r in raw:
                if r.get("value") is not None:
                    by_fuel.setdefault(r["fueltype"], {})[r["period"]] = float(r["value"])
            if "NG" not in by_fuel:
                raise RuntimeError("EIA returned no natural gas generation rows")
        gas = sorted((p, v * HEAT_RATE / BTU_PER_CF / 1e6) for p, v in by_fuel["NG"].items())  # MWh -> Bcf
        by = dict(gas)
        recent = gas[-90:]

        def ly_of(p):
            return (date.fromisoformat(p) - timedelta(days=364)).isoformat()
        ly = [[p, by[ly_of(p)]] for p, _ in recent if ly_of(p) in by]
        last7 = statistics.mean(v for _, v in gas[-7:])
        ly7 = [by[ly_of(p)] for p, _ in gas[-7:] if ly_of(p) in by]
        out = {"days": [[p, round(v, 2)] for p, v in recent], "last_year": [[p, round(v, 2)] for p, v in ly],
               "latest": round(gas[-1][1], 1), "avg7": round(last7, 1),
               "vs_ly7": round(last7 - statistics.mean(ly7), 1) if ly7 else None, "asof": gas[-1][0],
               "note": f"Estimated from EIA-930 US48 gas generation at {HEAT_RATE} MMBtu/MWh"}
        # wind + solar + hydro (GW, 7-day rolling) vs last year
        try:
            dates = sorted(set(by_fuel["WND"]) & set(by_fuel["SUN"]) & set(by_fuel["WAT"]))
            clean = [(p, (by_fuel["WND"][p] + by_fuel["SUN"][p] + by_fuel["WAT"][p]) / 24 / 1000) for p in dates]
            sm = dict(roll7(clean))
            cur = list(sm.items())[-90:]
            lyc = [[p, sm[ly_of(p)]] for p, _ in cur if ly_of(p) in sm]
            last_p, last_v = list(sm.items())[-1]
            delta = (last_v - sm[ly_of(last_p)]) if ly_of(last_p) in sm else None
            out["clean"] = {"days": [[p, round(v, 1)] for p, v in cur], "last_year": [[p, round(v, 1)] for p, v in lyc],
                            "latest_gw": round(last_v, 1), "asof": last_p,
                            "delta_gw": round(delta, 1) if delta is not None else None,
                            "delta_bcfd": round(mw_to_bcfd(delta * 1000), 2) if delta is not None else None}
        except Exception as e:
            out["clean"] = {"error": str(e)}
        return out
    out = dict(cached("power", 3600, go))
    try:  # separate cache so a nuclear-outage failure doesn't hide power burn
        out["nuclear"] = api_nuclear()
    except Exception as e:
        out["nuclear"] = {"error": str(e)}
    return out


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


# ---------- Baker Hughes rig counts (weekly report xlsx, total + by basin) ----------
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


def scrape_rigs():
    """Return {iso_date: {"gas", "oil", "basin": {name: [gas, oil]}}} of US weekly rig counts."""
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
                cols = {k: r.index(k) for k in ("Country", "Basin", "DrillFor", "US_PublishDate", "Rig Count Value")}
            continue
        if len(r) <= max(cols.values()) or r[cols["Country"]].upper() != "UNITED STATES":
            continue
        kind = r[cols["DrillFor"]].lower()
        if kind not in ("gas", "oil"):
            continue
        d = (date(1899, 12, 30) + timedelta(days=int(float(r[cols["US_PublishDate"]])))).isoformat()
        v = float(r[cols["Rig Count Value"]])
        h = hist.setdefault(d, {"gas": 0.0, "oil": 0.0, "basin": {}})
        h[kind] += v
        b = h["basin"].setdefault(r[cols["Basin"]] or "Other", [0.0, 0.0])
        b[0 if kind == "gas" else 1] += v
    if not hist:
        raise RuntimeError("No US rows parsed from Baker Hughes NAM Weekly sheet")
    return hist


FOCUS_BASINS = [("Haynesville", "gas"), ("Marcellus", "gas"), ("Utica", "gas"), ("Permian", "total"), ("Eagle Ford", "total")]


def api_rigs():
    def go():
        if DEMO:
            hist = {}
            for i in range(110):
                d = (date.today() - timedelta(days=7 * i)).isoformat()
                w = 6 * math.sin(i / 5)
                hist[d] = {"gas": 100 + round(w) + 35, "oil": 410, "basin": {
                    "Haynesville": [50 + round(w / 2), 0], "Marcellus": [22, 0], "Utica": [10, 0],
                    "Permian": [1, 265 + round(w)], "Eagle Ford": [9, 41]}}
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
        ly = date.fromisoformat(ds[-1]) - timedelta(days=364)
        ydate = next((d for d in ds if abs((date.fromisoformat(d) - ly).days) <= 3), None)
        basins, basin_weeks = [], {}

        def val(d, b, how):
            g, o = hist[d].get("basin", {}).get(b, [0, 0])
            return g if how == "gas" else g + o
        if "basin" in hist[ds[-1]]:
            for b, how in FOCUS_BASINS:
                now = val(ds[-1], b, how)
                wow = now - val(ds[-2], b, how) if len(ds) > 1 and "basin" in hist[ds[-2]] else None
                yoy = now - val(ydate, b, how) if ydate and "basin" in hist[ydate] else None
                basins.append({"basin": b, "metric": how, "latest": now, "wow": wow, "yoy": yoy,
                               "yoy_pct": (yoy / (now - yoy) * 100) if yoy is not None and now - yoy else None})
                basin_weeks[b] = [[d, val(d, b, how)] for d in ds[-104:] if "basin" in hist[d]]
        return {"weeks": [[d, hist[d]["gas"], hist[d].get("oil")] for d in ds[-104:]], "latest": last,
                "wow": last - prev if prev is not None else None,
                "yoy": last - hist[ydate]["gas"] if ydate else None, "asof": ds[-1], "points": len(ds),
                "basins": basins, "basin_weeks": basin_weeks}
    return cached("rigs", 6 * 3600, go)


# ---------- LNG feedgas (CSV drop-in; falls back to EIA monthly exports as a proxy) ----------
def api_lng():
    """LNG feedgas, best available: daily CSV > fresh EIA weekly page > EIA STEO current-month estimate > EIA monthly actuals."""
    def go():
        rows = load_csv("lng_feedgas.csv")
        if DEMO:
            return {"source": "demo", "days": [[(date.today() - timedelta(days=120 - i)).isoformat(), round(14 + 1.5 * math.sin(i / 9) + i * .01, 2)]
                                                for i in range(120)]}
        if rows:
            days = [[r["date"], float(r["bcfd"])] for r in rows if r.get("bcfd")][-180:]
            return {"source": "csv", "days": days, "asof": days[-1][0]}
        try:  # weekly S&P figure via EIA, only if the page is actually current
            nw = api_ngwu()
            if not nw["stale"]:
                c = nw["latest"]
                return {"source": "ngwu", "days": [[c["week_end"], c["lng"]]], "asof": c["week_end"],
                        "note": f"Weekly average, week ending {c['week_end']} (EIA Natural Gas Weekly Update / S&P Global)."}
        except Exception:
            pass
        mo = eia("move/expc", "N9133US2", "monthly", 36)  # actual exports, MMcf per month
        actual = [[p, v / 1000 / calendar.monthrange(int(p[:4]), int(p[5:7]))[1]] for p, v in mo]
        est = []
        try:  # STEO estimate for months after the last published actual, up to the current month
            cur = date.today().strftime("%Y-%m")
            steo = eia_fetch("steo", {"seriesId": ["NGEXPUS_LNG"]}, "monthly", 30, start=f"{actual[-1][0][:4]}-01")
            est = sorted([r["period"], float(r["value"])] for r in steo
                         if r.get("value") is not None and actual[-1][0] < r["period"] <= cur)
        except Exception:
            pass
        days = [[p + "-15", round(v, 2)] for p, v in actual[-24:]]
        if est:
            days += [[p + "-15", round(v, 2)] for p, v in est]
            return {"source": "steo_estimate", "days": days, "est_from": est[0][0] + "-15", "asof": est[-1][0],
                    "note": (f"Monthly, not weekly. Actuals through {actual[-1][0]}; {est[0][0]} to {est[-1][0]} are EIA STEO estimates "
                             "of LNG gross exports (approximate feedgas). Updates with each STEO and monthly export release.")}
        return {"source": "eia_proxy", "days": days, "asof": actual[-1][0],
                "note": f"Monthly LNG exports through {actual[-1][0]} (Bcf/d) as a proxy; no fresher free source available."}
    return cached("lng", 3600, go)


def api_dry():
    """US dry gas production (Bcf/d), best available: fresh EIA weekly page > EIA monthly actuals + STEO current-month estimate."""
    def go():
        if DEMO:
            return {"source": "demo", "days": [[f"{date.today().year - 2 + i // 12}-{i % 12 + 1:02d}-15", round(104 + i * .12 + 2 * math.sin(i / 3), 2)] for i in range(30)],
                    "est_from": None, "asof": date.today().strftime("%Y-%m"), "note": "Demo"}
        try:
            nw = api_ngwu()
            if not nw["stale"]:
                c = nw["latest"]
                return {"source": "ngwu", "days": [[c["week_end"], c["dry"]]], "asof": c["week_end"],
                        "note": f"Weekly average, week ending {c['week_end']} (EIA Natural Gas Weekly Update / S&P Global)."}
        except Exception:
            pass
        mo = eia("prod/sum", "N9070US2", "monthly", 36)  # actual dry production, MMcf per month
        actual = [[p, v / 1000 / calendar.monthrange(int(p[:4]), int(p[5:7]))[1]] for p, v in mo]
        est = []
        try:
            cur = date.today().strftime("%Y-%m")
            steo = eia_fetch("steo", {"seriesId": ["NGPRPUS"]}, "monthly", 30, start=f"{actual[-1][0][:4]}-01")
            est = sorted([r["period"], float(r["value"])] for r in steo
                         if r.get("value") is not None and actual[-1][0] < r["period"] <= cur)
        except Exception:
            pass
        days = [[p + "-15", round(v, 2)] for p, v in actual[-24:]]
        if est:
            days += [[p + "-15", round(v, 2)] for p, v in est]
            return {"source": "steo_estimate", "days": days, "est_from": est[0][0] + "-15", "asof": est[-1][0],
                    "note": (f"Monthly, not weekly. Actuals through {actual[-1][0]}; {est[0][0]} to {est[-1][0]} are EIA STEO estimates "
                             "of dry gas production. Updates with each STEO and the monthly production release.")}
        return {"source": "eia_monthly", "days": days, "est_from": None, "asof": actual[-1][0],
                "note": f"Monthly actuals through {actual[-1][0]}; no fresher free source available."}
    return cached("dry", 3600, go)


# ---------- Weather: GFS vs ECMWF vs 10-yr normal, revisions ----------
CITIES = [  # name, lat, lon, weight (rough gas-demand weighting)
    ("New York", 40.71, -74.01, 0.22), ("Chicago", 41.88, -87.63, 0.20),
    ("Boston", 42.36, -71.06, 0.10), ("Atlanta", 33.75, -84.39, 0.12),
    ("Houston", 29.76, -95.37, 0.14), ("Los Angeles", 34.05, -118.24, 0.08),
    ("Minneapolis", 44.98, -93.27, 0.08), ("Philadelphia", 39.95, -75.17, 0.06),
]
MODELS = {"gfs": "gfs_seamless", "ecmwf": "ecmwf_ifs025"}
MODEL_KEYS = ["gfs_hdd", "gfs_cdd", "ecmwf_hdd", "ecmwf_cdd"]


def locs_query():
    return "latitude=%s&longitude=%s" % (",".join(str(c[1]) for c in CITIES), ",".join(str(c[2]) for c in CITIES))


def api_normals():
    """Population-weighted HDD/CDD normal by MM-DD from 10 years of Open-Meteo archive data (cached ~1 year)."""
    def go():
        if DEMO:
            out = {}
            for i in range(366):
                d = date(2024, 1, 1) + timedelta(days=i)
                t = 55 + 24 * math.sin(d.timetuple().tm_yday / 366 * 2 * math.pi - 1.9)
                out[d.strftime("%m-%d")] = [round(max(0, 65 - t), 2), round(max(0, t - 65), 2)]
            return out
        y1 = date.today().year - 1
        res = http_json("https://archive-api.open-meteo.com/v1/archive?%s&start_date=%d-01-01&end_date=%d-12-31"
                        "&daily=temperature_2m_mean&temperature_unit=fahrenheit&timezone=auto" % (locs_query(), y1 - 9, y1), 90)
        acc = {}
        for (_, _, _, w), loc in zip(CITIES, res):
            per = {}
            for day, t in zip(loc["daily"]["time"], loc["daily"]["temperature_2m_mean"]):
                if t is not None:
                    per.setdefault(day[5:], []).append((max(0, 65 - t), max(0, t - 65)))
            for k, v in per.items():
                a = acc.setdefault(k, [0.0, 0.0])
                a[0] += w * statistics.mean(x[0] for x in v)
                a[1] += w * statistics.mean(x[1] for x in v)
        keys = sorted(acc)
        out = {}
        for i, k in enumerate(keys):  # +/-3 day circular smoothing
            win = [acc[keys[(i + j) % len(keys)]] for j in range(-3, 4)]
            out[k] = [round(statistics.mean(x[0] for x in win), 2), round(statistics.mean(x[1] for x in win), 2)]
        return out
    return cached("normals", 365 * 86400, go)


def wx_snapshot_history(now_totals):
    hist = load_json("wx_history.json", [])
    now = time.time()
    if DEMO:
        r = random.Random(1)
        return [{"ts": now - h * 3600, **{k: v + r.gauss(0, 4) for k, v in now_totals.items()}} for h in range(72, 0, -6)]
    if not hist or now - hist[-1]["ts"] > 50 * 60:
        hist.append({"ts": now, **now_totals})
        hist = hist[-720:]
        save_json("wx_history.json", hist)
    return hist


def api_weather():
    def go():
        today = date.today().isoformat()
        days = {}  # date -> {model: [hdd, cdd]}
        live = None
        if not DEMO:  # one multi-location request = one TLS connection through the proxy
            live = [x["daily"] for x in http_json(
                "https://api.open-meteo.com/v1/forecast?%s&daily=temperature_2m_mean&temperature_unit=fahrenheit"
                "&past_days=7&forecast_days=15&models=%s&timezone=auto" % (locs_query(), ",".join(MODELS.values())), 20)]
        for ci, (name, lat, lon, w) in enumerate(CITIES):
            if DEMO:
                for m in MODELS:
                    for i in range(22):
                        d0 = date.today() - timedelta(days=7) + timedelta(days=i)
                        t = 58 + 22 * math.sin(d0.timetuple().tm_yday / 365 * 2 * math.pi - 1.9) + random.Random(i * 7 + lat).gauss(0, 2) + (2.5 if m == "gfs" else 0)
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
        try:
            normals = api_normals()
        except Exception:
            normals = None
        rows = []
        for k, v in sorted(days.items()):
            row = {"date": k, "forecast": k > today,
                   **{m: {"hdd": round(v[m][0], 1), "cdd": round(v[m][1], 1)} for m in MODELS if m in v}}
            if normals:
                n = normals.get(k[5:]) or normals.get("02-28")
                row["normal"] = {"hdd": n[0], "cdd": n[1]}
            rows.append(row)
        fc = [r for r in rows if r["forecast"]][:15]
        tot = {}
        for m in MODELS:
            tot[m + "_hdd"] = round(sum(r.get(m, {}).get("hdd", 0) for r in fc), 1)
            tot[m + "_cdd"] = round(sum(r.get(m, {}).get("cdd", 0) for r in fc), 1)
        norm15, dev = None, None
        if normals:
            norm15 = {"hdd": round(sum(r["normal"]["hdd"] for r in fc), 1), "cdd": round(sum(r["normal"]["cdd"] for r in fc), 1)}
            nd = norm15["hdd"] + norm15["cdd"]
            dev = {m: round(tot[m + "_hdd"] + tot[m + "_cdd"] - nd, 1) for m in MODELS}
            dev["avg"] = round(statistics.mean(dev[m] for m in MODELS), 1)
        hist = wx_snapshot_history(tot)
        now = time.time()

        def rev(hours):
            old = [h for h in hist if h["ts"] <= now - hours * 3600]
            return {k: round(tot[k] - old[-1][k], 1) for k in MODEL_KEYS} if old else None
        return {"days": rows, "totals": tot, "normal15": norm15, "dev": dev, "rev_6h": rev(6), "rev_24h": rev(24),
                "history": [{"ts": h["ts"] * 1000, **{k: h[k] for k in MODEL_KEYS if k in h}} for h in hist[-240:]],
                "models": {"gfs": "GFS", "ecmwf": "ECMWF IFS"}}
    return cached("weather", 1800, go)


# ---------- Producing-region freeze-off watch ----------
PROD_REGIONS = [  # name, lat, lon, freeze-off threshold (deg F, daily minimum)
    ("Appalachia (Marcellus/Utica)", 40.0, -80.0, 10), ("Permian", 31.99, -102.08, 20), ("Haynesville", 32.5, -93.75, 20),
]


def api_prodwx():
    def go():
        if DEMO:
            res = [{"time": [(date.today() + timedelta(days=i)).isoformat() for i in range(15)],
                    "temperature_2m_min": [round(35 - 25 * math.exp(-((i - 8) / 3) ** 2) + random.Random(i + n).gauss(0, 3), 1) for i in range(15)]}
                   for n, _ in enumerate(PROD_REGIONS)]
        else:
            res = [x["daily"] for x in http_json(
                "https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&daily=temperature_2m_min"
                "&temperature_unit=fahrenheit&forecast_days=15&timezone=auto" % (
                    ",".join(str(r[1]) for r in PROD_REGIONS), ",".join(str(r[2]) for r in PROD_REGIONS)), 20)]
        regions, worst, worst_n = [], None, 0
        for (name, _, _, thr), d in zip(PROD_REGIONS, res):
            mins = [[a, b] for a, b in zip(d["time"], d["temperature_2m_min"]) if b is not None]
            n = sum(1 for _, t in mins if t <= thr)
            regions.append({"name": name, "threshold": thr, "days": mins, "risk_days": n,
                            "lowest": min(t for _, t in mins) if mins else None})
            if n > worst_n:
                worst, worst_n = name, n
        return {"regions": regions, "max_risk_days": worst_n, "worst_region": worst}
    return cached("prodwx", 3600, go)


# ---------- Tropical threat (NHC) ----------
GULF_CENTER = (27.5, -90.0)


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def api_tropics():
    def go():
        if DEMO:
            storms = [{"id": "al092026", "name": "Demo", "classification": "TS", "intensity": "50", "latitudeNumeric": 24.0,
                       "longitudeNumeric": -86.0, "movementDir": 315, "movementSpeed": 9,
                       "lastUpdate": datetime.now(timezone.utc).isoformat(), "publicAdvisory": {"url": "https://www.nhc.noaa.gov"}}]
        else:
            storms = http_json("https://www.nhc.noaa.gov/CurrentStorms.json", 15).get("activeStorms", [])
        out = []
        for s in storms:
            lat, lon, kt = s["latitudeNumeric"], s["longitudeNumeric"], int(s.get("intensity") or 0)
            atl = s["id"].lower().startswith("al")
            out.append({"id": s["id"], "name": s["name"], "classification": s["classification"], "kt": kt,
                        "mph": round(kt * 1.15), "lat": lat, "lon": lon,
                        "dist_km": round(haversine_km(lat, lon, *GULF_CENTER)), "atlantic": atl,
                        "in_gulf": atl and 17 <= lat <= 31 and -98 <= lon <= -80,
                        "dir": s.get("movementDir"), "speed": s.get("movementSpeed"), "updated": s.get("lastUpdate"),
                        "url": (s.get("publicAdvisory") or {}).get("url")})
        out.sort(key=lambda x: x["dist_km"])
        return {"storms": out, "asof": datetime.now(timezone.utc).isoformat()}
    return cached("tropics", 1800, go)


# ---------- EIA Natural Gas Weekly Update: weekly dry production, LNG pipeline receipts (feedgas) ----------
NGWU_URL = "https://www.eia.gov/naturalgas/weekly/"
MONTHS = {m: i + 1 for i, m in enumerate("January February March April May June July August September October November December".split())}
UP_WORDS = r"(?:increased|rose|grew|climbed|up)"
DOWN_WORDS = r"(?:decreased|fell|declined|dropped|down)"


def _pdate(txt):
    m = re.search(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", txt or "")
    return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))) if m and m.group(1) in MONTHS else None


def parse_ngwu(page_html):
    """Parse the narrative (not the tables, which are filled by placeholders in some copies)."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page_html, flags=re.S)
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", html.unescape(t))
    hdr = re.search(r"for week ending ([A-Z][a-z]+ \d{1,2}, \d{4})\s*\|\s*Release date:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", t)
    if not hdr:
        raise RuntimeError("NGWU: week-ending header not found (page layout changed?)")
    out = {"week_end": _pdate(hdr.group(1)).isoformat(), "release": _pdate(hdr.group(2)).isoformat()}

    def chg(pattern):
        m = re.search(pattern + r"\s+(?:by\s+)?([\d.]+)%\s*\(([\d.]+) Bcf/d\)", t)
        if not m:
            return None
        return (1 if re.fullmatch(UP_WORDS, m.group(1)) else -1) * float(m.group(3))
    m = re.search(r"[Dd]ry natural gas production.{0,160}?(?:average|averaged|was)\s+([\d.]+)\s*Bcf/d", t)
    out["dry"] = float(m.group(1)) if m else None
    out["dry_wow"] = chg(r"[Dd]ry natural gas production (" + UP_WORDS + "|" + DOWN_WORDS + ")")
    m = re.search(r"(?:LNG pipeline receipts\)?|deliveries to U\.S\. LNG export terminals).{0,160}?(?:averaged|to)\s+([\d.]+)\s*Bcf/d", t)
    out["lng"] = float(m.group(1)) if m else None
    m = re.search(r"deliveries to U\.S\. LNG export terminals (" + UP_WORDS + "|" + DOWN_WORDS + r") ([\d.]+) Bcf/d", t)
    out["lng_wow"] = (1 if m and re.fullmatch(UP_WORDS, m.group(1)) else -1) * float(m.group(2)) if m else None
    out["regions"] = [{"region": "Outside US Gulf Coast" if r.startswith("U.S.") else r, "pct": (1 if re.fullmatch(UP_WORDS, d) else -1) * float(p), "bcfd": (1 if re.fullmatch(UP_WORDS, d) else -1) * float(b)}
                      for r, d, p, b in re.findall(r"terminals (?:in|outside the) ((?:South Louisiana|South Texas|U\.S\. Gulf Coast)[A-Za-z. ]*?)\s+(" + UP_WORDS + "|" + DOWN_WORDS + r")\s+([\d.]+)%\s*\(([\d.]+) Bcf/d\)", t)]
    out["mexico_wow"] = chg(r"exports to Mexico (" + UP_WORDS + "|" + DOWN_WORDS + ")")
    out["canada_wow"] = chg(r"net imports from Canada (" + UP_WORDS + "|" + DOWN_WORDS + ")")
    if out["dry"] is None or out["lng"] is None:
        raise RuntimeError("NGWU: could not find dry production / LNG receipts sentences")
    if not (80 <= out["dry"] <= 140 and 5 <= out["lng"] <= 30):
        raise RuntimeError(f"NGWU: implausible values (dry {out['dry']}, LNG {out['lng']} Bcf/d); ignoring")
    return out


def api_ngwu():
    """Latest week only (no history)."""
    def go():
        if DEMO:
            we = date.today() - timedelta(days=(date.today().weekday() - 2) % 7)
            cur = {"week_end": we.isoformat(), "release": (we + timedelta(days=1)).isoformat(), "dry": 106.0, "lng": 16.6,
                   "dry_wow": -.3, "lng_wow": .4, "mexico_wow": .2, "canada_wow": -.1,
                   "regions": [{"region": "South Louisiana", "pct": 2.3, "bcfd": .3}, {"region": "South Texas", "pct": 10.7, "bcfd": .5},
                               {"region": "Outside US Gulf Coast", "pct": -48.5, "bcfd": -.5}]}
        else:
            cur = parse_ngwu(http_get(NGWU_URL, 30, BH_HEADERS).decode("utf8", "ignore"))
        age = (date.today() - date.fromisoformat(cur["week_end"])).days
        return {"latest": cur, "age_days": age, "stale": age > 12,
                "note": "EIA Natural Gas Weekly Update (S&P Global Commodity Insights); weekly average, parsed from the page narrative."}
    return cached("ngwu", 3 * 3600, go)


# ---------- Scores ----------
def api_scores():
    d = {}
    for name, fn in (("storage", api_storage), ("weather", api_weather), ("power", api_power), ("rigs", api_rigs),
                     ("cftc", api_cftc), ("lng", api_lng), ("fund", api_fundamentals), ("tropics", api_tropics),
                     ("prodwx", api_prodwx), ("ngwu", api_ngwu), ("dry", api_dry)):
        try:
            d[name] = fn()
        except Exception as e:
            d[name] = {"error": str(e)}
    return scoring.compute(d, load_json("score_config.json", {}))


ROUTES = {"/api/storage": api_storage, "/api/fundamentals": api_fundamentals, "/api/power": api_power,
          "/api/cftc": api_cftc, "/api/rigs": api_rigs, "/api/lng": api_lng, "/api/weather": api_weather,
          "/api/prodwx": api_prodwx, "/api/tropics": api_tropics, "/api/steo": api_steo, "/api/ngwu": api_ngwu, "/api/dry": api_dry, "/api/scores": api_scores}
CACHE_KEYS = {"/api/storage": ("storage", "storage_regions"), "/api/fundamentals": ("fund_spot", "fund_production", "fund_lng_exports", "fund_mexico_exports"), "/api/power": ("power", "nuclear"),
              "/api/cftc": "cftc", "/api/rigs": "rigs", "/api/lng": "lng", "/api/weather": "weather",
              "/api/prodwx": "prodwx", "/api/tropics": "tropics", "/api/steo": "steo", "/api/ngwu": "ngwu", "/api/dry": "dry"}


def set_eia_key(key):
    """Validate a pasted key against EIA, then use it now and remember it in data/eia_key.txt."""
    global EIA_KEY, KEY_SOURCE
    key = (key or "").strip().strip('"').strip("'").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{20,60}", key):
        raise ValueError("That does not look like an EIA key (letters and numbers only, about 40 characters). Check for extra spaces.")
    url = ("https://api.eia.gov/v2/natural-gas/stor/wkly/data/?" +
           urllib.parse.urlencode({"api_key": key, "frequency": "weekly", "data[0]": "value", "length": "1"}))
    try:
        http_get(url, 20)
    except OSError as e:
        text = str(e)
        if "429" in text:
            pass  # valid key, just rate limited right now
        elif "403" in text or "401" in text or "400" in text:
            raise ValueError("EIA rejected this key. Copy it again from the EIA email.")
        else:
            raise ValueError("Could not reach EIA to check the key (" + text[:80] + "). Check your internet connection.")
    os.makedirs(DATA, exist_ok=True)
    with open(KEY_FILE, "w") as f:
        f.write(key)
    os.environ["EIA_API_KEY"] = key
    EIA_KEY, KEY_SOURCE = key, "saved from the dashboard"
    for k in list(_cache):  # drop anything cached while the key was missing
        if k not in ("normals",):
            _cache.pop(k, None)


def diag():
    try:
        env_files = sorted(n for n in os.listdir(ROOT) if n.lower().startswith(".env") or n.lower().startswith("env"))
    except OSError:
        env_files = []
    return {"folder": ROOT, "env_files": env_files, "key_source": KEY_SOURCE, "key_loaded": bool(EIA_KEY)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **kw)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            return self.send_json({"demo": DEMO, "eia": bool(EIA_KEY), "version": VERSION, "diag": diag(),
                                   "stale": {k: v["error"] for k, v in _meta.items() if v.get("stale")}})
        if path in ROUTES:
            if "fresh" in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query):
                keys = CACHE_KEYS.get(path, ())  # bypass cache to catch a new release
                for k in ([keys] if isinstance(keys, str) else keys):
                    _cache.pop(k, None)
            try:
                return self.send_json(ROUTES[path]())
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
        return super().do_GET()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/setkey":
            return self.send_json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 2000:
                raise ValueError("Request too large.")
            set_eia_key(json.loads(self.rfile.read(n) or b"{}").get("key"))
            return self.send_json({"ok": True})
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            return self.send_json({"error": "Could not save the key: " + str(e)}, 500)

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
    host = os.environ.get("HOST", "127.0.0.1")  # this computer only; set HOST=0.0.0.0 to share on your network
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"\n*** Could not start on port {port}: {e}\n"
              f"    The dashboard is probably already running in another window. Open http://localhost:{port}\n"
              f"    or close that window first. To use a different port: set PORT=8001 and start again. ***\n", file=sys.stderr)
        sys.exit(1)
    print(f"Dashboard version {VERSION} serving on http://localhost:{port}  demo={DEMO}  eia_key={'yes (' + KEY_SOURCE + ')' if EIA_KEY else 'NO'}", file=sys.stderr)
    if not EIA_KEY and not DEMO:
        print("\n*** No EIA key found yet. Open http://localhost:%d and paste the key into the box at the top of the page. ***\n" % port, file=sys.stderr)
    server.serve_forever()
