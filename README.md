# Natural Gas Fundamentals Dashboard

Live dashboard of the fundamental drivers of Henry Hub natural gas, with a transparent bullish/bearish score next to every metric. Python stdlib only, no build step. No futures price panels.

## Panels and sources
| Area | Panel | Source | Setup |
|---|---|---|---|
| Weather | GFS vs ECMWF pop-weighted HDD+CDD vs **10-yr normal**; 15-day totals and deviation | Open-Meteo forecast + archive | none |
| Weather | Forecast updates: change in 15-day totals vs ~6h / ~24h ago | server snapshots (`data/wx_history.json`) | keep server running |
| Weather | **Producing-region freeze watch** (Appalachia, Permian, Haynesville daily minimums) | Open-Meteo | none |
| Storage | Working gas vs 5-yr avg/range, w/w change vs 5-yr avg change, **end-of-season projection** | EIA | `EIA_API_KEY` |
| Storage | **By region** (East, Midwest, South Central, Mountain, Pacific, salt / non-salt) | EIA | `EIA_API_KEY` |
| Demand | Power burn (Bcf/d, estimated from US48 gas generation) | EIA-930 | `EIA_API_KEY` |
| Demand | **Wind + solar + hydro** vs last year; **nuclear outages** vs last year (both converted to Bcf/d of gas) | EIA-930, EIA nuclear outages | `EIA_API_KEY` |
| Demand | **Weekly LNG feedgas** (pipeline receipts to LNG terminals, latest week only, with regional changes) | EIA Natural Gas Weekly Update (S&P Global) | none |
| Demand | LNG feedgas (daily CSV) or LNG exports proxy; Mexico pipeline exports | `data/lng_feedgas.csv` / EIA | CSV optional |
| Supply | Gas rigs, **rigs by basin** (Haynesville, Marcellus, Utica, Permian, Eagle Ford) | Baker Hughes weekly report | none |
| Supply | **Dry gas production**: weekly if EIA's weekly page is current, else monthly actuals plus STEO current-month estimate | EIA NGWU / EIA API | `EIA_API_KEY` |
| Supply | Marketed production, **EIA STEO** production and inventory outlook | EIA | `EIA_API_KEY` |
| Positioning | CFTC managed money / producer / swap net (NYMEX Henry Hub, disaggregated futures-only) | CFTC public reporting API | none |
| Risk | **Tropical watch** (NHC active storms, distance to central Gulf) | NHC CurrentStorms.json | none |

## Bullish / bearish score
Every scored metric gets **-2 (strongly bearish) … +2 (strongly bullish) for price**, shown as a badge next to the metric (hover for the reasoning). The composite is the weight-normalised average, -100…+100. Examples of the rules (all in `scoring.py`):
- Storage surplus vs 5-yr avg: deficit >5% = +2, >2% = +1; surplus mirrors.
- Weekly injection vs 5-yr avg change: 15+ Bcf smaller build = +2.
- 15-day HDD+CDD vs normal: +1.5 degree-days/day above normal = +2.
- Power burn vs last year (Bcf/d), renewables/nuclear converted to Bcf/d-equivalent.
- Gas rigs and production y/y: growth is bearish. CFTC is **contrarian** (crowded shorts = squeeze risk). Gulf tropical storms are net bearish (LNG/demand loss outweighs shut-in supply).
- Region and basin scores are shown but weight 0 (not in composite).

These are judgement-call heuristics, not a forecast. Change weights (0 = show but exclude) in `data/score_config.json`:
```json
{"weights": {"wx_vs_normal": 20, "cftc": 0}}
```

## Release schedule and alerts
The schedule card lists when each source is next expected with a countdown. **Enable alerts** for browser notifications: (1) when a release is due, (2) when the server actually sees new data (polls `?fresh=1` every minute for up to 45 min), including **each metric's score and the composite change**, e.g. `Storage 3,351 Bcf ... / Weekly injection vs 5-yr avg: Strongly bullish (+2) / Composite -14 (was -8)`.

| Source | Expected |
|---|---|
| EIA storage | Thu 10:30 ET |
| Baker Hughes rigs | Fri 1:00 pm ET |
| CFTC COT | Fri 3:30 pm ET |
| EIA-930 power generation | weekdays ~11:00 ET (approx) |
| GFS / ECMWF runs | ~04/10/16/22Z and ~08/20Z (approx) |
| NHC advisories | 03/09/15/21Z |
| EIA STEO | Tuesday after the first Thursday, 12:00 ET (approx) |
| EIA monthly production/exports | last weekday of month (approx) |

New data is detected per source, not only at the scheduled time: if EIA (or any source) publishes early or late, the next refresh or one-minute check announces it and the row shows "✓ updated HH:MM". If the source can't be read during a check, the row says so. Alerts only fire while the dashboard tab is open. Holiday weeks can shift releases.

