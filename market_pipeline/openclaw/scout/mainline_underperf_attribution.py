"""主线 Top10 长周期跑输归因.

两条归因线 (都用"能凑满250日窗口"的同一批样本, 保证可比):
  1. 路径分段: 选后 [1-60] / [61-120] / [121-250] 三段, 每段算板块累计/大盘累计/超额,
     看跑输到底发生在前段(还在冲)还是后段(见顶回落).
  2. 饱和度分位: 按选时 距MA20% / 近20日涨幅 / 趋势分 分5档(quintile),
     看"买得越饱和(越高)长周期超额是否越差" —— 验证 买在高位 假设.

口径同 export_mainline_report.py: 板块 concept_board_daily.pct_change, 大盘=全A等权(daily.pct_chg).

用法: /usr/bin/python3.12 mainline_underperf_attribution.py [--top 10]
"""
from __future__ import annotations
import argparse, sqlite3, statistics
from collections import defaultdict

DB = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))


def cum(pcts):
    if not pcts:
        return None
    v = 1.0
    for p in pcts:
        v *= (1 + p / 100)
    return (v - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--start", default="20240101")
    a = ap.parse_args()
    c = sqlite3.connect(DB)

    cal = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily ORDER BY trade_date")]
    idxof = {d: i for i, d in enumerate(cal)}

    bpct = defaultdict(dict)
    for bc, d, p in c.execute(
            "SELECT board_code, trade_date, pct_change FROM concept_board_daily"):
        if p is not None:
            bpct[bc][d] = p
    mkt = {d: r for d, r in c.execute(
        "SELECT trade_date, AVG(pct_chg) FROM daily WHERE pct_chg IS NOT NULL GROUP BY trade_date")}

    # 主线选中 + 选时强度
    sel = []  # (date, rank, code, score, ret20, ret5, dist_ma20)
    for d, rk, bc, sc, r20, r5, dm in c.execute(
            "SELECT trade_date, rank, board_code, trend_score, ret20, ret5, dist_ma20 "
            "FROM board_trend_daily WHERE rank<=? ORDER BY trade_date, rank", (a.top,)):
        if d >= a.start:
            sel.append((d, rk, bc, sc, r20, r5, dm))

    def seg_ret(src_get, d, lo, hi):
        """src_get(x)->pct; 第 lo..hi 个交易日(含)累计%; 窗口不足返回 None."""
        i = idxof.get(d)
        if i is None:
            return None
        win = cal[i + lo:i + hi + 1]
        if len(win) < (hi - lo + 1):
            return None
        ps = [src_get(x) for x in win if src_get(x) is not None]
        return cum(ps) if ps else None

    # 只保留能凑满 250 日的样本
    SEGS = [("1-60日", 1, 60), ("61-120日", 61, 120), ("121-250日", 121, 250)]
    samples = []  # dict per board-day
    for d, rk, bc, sc, r20, r5, dm in sel:
        full_b = seg_ret(lambda x: bpct.get(bc, {}).get(x), d, 1, 250)
        full_m = seg_ret(lambda x: mkt.get(x), d, 1, 250)
        if full_b is None or full_m is None:
            continue
        rec = {"date": d, "rank": rk, "score": sc, "ret20": r20, "ret5": r5, "dist_ma20": dm,
               "full_ex": full_b - full_m, "segs": {}}
        for label, lo, hi in SEGS:
            sb = seg_ret(lambda x: bpct.get(bc, {}).get(x), d, lo, hi)
            sm = seg_ret(lambda x: mkt.get(x), d, lo, hi)
            rec["segs"][label] = (sb, sm, (sb - sm) if (sb is not None and sm is not None) else None)
        samples.append(rec)

    print(f"=== 主线 Top{a.top} 长周期跑输归因 ===")
    print(f"满250日窗口样本 n={len(samples)} (选中日 {samples[0]['date']}~{samples[-1]['date']})")
    print(f"全程250日超额 均值 {statistics.mean(s['full_ex'] for s in samples):+.2f}% "
          f"中位 {statistics.median(s['full_ex'] for s in samples):+.2f}% "
          f"超额胜率 {sum(1 for s in samples if s['full_ex']>0)/len(samples)*100:.1f}%\n")

    # ── 归因1: 路径分段 ──
    print("── 归因1: 跑输发生在哪一段 (各段 板块累计 vs 大盘累计) ──")
    print(f"{'区段':<10}{'板块均值':>9}{'大盘均值':>9}{'段超额均值':>11}{'段超额中位':>11}{'段超额胜率':>11}")
    for label, lo, hi in SEGS:
        sb = [s['segs'][label][0] for s in samples if s['segs'][label][0] is not None]
        sm = [s['segs'][label][1] for s in samples if s['segs'][label][1] is not None]
        ex = [s['segs'][label][2] for s in samples if s['segs'][label][2] is not None]
        wr = sum(1 for e in ex if e > 0) / len(ex) * 100
        print(f"{label:<11}{statistics.mean(sb):>+8.2f}%{statistics.mean(sm):>+8.2f}%"
              f"{statistics.mean(ex):>+10.2f}%{statistics.median(ex):>+10.2f}%{wr:>10.1f}%")
    print()

    # ── 归因2: 饱和度分位 ──
    def quintile_attr(field, fname):
        vals = [(s[field], s['full_ex']) for s in samples if s[field] is not None]
        vals.sort(key=lambda x: x[0])
        n = len(vals)
        print(f"── 归因2-{fname}: 按选时{fname}分5档 → 全程250日超额 (越高档=选时越饱和) ──")
        print(f"{'档位':<8}{'样本':>6}{fname+'区间':>20}{'250日超额均值':>14}{'超额中位':>10}{'超额胜率':>10}")
        for q in range(5):
            lo_i = q * n // 5
            hi_i = (q + 1) * n // 5
            chunk = vals[lo_i:hi_i]
            exs = [x[1] for x in chunk]
            wr = sum(1 for e in exs if e > 0) / len(exs) * 100
            rng = f"[{chunk[0][0]:.1f},{chunk[-1][0]:.1f}]"
            print(f"Q{q+1}{'(最低)' if q==0 else '(最高)' if q==4 else '':<5}{len(chunk):>6}{rng:>20}"
                  f"{statistics.mean(exs):>+13.2f}%{statistics.median(exs):>+9.2f}%{wr:>9.1f}%")
        print()

    quintile_attr("dist_ma20", "距MA20%")
    quintile_attr("ret20", "近20日涨幅%")
    quintile_attr("score", "趋势分")


if __name__ == "__main__":
    main()
