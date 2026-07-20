# 端到端回放验证 sample-combined-0630.csv (含 record_type=position 期初持仓行):
#   1) 期初持仓 + 订单流水(按 confirm_date 生效)逐日重放 -> 每日每基金份额必须对齐源库持仓快照
#   2) 现金重放(期初现金 + 卖出净额 - 申购金额, 按 confirm_date; bot101 申购按 biz_date 扣) -> 对齐 nav 行 cash
#   3) position 行自洽: confirm_vol*confirm_nav == market_value == apply_amount(成本)
# 用法: /usr/bin/python3.12 scripts/check_sample_combined_replay.py
import csv, sqlite3, sys
from collections import defaultdict

DOCS = "/home/rooot/agent_invest_lab/docs"
DB = "/home/rooot/.openclaw/data/fund.db"
CUTOFF = "2026-06-29"
SHARE_TOL, CASH_TOL = 0.02, 0.05

rows = list(csv.DictReader(open(f"{DOCS}/sample-combined-0630.csv", encoding="utf-8-sig")))
init = [r for r in rows if r["record_type"] == "position"]
errors = []

# --- 3) position 行自洽 ---
for r in init:
    shv, nav, mv, cost = float(r["confirm_vol"]), float(r["confirm_nav"]), float(r["market_value"]), float(r["apply_amount"])
    if abs(shv * nav - mv) > 0.01 or abs(cost - mv) > 0.01:
        errors.append(f"position 行自洽: {r['portfolio_id']} {r['fund_code']} shv*nav={shv*nav:.4f} mv={mv} cost={cost}")
print(f"position 期初持仓行: {len(init)} 行, 自洽校验 {'通过' if not errors else '失败'}")

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
pids = sorted({r["portfolio_id"] for r in rows})
for pid in pids:
    prows = [r for r in rows if r["portfolio_id"] == pid]
    bot = prows[0]["portfolio_name"]
    navs = {r["biz_date"]: r for r in prows if r["record_type"] == "nav"}
    orders = [r for r in prows if r["record_type"] == "order" and r["order_status"] == "confirmed"]
    days = sorted(navs)
    is_bot101 = "bot101" in bot

    # 现金重放
    cash = float(navs[days[0]]["cash"]) if not is_bot101 else 1_000_000.0
    # 份额重放(仅单基金 bot, 源库有持仓快照可对)
    shares = defaultdict(float)
    if not is_bot101:
        for r in init:
            if r["portfolio_id"] == pid: shares[r["fund_code"]] = float(r["confirm_vol"])
        snap = defaultdict(dict)
        for r in conn.execute("SELECT trade_date,fund_code,shares FROM fund_bot_position_snapshots WHERE bot_id=? AND trade_date<=?", (bot, CUTOFF)):
            snap[r["trade_date"]][r["fund_code"]] = r["shares"]

    sh_bad = ca_bad = 0
    prev = ""
    for i, d in enumerate(days):
        # 按 (prev, d] 区间累计生效, 覆盖 nav 行缺日(如 bot5 05-14)但订单照常确认的情况
        for o in orders:
            eff_cash = o["biz_date"] if is_bot101 else o["confirm_date"]  # bot101 申购当日扣款
            if o["busin_type_desc"] == "申购":
                if prev < eff_cash <= d: cash -= float(o["apply_amount"])
                if prev < o["confirm_date"] <= d: shares[o["fund_code"]] += float(o["confirm_vol"])
            else:
                if prev < o["confirm_date"] <= d:
                    cash += float(o["confirm_amount"])
                    shares[o["fund_code"]] -= float(o["confirm_vol"])
        prev = d
        # 现金对齐(期初日即基准, 从第2日起校验)
        if (i or is_bot101) and abs(cash - float(navs[d]["cash"])) > CASH_TOL:
            ca_bad += 1
            if ca_bad <= 3: errors.append(f"现金: {pid} {d} 重放={cash:.2f} nav行={navs[d]['cash']}")
        # 份额对齐(逐基金, 对每个源库快照日)
        if not is_bot101 and i and d in snap:
            for fc in set(shares) | set(snap[d]):
                if abs(shares.get(fc, 0.0) - snap[d].get(fc, 0.0)) > SHARE_TOL:
                    sh_bad += 1
                    if sh_bad <= 3: errors.append(f"份额: {pid} {d} {fc} 重放={shares.get(fc,0.0):.4f} 快照={snap[d].get(fc,0.0):.4f}")
    tag = "现金" if is_bot101 else "份额+现金"
    print(f"{pid} ({bot}): {len(days)} 天 {len(orders)} 单, {tag}逐日重放 {'通过' if not (sh_bad or ca_bad) else f'份额错{sh_bad} 现金错{ca_bad}'}")

if errors:
    print("\n错误明细(前若干):")
    for e in errors[:20]: print(" ", e)
sys.exit(1 if errors else 0)
