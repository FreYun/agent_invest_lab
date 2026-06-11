"""Pure-Python (no pandas) replication of local_sweep.py for 中证500 / 中证A500.

Why: validate whether the *mechanical timing anchors* referenced in
strategies/index-products/zz500.md (donchian / dd_ladder / crash-derisk /
vol-target / momentum) actually beat buy-and-hold on mid-cap 中证500 — the
same way the project validated hs300/zz1000 in local_sweep.py.

Hygiene (copied from local_sweep.py):
  IS  = 2017-01-01 .. 2023-12-31   (rank candidates by Calmar, post 5bp)
  OOS = 2024-01-01 .. 2026-06-06   (evaluate IS top-8 once, vs buyhold)
  long-only fractional position, sig.shift(1) applied to next-bar return,
  cost = |turnover| * 5bp on the bar position changes.

Engine self-check: we re-run hs300(510300)/zz1000(512100) and compare to the
committed summary.csv numbers before trusting the 中证500 result.

Data: Eastmoney push2his klines, fqt=1 (qfq), same adjustment as the akshare
qfq loader the project used. ETF proxies (akshare/quotes don't serve raw
000905/000510). Fetched CSVs are cached under data/ in the project's format.
"""
import csv
import json
import math
import os
import urllib.request
from pathlib import Path

BASE = Path(os.path.expanduser("~/rooot/agent_invest_lab/research/index_timing"))
DATA = BASE / "data"
COMMISSION = 0.0005
IS_START, IS_END = "2017-01-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2026-06-06"

# label -> (eastmoney secid, csv code)
FETCH = {
    "hs300":  ("1.510300", "510300.SH"),   # engine self-check vs summary.csv
    "zz1000": ("1.512100", "512100.SH"),   # engine self-check vs summary.csv
    "zz500":  ("1.510500", "510500.SH"),   # 中证500 ETF  — the question
    "a500_a": ("1.512050", "512050.SH"),   # 中证A500 ETF candidate (SH)
    "a500_b": ("0.159338", "159338.SZ"),   # 中证A500 ETF candidate (SZ)
}


