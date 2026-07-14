"""导出 scout 逐日主线报告 — 三种口径 + 全周期 Top10.

口径:
  1. 选时强度 : 读 board_trend_daily 已落库的 trend_score/ret20/ret5/dist_ma20 (系统口径).
  2. 选后前瞻 : 用 concept_board_daily 板块实际 pct_change, 算被选当日起 5/10/20 日累计涨幅
               及相对大盘(全A等权)超额.
  3. 净值曲线 : 把每日 Top10 等权当成策略 —— t 日盘后选出的 Top10, 持有到 t+1,
               逐日累乘成组合净值, 对照全A等权大盘净值, 差距=累计超额.

大盘基准 = 全A等权日收益 (daily.pct_chg 按交易日等权平均); 另附上证综指(000001.SH)参考.

输出 (mainline_report/ 下):
  - mainline_daily_top10.csv   逐日 Top10 明细 (核心数据底座)
  - mainline_portfolio_nav.csv 主线组合净值 vs 大盘 净值 逐日
  - 主线逐日报告.md            人看的汇总文档

用法: /usr/bin/python3.12 export_mainline_report.py [--start 20240101] [--top 10]
"""
from __future__ import annotations
import argparse, os, sqlite3, statistics, csv
from collections import defaultdict

DB = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mainline_report")


