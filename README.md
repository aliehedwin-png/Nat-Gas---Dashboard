# Natural Gas Futures Dashboard

Live dashboard of the main drivers of Henry Hub natural gas futures. Python stdlib only, no build step.

| Panel | Source | Key needed |
|---|---|---|
| NG front month (1D/3M/1Y), WTI, TTF, oil/gas ratio | Yahoo Finance | no |
| 12-month forward curve | Yahoo Finance (`NG<month><yy>.NYM`) | no |
| Weekly storage vs 5-yr avg/range, y/y | EIA | yes |
| Henry Hub spot, production, LNG + Mexico exports | EIA | yes |
| Weighted HDD/CDD, past 7d + 15d forecast | Open-Meteo | no |

## Run
```
export EIA_API_KEY=xxxx        # free: https://www.eia.gov/opendata/
python3 server.py              # http://localhost:8000
NG_DEMO=1 python3 server.py    # synthetic data, no network
```
Page auto-refreshes every 60s; the server caches (prices 60s, curve 10min, EIA/weather 1h).

## Notes
- Yahoo's endpoint is unofficial and may rate-limit or change; failures show per-panel errors.
- Weather weights are a rough city-based proxy, not official gas-weighted HDD.
- Not investment advice.
