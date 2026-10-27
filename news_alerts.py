"""Crude oil (CL) and natural gas (NG) news alerts. INFORMATION ONLY: it never places, changes or cancels orders.

Every few minutes it reads news headlines from public RSS feeds and, when TWS or IB Gateway is running, from the free
IBKR news providers (read-only API connection). Headlines are kept only if they are about crude oil or natural gas,
tagged by topic (EIA, OPEC+, Middle East supply, weather, LNG, ...) and given a rough bullish / bearish keyword guess.
Results go to data/news/news.json for the dashboard's News tab. Important headlines also pop up as a Windows
notification. Reminders pop up 5 minutes before the EIA crude (Wed) and natural gas storage (Thu) reports.

Run:   python news_alerts.py            (keep it open; "Run News Alerts (Windows).bat" restarts it if it stops)
       python news_alerts.py --once     (one check, prints what it found, no pop-ups)
Options: --no-ibkr  --no-popups  --every 300
"""
import argparse
import email.utils
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "news")
NEWS = os.path.join(OUT, "news.json")
STATUS = os.path.join(OUT, "status.json")
SEEN = os.path.join(OUT, "seen.json")
ALERTS = os.path.join(OUT, "alerts.csv")

EVERY = 300             # seconds between checks
KEEP = 300              # headlines kept in news.json
MAX_AGE = 2 * 86400     # older headlines are ignored
POPUP_GAP = 90          # seconds between pop-ups; extra important headlines are grouped into the next one
POPUP_PER_HOUR = 12
CLIENT_ID = 23          # dashboard feed uses 17, exports 18, paper engine 21
PORTS = [7497, 7496, 4002, 4001]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def gnews(q):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})


FEEDS = {
    "Google News: crude oil": gnews('"crude oil" OR "oil prices" OR "WTI crude" OR "Brent crude"'),
    "Google News: OPEC+": gnews("OPEC"),
    "Google News: oil supply Middle East": gnews("oil Hormuz OR Iran OR Saudi OR Houthi OR tanker"),
    "Google News: EIA inventories": gnews("EIA crude inventories OR \"natural gas storage\" OR \"API crude\""),
    "Google News: natural gas": gnews('"natural gas" prices OR "Henry Hub" OR futures'),
    "Google News: LNG": gnews("LNG exports OR feedgas OR \"Freeport LNG\" OR \"Sabine Pass\""),
    "Google News: gas weather": gnews("natural gas weather forecast OR heating demand OR cold OR heat wave"),
    "OilPrice.com": "https://oilprice.com/rss/main",
    "Rigzone": "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
    "Natural Gas Intelligence": "https://www.naturalgasintel.com/feed/",
    "FXEmpire commodities": "https://www.fxempire.com/api/v1/en/articles/rss/news?category=commodities",
}

# A headline must mention its market to be kept.
MARKET_WORDS = {
    "CL": r"crude|\boil\b|oil price|\bwti\b|brent (crude|oil|futures|price)|opec|petroleum|gasoline|distillate|refiner|barrel|\bbpd\b|tanker|shale",
    "NG": r"natural gas|nat ?gas|\blng\b|henry hub|gas storage|gas prices|feedgas|\bbcf\b|ttf\b|gas futures|gas inventor",
}
TOPICS = {
    "EIA / inventories": r"\beia\b|inventor|stockpile|storage (report|build|draw|injection)|\bapi\b.*(crude|stock)|crude (build|draw)|injection|withdrawal",
    "OPEC+": r"opec|saudi|aramco|\buae\b|output (cut|hike|increase)|production (cut|quota)|quota",
    "Middle East / Hormuz": r"hormuz|\biran|israel|houthi|red sea|iraq|gaza|lebanon|yemen|strait|tanker (attack|seiz)|middle east",
    "Russia / sanctions": r"russia|ukrain|sanction|price cap|urals|druzhba|venezuela",
    "Weather": r"weather|forecast|cold|freeze|frigid|heat ?wave|mild|warm|\bhdd\b|\bcdd\b|polar vortex|noaa|winter",
    "Hurricane": r"hurricane|tropical storm|gulf of (mexico|america) storm|named storm",
    "LNG": r"\blng\b|feedgas|freeport|sabine|cameron|corpus christi|plaquemines|calcasieu|golden pass|export terminal",
    "US supply": r"rig count|baker hughes|production record|shale|permian|haynesville|appalachia|output",
    "Demand / macro": r"china|demand|recession|tariff|fed\b|rate cut|economy|imports",
}
# Words that usually push price up / down. The guess is a keyword count, not a forecast.
UP = (r"cut|outage|disrupt|attack|strike|\bdraw|deficit|shortage|sanction|cold|freeze|frigid|heat ?wave|hurricane|"
      r"explosion|fire\b|shut|halt|force majeure|seiz|(?<!de-)(?<!de)escalat|surge|jump|rally|soar|record demand|tighten|lower output|below expect")