def cum(series_pcts):
    """list[pct%] -> 累计涨幅%; 空->None."""
    if not series_pcts:
        return None
    v = 1.0
    for p in series_pcts:
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

    # 板块逐日 pct_change
    bpct = defaultdict(dict)  # bc -> {date: pct}
    for bc, d, p in c.execute(
            "SELECT board_code, trade_date, pct_change FROM concept_board_daily"):
        if p is not None:
            bpct[bc][d] = p

    # 大盘: 全A等权日收益 + 上证综指
    mkt = {d: r for d, r in c.execute(
        "SELECT trade_date, AVG(pct_chg) FROM daily WHERE pct_chg IS NOT NULL GROUP BY trade_date")}
    szindex = {d: p for d, p in c.execute(
        "SELECT trade_date, pct_chg FROM index_daily WHERE ts_code='000001.SH'")}

    # 落库主线 (选时强度字段直接取)
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

    def fwd_mkt(src, d, w):
        i = idxof.get(d)
        win = cal[i + 1:i + 1 + w]
        if len(win) < w:
            return None
        ps = [src[x] for x in win if x in src]
        return cum(ps) if ps else None

    # ── 1. 逐日明细 CSV ───────────────────────────────────────────
    detail_path = os.path.join(OUT, "mainline_daily_top10.csv")
    with open(detail_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["交易日", "排名", "板块", "板块代码", "趋势分",
                    "选时近20日%", "选时近5日%", "选时距MA20%",
                    "选后5日%", "选后10日%", "选后20日%",
                    "大盘5日%", "大盘10日%", "大盘20日%",
                    "超额5日", "超额10日", "超额20日"])
        for d in days:
            for rk, bn, bc, sc, r20, r5, dm in ml[d]:
                f5, f10, f20 = fwd_board(bc, d, 5), fwd_board(bc, d, 10), fwd_board(bc, d, 20)
                m5, m10, m20 = fwd_mkt(mkt, d, 5), fwd_mkt(mkt, d, 10), fwd_mkt(mkt, d, 20)
                def ex(a_, b_): return round(a_ - b_, 2) if (a_ is not None and b_ is not None) else None
                def rd(x): return round(x, 2) if x is not None else None
                w.writerow([d, rk, bn, bc, sc, r20, r5, dm,
                            rd(f5), rd(f10), rd(f20), rd(m5), rd(m10), rd(m20),
                            ex(f5, m5), ex(f10, m10), ex(f20, m20)])

    # ── 2. 组合净值 CSV ───────────────────────────────────────────
    nav_path = os.path.join(OUT, "mainline_portfolio_nav.csv")
    ml_nav = mkt_nav = sz_nav = 1.0
    rows_nav = []
    for i, d in enumerate(cal):
        if d < a.start:
            continue
        prev = cal[i - 1] if i > 0 else None
        if prev is None or prev not in ml:
            # 当日无昨日主线持仓, 净值不动 (用大盘照常推进基准从同起点)
            pass
        # 当日组合收益 = 昨日选出 Top10 的今日等权 pct
        if prev in ml:
            ps = [bpct[bc][d] for (_, _, bc, *_ ) in ml[prev] if d in bpct.get(bc, {})]
            mlret = statistics.mean(ps) if ps else 0.0
        else:
            mlret = 0.0
        mret = mkt.get(d, 0.0) or 0.0
        sret = szindex.get(d, 0.0) or 0.0
        ml_nav *= (1 + mlret / 100)
        mkt_nav *= (1 + mret / 100)
        sz_nav *= (1 + sret / 100)
        rows_nav.append((d, round(mlret, 3), round(ml_nav, 4),
                         round(mret, 3), round(mkt_nav, 4), round(sz_nav, 4),
                         round((ml_nav / mkt_nav - 1) * 100, 2)))
    with open(nav_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["交易日", "主线组合当日%", "主线组合净值",
                    "大盘(全A等权)当日%", "大盘净值", "上证综指净值", "累计超额%"])
        w.writerows(rows_nav)

    # ── 3. Markdown 文档 ─────────────────────────────────────────
    # 月度汇总
    M = defaultdict(lambda: {"f10": [], "ex10": [], "names": defaultdict(int)})
    for d in days:
        mo = d[:6]
        for rk, bn, bc, *_ in ml[d]:
            M[mo]["names"][bn] += 1
            f10 = fwd_board(bc, d, 10)
            m10 = fwd_mkt(mkt, d, 10)
            if f10 is not None:
                M[mo]["f10"].append(f10)
            if f10 is not None and m10 is not None:
                M[mo]["ex10"].append(f10 - m10)

    md = []
    md.append("# scout 逐日主线报告\n")
    md.append(f"> 区间 {days[0]} ~ {days[-1]}, 每日主线 Top{a.top}, 共 {len(days)} 个交易日\n")
    md.append("## 口径说明\n")
    md.append("- **选时强度**: 读 `board_trend_daily` 落库的 trend_score / 近20日 / 近5日 / 距MA20%(系统打分口径)。\n"
              "- **选后前瞻**: 用板块实际 `pct_change` 算被选当日起 5/10/20 日累计涨幅;**超额 = 板块 − 大盘**。\n"
              "- **净值曲线**: 每日 Top10 等权当策略,t 日盘后选出、持有到 t+1,逐日累乘;对照大盘净值。\n"
              "- **大盘基准**: 全A等权日收益(全市场个股 pct_chg 等权平均);另附上证综指。\n")

    last = rows_nav[-1]
    md.append("## 一、组合净值 vs 大盘(累计)\n")
    md.append(f"截至 {last[0]}: **主线组合净值 {last[2]}**, 大盘(全A等权) {last[4]}, 上证综指 {last[5]}, "
              f"**累计超额 {last[6]:+.2f}%**。\n")
    md.append("\n| 月末 | 主线组合净值 | 大盘净值 | 上证净值 | 累计超额% |\n|---|---|---|---|---|")
    seen_mo = {}
    for r in rows_nav:
        seen_mo[r[0][:6]] = r
    for mo in sorted(seen_mo):
        r = seen_mo[mo]
        md.append(f"| {mo} | {r[2]} | {r[4]} | {r[5]} | {r[6]:+.2f} |")
    md.append("")

    md.append("## 二、月度选后前瞻(10日)与高频主线\n")
    md.append("| 月份 | 主线fwd10均值 | 相对大盘超额均值 | 当月最常上榜板块(次数) |\n|---|---|---|---|")
    for mo in sorted(M):
        m = M[mo]
        f10 = f"{statistics.mean(m['f10']):+.2f}%" if m["f10"] else "—"
        ex10 = f"{statistics.mean(m['ex10']):+.2f}%" if m["ex10"] else "—"
        topn = sorted(m["names"].items(), key=lambda x: -x[1])[:3]
        names = ", ".join(f"{n}({k})" for n, k in topn)
        md.append(f"| {mo} | {f10} | {ex10} | {names} |")
    md.append("")

    md.append("## 三、最近 30 交易日逐日主线 Top10\n")
    for d in days[-30:]:
        items = ml[d]
        line = " · ".join(f"{bn}({sc})" for rk, bn, bc, sc, *_ in items)
        md.append(f"- **{d}**: {line}")
    md.append("")
    md.append("## 文件\n")
    md.append(f"- 逐日 Top10 全量明细(选时+前瞻+超额): `{detail_path}`\n"
              f"- 组合净值逐日: `{nav_path}`\n"
              "完整逐日数据在 CSV 里,可自行用 Excel/pandas 筛选排序。\n")

    md_path = os.path.join(OUT, "主线逐日报告.md")
    with open(md_path, "w") as f:
        f.write("\n".join(md))

    print(f"明细 CSV : {detail_path} ({len(days)} 天 × Top{a.top})")
    print(f"净值 CSV : {nav_path} ({len(rows_nav)} 行)")
    print(f"文档     : {md_path}")
    print(f"\n截至 {last[0]}: 主线组合净值 {last[2]} vs 大盘 {last[4]} vs 上证 {last[5]}, 累计超额 {last[6]:+.2f}%")
    c.close()


if __name__ == "__main__":
    main()
