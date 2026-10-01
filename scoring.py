"""Rule-based bullish/bearish scoring for natural gas fundamentals.

Every metric gets an integer score from -2 (strongly bearish for price) to +2 (strongly bullish),
a one-line reason, and a weight. The composite is the weight-normalised average scaled to -100..+100.

These are transparent heuristics, not a forecast: thresholds below are judgement calls. Override weights
(set a weight to 0 to show a metric but exclude it from the composite) in data/score_config.json:
    {"weights": {"wx_vs_normal": 20, "cftc": 0}}
"""
from statistics import mean

LABELS = {2: "Strongly bullish", 1: "Bullish", 0: "Neutral", -1: "Bearish", -2: "Strongly bearish"}

# id -> (metric name, group, default weight)
METRICS = {
    "storage_surplus":    ("Storage vs 5-yr avg", "Storage", 14),
    "storage_injection":  ("Weekly injection vs 5-yr avg", "Storage", 10),
    "storage_projection": ("End-of-season storage projection", "Storage", 4),
    "wx_vs_normal":       ("15-day weather vs normal", "Weather", 16),
    "wx_revision":        ("Forecast revision (HDD+CDD)", "Weather", 8),
    "freeze":             ("Producing-region freeze risk", "Weather", 3),
    "power_burn":         ("Power burn vs last year", "Demand", 8),
    "renewables":         ("Wind+solar+hydro vs last year", "Demand", 4),
    "nuclear":            ("Nuclear outages vs last year", "Demand", 3),
    "lng":                ("LNG feedgas / exports trend", "Demand", 12),
    "lng_weekly":         ("LNG feedgas, weekly change (EIA NGWU)", "Demand", 0),
    "dry_production":     ("Dry gas production trend", "Supply", 4),
    "mexico":             ("Pipeline exports to Mexico y/y", "Demand", 3),
    "production":         ("Dry gas production y/y (monthly actual)", "Supply", 8),
    "rigs_gas":           ("Gas rigs y/y", "Supply", 4),
    "cftc":               ("Managed money positioning (contrarian)", "Positioning", 4),
    "hurricane":          ("Gulf tropical threat", "Risk", 2),
}


def z(x):
    """Round-to-zero helper so reasons never print '-0'."""
    return 0 if abs(x) < 0.5 else x


def tier(x, t1, t2):
    """x >= t2 -> +2, >= t1 -> +1, <= -t2 -> -2, <= -t1 -> -1, else 0."""
    if x is None:
        return None
    return 2 if x >= t2 else 1 if x >= t1 else -2 if x <= -t2 else -1 if x <= -t1 else 0


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def good(x):
    return isinstance(x, dict) and "error" not in x


def pct_change(new, old):
    return None if not old else (new - old) / abs(old) * 100


