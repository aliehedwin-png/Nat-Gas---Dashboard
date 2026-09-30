# Natural Gas Fundamentals Dashboard

Live dashboard of the fundamental drivers of Henry Hub natural gas. Python stdlib only, no build step. No futures price panels.

| Panel | Source | Key / setup |
|---|---|---|
| Storage vs 5-yr avg/range, y/y | EIA | `EIA_API_KEY` |
| Henry Hub spot, production, LNG + Mexico exports | EIA | `EIA_API_KEY` |
| Power burn (Bcf/d, estimated from US48 gas generation) | EIA-930 | `EIA_API_KEY` |
| CFTC managed money / producer / swap net (NYMEX Henry Hub, disaggregated futures-only) | CFTC public reporting API | none |
| Gas rig count (history back to Jan 2024 on first run) | Baker Hughes weekly report xlsx, `NAM Weekly` sheet summed for US gas/oil; cached in `data/rigs.json` | optional `data/rigs.csv` (`date,gas,oil`) |
| LNG feedgas | No free API: `data/lng_feedgas.csv` (`date,bcfd`). Falls back to EIA monthly LNG exports as a proxy | CSV |
| Weather: GFS vs ECMWF pop-weighted HDD/CDD, 15-day totals | Open-Meteo | none |
| Forecast updates: change in 15-day totals vs ~6h / ~24h ago | server snapshots in `data/wx_history.json` | keep server running |

## Release alerts
The "Data release schedule" card lists when each source is next expected, with a countdown. Click **Enable alerts** to get browser notifications:
1. when a release becomes due, and
2. once the server actually sees new data (it polls with `?fresh=1` every minute for up to 45 min), with the new numbers. The dashboard also refreshes itself.

| Source | Expected |
|---|---|
| EIA storage report | Thu 10:30 ET |
| Baker Hughes rigs | Fri 1:00 pm ET |
| CFTC COT | Fri 3:30 pm ET |
| EIA-930 power generation | weekdays ~11:00 ET (approx) |
| GFS / ECMWF runs | ~04/10/16/22Z and ~08/20Z (approx, includes processing lag) |

Alerts only fire while the dashboard tab is open. Schedules ignore federal holidays; a holiday week may shift a release by a day.

## Run
```
export EIA_API_KEY=xxxx        # free: https://www.eia.gov/opendata/
python3 server.py              # http://localhost:8000
NG_DEMO=1 python3 server.py    # synthetic data, no network
```
Page auto-refreshes every 5 min; server caches (weather 30 min, EIA/power/LNG 1h, rigs/CFTC 6h).

## Caveats
- Power burn uses a fixed 7.6 MMBtu/MWh heat rate; treat it as an index, not EIA's reported burn.
- Verified live: CFTC, Baker Hughes rigs (matches their published summary), Open-Meteo, and EIA storage/spot/production/exports/LNG proxy/power burn. A failing panel shows its error in place and serves last good data if a refresh fails.
- `rigcount.bakerhughes.com` stalls or 403s most client User-Agents (including browser-like ones) but serves curl's, so that one host is fetched with a curl User-Agent.
- EIA's shared `DEMO_KEY` rate-limits quickly; use your own key.
- Weather weights are a rough 8-city proxy, not official gas-weighted HDD. Revision history only exists from when the server started collecting.