DOWN = (r"\bbuild\b|surplus|glut|oversuppl|ceasefire|truce|peace|mild|warm|output (hike|increase|rise)|raise output|boost output|"
        r"de-?escalat|feedgas cut|increase production|record (output|production)|weak demand|slump|plunge|tumble|drop|\bfall|slide|above expect|larger than expected injection|resume|restart|\bease")
# A headline with one of these words is important enough for a pop-up.
URGENT = (r"opec|hormuz|attack|explosion|force majeure|outage|shut|halt|seiz|hurricane|sanction|ceasefire|(?<!de-)(?<!de)escalat|missile|drone|"
          r"strike on|eia (report|data)|inventor(y|ies) (build|draw|rose|fell)|storage (build|injection|report)|surprise|emergency|"
          r"spr\b|strategic petroleum|plunge|soar|surge|limit (up|down)|freeport")
BAD_SOURCE = r"\b(seeking alpha premium|subscribe|podcast)\b"


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_time(txt):
    if not txt:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(txt)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(txt.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_feed(blob, source):
    """RSS or Atom bytes -> list of {title, link, ts, source, summary}."""
    root = ET.fromstring(blob)
    out = []
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        f = {c.tag.rsplit("}", 1)[-1]: c for c in it}
        title = clean(f["title"].text if "title" in f else "")
        link = ""
        if "link" in f:
            link = (f["link"].text or f["link"].get("href") or "").strip()
        when = None
        for k in ("pubDate", "published", "updated", "date"):
            if k in f:
                when = parse_time(f[k].text)
                break
        src = source
        if "source" in f and (f["source"].text or "").strip():
            src = f"{f['source'].text.strip()} (via Google News)"
            title = re.sub(r"\s+-\s+" + re.escape(f["source"].text.strip()) + r"$", "", title)
        desc = f.get("description", f.get("summary"))
        summary = clean(desc.text if desc is not None else "")
        if title:
            out.append({"title": title, "link": link, "ts": when, "source": src, "summary": summary[:300]})
    return out


def key_of(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:90]


def classify(title, summary=""):
    """Return None if not about CL or NG, else markets, topics, bias guess and urgency."""
    t = title.lower()
    full = (t + " " + (summary or "").lower())
    if re.search(BAD_SOURCE, t):
        return None
    markets = [m for m, rx in MARKET_WORDS.items() if re.search(rx, t)]
    if not markets:   # summary may name the market when the headline does not, but then it must hit a topic in the title
        markets = [m for m, rx in MARKET_WORDS.items() if re.search(rx, full)]
        if not markets or not any(re.search(rx, t) for rx in TOPICS.values()):
            return None
    topics = [k for k, rx in TOPICS.items() if re.search(rx, t)]
    up, down = len(re.findall(UP, t)), len(re.findall(DOWN, t))
    bias = "bullish" if up > down else "bearish" if down > up else "neutral"
    urgent = bool(re.search(URGENT, t))
    score = 1 + 2 * urgent + len(topics) + (bias != "neutral")
    return {"markets": markets, "topics": topics, "bias": bias, "urgent": urgent, "score": score}


# ---------- US Eastern time without tzdata (Windows Python often lacks it) ----------
def eastern(now_utc):
    """US Eastern local time: DST from the 2nd Sunday of March 07:00 UTC to the 1st Sunday of November 06:00 UTC."""
    y = now_utc.year
    mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    off = -4 if start <= now_utc < end else -5
    return (now_utc + timedelta(hours=off)).replace(tzinfo=None)


def report_reminder(now_utc):
    """Reminder text if an EIA report is due in about 5 minutes (10:25-10:29 ET), else None."""
    et = eastern(now_utc)
    if not (et.hour == 10 and 25 <= et.minute < 30):
        return None
    if et.weekday() == 2:
        return "EIA weekly crude oil inventories at 10:30 ET (CL)"
    if et.weekday() == 3:
        return "EIA natural gas storage report at 10:30 ET (NG)"
    return None


# ---------- Windows pop-ups ----------
PS_TOAST = r"""
$ErrorActionPreference='Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:NEWS_TOAST_XML)
$t = New-Object Windows.UI.Notifications.ToastNotification $x
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($t)
"""


def toast_xml(title, lines, link=""):
    esc = lambda s: html.escape(s, quote=True)
    body = "".join(f"<text>{esc(l)}</text>" for l in lines[:2])
    launch = f' activationType="protocol" launch="{esc(link)}"' if link else ""
    return f'<toast{launch}><visual><binding template="ToastGeneric"><text>{esc(title)}</text>{body}</binding></visual></toast>'


def popup(title, lines, link=""):
    """Show a Windows notification. Returns True if shown. Falls back to the console bell elsewhere."""
    print(f"\a*** {title}: " + " | ".join(lines))
    if os.name != "nt":
        return False
    env = dict(os.environ, NEWS_TOAST_XML=toast_xml(title, lines, link))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_TOAST], env=env,
                           capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            print("Pop-up failed:", r.stderr.decode(errors="replace")[:300])
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001 - a failed pop-up must not stop the news check
        print("Pop-up failed:", e)
        return False