def compute(d, cfg=None):
    cfg = cfg or {}
    weights = {k: v[2] for k, v in METRICS.items()}
    weights.update(cfg.get("weights", {}))
    items = []

    def add(id_, score, why, metric=None, group=None, weight=None):
        if score is None:
            return
        m = METRICS.get(id_)
        items.append({"id": id_, "metric": metric or m[0], "group": group or m[1], "score": score,
                      "label": LABELS[score], "why": why,
                      "weight": weights.get(id_, 0) if weight is None else weight})

    # ---- storage
    st = d.get("storage")
    if good(st):
        pct = st.get("pct_vs_avg5")
        if pct is not None:
            add("storage_surplus", tier(-pct, 2, 5), f"{z(st['vs_avg5']):+.0f} Bcf ({pct:+.1f}%) vs 5-yr avg")
        if st.get("chg_avg5") is not None:
            diff = st["change"] - st["chg_avg5"]
            add("storage_injection", tier(-diff, 5, 15),
                f"{st['change']:+.0f} Bcf vs 5-yr avg {st['chg_avg5']:+.0f} ({z(diff):+.0f})")
        pr = st.get("projection")
        if pr and pr.get("max5_end") is not None and pr["max5_end"] > pr["min5_end"]:
            pos = (pr["level"] - pr["min5_end"]) / (pr["max5_end"] - pr["min5_end"])
            s = -2 if pos > 1 else -1 if pos > .75 else 2 if pos < 0 else 1 if pos < .25 else 0
            add("storage_projection", s, f"Projected {pr['level']:,.0f} Bcf by {pr['date']} "
                f"(5-yr range {pr['min5_end']:,.0f}-{pr['max5_end']:,.0f})")
        regs = st.get("regions")
        if isinstance(regs, list):
            for r in regs:
                if r.get("pct_vs_avg5") is not None:
                    add("storage_region_" + r["key"], tier(-r["pct_vs_avg5"], 2, 5),
                        f"{z(r['vs_avg5']):+.0f} Bcf ({r['pct_vs_avg5']:+.1f}%) vs 5-yr avg",
                        metric=f"Storage: {r['name']}", group="Storage", weight=0)

    # ---- weather
    w = d.get("weather")
    if good(w):
        dev = (w.get("dev") or {}).get("avg")
        if dev is not None:
            per = dev / 15
            add("wx_vs_normal", tier(per, .5, 1.5), f"GFS/ECMWF 15-day avg {dev:+.0f} degree-days vs normal ({per:+.1f}/day)")
        r = w.get("rev_24h") or w.get("rev_6h")
        if r:
            ch = mean([r["gfs_hdd"] + r["gfs_cdd"], r["ecmwf_hdd"] + r["ecmwf_cdd"]])
            add("wx_revision", tier(ch, 5, 15), f"15-day HDD+CDD total {ch:+.1f} vs "
                f"{'~24h' if w.get('rev_24h') else '~6h'} ago")
    pw = d.get("prodwx")
    if good(pw):
        n = pw.get("max_risk_days", 0)
        add("freeze", 2 if n >= 6 else 1 if n >= 3 else 0,
            f"{n} forecast day(s) at/below freeze-off threshold in {pw.get('worst_region') or 'any producing region'}")

    # ---- power / demand
    p = d.get("power")
    if good(p):
        if p.get("vs_ly7") is not None:
            add("power_burn", tier(p["vs_ly7"], .75, 2), f"7-day avg {p['avg7']:.1f} Bcf/d, {p['vs_ly7']:+.1f} vs last year")
        c = p.get("clean")
        if good(c) and c.get("delta_bcfd") is not None:
            add("renewables", tier(-c["delta_bcfd"], .5, 1.5),
                f"Wind+solar+hydro {c['delta_gw']:+.1f} GW vs last year (~{c['delta_bcfd']:+.1f} Bcf/d of gas displaced)")
        n = p.get("nuclear")
        if good(n) and n.get("delta_bcfd") is not None:
            add("nuclear", tier(n["delta_bcfd"], .3, .8),
                f"Outages {n['latest_pct']:.1f}% of capacity, {n['delta_mw']:+,.0f} MW vs last year (~{n['delta_bcfd'] + 0:+.1f} Bcf/d)")
    lng = d.get("lng")
    if good(lng) and lng.get("days"):
        vals = [v for _, v in lng["days"]]
        if lng.get("source") in ("eia_proxy", "steo_estimate"):
            if len(vals) >= 4:
                x = vals[-1] - mean(vals[-4:-1])
                add("lng", tier(x, .5, 1.5), f"Latest month {vals[-1]:.1f} Bcf/d vs prior-3-month avg ({x:+.1f}) [{'EIA estimate' if lng['source'] == 'steo_estimate' else 'exports proxy'}]")
        elif len(vals) >= 14:
            last7 = mean(vals[-7:]); prev = mean(vals[-37:-7] or vals[:-7])
            add("lng", tier(last7 - prev, .4, 1.0), f"7-day avg {last7:.1f} Bcf/d vs prior-30-day {prev:.1f} ({last7 - prev:+.1f})")
    fu = d.get("fund")
    if good(fu):
        prod = fu.get("production")
        if isinstance(prod, list) and len(prod) > 13:
            y = pct_change(prod[-1][1], prod[-13][1])
            if y is not None:
                add("production", tier(-y, 2, 5), f"{prod[-1][0]} marketed production {y:+.1f}% y/y")
        mex = fu.get("mexico_exports")
        if isinstance(mex, list) and len(mex) > 13:
            y = pct_change(mex[-1][1], mex[-13][1])
            if y is not None:
                add("mexico", tier(y, 4, 10), f"{mex[-1][0]} exports to Mexico {y:+.1f}% y/y")

    nw = d.get("ngwu")
    if good(nw) and not nw.get("stale"):
        cur = nw["latest"]
        if cur.get("lng_wow") is not None:
            add("lng_weekly", tier(cur["lng_wow"], .4, 1.0),
                f"LNG pipeline receipts {cur['lng']:.1f} Bcf/d, {cur['lng_wow']:+.1f} w/w (week ending {cur['week_end']})")

    dry = d.get("dry")
    if good(dry) and dry.get("days"):
        v = [x[1] for x in dry["days"]]
        if dry.get("source") == "ngwu":
            cur = (d.get("ngwu") or {}).get("latest") or {}
            if cur.get("dry_wow") is not None:
                add("dry_production", tier(-cur["dry_wow"], .5, 1.5),
                    f"Dry production {cur['dry']:.1f} Bcf/d, {cur['dry_wow']:+.1f} w/w (week ending {cur['week_end']})")
        elif dry.get("source") in ("steo_estimate", "eia_monthly", "demo") and len(v) >= 4:
            x = v[-1] - mean(v[-4:-1])
            add("dry_production", tier(-x, .5, 1.5), f"{dry['asof']} dry production {v[-1]:.1f} Bcf/d vs prior-3-month avg ({x:+.1f})"
                + (" [EIA estimate]" if dry.get("source") == "steo_estimate" else ""))

    # ---- supply / rigs
    r = d.get("rigs")
    if good(r):
        if r.get("yoy") is not None and r["latest"] - r["yoy"]:
            y = r["yoy"] / (r["latest"] - r["yoy"]) * 100
            add("rigs_gas", tier(-y, 8, 18), f"{r['latest']:.0f} gas rigs, {r['yoy']:+.0f} ({y:+.0f}%) y/y, {r['wow']:+.0f} w/w")
        for b in r.get("basins") or []:
            if b.get("yoy_pct") is not None:
                add("rigs_basin_" + b["basin"].lower().replace(" ", "_"), tier(-b["yoy_pct"], 8, 18),
                    f"{b['latest']:.0f} rigs ({b['metric']}), {b['yoy_pct']:+.0f}% y/y",
                    metric=f"Rigs: {b['basin']}", group="Supply", weight=0)

    # ---- positioning
    c = d.get("cftc")
    if good(c):
        pc = c["pctile_3y"]
        s = 2 if pc <= 10 else 1 if pc <= 25 else -2 if pc >= 90 else -1 if pc >= 75 else 0
        add("cftc", s, f"Managed money net {c['latest']['mm_net']:,.0f} = {ordinal(pc)} percentile of 3 years "
            "(crowded shorts = squeeze risk, crowded longs = vulnerable)")

    # ---- tropics
    t = d.get("tropics")
    if good(t):
        worst, names = 0, []
        for s in t["storms"]:
            if not s["atlantic"] or not (s["in_gulf"] or s["dist_km"] <= 800) or s["kt"] < 34:
                continue
            names.append(f"{s['name']} ({s['classification']}, {s['kt']} kt, {s['dist_km']:.0f} km)")
            worst = min(worst, -2 if s["kt"] >= 64 else -1)
        add("hurricane", worst, ("; ".join(names) + " - net bearish historically: LNG/demand loss outweighs shut-in supply")
            if names else "No tropical storm/hurricane threatening the Gulf")

    scored = [i for i in items if i["weight"] > 0]
    tw = sum(i["weight"] for i in scored)
    comp = round(100 * sum(i["weight"] * i["score"] for i in scored) / (2 * tw)) if tw else None
    all_w = sum(w_ for k, w_ in weights.items() if k in METRICS)
    label = (None if comp is None else "Strongly bullish" if comp >= 40 else "Bullish" if comp >= 15
             else "Strongly bearish" if comp <= -40 else "Bearish" if comp <= -15 else "Neutral")
    return {"items": items, "composite": {"value": comp, "label": label,
                                          "coverage": round(tw / all_w, 2) if all_w else 0}}