## Run
Easiest: start the server and paste your key into the box that appears at the top of the page; it is checked with EIA and saved in `data/eia_key.txt`. Alternatively put your free EIA key (https://www.eia.gov/opendata/) in a `.env` file next to `server.py` (copy `.env.example`; `.env` is gitignored) or export it:
```
cp .env.example .env           # then edit EIA_API_KEY=...
# or: export EIA_API_KEY=xxxx
python3 server.py              # http://localhost:8000 (this computer only; HOST=0.0.0.0 to share on your network)
NG_DEMO=1 python3 server.py    # synthetic data, no network
```
Server caches responses (weather 30 min, most EIA 1h, CFTC/rigs/STEO 6h, weather normals ~1 year) and persists them in `data/cache.json`, so restarts don't re-spend API quota; if a refresh fails it serves the last good data and the page flags it.

## Changing or fixing the EIA key
The **EIA key** button at the top of the page is always available. The key box also opens by itself when EIA rejects the saved key. A key pasted into the page is saved in `data/eia_key.txt` and takes priority over the environment variable and any `.env` file (delete `data/eia_key.txt` to go back to them).

## Why the page is quick to show the key box
The page asks the program for its status first (instant) and draws the key box and "Connected" status before requesting any data. Data requests are limited to one open request per source, so a slow source (the first-time weather history download) can never crowd out the rest. The weather forecast no longer waits for the 10-year normals: they download in the background and appear by themselves.

## If the page opens but nothing works
- The page shows a red **"not connected to the dashboard program"** message if the program (`server.py`) is not running or if `index.html` was opened as a file. Start it with the launcher and use http://localhost:8000. The page reconnects by itself.
- The launchers set `OPEN_BROWSER=1`, so the program opens your browser itself once it is listening (no timing guesswork). Run `python3 server.py` yourself and it will not open a browser; add `OPEN_BROWSER=1` if you want that.
- A blue note appears while the first start downloads weather history (1 to 2 minutes).

## Reliability
- First start downloads ten years of weather history for the normals (about a minute); the server does this in the background and the page fills in by itself.
- If a source fails, the last good data is served and the page says which panels are older; a failed source is not retried for 45 seconds (so a down source never stalls the page). A forced refresh (release checks) ignores that pause.
- Tested: all endpoints on live and demo data, scoring edge cases, phone/tablet/desktop widths, dark mode, three time zones, DST and month/year-end release dates, network down with and without saved data.

## Caveats
- **Dry gas production source order:** EIA Natural Gas Weekly Update if under 12 days old > EIA monthly actuals (series N9070US2, which is *dry* production) extended with the STEO estimate for months not yet published (monthly, not weekly; labelled as an estimate). Earlier versions mislabelled the monthly N9070US2 panel as "marketed" production; it is dry production. STEO's separate marketed-production outlook (NGMPPUS) is unchanged.
- **LNG feedgas source order:** daily CSV (`data/lng_feedgas.csv`) > EIA Natural Gas Weekly Update if under 12 days old > EIA STEO current-month estimate of LNG gross exports (monthly, approximate feedgas) > EIA monthly actuals. The card and tile state which one is shown and its date. No free source of a current weekly or daily feedgas figure was reachable when this was built; the EIA weekly page available there was frozen at the Jan 21, 2026 report, so its weekly cards are hidden while stale.
- **Weekly LNG / dry production (EIA NGWU):** parsed from the page narrative; latest week only, no history. It is flagged stale (and not scored) if the page is >12 days old. The copy reachable from the build sandbox was dated Jan 2026 and repeated identical figures across archived weeks, so these numbers could not be validated there. Check a couple of weeks against eia.gov/naturalgas/weekly before relying on them; their score weights (`lng_weekly`, `dry_production`) default to 0 (shown, not in the composite). Set them in `data/score_config.json` once verified.
- Verified live: CFTC, Baker Hughes (total + basins, matches their summary), Open-Meteo (forecast, archive normals, producing regions), NHC, and EIA storage (total + regions sum to total), power burn, generation mix, nuclear outages, STEO. EIA spot/production/exports/LNG proxy were verified earlier. The shared `DEMO_KEY` rate-limits quickly: use your own key.
- Power burn uses a fixed 7.6 MMBtu/MWh heat rate; treat it as an index. Renewables/nuclear Bcf/d figures use the same conversion.
- Weather weights are a rough 8-city proxy, not official gas-weighted HDD. Forecast-revision scores need ~6h of server uptime.
- `rigcount.bakerhughes.com` stalls/403s most client User-Agents but serves curl's, so that one host is fetched with a curl User-Agent.
- Not available for free: daily production, pipeline flows, daily LNG feedgas (drop a CSV into `data/lng_feedgas.csv`), and price/basis/spread data (removed by request).
- Not investment advice.
