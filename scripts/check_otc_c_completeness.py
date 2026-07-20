#!/usr/bin/env python3
"""场外C候选宇宙 数据完整性闸门检查（重跑报告前的前置校验）。

校验 759 只场外C：① 基本信息(name/跟踪指数/费率/赎回费) ② 净值覆盖回测窗口
[窗口起始-约120交易日, 窗口末]。输出汇总 + 三类缺口清单。
"""
from __future__ import annotations
import sqlite3, json, datetime as dt

DB = "/home/rooot/agent_invest_lab/data/fund.db"
WIN_FROM = "2025-06-01"      # 回测窗口起（world-multi-fund-backtest.yaml replay.from）
WIN_TO   = "2026-06-09"      # 回测窗口末
# corr 回看 ~120 交易日 ≈ 约 170 自然日 → 净值最好覆盖到此日之前
NAV_NEED_FROM = "2024-12-10"

def main():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT fund_code, fund_name, track_index_code, track_index_name, "
        "       mgmt_fee, custody_fee, sales_service_fee, redeem_fee_json "
        "FROM fund_info "
        "WHERE track_index_code IS NOT NULL AND track_index_code!='' AND fund_name LIKE '%C'"
    ).fetchall()
    total = len(rows)

    bad_info = []        # 基本信息缺失
    no_nav = []          # 完全无净值
    late_launch = []     # 净值起始晚于 NAV_NEED_FROM（窗口早段无载体）
    short_recent = []    # 净值未覆盖到窗口末附近
    ok = 0

    for code, name, tic, tin, mgmt, cust, ssf, redeem in rows:
        problems = []
        if not name or not tic:
            problems.append("info")
        # 费率：C 申购费可为0，但管理/托管/赎回应有
        if mgmt is None or cust is None:
            problems.append("fee")
        try:
            rj = json.loads(redeem) if redeem else None
            if not rj:
                problems.append("redeem")
        except Exception:
            problems.append("redeem")
        if problems:
            bad_info.append({"code": code, "name": name, "miss": problems})

        nav = con.execute(
            "SELECT MIN(nav_date), MAX(nav_date), COUNT(*) FROM fund_nav WHERE fund_code=?",
            (code,)).fetchone()
        nmin, nmax, ncnt = nav
        if not ncnt:
            no_nav.append({"code": code, "name": name})
            continue
        if nmin and nmin > NAV_NEED_FROM:
            late_launch.append({"code": code, "name": name, "nav_from": nmin, "idx": tic})
        if nmax and nmax < WIN_TO:
            short_recent.append({"code": code, "name": name, "nav_to": nmax})
        if not problems and (nmin and nmin <= NAV_NEED_FROM) and (nmax and nmax >= WIN_TO):
            ok += 1

    gap = json.load(open("/tmp/otc_c_index_gap.json"))

    print("="*64)
    print(f"场外C候选宇宙总数:           {total}")
    print(f"  ✓ 完整(信息全+净值全窗口):  {ok}")
    print(f"  基本信息有缺失:            {len(bad_info)}")
    print(f"  完全无净值:                {len(no_nav)}")
    print(f"  晚于{NAV_NEED_FROM}成立(窗口早段无该载体): {len(late_launch)}")
    print(f"  净值未覆盖到窗口末{WIN_TO}:  {len(short_recent)}")
    print(f"指数缺口(去ETF后无任何场外C载体): {len(gap)} 个")
    print("="*64)
    if bad_info:
        print("\n[基本信息缺失] 抽样:")
        for x in bad_info[:15]: print("  ", x["code"], x["name"], x["miss"])
    if no_nav:
        print("\n[完全无净值] 全部:")
        for x in no_nav: print("  ", x["code"], x["name"])
    if late_launch:
        print(f"\n[晚成立·窗口早段无载体] {len(late_launch)} 只, 抽样20:")
        for x in sorted(late_launch, key=lambda v: v["nav_from"], reverse=True)[:20]:
            print("  ", x["code"], x["name"], "起", x["nav_from"])
    if short_recent:
        print(f"\n[净值未到窗口末] {len(short_recent)} 只, 抽样:")
        for x in short_recent[:15]: print("  ", x["code"], x["name"], "止", x["nav_to"])

    json.dump({"bad_info": bad_info, "no_nav": no_nav, "late_launch": late_launch,
               "short_recent": short_recent, "index_gap": gap},
              open("/tmp/otc_c_completeness.json", "w"), ensure_ascii=False, indent=1)
    con.close()

if __name__ == "__main__":
    main()