# ───────────────────────── data ─────────────────────────
def fetch(secid: str, code: str) -> list:
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56&"
           "klt=101&fqt=1&beg=0&end=20500101")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if not d.get("data") or not d["data"].get("klines"):
        return []
    rows = []  # (date, open, high, low, close, volume)  -> project CSV order
    for ln in d["data"]["klines"]:
        p = ln.split(",")  # date,open,close,high,low,volume
        rows.append((p[0], float(p[1]), float(p[3]), float(p[4]), float(p[2]), float(p[5])))
    (DATA / f"{code}.csv").write_text(
        "trade_date,open,high,low,close,volume\n"
        + "\n".join(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{int(r[5])}" for r in rows) + "\n")
    return rows


def load_or_fetch(secid, code):
    rows = fetch(secid, code)
    dates = [r[0] for r in rows]
    o = [r[1] for r in rows]; h = [r[2] for r in rows]
    lo = [r[3] for r in rows]; c = [r[4] for r in rows]
    return {"date": dates, "open": o, "high": h, "low": lo, "close": c}


# ─────────────────── rolling primitives (NaN = None) ───────────────────
def shift(a, n):
    return [None] * n + a[:-n] if n > 0 else a[:]

def pct_change(c):
    out = [None]
    for i in range(1, len(c)):
        out.append(c[i] / c[i - 1] - 1.0 if c[i - 1] else None)
    return out

def roll_max(a, w, on_shift1=False):
    src = shift(a, 1) if on_shift1 else a
    out = []
    for i in range(len(src)):
        if i < w - 1:
            out.append(None); continue
        seg = [x for x in src[i - w + 1:i + 1] if x is not None]
        out.append(max(seg) if len(seg) == w else None)
    return out

def roll_min(a, w, on_shift1=False):
    src = shift(a, 1) if on_shift1 else a
    out = []
    for i in range(len(src)):
        if i < w - 1:
            out.append(None); continue
        seg = [x for x in src[i - w + 1:i + 1] if x is not None]
        out.append(min(seg) if len(seg) == w else None)
    return out

def roll_mean(a, w):
    out = []
    for i in range(len(a)):
        if i < w - 1:
            out.append(None); continue
        seg = [x for x in a[i - w + 1:i + 1] if x is not None]
        out.append(sum(seg) / w if len(seg) == w else None)
    return out

def roll_std(a, w):  # ddof=1, matches pandas .rolling().std()
    out = []
    for i in range(len(a)):
        if i < w - 1:
            out.append(None); continue
        seg = [x for x in a[i - w + 1:i + 1] if x is not None]
        if len(seg) < w:
            out.append(None); continue
        m = sum(seg) / w
        out.append(math.sqrt(sum((x - m) ** 2 for x in seg) / (w - 1)))
    return out

def diff_n(a, n):
    return [None if (a[i] is None or a[i - n] is None or i < n) else a[i] - a[i - n]
            for i in range(len(a))]

def ffill(a, init=0.0):
    out = []; last = init
    for x in a:
        if x is not None:
            last = x
        out.append(last)
    return out

SQ = math.sqrt(252.0)


# ───────────────────────── signals (faithful to local_sweep.py) ─────────────
def crash_flag(c, look, thr, cool):
    ret = [None if (i < look or not c[i - look]) else c[i] / c[i - look] - 1.0 for i in range(len(c))]
    raw = [(r is not None and r < thr) for r in ret]
    return [any(raw[max(0, i - cool + 1):i + 1]) for i in range(len(c))]

def sig_buyhold(d): return [1.0] * len(d["close"])

def sig_dl_crash(d, thr=-0.10, look=10, cool=10):
    c = crash_flag(d["close"], look, thr, cool)
    return [0.0 if c[i] else 1.0 for i in range(len(c))]

def sig_dl_crashbear(d, slow=200, sw=20, bear_to=0.0):
    c = d["close"]; ma = roll_mean(c, slow); md = diff_n(ma, sw)
    cf = crash_flag(c, 10, -0.10, 10)
    out = []
    for i in range(len(c)):
        if ma[i] is None: out.append(1.0); continue
        if cf[i]: out.append(0.0); continue
        bear = (c[i] < ma[i]) and (md[i] is not None and md[i] < 0)
        out.append(bear_to if bear else 1.0)
    return out

def sig_dl_bearonly(d, slow=200, sw=20):
    c = d["close"]; ma = roll_mean(c, slow); md = diff_n(ma, sw)
    out = []
    for i in range(len(c)):
        if ma[i] is None: out.append(1.0); continue
        bear = (c[i] < ma[i]) and (md[i] is not None and md[i] < 0)
        out.append(0.0 if bear else 1.0)
    return out

def sig_dl_deepdd(d, peak=120, dd=-0.15):
    c = d["close"]; pk = roll_max(c, peak); cf = crash_flag(c, 10, -0.10, 10)
    out = []
    for i in range(len(c)):
        deep = (pk[i] is not None and c[i] / pk[i] - 1.0 < dd)
        out.append(0.0 if (cf[i] or deep) else 1.0)
    return out

def sig_dl_voltrim(d, tv=0.20, floor=0.3, win=20):
    c = d["close"]; ret = pct_change(c); rv = roll_std(ret, win); cf = crash_flag(c, 10, -0.10, 10)
    out = []
    for i in range(len(c)):
        if cf[i]: out.append(0.0); continue
        if rv[i] is None or rv[i] == 0: out.append(1.0); continue
        out.append(min(1.0, max(floor, tv / (rv[i] * SQ))))
    return out

def sig_trendon(d, slow=200, sw=20):
    c = d["close"]; ma = roll_mean(c, slow); md = diff_n(ma, sw)
    out = []
    for i in range(len(c)):
        if ma[i] is None: out.append(0.0); continue
        out.append(1.0 if (c[i] > ma[i] and md[i] is not None and md[i] > 0) else 0.0)
    return out

def sig_donchian(d, win=120):
    c = d["close"]; hh = roll_max(d["high"], win, on_shift1=True); ll = roll_min(d["low"], win, on_shift1=True)
    st = []
    for i in range(len(c)):
        if hh[i] is not None and c[i] > hh[i]: st.append(1.0)
        elif ll[i] is not None and c[i] < ll[i]: st.append(0.0)
        else: st.append(None)
    return ffill(st, 0.0)

def sig_mom(d, look=60, thr=0.0):
    c = d["close"]; out = []
    for i in range(len(c)):
        if i < look: out.append(0.0); continue
        out.append(1.0 if (c[i] / c[i - look] - 1.0) > thr else 0.0)
    return out

def sig_vol_target(d, tv=0.15, win=20, cap=1.0):
    ret = pct_change(d["close"]); rv = roll_std(ret, win); out = []
    for i in range(len(ret)):
        if rv[i] is None or rv[i] == 0: out.append(1.0)
        else: out.append(min(cap, max(0.0, tv / (rv[i] * SQ))))
    return out

def sig_trail_dd(d, peak_win=120, dd=-0.10, re_ma=60):
    c = d["close"]; pk = roll_max(c, peak_win); ma = roll_mean(c, re_ma); st = []
    for i in range(len(c)):
        on = (ma[i] is not None and c[i] > ma[i])
        flat = (pk[i] is not None and c[i] / pk[i] - 1.0 < dd)
        if on: st.append(1.0)
        elif flat: st.append(0.0)
        else: st.append(None)
    return ffill(st, 1.0)

def sig_chandelier(d, atr_win=22, k=3.0, hh_win=22, re_ma=60):
    h, lo, c = d["high"], d["low"], d["close"]
    tr = [None]
    for i in range(1, len(c)):
        tr.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))
    atr = roll_mean(tr, atr_win); hh = roll_max(h, hh_win); ma = roll_mean(c, re_ma); st = []
    for i in range(len(c)):
        on = (ma[i] is not None and c[i] > ma[i])
        flat = (hh[i] is not None and atr[i] is not None and c[i] < hh[i] - k * atr[i])
        if on: st.append(1.0)
        elif flat: st.append(0.0)
        else: st.append(None)
    return ffill(st, 1.0)

