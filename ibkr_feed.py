"""Optional live bar feed for the Technical tab (READ-ONLY).

Connects to YOUR OWN TWS or IB Gateway through the IBKR socket API (library: ib_async), downloads bars for the
active NG and CL futures contract and writes them to data/bars/<MKT>_<TF>.json, the files the dashboard reads.
It never places, changes or cancels orders: the connection is opened with readonly=True and no order call exists here.

Run:   python ibkr_feed.py            (keep it open next to the dashboard)
Setup: see TA_SETUP.txt.   Options: --host 127.0.0.1 --port 7497 --client-id 17 --once
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "bars")

# bar size, history length, how often to refresh (seconds). Separate native bars per timeframe, never aggregated.
TFS = {
    "15m": ("15 mins", "5 D", 300),
    "1H": ("1 hour", "30 D", 300),
    "4H": ("4 hours", "120 D", 1800),
    "1D": ("1 day", "1 Y", 1800),
}
MARKETS = {"NG": "NYMEX", "CL": "NYMEX"}
PORTS = [7497, 7496, 4002, 4001]      # TWS paper, TWS live, Gateway paper, Gateway live
ROLL_DAYS = 7                           # about 5 trading days before the last trade date
ROLL_SESSIONS = 2                       # next month busier than front month for 2 sessions in a row


def parse_last_trade(s):
    s = (s or "")[:8]
    return date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s) == 8 else None


def pick_contract(contracts, daily_volume, today):
    """contracts: [(localSymbol, lastTradeDate)] sorted by expiry. daily_volume: {localSymbol: [vol of last sessions, oldest first]}.
    Returns the localSymbol to use: the front month until the roll rule says the next month has taken over."""
    live = [(sym, d) for sym, d in contracts if d and d >= today]
    if not live:
        raise RuntimeError("no unexpired contract found")
    front = live[0]
    if len(live) == 1:
        return front[0]
    nxt = live[1]
    if (front[1] - today).days <= ROLL_DAYS:
        return nxt[0]
    fv, nv = daily_volume.get(front[0], []), daily_volume.get(nxt[0], [])
    k = ROLL_SESSIONS
    if len(fv) >= k and len(nv) >= k and all(nv[-i] > fv[-i] for i in range(1, k + 1)):
        return nxt[0]
    return front[0]


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_payload(market, contract, tf, bars, fetched_ts):
    """bars: list of (epoch, o, h, l, c, v). Column format, same as sample_bars/."""
    return {"market": market, "contract": contract, "exchange": MARKETS[market], "tf": tf, "source": "ibkr-feed",
            "fetched": iso(fetched_ts), "time": [iso(b[0]) for b in bars], "open": [b[1] for b in bars],
            "high": [b[2] for b in bars], "low": [b[3] for b in bars], "close": [b[4] for b in bars],
            "volume": [b[5] for b in bars]}


def valid(bars):
    for b in bars:
        if not (b[3] <= min(b[1], b[4]) + 1e-9 and b[2] >= max(b[1], b[4]) - 1e-9 and b[2] >= b[3]):
            return False
    return len(bars) > 0


def write_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)


STATUS = os.path.join(HERE, "data", "ibkr_status.json")


def status(state, message, **extra):
    """Tell the dashboard what the feed is doing: state is starting, waiting, connected or error."""
    try:
        write_atomic(STATUS, dict({"state": state, "message": message, "ts": int(time.time()), "pid": os.getpid()}, **extra))
    except OSError:
        pass


def connect(IB, host, port, client_id):
    """Try each API port once; return a connected IB or None (the caller retries, so TWS may be started later)."""
    ports = [port] if port else PORTS
    last = None
    for p in ports:
        ib = IB()
        try:
            ib.connect(host, p, clientId=client_id, readonly=True, timeout=6)
            print(f"Connected to {host}:{p} (read-only)")
            status("connected", f"Connected to TWS / IB Gateway on port {p} (read-only)", port=p)
            return ib
        except Exception as e:  # noqa: BLE001 - report and try the next port
            last = e
    msg = ("Waiting for TWS or IB Gateway: start it, log in and enable the API (TA_SETUP.txt). "
           f"Tried ports {', '.join(map(str, ports))}.")
    print(msg, f"({last})" if last else "")
    status("waiting", msg)
    return None


def fetch_bars(ib, contract, size, duration):
    hist = ib.reqHistoricalData(contract, endDateTime="", durationStr=duration, barSizeSetting=size,
                                whatToShow="TRADES", useRTH=False, formatDate=2, keepUpToDate=False)
    out = []
    for b in hist:
        d = b.date
        if not isinstance(d, datetime) and isinstance(d, date):     # daily bars come as a plain date
            d = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        ts = int(d.timestamp()) if hasattr(d, "timestamp") else int(d)
        out.append((ts, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)))
    return out


def choose(ib, Future, market):
    cds = ib.reqContractDetails(Future(symbol=market, exchange=MARKETS[market], currency="USD"))
    cons = sorted(((cd.contract, parse_last_trade(cd.contract.lastTradeDateOrContractMonth)) for cd in cds), key=lambda x: x[1] or date.max)
    today = datetime.now(timezone.utc).date()
    live = [(c, d) for c, d in cons if d and d >= today][:2]
    vols = {}
    for c, _ in live:
        try:
            vols[c.localSymbol] = [b[5] for b in fetch_bars(ib, c, "1 day", "10 D")]
        except Exception:  # noqa: BLE001
            vols[c.localSymbol] = []
    sym = pick_contract([(c.localSymbol, d) for c, d in cons], vols, today)
    return next(c for c, _ in cons if c.localSymbol == sym)


def check_parent(args):
    """Exit when the dashboard that started this feed has gone away."""
    if args.parent:
        try:
            os.kill(args.parent, 0)
        except OSError:
            raise SystemExit(0)


def run(args):
    status("starting", "Starting the IBKR feed")
    try:
        from ib_async import IB, Future
    except ImportError:
        status("error", "The ib_async package is missing. Run:  python -m pip install ib_async  (the Start Dashboard launcher does this), then restart.")
        raise SystemExit("The ib_async package is missing. Run:  python -m pip install ib_async   (see TA_SETUP.txt)")
    ib = None
    while ib is None:
        check_parent(args)
        ib = connect(IB, args.host, args.port, args.client_id)
        if ib is None:
            if args.once:
                raise SystemExit("Could not connect to TWS or IB Gateway. See TA_SETUP.txt.")
            time.sleep(10)
    last = {}
    held = {}
    try:
        while True:
            check_parent(args)
            if not ib.isConnected():
                print("Lost the connection to TWS / IB Gateway; reconnecting")
                status("waiting", "Lost the connection to TWS / IB Gateway; reconnecting")
                try:
                    ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                ib = None
                while ib is None:
                    check_parent(args)
                    time.sleep(10)
                    ib = connect(IB, args.host, args.port, args.client_id)
            now = time.time()
            for market in MARKETS:
                try:
                    con = held.get(market)
                    if con is None or now - last.get((market, "pick"), 0) > 3600:
                        new = choose(ib, Future, market)
                        if con is not None and new.localSymbol != con.localSymbol:
                            print(f"{market}: rolled {con.localSymbol} -> {new.localSymbol}; rebuilding all timeframes")
                            for k in [k for k in last if k[0] == market and k[1] != "pick"]:
                                del last[k]
                        held[market], con = new, new
                        last[(market, "pick")] = now
                    for tf, (size, dur, every) in TFS.items():
                        if now - last.get((market, tf), 0) < every:
                            continue
                        bars = fetch_bars(ib, con, size, dur)
                        if not valid(bars):
                            print(f"{market} {tf}: bad or empty data, kept the old file")
                            continue
                        write_atomic(os.path.join(OUT, f"{market}_{tf}.json"), to_payload(market, con.localSymbol, tf, bars, time.time()))
                        last[(market, tf)] = now
                        status("connected", f"Connected; last update {market} {con.localSymbol} {tf}", updated=int(time.time()))
                        print(f"{time.strftime('%H:%M:%S')} {market} {con.localSymbol} {tf}: {len(bars)} bars")
                        ib.sleep(1.5)     # stay well inside IBKR pacing limits
                except Exception as e:  # noqa: BLE001 - keep the feed alive
                    print(f"{market}: {e}")
            if args.once:
                break
            ib.sleep(15)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    ap.add_argument("--parent", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT") or 0), help="API port; default tries 7497, 7496, 4002, 4001")
    ap.add_argument("--client-id", type=int, default=17)
    ap.add_argument("--once", action="store_true", help="download everything once and exit")
    try:
        run(ap.parse_args())
    except KeyboardInterrupt:
        sys.exit(0)
