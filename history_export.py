"""One-time export of long daily history for backtests (READ-ONLY).

Connects to YOUR OWN TWS or IB Gateway, downloads IBKR's continuous-futures daily bars for NG and CL
(as many years as IBKR returns, up to 20) and writes them to data/history/<MKT>_daily_contfut.csv.
It never places, changes or cancels orders: the connection is opened with readonly=True and no order call exists here.
It can run while the dashboard is open (it uses its own client id).

Run:   python history_export.py        then send the two CSV files from data/history/ back to Claude.
Options: --host 127.0.0.1 --port 7496 --client-id 18
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timezone

import ibkr_feed as feed

OUT = os.path.join(feed.HERE, "data", "history")
DURATIONS = ["20 Y", "15 Y", "10 Y", "5 Y", "2 Y"]   # longest first; IBKR refuses what it does not have


def export(ib, ContFuture, market):
    con = ContFuture(symbol=market, exchange=feed.MARKETS[market], currency="USD")
    ib.qualifyContracts(con)
    last = None
    for dur in DURATIONS:
        try:
            bars = feed.fetch_bars(ib, con, "1 day", dur)
        except Exception as e:  # noqa: BLE001 - try a shorter span
            last, bars = e, []
        if bars:
            break
        print(f"{market}: no data for {dur}{f' ({last})' if last else ''}, trying shorter")
    if not bars:
        raise RuntimeError(f"{market}: IBKR returned no daily bars (NYMEX market data on the account?)")
    if not feed.valid(bars):
        print(f"{market}: warning, some bars have high/low outside open/close; kept as IBKR sent them")
    path = os.path.join(OUT, f"{market}_daily_contfut.csv")
    os.makedirs(OUT, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for ts, o, h, l, c, v in bars:
            w.writerow([datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"), o, h, l, c, v])
    first = datetime.fromtimestamp(bars[0][0], timezone.utc).date()
    lastd = datetime.fromtimestamp(bars[-1][0], timezone.utc).date()
    print(f"{market}: {len(bars)} daily bars, {first} to {lastd} ({dur} requested) -> {path}")
    return path


def main(args):
    try:
        from ib_async import IB, ContFuture
    except ImportError:
        raise SystemExit("The ib_async package is missing. Run:  python -m pip install ib_async")
    ports = [args.port] if args.port else feed.PORTS
    ib = None
    for p in ports:
        ib = IB()
        try:
            ib.connect(args.host, p, clientId=args.client_id, readonly=True, timeout=6)
            print(f"Connected to {args.host}:{p} (read-only)")
            break
        except Exception:  # noqa: BLE001 - try the next port
            ib = None
    if ib is None:
        raise SystemExit(f"Could not connect to TWS or IB Gateway on ports {ports}. Log in to TWS and enable the API (TA_SETUP.txt).")
    failed = False
    try:
        for market in feed.MARKETS:
            try:
                export(ib, ContFuture, market)
            except Exception as e:  # noqa: BLE001 - still try the other market
                failed = True
                print(f"{market}: failed: {e}")
            ib.sleep(2)
    finally:
        ib.disconnect()
    print("Done. Send the CSV files in data/history/ back to Claude." if not failed else "Finished with errors (see above).")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT") or 0))
    ap.add_argument("--client-id", type=int, default=18)
    sys.exit(main(ap.parse_args()))
