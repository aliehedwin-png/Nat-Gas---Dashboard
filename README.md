# Natural Gas Fundamentals Dashboard

Live dashboard of the fundamental drivers of Henry Hub natural gas. Python stdlib only, no build step. No futures price panels.

| Panel | Source | Key / setup |
|---|---|---|
| Storage vs 5-yr avg/range, y/y | EIA | `EIA_API_KEY` |
| Henry Hub spot, production, LNG + Mexico exports | EIA | `EIA_API_KEY` |
| Power burn (Bcf/d, estimated from US48 gas generation) | EIA-930 | `EIA_API_KEY` |
| CFTC managed money / producer / swap net (NYMEX Henry Hub, disaggregated futures-only) | CFTC public reporting API | none |
| Gas rig count | Baker Hughes weekly xlsx (best-effort scrape); history saved in `data/rigs.json` | optional `data/rigs.csv` (`date,gas,oil`) |
| LNG feedgas | No free API: `data/lng_feedgas.csv` (`date,bcfd`). Falls back to EIA monthly LNG exports as a proxy | CSV |
| Weather: GFS vs ECMWF pop-weighted HDD/CDD, 15-day totals | Open-Meteo | none |
| Forecast updates: change in 15-day totals vs ~6h / ~24h ago | server snapshots in `data/wx_history.json` | keep server running |

## Run
```
export EIA_API_KEY=xxxx        # free: https://www.eia.gov/opendata/
python3 server.py              # http://localhost:8000
NG_DEMO=1 python3 server.py    # synthetic data, no network
```
Page auto-refreshes every 5 min; server caches (weather 30 min, EIA/power/LNG 1h, rigs/CFTC 6h).

## Caveats
- Power burn uses a fixed 7.6 MMBtu/MWh heat rate; treat it as an index, not EIA's reported burn.
- Rig scrape and CFTC field names were written without live access; a panel that fails shows its error in place.
- Weather weights are a rough 8-city proxy, not official gas-weighted HDD. Revision history only exists from when the server started collecting.
