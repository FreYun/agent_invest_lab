"""scout 主线 Top10 选后 60/120/250 日累计收益 + 超额统计.

口径与 export_mainline_report.py 完全一致 (board-day 样本前瞻):
  - 选中样本 = board_trend_daily 每日 rank<=top 的 (交易日, 板块).
  - 选后累计 = 板块 concept_board_daily.pct_change 从被选当日 t+1 起累乘 N 个交易日.
  - 大盘基准 = 全A等权日收益 (daily.pct_chg 等权平均) 同窗累乘.
  - 超额 = 板块累计 − 大盘累计.
  - 窗口不足 N 个交易日的样本丢弃 (所以 250 日只覆盖 <=2025-05 选中的样本).

聚合: 整体 + 分年(按选中日) + 全周期/近一年, 给 均值/中位/胜率(超额>0占比)/样本数.

用法: /usr/bin/python3.12 mainline_longterm_stats.py [--start 20240101] [--top 10]
"""
from __future__ import annotations
import argparse, os, sqlite3, statistics, csv
from collections import defaultdict

DB = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mainline_report")
WINDOWS = [60, 120, 250]


def cum(pcts):
    if not pcts:
        return None
    v = 1.0
    for p in pcts:
        v *= (1 + p / 100)
    return (v - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20240101")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
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

    ml = defaultdict(list)  # date -> [(rank, name, code, score, ret20, ret5, dist_ma20)]
    for d, rk, bn, bc, sc, r20, r5, dm in c.execute(
            "SELECT trade_date, rank, board_name, board_code, trend_score, ret20, ret5, dist_ma20 "
            "FROM board_trend_daily WHERE rank<=? ORDER BY trade_date, rank", (a.top,)):
        if d >= a.start:
            ml[d].append((rk, bn, bc, sc, r20, r5, dm))
    days = sorted(ml.keys())

    def fwd_board(bc, d, w):
        i = idxof.get(d)
        if i is None:
            return None
        win = cal[i + 1:i + 1 + w]
        if len(win) < w:
            return None
        ps = [bpct[bc][x] for x in win if x in bpct.get(bc, {})]
        return cum(ps) if ps else None

    def fwd_mkt(d, w):
        i = idxof.get(d)
        win = cal[i + 1:i + 1 + w]
        if len(win) < w:
            return None
        ps = [mkt[x] for x in win if x in mkt]
        return cum(ps) if ps else None

    # 逐样本算各窗口
    rows = []  # (date, rank, name, code, score, ret20, ret5, dist_ma20, {w: (board, mkt, excess)})
    mkt_cache = {}
    for d in days:
        for rk, bn, bc, sc, r20, r5, dm in ml[d]:
            rec = {}
            for w in WINDOWS:
                fb = fwd_board(bc, d, w)
                key = (d, w)
                if key not in mkt_cache:
                    mkt_cache[key] = fwd_mkt(d, w)
                fm = mkt_cache[key]
                ex = (fb - fm) if (fb is not None and fm is not None) else None
                rec[w] = (fb, fm, ex)
            rows.append((d, rk, bn, bc, sc, r20, r5, dm, rec))

    def agg(samples):
        """samples = list[(board, mkt, excess)]; 返回汇总 dict."""
        bs = [s[0] for s in samples if s[0] is not None]
        exs = [s[2] for s in samples if s[2] is not None]
        ms = [s[1] for s in samples if s[1] is not None]
        if not bs:
            return None
        win_rate = sum(1 for e in exs if e > 0) / len(exs) * 100 if exs else None
        return {
            "n": len(bs),
            "board_mean": statistics.mean(bs),
            "board_med": statistics.median(bs),
            "mkt_mean": statistics.mean(ms) if ms else None,
            "ex_mean": statistics.mean(exs) if exs else None,
            "ex_med": statistics.median(exs) if exs else None,
            "win": win_rate,
        }

    # 行索引: 0=date 1=rank 2=name 3=code 4=score 5=ret20 6=ret5 7=dist_ma20 8=rec
    def bucket(filt):
        out = {}
        for w in WINDOWS:
            samples = [r[8][w] for r in rows if filt(r)]
            out[w] = agg(samples)
        return out

    overall = bucket(lambda r: True)
    last_year = bucket(lambda r: r[0] >= "20250701")
    by_year = {}
    for y in ["2024", "2025", "2026"]:
        by_year[y] = bucket(lambda r, y=y: r[0][:4] == y)

    # 按选时排名分桶 (验证 "排名越靠前长周期反而越差")
    rank_buckets = {
        "rank 1-3": lambda r: r[1] <= 3,
        "rank 4-6": lambda r: 4 <= r[1] <= 6,
        "rank 7-10": lambda r: r[1] >= 7,
    }
    by_rank = {k: bucket(v) for k, v in rank_buckets.items()}

    # ── 打印 ──
    def fmt(s):
        if not s:
            return "  (样本不足)"
        return (f"  n={s['n']:>4}  板块累计 均值{s['board_mean']:+6.2f}% 中位{s['board_med']:+6.2f}%  "
                f"大盘 {s['mkt_mean']:+6.2f}%  超额 均值{s['ex_mean']:+6.2f}% 中位{s['ex_med']:+6.2f}%  "
                f"超额胜率 {s['win']:4.1f}%")

    lines = []
    lines.append(f"=== scout 主线 Top{a.top} 选后长周期累计收益 + 超额 ===")
    lines.append(f"区间 {days[0]}~{days[-1]}, 共 {len(days)} 个选中交易日; 板块行情至 {cal[-1]}")
    lines.append("口径: board-day 样本前瞻累乘, 超额=板块−全A等权; 窗口不足则丢弃\n")
    for w in WINDOWS:
        lines.append(f"── 选后 {w} 日 ──")
        lines.append("全周期 " + fmt(overall[w]))
        lines.append("近一年 " + fmt(last_year[w]) + "  (20250701起选中)")
        for y in ["2024", "2025", "2026"]:
            lines.append(f"  {y}年 " + fmt(by_year[y][w]))
        lines.append("  -- 按选时排名分桶 --")
        for k in rank_buckets:
            lines.append(f"  {k:<9}" + fmt(by_rank[k][w]))
        lines.append("")
    out = "\n".join(lines)
    print(out)

    # ── 落明细 CSV ──
    detail = os.path.join(OUT, "mainline_longterm_detail.csv")
    with open(detail, "w", newline="") as f:
        wr = csv.writer(f)
        hdr = ["交易日", "排名", "板块", "板块代码", "趋势分", "选时近20日%", "选时近5日%", "选时距MA20%"]
        for w in WINDOWS:
            hdr += [f"选后{w}日%", f"大盘{w}日%", f"超额{w}日"]
        wr.writerow(hdr)
        rd = lambda x: round(x, 2) if x is not None else None
        for d, rk, bn, bc, sc, r20, r5, dm, rec in rows:
            row = [d, rk, bn, bc, sc, rd(r20), rd(r5), rd(dm)]
            for w in WINDOWS:
                fb, fm, ex = rec[w]
                row += [rd(fb), rd(fm), rd(ex)]
            wr.writerow(row)

    # ── 落汇总 md ──
    md = ["# scout 主线 Top10 选后 60/120/250 日累计收益 + 超额\n",
          f"> 区间 {days[0]}~{days[-1]}, 板块行情至 {cal[-1]}; board-day 样本前瞻累乘, 超额=板块−全A等权\n",
          "> ⚠️ 窗口不足则丢弃: 250日仅覆盖 ≤2025-05-22 选中样本, 120日 ≤2025-12-01, 60日 ≤2026-03-05\n"]
    for w in WINDOWS:
        md.append(f"\n## 选后 {w} 日\n")
        md.append("| 分组 | 样本 | 板块累计均值 | 板块累计中位 | 大盘均值 | 超额均值 | 超额中位 | 超额胜率 |")
        md.append("|---|---|---|---|---|---|---|---|")
        def mdrow(label, s):
            if not s:
                return f"| {label} | — | — | — | — | — | — | — |"
            return (f"| {label} | {s['n']} | {s['board_mean']:+.2f}% | {s['board_med']:+.2f}% | "
                    f"{s['mkt_mean']:+.2f}% | {s['ex_mean']:+.2f}% | {s['ex_med']:+.2f}% | {s['win']:.1f}% |")
        md.append(mdrow("全周期", overall[w]))
        md.append(mdrow("近一年(2025H2起)", last_year[w]))
        for y in ["2024", "2025", "2026"]:
            md.append(mdrow(f"{y}年", by_year[y][w]))
        for k in rank_buckets:
            md.append(mdrow(k, by_rank[k][w]))
    md.append(f"\n明细逐样本: `{detail}`\n")
    md_path = os.path.join(OUT, "主线长周期收益.md")
    with open(md_path, "w") as f:
        f.write("\n".join(md))
    print(f"\n明细 CSV: {detail}")
    print(f"汇总 MD : {md_path}")


if __name__ == "__main__":
    main()
