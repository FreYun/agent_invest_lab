"""有色散户申赎 × 自身指数未来收益 — 结构诊断（非回测）。

每个有色子指数用「自己的」散户(个人)申赎信号预测「自己的」未来收益。
- 跨标的相关性：量化三个有色指数有多独立（高相关=跨标的检验说服力弱，要诚实标注）
- 信号 IC：cumN-z(净申赎/申请/赎回...) vs 未来 1/5/10/20 日收益 的 Spearman
  contrarian 假设成立 ⟺ z(净申赎) 与未来收益 **负相关**
- 分位：按信号 z 分 3 档，看每档未来收益

n≈20 月稳定数据，纯探索；无 IS/OOS，无多重检验矫正。只回答"有没有结构"。
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

BASE = Path("/home/rooot/agent_invest_lab/research/nonferrous_sr")
DATA = BASE / "data"

# (label, index_code, price_file)
UNIVERSE = [
    ("swnf", "000819", "swnf_000819.csv"),   # 申万有色（主力，对应 512400 ETF）
    ("csnf", "930708", "csnf_930708.csv"),   # 中证有色金属
    ("gznf", "399395", "gznf_399395.csv"),   # 国证有色
]
SOURCES = ["net", "net_ex_aip", "applied", "applied_ex_aip", "redeemed"]
FWD = [1, 5, 10, 20]
CUMN_REF, ZWIN_REF = 15, 90   # 参考参数（对齐已生产的 market_retail_contrarian_15_90）


def load_close(fn: str) -> pd.Series:
    df = pd.read_csv(DATA / fn, parse_dates=["date"]).set_index("date").sort_index()
    s = df["close"].astype(float)
    s.index = pd.to_datetime(s.index).normalize()
    return s


def load_sr() -> pd.DataFrame:
    # index_code 必须按字符串读，否则 000819 被解析成整数 819 → 匹配不上
    return pd.read_csv(BASE / "sr_daily.csv", parse_dates=["date"], dtype={"index_code": str})


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """rank 后算 pearson = spearman，免 scipy 依赖。"""
    return float(a.rank().corr(b.rank()))


def sr_series(sr: pd.DataFrame, code: str, col: str) -> pd.Series:
    sub = sr[(sr["index_code"].astype(str) == code) & (sr["persona"] == "个人")]
    s = sub.set_index("date")[col].astype(float).sort_index()
    s.index = pd.to_datetime(s.index).normalize()
    return s[~s.index.duplicated(keep="last")]


def cumN_z(s: pd.Series, n: int, z_win: int) -> pd.Series:
    cum = s.rolling(n, min_periods=max(3, n // 2)).sum()
    mu = cum.rolling(z_win, min_periods=20).mean()
    sd = cum.rolling(z_win, min_periods=20).std()
    return (cum - mu) / sd


def main():
    sr = load_sr()
    closes = {lab: load_close(fn) for lab, code, fn in UNIVERSE}

    # ── 跨标的相关性（日收益）──
    rets = pd.DataFrame({lab: closes[lab].pct_change() for lab, _, _ in UNIVERSE}).dropna()
    print("══ 三个有色指数 日收益相关性（量化跨标的独立性）══")
    print(rets.corr().to_string(float_format=lambda x: f"{x:.3f}"))
    print("  → 相关性越高，'跨3标的稳健'的说服力越弱（同一板块）\n")

    # ── 每个指数：自身散户信号 vs 自身未来收益 ──
    for lab, code, fn in UNIVERSE:
        close = closes[lab]
        ret_fwd = {f"fwd{h}": close.shift(-h) / close - 1.0 for h in FWD}
        fwd_df = pd.DataFrame(ret_fwd)
        print(f"══════ {lab.upper()} ({code})  价格 bars={len(close)} ══════")
        rows = []
        for src in SOURCES:
            raw = sr_series(sr, code, src)
            if raw.empty:
                continue
            z = cumN_z(raw, CUMN_REF, ZWIN_REF).reindex(close.index, method="ffill")
            j = pd.concat([z.rename("z"), fwd_df], axis=1).dropna(subset=["z"])
            row = {"src": src, "n": int(j["z"].notna().sum())}
            for h in FWD:
                sub = j[["z", f"fwd{h}"]].dropna()
                row[f"sp{h}"] = _spearman(sub["z"], sub[f"fwd{h}"]) if len(sub) >= 30 else None
            rows.append(row)
        ct = pd.DataFrame(rows)
        print("  cumN-z Spearman IC vs 未来收益 (contrarian ⟺ 负):")
        print("  " + ct.to_string(index=False, float_format=lambda x: f"{x:+.3f}" if pd.notna(x) else " - ").replace("\n", "\n  "))

        # 分位（3 档）用 net 源
        z_net = cumN_z(sr_series(sr, code, "net"), CUMN_REF, ZWIN_REF).reindex(close.index, method="ffill")
        for h in [5, 10, 20]:
            sub = pd.concat([z_net.rename("z"), fwd_df[f"fwd{h}"]], axis=1).dropna()
            if len(sub) < 60:
                continue
            sub = sub.copy()
            sub["bin"] = pd.qcut(sub["z"], 3, labels=["Q1低(散户冷)", "Q2", "Q3高(散户热)"], duplicates="drop")
            g = sub.groupby("bin", observed=True)[f"fwd{h}"].agg(["mean", "count"])
            print(f"\n  net z 分3档 → 未来{h}日收益均值 (contrarian ⟺ Q1>Q3):")
            print("    " + g.to_string(float_format=lambda x: f"{x:+.4f}").replace("\n", "\n    "))
        print()


if __name__ == "__main__":
    main()
