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
| Supply | **Weekly dry gas production** (latest week only, w/w, plus Mexico exports and Canada imports w/w) | EIA Natural Gas Weekly Update (S&P Global) | none |
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

Alerts only fire while the dashboard tab is open. Holiday weeks can shift releases.

## Run
```
export EIA_API_KEY=xxxx        # free: https://www.eia.gov/opendata/
python3 server.py              # http://localhost:8000
NG_DEMO=1 python3 server.py    # synthetic data, no network
```
Server caches responses (weather 30 min, most EIA 1h, CFTC/rigs/STEO 6h, weather normals ~1 year) and persists them in `data/cache.json`, so restarts don't re-spend API quota; if a refresh fails it serves the last good data and the page flags it.

## Caveats
- **Weekly LNG / dry production (EIA NGWU):** parsed from the page narrative; latest week only, no history. It is flagged stale (and not scored) if the page is >12 days old. The copy reachable from the build sandbox was dated Jan 2026 and repeated identical figures across archived weeks, so these numbers could not be validated there. Check a couple of weeks against eia.gov/naturalgas/weekly before relying on them; their score weights (`lng_weekly`, `dry_production`) default to 0 (shown, not in the composite). Set them in `data/score_config.json` once verified.
- Verified live: CFTC, Baker Hughes (total + basins, matches their summary), Open-Meteo (forecast, archive normals, producing regions), NHC, and EIA storage (total + regions sum to total), power burn, generation mix, nuclear outages, STEO. EIA spot/production/exports/LNG proxy were verified earlier. The shared `DEMO_KEY` rate-limits quickly: use your own key.
- Power burn uses a fixed 7.6 MMBtu/MWh heat rate; treat it as an index. Renewables/nuclear Bcf/d figures use the same conversion.
- Weather weights are a rough 8-city proxy, not official gas-weighted HDD. Forecast-revision scores need ~6h of server uptime.
- `rigcount.bakerhughes.com` stalls/403s most client User-Agents but serves curl's, so that one host is fetched with a curl User-Agent.
- Not available for free: daily production, pipeline flows, daily LNG feedgas (drop a CSV into `data/lng_feedgas.csv`), and price/basis/spread data (removed by request).
- Not investment advice.
