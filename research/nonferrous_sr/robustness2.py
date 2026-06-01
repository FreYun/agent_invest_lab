"""纯散户·补充构造：申赎比 + 三指数加总降噪。聚焦可交易的 512400 ETF。

上一轮(robustness.py)用绝对额 net/applied/redeemed 的 cumN-z，结论无效。
这轮补两类上一轮没覆盖的散户构造：
  A) 单指数(000819)散户 归一化强度：net_intensity=cum(net)/cum(申请+赎回)、
     redeem_pressure=cum(赎回)/cum(申请+赎回)  ← 剔除规模漂移
  B) 三有色指数散户【加总】(降噪)：net/applied 加总后再 cumN-z
评估固化教训：只认"真在交易"的有效配置(暴露 0.3~0.9 且 年翻转≥4)，
杜绝空仓撞运气的假 Calmar。
"""
from __future__ import annotations
from pathlib import Path
from itertools import product
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/nonferrous_sr")
DATA = BASE / "data"
COMMISSION = 0.0005
WARMUP = 60
FWD = [1, 5, 10, 20]
CUM_NS = [10, 15, 20]
Z_WINS = [60, 90]
THRESHOLDS = [0.5, 1.0]


def load_close(fn):
    df = pd.read_csv(DATA / fn, parse_dates=["date"]).set_index("date").sort_index()
    s = df["close"].astype(float); s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr():
    return pd.read_csv(BASE / "sr_daily.csv", parse_dates=["date"], dtype={"index_code": str})


def retail(sr, code):
    """单指数散户(个人) applied/redeemed/net 三列，按日去重。"""
    sub = sr[(sr["index_code"] == code) & (sr["persona"] == "个人")].set_index("date").sort_index()
    sub = sub[~sub.index.duplicated(keep="last")]
    sub.index = pd.to_datetime(sub.index).normalize()
    return sub[["applied", "redeemed", "net"]].astype(float)


def retail_sum(sr, codes):
    """三指数散户加总(降噪)。"""
    acc = None
    for c in codes:
        r = retail(sr, c)
        acc = r if acc is None else acc.add(r, fill_value=0.0)
    return acc


def zscore(s, z_win):
    mu = s.rolling(z_win, min_periods=20).mean()
    sd = s.rolling(z_win, min_periods=20).std()
    return (s - mu) / sd


