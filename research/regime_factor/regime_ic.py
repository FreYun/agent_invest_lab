"""regime_classify_daily.total_score timing validity for the 50/50 HS300+ZZ1000 combo.

Benchmark = daily-rebalanced equal weight: combo_ret = 0.5*r(000300) + 0.5*r(000852).
Factor = total_score (v2). Higher score => more bullish => expect higher fwd combo return.

PIT/causal: score_T uses same-day trailing market breadth (known at T close) -> predict
T+1 onward. IC vs fwd Hd combo return; significance on NON-overlapping samples (every Hth).
"""
from pathlib import Path
import sqlite3, numpy as np, pandas as pd

DB = "/home/rooot/database/market.db"
IS_END = pd.Timestamp("2022-12-31")   # ~8.5y IS (2014-2022), ~3.4y OOS (2023-2026)
HZ = [5, 10, 20]
rng = np.random.default_rng(17)


def load():
    c = sqlite3.connect(DB)
    reg = pd.read_sql("SELECT trade_date,total_score,score_ma_position,score_advance_decline,"
                      "score_sentiment_delta,score_sentiment_index,score_streak_height,score_volume_trend "
                      "FROM regime_classify_daily WHERE rules_version='v2'", c)
    reg["date"] = pd.to_datetime(reg["trade_date"]); reg = reg.set_index("date").sort_index().drop(columns="trade_date")
    idx = pd.read_sql("SELECT trade_date,ts_code,close FROM index_daily WHERE ts_code IN ('000300.SH','000852.SH')", c)
    c.close()
    idx["date"] = pd.to_datetime(idx["trade_date"], format="%Y%m%d")
    piv = idx.pivot(index="date", columns="ts_code", values="close").sort_index()
    r300 = piv["000300.SH"].pct_change(); r852 = piv["000852.SH"].pct_change()
    combo = (0.5 * r300 + 0.5 * r852)
    return reg, combo


def ic_np(x, y):
    s = pd.concat([x, y], axis=1).dropna()
    if len(s) < 20: return np.nan, 0
    return s.iloc[:, 0].corr(s.iloc[:, 1], method="spearman"), len(s)


def main():
    reg, combo = load()
    print("regime v2:", reg.index[0].date(), "->", reg.index[-1].date(), "n=", len(reg))
    print("combo ret:", combo.dropna().index[0].date(), "->", combo.dropna().index[-1].date())
    feats = ["total_score","score_ma_position","score_advance_decline","score_sentiment_delta",
             "score_sentiment_index","score_streak_height","score_volume_trend"]
    # align
    df = reg.copy()
    for h in HZ:
        fwd = (1+combo).rolling(h).apply(np.prod, raw=True).shift(-h) - 1   # fwd Hd combo return from T (using T+1..T+h)
        # simpler: cumulative fwd return close_T->T+h on combo
        df[f"fwd{h}"] = (combo.shift(-1).fillna(0)+1).rolling(h).apply(np.prod,raw=True).reindex(df.index)  # placeholder
    # recompute fwd cleanly
    cr = combo.fillna(0.0)
    eqindex = (1+cr).cumprod()
    for h in HZ:
        fwd = eqindex.shift(-h)/eqindex - 1.0   # T -> T+h cumulative combo return
        df[f"fwd{h}"] = fwd.reindex(df.index)

    print("\n=== contemporaneous sanity: corr(score_T, SAME-day combo ret) (should be high=uses same-day breadth) ===")
    same = combo.reindex(df.index)
    print("  corr(total_score, same-day ret):", round(df["total_score"].corr(same, method="spearman"),3))

    for split,lo,hi in [("ALL",pd.Timestamp("2000-1-1"),pd.Timestamp("2100-1-1")),
                        ("IS",pd.Timestamp("2000-1-1"),IS_END),
                        ("OOS",IS_END,pd.Timestamp("2100-1-1"))]:
        sub = df[(df.index>lo)&(df.index<=hi)]
        print(f"\n========= {split}  Spearman IC vs fwd combo return (full-sample + non-overlap p) =========")
        print(f"  {'feature':<24}" + "".join(f"{'fwd'+str(h):>10}{'(p)':>7}" for h in HZ))
        for f in feats:
            row=f"  {f:<24}"
            for h in HZ:
                ic,n = ic_np(sub[f], sub[f"fwd{h}"])
                # non-overlap p
                s2 = pd.concat([sub[f],sub[f"fwd{h}"]],axis=1).dropna().iloc[::h]
                if len(s2)>=10:
                    icn=s2.iloc[:,0].corr(s2.iloc[:,1],method="spearman")
                    null=[s2.iloc[:,0].corr(pd.Series(rng.permutation(s2.iloc[:,1].values),index=s2.index),method="spearman") for _ in range(2000)]
                    p=(np.array(null)>=icn).mean()
                else: icn,p=np.nan,np.nan
                row+=f"{ic:>+10.3f}{p:>7.3f}"
            print(row)


if __name__=="__main__":
    main()