# ---------- IBKR news (read-only) ----------
class Ibkr:
    def __init__(self, host="127.0.0.1"):
        self.host, self.ib, self.providers, self.conids, self.msg = host, None, "", {}, "not tried"

    def connect(self):
        try:
            from ib_async import IB, Future
        except ImportError:
            self.msg = "ib_async not installed"
            return False
        if self.ib is not None and self.ib.isConnected():
            return True
        for p in PORTS:
            ib = IB()
            try:
                ib.connect(self.host, p, clientId=CLIENT_ID, readonly=True, timeout=6)
            except Exception:  # noqa: BLE001 - try the next port
                continue
            self.ib = ib
            try:
                provs = ib.reqNewsProviders()
                self.providers = "+".join(x.code for x in provs)
                for sym in ("CL", "NG"):
                    det = ib.reqContractDetails(Future(sym, exchange="NYMEX"))
                    det.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
                    if det:
                        self.conids[sym] = det[0].contract.conId
            except Exception as e:  # noqa: BLE001
                self.msg = f"connected on {p}, setup failed: {e}"
                return True
            self.msg = f"connected on port {p}; providers: {self.providers or 'none'}"
            return True
        self.msg = "TWS / IB Gateway not reachable (ports " + ", ".join(map(str, PORTS)) + ")"
        return False

    def fetch(self):
        if not self.connect() or not self.providers:
            return []
        out = []
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y%m%d %H:%M:%S")
        for sym, cid in self.conids.items():
            try:
                rows = self.ib.reqHistoricalNews(cid, self.providers, start, "", 50)
            except Exception as e:  # noqa: BLE001
                self.msg = f"news request failed: {e}"
                continue
            for r in rows or []:
                title = clean(re.sub(r"^\{[^}]*\}", "", r.headline or ""))
                ts = r.time if isinstance(r.time, datetime) else None
                out.append({"title": title, "link": "", "ts": int(ts.replace(tzinfo=ts.tzinfo or timezone.utc).timestamp()) if ts else None,
                            "source": f"IBKR {r.providerCode}", "summary": "", "hint": sym})
        return out


# ---------- state ----------
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def log_alert(item):
    new = not os.path.exists(ALERTS)
    with open(ALERTS, "a", encoding="utf-8") as f:
        if new:
            f.write("utc,markets,bias,topics,source,title,link\n")
        q = lambda s: '"' + str(s).replace('"', "'") + '"'
        f.write(",".join([datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "/".join(item["markets"]), item["bias"],
                          q("; ".join(item["topics"])), q(item["source"]), q(item["title"]), q(item["link"])]) + "\n")