def sig_dd_ladder(d, peak_win=120, dd_half=-0.07, dd_flat=-0.15, re_ma=60):
    c = d["close"]; pk = roll_max(c, peak_win); ma = roll_mean(c, re_ma); st = []
    for i in range(len(c)):
        dd = (c[i] / pk[i] - 1.0) if pk[i] is not None else None
        on = (ma[i] is not None and c[i] > ma[i])
        flat = (dd is not None and dd < dd_flat)
        half = (dd is not None and dd < dd_half and not flat)
        if on: st.append(1.0)
        elif half: st.append(0.5)
        elif flat: st.append(0.0)
        else: st.append(None)
    return ffill(st, 1.0)

def sig_bbands(d, win=120, k=2.0):
    c = d["close"]; ma = roll_mean(c, win); sd = roll_std(c, win); st = []
    for i in range(len(c)):
        if ma[i] is not None and c[i] > ma[i]: st.append(1.0)
        elif ma[i] is not None and sd[i] is not None and c[i] < ma[i] - k * sd[i]: st.append(0.0)
        else: st.append(None)
    return ffill(st, 1.0)

GRID = [
    ("buyhold", sig_buyhold, {}),
    ("dl_crash_t10", sig_dl_crash, {}),
    ("dl_crashbear_200", sig_dl_crashbear, dict(slow=200)),
    ("dl_crashbear_half200", sig_dl_crashbear, dict(slow=200, bear_to=0.5)),
    ("dl_bearonly_200", sig_dl_bearonly, dict(slow=200)),
    ("dl_deepdd15_p120", sig_dl_deepdd, dict(peak=120, dd=-0.15)),
    ("dl_voltrim_f30", sig_dl_voltrim, dict(tv=0.20, floor=0.3)),
    ("trendon_200_20", sig_trendon, dict(slow=200)),
    ("donchian_120", sig_donchian, dict(win=120)),
    ("donchian_60", sig_donchian, dict(win=60)),
    ("mom_60", sig_mom, dict(look=60)),
    ("mom_120", sig_mom, dict(look=120)),
    ("vol_tgt_15_20", sig_vol_target, dict(tv=0.15, win=20)),
    ("trail_dd10_p120", sig_trail_dd, dict(peak_win=120, dd=-0.10, re_ma=60)),
    ("chand_22_k3", sig_chandelier, {}),
    ("dd_ladder_10_20", sig_dd_ladder, dict(dd_half=-0.10, dd_flat=-0.20)),
    ("dd_ladder_7_15", sig_dd_ladder, dict(dd_half=-0.07, dd_flat=-0.15)),
    ("bbands_break_flat", sig_bbands, dict(win=120, k=2.0)),
]


