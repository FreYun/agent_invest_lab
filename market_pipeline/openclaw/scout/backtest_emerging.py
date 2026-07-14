"""萌芽信号离线探索/验证 — 不写生产表.

靶标"持续强势M日": 报警日后 horizon 个交易日, 板块相对大盘(全A等权)日超额 >0
天数占比 >= min_ratio. 持续跑赢而非冲一天, 排除一日游.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def is_sustained(board_rets, mkt_rets, horizon=10, min_ratio=0.6):
    """board_rets/mkt_rets: 未来日收益%(等长). 不足 horizon → None."""
    n = min(len(board_rets), len(mkt_rets))
    if n < horizon:
        return None
    pos = sum(1 for i in range(horizon) if board_rets[i] - mkt_rets[i] > 0)
    return (pos / horizon) >= min_ratio


import argparse, sqlite3, statistics
from collections import defaultdict
import emerging_signals as es

DB = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))


def _load(conn):
    cal = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM concept_board_daily ORDER BY trade_date")]
    bpct = defaultdict(dict)
    for bc, d, p in conn.execute(
            "SELECT board_code,trade_date,pct_change FROM concept_board_daily"):
        if p is not None:
            bpct[bc][d] = p
    mkt = {d: r for d, r in conn.execute(
        "SELECT trade_date, AVG(pct_chg) FROM daily WHERE pct_chg IS NOT NULL GROUP BY trade_date")}
    return cal, bpct, mkt


def _fwd(series_map, bc, cal, idxof, d, h):
    i = idxof[d]
    return [series_map[bc].get(cal[j]) for j in range(i + 1, i + 1 + h)]


def _subperiod(d):
    if d < "20240701": return "2024H1"
    if d < "20250101": return "2024H2"
    if d < "20260101": return "2025"
    return "2026"


def run(start="20240101", horizon=10, min_ratio=0.6):
    conn = sqlite3.connect(DB)
    cal, bpct, mkt = _load(conn)
    idxof = {d: i for i, d in enumerate(cal)}
    days = [d for d in cal if d >= start and idxof[d] + horizon < len(cal)]

    regime = {}
    try:
        for d, r in conn.execute("SELECT trade_date, regime FROM regime_daily"):
            regime[d] = r
    except sqlite3.OperationalError:
        pass

    persig = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    base = defaultdict(lambda: [0, 0])
    configs = [(2, 2), (2, 3), (3, 2), (3, 3)]
    fire_hist = defaultdict(dict)
    wf = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    for d in days:
        sig = es.compute_core_signals(conn, d)
        if not sig:
            continue
        sub = _subperiod(d)
        for bc, r in sig.items():
            fwd_b = _fwd(bpct, bc, cal, idxof, d, horizon)
            fwd_m = [mkt.get(cal[j]) for j in range(idxof[d] + 1, idxof[d] + 1 + horizon)]
            if any(x is None for x in fwd_b) or any(x is None for x in fwd_m):
                continue
            sustained = is_sustained(fwd_b, fwd_m, horizon, min_ratio)
            if sustained is None:
                continue
            base[sub][1] += 1
            if sustained:
                base[sub][0] += 1
            for s in es.CORE_SIGNALS:
                if s in r["fired"]:
                    persig[sub][s][1] += 1
                    if sustained:
                        persig[sub][s][0] += 1
            fire_hist[bc][d] = r["n_signals"]

    print("\n=== 单信号 lift (P(持续强势|点头) / 基础概率), 分时段 ===")
    for sub in ["2024H1", "2024H2", "2025", "2026"]:
        b = base[sub]
        if b[1] == 0:
            continue
        base_p = b[0] / b[1]
        print(f"\n[{sub}] 基础概率={base_p:.1%} (n={b[1]})")
        for s in es.CORE_SIGNALS:
            f = persig[sub][s]
            if f[1] == 0:
                continue
            p = f[0] / f[1]
            print(f"  {s:<12} 点头后={p:.1%} lift={p/base_p:.2f} (n={f[1]})")

    def consecutive_alert(bc, d, K, D):
        i = idxof[d]
        for back in range(D):
            dd = cal[i - back] if i - back >= 0 else None
            if dd is None or fire_hist[bc].get(dd, 0) < K:
                return False
        return True

    for d in days:
        split = "train(24-25)" if d < "20260101" else "test(26)"
        for bc in list(fire_hist.keys()):
            if d not in fire_hist[bc]:
                continue
            fwd_b = _fwd(bpct, bc, cal, idxof, d, horizon)
            fwd_m = [mkt.get(cal[j]) for j in range(idxof[d] + 1, idxof[d] + 1 + horizon)]
            if any(x is None for x in fwd_b) or any(x is None for x in fwd_m):
                continue
            sustained = is_sustained(fwd_b, fwd_m, horizon, min_ratio)
            if sustained is None:
                continue
            for (K, D) in configs:
                if consecutive_alert(bc, d, K, D):
                    wf[split][(K, D)][1] += 1
                    if sustained:
                        wf[split][(K, D)][0] += 1

    print("\n=== walk-forward N选K+持续D: 样本外精度 & 频次 ===")
    for split in ["train(24-25)", "test(26)"]:
        print(f"\n[{split}]")
        for (K, D) in configs:
            a = wf[split][(K, D)]
            if a[1] == 0:
                print(f"  K={K} D={D}: 无报警")
                continue
            print(f"  K={K} D={D}: 精度={a[0]/a[1]:.1%}  报警数={a[1]}")

    if regime:
        byreg = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for d in days:
            for bc in list(fire_hist.keys()):
                if d not in fire_hist[bc]:
                    continue
                fwd_b = _fwd(bpct, bc, cal, idxof, d, horizon)
                fwd_m = [mkt.get(cal[j]) for j in range(idxof[d] + 1, idxof[d] + 1 + horizon)]
                if any(x is None for x in fwd_b) or any(x is None for x in fwd_m):
                    continue
                sustained = is_sustained(fwd_b, fwd_m, horizon, min_ratio)
                if sustained is None:
                    continue
                reg = regime.get(d, "?")
                for (K, D) in configs:
                    if consecutive_alert(bc, d, K, D):
                        byreg[(K, D)][reg][1] += 1
                        if sustained:
                            byreg[(K, D)][reg][0] += 1
        print("\n=== 分 regime 精度(各 K/D) ===")
        for (K, D) in configs:
            parts = []
            for reg, a in sorted(byreg[(K, D)].items()):
                if a[1]:
                    parts.append(f"{reg}:{a[0]/a[1]:.0%}(n={a[1]})")
            if parts:
                print(f"  K={K} D={D}: " + "  ".join(parts))
    else:
        print("\n(无 regime_daily 表, 跳过分 regime)")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20240101")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--min-ratio", type=float, default=0.6)
    a = ap.parse_args()
    run(a.start, a.horizon, a.min_ratio)


if __name__ == "__main__":
    main()