def check(state, ibkr, now=None):
    """One pass over all sources. Returns the new relevant items (newest first)."""
    now = now or time.time()
    seen = state["seen"]
    sources, raw = {}, []
    for name, url in FEEDS.items():
        try:
            items = parse_feed(http_get(url), name)
            raw += items
            sources[name] = {"ok": True, "items": len(items), "ts": int(now)}
        except Exception as e:  # noqa: BLE001 - one bad feed must not stop the others
            sources[name] = {"ok": False, "error": str(e)[:200], "ts": int(now)}
    if ibkr is not None:
        items = ibkr.fetch()
        raw += items
        sources["IBKR news"] = {"ok": ibkr.ib is not None and ibkr.ib.isConnected(), "items": len(items), "note": ibkr.msg, "ts": int(now)}
    new = []
    for it in raw:
        k = key_of(it["title"])
        if not k or k in seen:
            continue
        ts = it["ts"] or int(now)
        if now - ts > MAX_AGE or ts > now + 3600:
            continue
        c = classify(it["title"], it.get("summary", ""))
        if c is None and it.get("hint"):   # IBKR headlines are already filed under the contract
            c = classify(it["title"] + (" crude oil" if it["hint"] == "CL" else " natural gas"))
        if c is None:
            continue
        seen[k] = int(now)
        new.append(dict(it, ts=ts, found=int(now), **c))
    for k in [k for k, v in seen.items() if now - v > 4 * 86400]:
        del seen[k]
    new.sort(key=lambda x: -x["ts"])
    state["sources"] = sources
    return new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one check, print results, no pop-ups")
    ap.add_argument("--no-ibkr", action="store_true")
    ap.add_argument("--no-popups", action="store_true")
    ap.add_argument("--every", type=int, default=EVERY)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    ibkr = None if args.no_ibkr else Ibkr()
    stored = load(NEWS, {"items": []})
    kept = []
    for it in stored["items"]:   # re-check stored headlines so filter changes apply to them too
        c = classify(it["title"], it.get("summary", "")) or (it.get("hint") and classify(it["title"] + (" crude oil" if it["hint"] == "CL" else " natural gas")))
        if c:
            kept.append(dict(it, **c))
    stored["items"] = kept
    first = not os.path.exists(SEEN)
    state = {"seen": load(SEEN, {}), "sources": {}}
    pending, last_pop, pops, reminded = [], 0, [], ""
    print("CL / NG news alerts (information only, no trading). Checking every", args.every, "s. Close this window to stop.")
    mtime = os.path.getmtime(__file__)
    if not args.once and not args.no_popups:
        popup("CL / NG news alerts are running", ["Important crude oil and natural gas headlines will pop up here.",
                                                 "All headlines: dashboard News tab."])
    while True:
        now = time.time()
        try:
            new = check(state, ibkr, now)
        except Exception as e:  # noqa: BLE001
            print("Check failed:", e)
            new = []
        items = (new + stored["items"])[:KEEP]
        stored = {"updated": int(now), "items": items}
        save(NEWS, stored)
        save(SEEN, state["seen"])
        ok = sum(1 for s in state["sources"].values() if s.get("ok"))
        save(STATUS, {"ts": int(now), "pid": os.getpid(), "sources": state["sources"], "ok": ok, "total": len(state["sources"]), "new": len(new)})
        print(f"{datetime.now().strftime('%H:%M')}  {len(new)} new CL/NG headlines  ({ok}/{len(state['sources'])} sources ok)")
        for it in new[:15]:
            print(f"   [{'/'.join(it['markets'])} {it['bias']}{' !' if it['urgent'] else ''}] {it['title']}  ({it['source']})")
        if args.once:
            return
        # Pop-ups: urgent and recent (on the very first run only the last 30 minutes, so old news does not flood in).
        fresh = 1800 if first else 6 * 3600
        for it in new:
            if it["urgent"] and now - it["ts"] < fresh:
                pending.append(it)
                log_alert(it)
        first = False
        pops = [t for t in pops if now - t < 3600]
        if pending and not args.no_popups and now - last_pop >= POPUP_GAP and len(pops) < POPUP_PER_HOUR:
            top = pending[0]
            more = f" (+{len(pending) - 1} more on the dashboard News tab)" if len(pending) > 1 else ""
            title = f"{'/'.join(top['markets'])} news, looks {top['bias']}" if top["bias"] != "neutral" else f"{'/'.join(top['markets'])} news"
            popup(title, [top["title"], top["source"] + more], top["link"])
            pending, last_pop = [], now
            pops.append(now)
        rem = report_reminder(datetime.now(timezone.utc))
        if rem and rem != reminded and not args.no_popups:
            popup("Report in 5 minutes", [rem])
            reminded = rem
        elif not rem:
            reminded = ""
        # wake every minute so report reminders are on time; fetch news every args.every seconds
        nxt = now + args.every
        while time.time() < nxt:
            if os.path.getmtime(__file__) != mtime:   # updated code: exit so the .bat restarts it with the new version
                print("news_alerts.py was updated; restarting.")
                return
            time.sleep(min(60, max(1, nxt - time.time())))
            rem = report_reminder(datetime.now(timezone.utc))
            if rem and rem != reminded and not args.no_popups:
                popup("Report in 5 minutes", [rem])
                reminded = rem


if __name__ == "__main__":
    main()
