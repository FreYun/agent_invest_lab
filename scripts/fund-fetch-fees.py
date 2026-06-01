#!/usr/bin/env python3
"""拉取基金费率写入 fund.db 的 fund_info 表。

来源：天天基金费率 API（http://ttjj-data-api.jijinmima.cn/api/fund/rate）。
口径：
- purchase_fee：取「申购费率」第一档的「优惠费率标准」（1 折，平台实际费率），百分比数值。
  缺失/0 折/固定金额档 → 0。
- mgmt_fee / custody_fee / sales_service_fee：取「基础费率」里的年化费率（百分比数值）。
  注意这些已内含在公布单位净值里，模拟时不再二次计提，仅存表供展示。
- redeem_fee_json：API 不返回赎回费率，统一写监管标准持有期阶梯（A 类常见值）：
    [{"max_days":7,"rate":0.015},{"max_days":30,"rate":0.005},{"max_days":null,"rate":0.0}]
  含义：持有 <7 天 1.5%，7–30 天 0.5%，≥30 天 0%。

用法：
  python3 scripts/fund-fetch-fees.py            # 给 fund_info 里所有 fund 补费率
  python3 scripts/fund-fetch-fees.py 003957 002611   # 只补指定 fund
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time

import requests

DB_PATH = "/home/rooot/agent_invest_lab/data/fund.db"
RATE_API = "http://ttjj-data-api.jijinmima.cn/api/fund/rate"
TIMEOUT = 15

# 监管标准赎回费阶梯（A 类公募常见；C 类多为 <7 天 1.5% / ≥7 天 0%，这里用更保守的统一值）
STANDARD_REDEEM_TIERS = [
    {"max_days": 7, "rate": 0.015},
    {"max_days": 30, "rate": 0.005},
    {"max_days": None, "rate": 0.0},
]

_PCT_RE = re.compile(r"([\d.]+)\s*%")


def _parse_pct(text: str) -> float | None:
    """'0.08%' -> 0.08 ；'1000元/笔' / '' -> None。"""
    if not text:
        return None
    m = _PCT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def fetch_rate(fund_code: str) -> dict | None:
    try:
        r = requests.post(RATE_API, json={"fund_code": fund_code}, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            return None
        return j.get("data")
    except Exception as e:
        print(f"  [{fund_code}] fetch 失败: {e}")
        return None


def extract_fees(data: dict) -> dict:
    base = data.get("基础费率") or {}
    detail = data.get("费率明细") or {}
    # 申购费率：优先「申购费率」，缺失退「认购费率」；取第一档优惠费率
    purchase_list = detail.get("申购费率") or detail.get("认购费率") or []
    purchase_fee = 0.0
    for tier in purchase_list:
        v = _parse_pct(tier.get("优惠费率标准") or "")
        if v is not None:
            purchase_fee = v
            break
    return {
        "mgmt_fee": base.get("基金管理费率"),
        "custody_fee": base.get("基金托管费率"),
        "sales_service_fee": base.get("销售服务费率"),
        "purchase_fee": purchase_fee,
        "redeem_fee_json": json.dumps(STANDARD_REDEEM_TIERS, ensure_ascii=False),
    }


def main() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    args = sys.argv[1:]
    if args:
        codes = args
    else:
        codes = [r["fund_code"] for r in conn.execute("SELECT fund_code FROM fund_info ORDER BY fund_code")]
    print(f"待补费率 fund 数: {len(codes)}")

    ok = fail = 0
    for i, code in enumerate(codes, 1):
        data = fetch_rate(code)
        if not data:
            fail += 1
            continue
        fees = extract_fees(data)
        conn.execute(
            "UPDATE fund_info SET mgmt_fee=?, custody_fee=?, sales_service_fee=?, "
            "purchase_fee=?, redeem_fee_json=?, updated_at=datetime('now') WHERE fund_code=?",
            (fees["mgmt_fee"], fees["custody_fee"], fees["sales_service_fee"],
             fees["purchase_fee"], fees["redeem_fee_json"], code),
        )
        ok += 1
        if i % 20 == 0:
            conn.commit()
            print(f"  ...{i}/{len(codes)}")
        time.sleep(0.05)  # 别打太快
    conn.commit()
    conn.close()
    print(f"完成：成功 {ok}，失败 {fail}")


if __name__ == "__main__":
    main()
