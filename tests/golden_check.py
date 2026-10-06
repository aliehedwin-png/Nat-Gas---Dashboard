"""Dev-only golden check: compares ta.py indicators with the `ta` + pandas libraries on a long synthetic series.
Run:  pip install pandas ta   then   python tests/golden_check.py
(The shipped dashboard itself needs neither library.)"""
import os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange, BollingerBands
import ta_engine as eng

rnd = random.Random(7)
c, h, l = [], [], []
p = 3.0
for _ in range(3000):
    o = p
    p = max(0.5, p * (1 + rnd.gauss(0, 0.012)))
    hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, 0.004))), min(o, p) * (1 - abs(rnd.gauss(0, 0.004)))
    c.append(p); h.append(hi); l.append(lo)
S, H, L = pd.Series(c), pd.Series(h), pd.Series(l)

def maxrel(mine, ref, skip=2200):   # EMA seeds differ (SMA seed here, first-value seed in the reference); they converge long before bar 2200
    worst = 0.0
    for a, b in zip(mine[skip:], list(ref)[skip:]):
        worst = max(worst, abs(a - b) / max(abs(b), 1e-9))
    return worst

checks = {
    "SMA 20": (eng.sma(c, 20), SMAIndicator(S, 20).sma_indicator()),
    "SMA 50": (eng.sma(c, 50), SMAIndicator(S, 50).sma_indicator()),
    "EMA 20": (eng.ema(c, 20), EMAIndicator(S, 20).ema_indicator()),
    "EMA 50": (eng.ema(c, 50), EMAIndicator(S, 50).ema_indicator()),
    "EMA 100": (eng.ema(c, 100), EMAIndicator(S, 100).ema_indicator()),
    "EMA 200": (eng.ema(c, 200), EMAIndicator(S, 200).ema_indicator()),
    "RSI 14": (eng.rsi(c, 14), RSIIndicator(S, 14).rsi()),
}
m = MACD(S, 26, 12, 9)
ml, ms, mh = eng.macd(c)
checks["MACD line"] = (ml, m.macd()); checks["MACD signal"] = (ms, m.macd_signal()); checks["MACD hist"] = (mh, m.macd_diff())
bb = BollingerBands(S, 20, 2)
bu, bmid, bl = eng.bollinger(c, 20, 2)
checks["BB upper"] = (bu, bb.bollinger_hband()); checks["BB mid"] = (bmid, bb.bollinger_mavg()); checks["BB lower"] = (bl, bb.bollinger_lband())
checks["ATR 14"] = (eng.atr(h, l, c, 14), AverageTrueRange(H, L, S, 14).average_true_range())
bad = 0
for name, (mine, ref) in checks.items():
    w = maxrel(mine, ref)
    ok = w < 1e-6 if name != "MACD hist" else w < 1e-5   # hist is a small difference of two large numbers
    bad += (not ok)
    print(f"{name:12} max relative difference {w:.2e}  {'OK' if ok else 'MISMATCH'}")
print("ALL MATCH" if not bad else f"{bad} MISMATCH(ES)")
sys.exit(1 if bad else 0)