# ───────────────────────── backtest (faithful metrics) ─────────────
def backtest(d, sig, eval_start, eval_end):
    c = d["close"]; dates = d["date"]
    ret = [0.0 if x is None else x for x in pct_change(c)]
    pos = [0.0] + [(0.0 if sig[i - 1] is None else min(1.0, max(0.0, sig[i - 1]))) for i in range(1, len(c))]
    turn = [0.0] + [abs(pos[i] - pos[i - 1]) for i in range(1, len(c))]
    sret = [pos[i] * ret[i] - turn[i] * COMMISSION for i in range(len(c))]
    idx = [i for i in range(len(c)) if eval_start <= dates[i] <= eval_end]
    if not idx:
        return None
    sr = [sret[i] for i in idx]; bh = [ret[i] for i in idx]
    pe = [pos[i] for i in idx]; te = [turn[i] for i in idx]
    n = len(sr)
    def stats(r):
        eq = 1.0
        peak = 1.0; mdd = 0.0
        for x in r:
            eq *= (1.0 + x); peak = max(peak, eq); mdd = min(mdd, eq / peak - 1.0)
        tot = eq - 1.0
        ann = (1.0 + tot) ** (252.0 / n) - 1.0
        mean = sum(r) / n
        var = sum((x - mean) ** 2 for x in r) / (n - 1) if n > 1 else 0.0
        sd = math.sqrt(var)
        shp = mean / sd * SQ if sd > 0 else 0.0
        cal = ann / abs(mdd) if mdd < 0 else 0.0
        return tot, ann, shp, mdd, cal
    tot, ann, shp, mdd, cal = stats(sr)
    return {"total_return": tot, "annual_return": ann, "sharpe": shp, "max_drawdown": mdd,
            "calmar": cal, "trade_flips": sum(1 for x in te if x > 0),
            "avg_exposure": sum(pe) / n, "bars": n}

def fmt(name, m):
    if not m: return f"  {name:<22} (no data in window)"
    return (f"  {name:<22} ret={m['total_return']:+.3f} ann={m['annual_return']:+.3f} "
            f"shp={m['sharpe']:+.2f} dd={m['max_drawdown']:+.2f} cal={m['calmar']:+.2f} "
            f"exp={m['avg_exposure']:.2f} flips={m['trade_flips']}")


def run_index(label, d):
    print(f"\n══════ {label.upper()}  rows={len(d['close'])}  "
          f"{d['date'][0]}..{d['date'][-1]} ══════")
    is_rows = []
    for name, fn, kw in GRID:
        m = backtest(d, fn(d, **kw), IS_START, IS_END)
        is_rows.append((name, fn, kw, m))
    print(f"-- IS {IS_START}..{IS_END} (ranked by Calmar) --")
    ranked = sorted([r for r in is_rows if r[3]], key=lambda r: r[3]["calmar"], reverse=True)
    for name, fn, kw, m in ranked[:8]:
        print(fmt(name, m))
    bh_is = [r for r in is_rows if r[0] == "buyhold"][0][3]
    print(fmt("buyhold (bench)", bh_is))
    print(f"-- OOS {OOS_START}..{OOS_END} (IS top-8, evaluated once) --")
    for name, fn, kw, _ in ranked[:8]:
        print(fmt(name, backtest(d, fn(d, **kw), OOS_START, OOS_END)))
    print(fmt("buyhold (bench)", backtest(d, sig_buyhold(d), OOS_START, OOS_END)))


def main():
    data = {}
    for label, (secid, code) in FETCH.items():
        d = load_or_fetch(secid, code)
        data[label] = d
        print(f"fetched {label:8} {code:12} bars={len(d['close']):5} "
              f"{(d['date'][0] + '..' + d['date'][-1]) if d['close'] else '(empty)'}")
    for label in ("hs300", "zz1000", "zz500"):
        run_index(label, data[label])
    # A500: show it's un-backtestable (too short for IS/OOS)
    for label in ("a500_a", "a500_b"):
        d = data[label]
        if not d["close"]:
            print(f"\n══════ {label}: no data ══════"); continue
        is_bars = sum(1 for x in d["date"] if IS_START <= x <= IS_END)
        print(f"\n══════ {label} ({FETCH[label][1]}) rows={len(d['close'])} "
              f"{d['date'][0]}..{d['date'][-1]} ══════")
        print(f"  IS-window bars (2017..2023) = {is_bars}  → "
              f"{'CANNOT run IS/OOS split (history too short)' if is_bars < 250 else 'ok'}")
        print(fmt("buyhold (full live history)", backtest(d, sig_buyhold(d), d["date"][0], d["date"][-1])))


if __name__ == "__main__":
    main()
