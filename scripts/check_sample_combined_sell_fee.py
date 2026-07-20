# 校验 combined 格式 CSV 中 confirmed 卖出单的扣费口径:
#   confirm_amount ~= confirm_vol * confirm_nav - fee   (容差 0.01, 与导入程序一致)
# 用法: /usr/bin/python3.12 scripts/check_sample_combined_sell_fee.py docs/sample-combined-0630.csv
import csv, sys

path = sys.argv[1] if len(sys.argv) > 1 else "/home/rooot/agent_invest_lab/docs/sample-combined-0630.csv"
TOL = 0.01

total = ok = 0
bad = []
with open(path, encoding="utf-8-sig") as f:
    for i, r in enumerate(csv.DictReader(f), start=2):  # i = CSV 原始行号(含表头)
        if r["record_type"] != "order" or r["order_status"] != "confirmed":
            continue
        if r["busin_type_desc"] != "赎回":
            continue
        total += 1
        vol, nav = float(r["confirm_vol"]), float(r["confirm_nav"])
        amt, fee = float(r["confirm_amount"]), float(r["fee"] or 0)
        diff = vol * nav - fee - amt
        if abs(diff) <= TOL:
            ok += 1
        else:
            bad.append((i, r["portfolio_name"], r["fund_code"], vol, nav, fee, amt, round(diff, 4)))

print(f"{path}")
print(f"confirmed 卖出单: {total}  通过扣费口径(|vol*nav-fee-amt|<={TOL}): {ok}  不通过: {len(bad)}")
for b in bad:
    print("  行号", b[0], b[1:])
sys.exit(1 if bad else 0)