def level_signals(r, cumN):
    """从 applied/redeemed/net 派生归一化 level 信号(已剔规模)。"""
    ca = r["applied"].rolling(cumN, min_periods=max(3, cumN // 2)).sum()
    cr = r["redeemed"].rolling(cumN, min_periods=max(3, cumN // 2)).sum()
    cn = r["net"].rolling(cumN, min_periods=max(3, cumN // 2)).sum()
    gross = (ca + cr).replace(0.0, np.nan)
    return {
        "net_intensity": cn / gross,           # 散户净买强度 ∈[-1,1]，高=净买狂热
        "redeem_pressure": cr / gross,         # 赎回压力 ∈[0,1]，高=恐慌
        "net_abs_cum": cn,                     # 加总绝对额(给加总源用)
        "applied_abs_cum": ca,
    }


def make_pos(close, level, z_win, thr, direction):
    z = zscore(level, z_win).reindex(close.index, method="ffill")
    hi, lo = (0.0, 1.0) if direction == "contra" else (1.0, 0.0)
    state = pd.Series(np.nan, index=close.index)
    state = state.where(~(z > thr), hi)
    state = state.where(~(z < -thr), lo)
    return state.ffill().fillna(0.5)


def backtest(close, sig):
    sig = sig.clip(0, 1).fillna(0).reindex(close.index).fillna(0)
    ret = close.pct_change().fillna(0)
    pos = sig.shift(1).fillna(0)
    turn = pos.diff().abs().fillna(0)
    strat = pos * ret - turn * COMMISSION
    s = strat.iloc[WARMUP:]; bh = ret.iloc[WARMUP:]; pe = pos.iloc[WARMUP:]
    n = len(s); yrs = n / 252
    def st(r):
        eq = (1 + r).cumprod(); tot = float(eq.iloc[-1] - 1)
        ann = float((1 + tot) ** (252 / max(n, 1)) - 1)
        sd = float(r.std(ddof=1)) or 0.0
        shp = float(r.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
        dd = float((eq / eq.cummax() - 1).min()); cal = float(ann / abs(dd)) if dd < 0 else 0.0
        return shp, cal
    shp, cal = st(s); bshp, bcal = st(bh)
    return {"shp": shp, "cal": cal, "dcal": cal - bcal, "dshp": shp - bshp,
            "exp": float(pe.mean()), "flips_yr": int((turn.iloc[WARMUP:] > 0.001).sum()) / yrs}


def ic(level, close):
    z = zscore(level, 90).reindex(close.index, method="ffill")
    fwd = {h: close.shift(-h) / close - 1 for h in FWD}
    out = {}
    for h in FWD:
        sub = pd.concat([z.rename("z"), fwd[h].rename("f")], axis=1).dropna()
        out[h] = float(sub["z"].rank().corr(sub["f"].rank())) if len(sub) >= 30 else None
    return out


def main():
    sr = load_sr()
    swnf = load_close("swnf_000819.csv")
    etf = load_close("etf_512400.csv")
    r819 = retail(sr, "000819")
    rsum = retail_sum(sr, ["000819", "930708", "399395"])

    bh = backtest(etf, pd.Series(1.0, index=etf.index))
    print(f"512400 ETF buyhold: cal={bh['cal']:+.2f} shp={bh['shp']:+.2f}\n")

    # ── 信号源集合 ──
    SRC = {
        "819_net_intensity":   ("net_intensity", r819),
        "819_redeem_pressure": ("redeem_pressure", r819),
        "SUM_net_intensity":   ("net_intensity", rsum),
        "SUM_redeem_pressure": ("redeem_pressure", rsum),
        "SUM_net_abs":         ("net_abs_cum", rsum),
        "SUM_applied_abs":     ("applied_abs_cum", rsum),
    }

    # ── IC(对可交易 512400)──
    print("══ 新散户信号 IC vs 512400 未来收益 (contrarian⟺负, cumN15/zw90) ══")
    for name, (key, r) in SRC.items():
        lv = level_signals(r, 15)[key]
        d = ic(lv, etf)
        print(f"  {name:>20}: fwd1={d[1]:+.3f} fwd5={d[5]:+.3f} fwd10={d[10]:+.3f} fwd20={d[20]:+.3f}")

    # ── ETF contrarian sweep，只认有效配置 ──
    print(f"\n══ 512400 ETF contrarian sweep ({len(CUM_NS)}cumN×{len(Z_WINS)}zw×{len(THRESHOLDS)}thr/源) ══")
    print("   有效配置 = 暴露∈[0.3,0.9] 且 年翻转≥4 (排除空仓撞运气/满仓=buyhold)")
    rows = []
    for name, (key, r) in SRC.items():
        for cumN, z_win, thr in product(CUM_NS, Z_WINS, THRESHOLDS):
            lv = level_signals(r, cumN)[key]
            for d in ("contra", "mom"):
                m = backtest(etf, make_pos(etf, lv, z_win, thr, d))
                rows.append({"src": name, "dir": d, "cumN": cumN, "z_win": z_win, "thr": thr, **m})
    df = pd.DataFrame(rows)
    df["valid"] = ((df.exp >= 0.3) & (df.exp <= 0.9) & (df.flips_yr >= 4)).astype(int)
    df.to_csv(BASE / "robustness2_full.csv", index=False)

    for d in ("contra", "mom"):
        sub = df[(df.dir == d) & (df.valid == 1)]
        n = len(sub)
        beat = int((sub.dcal > 0).sum())
        print(f"\n  方向={d}: 有效配置 {n} 个，其中击败 buyhold Calmar {beat} 个 = {beat/n:.0%}" if n else f"\n  方向={d}: 无有效配置")
        if n:
            print(f"    有效配置 median dCalmar={sub.dcal.median():+.2f}  mean dSharpe={sub.dshp.mean():+.2f}")
    print("\n  -- contra 有效配置 top5 (按 dCalmar) --")
    top = df[(df.dir == "contra") & (df.valid == 1)].sort_values("dcal", ascending=False).head(5)
    if len(top):
        print(top[["src", "cumN", "z_win", "thr", "cal", "dcal", "shp", "dshp", "exp", "flips_yr"]].to_string(
            index=False, float_format=lambda x: f"{x:+.2f}"))
    else:
        print("    (无)")


if __name__ == "__main__":
    main()
