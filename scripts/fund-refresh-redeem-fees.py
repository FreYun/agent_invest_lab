#!/usr/bin/env python3
"""从天天基金上游 FundRateInfoV2 抓取真实赎回费阶梯，刷新 fund_info.redeem_fee_json。

背景：ttjj-api 本地服务的 /api/fund/rate 在映射时丢了上游的 sh/shhd 字段，
导致 fund-fetch-fees.py 一直用监管标准阶梯兜底。本脚本直接打上游拿真实数据。

阶梯语义与 fund-portfolio-mcp/server.py 的 _redeem_fee_rate 一致：
  [{"max_days": int|None, "rate": float}]  按升序，holding_days < max_days 命中；
  max_days=None 表示最后一档无上限；rate 是小数（0.015 = 1.5%）。

上游 sh 字段 time 文本格式（已实测抽样）：
  "持有期限 < 7天" / "7天 ≤ 持有期限 < 30天" / "持有期限 ≥ 30天"
  "持有期限 ≤ 6天"（闭区间，等价 < 7天）
  单位：天 / 月(×30) / 年(×365)

用法：
  /usr/bin/python3.12 scripts/fund-refresh-redeem-fees.py            # 全量刷新
  /usr/bin/python3.12 scripts/fund-refresh-redeem-fees.py 024195 ... # 只刷指定基金
  --dry-run  只打印不写库

注意：上游不要并发请求（会被 reset），全程串行 + 0.2s 间隔。
"""
import json
import re
import sqlite3
import sys
import time
import urllib.request

DB_PATH = "/home/rooot/agent_invest_lab/data/fund.db"
API_URL = "https://fundcomapi.eastmoney.com/mm/FundMNewApi/FundRateInfoV2?FCODE={code}"
SLEEP = 0.2
RETRIES = 3

_PCT_RE = re.compile(r"([\d.]+)\s*%")
# 上界："< 7天" / "≤ 6天"，单位 天/月/年
_UPPER_RE = re.compile(r"([<≤])\s*([\d.]+)\s*(天|个月|月|年)")
_UNIT_DAYS = {"天": 1, "月": 30, "个月": 30, "年": 365}


def parse_rate(text: str) -> float | None:
    """'1.50%' -> 0.015"""
    m = _PCT_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1)) / 100.0


def parse_tier_upper(text: str) -> int | None | str:
    """time 文本 -> max_days（开区间上界天数）；'≥/＞' 无上界 -> None；解析不了 -> 'UNPARSED'。"""
    t = (text or "").strip()
    if not t:
        return "UNPARSED"
    m = _UPPER_RE.search(t)
    if m:
        op, num, unit = m.group(1), float(m.group(2)), m.group(3)
        days = num * _UNIT_DAYS[unit]
        if days != int(days):
            return "UNPARSED"
        days = int(days)
        # "≤ 6天" 闭区间 → 开区间上界 7；"< 7天" 直接 7
        return days + 1 if op == "≤" else days
    if "≥" in t or "＞" in t or ">" in t:
        return None  # 最后一档，无上界
    return "UNPARSED"


def fetch_sh(code: str) -> list | None:
    """拉上游 sh 字段。网络失败返回 None（与空列表区分）。"""
    url = API_URL.format(code=code)
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MCP Client/1.0.0",
                                                       "Accept": "application/json"})
            j = json.load(urllib.request.urlopen(req, timeout=20))
            data = j.get("data")
            if not isinstance(data, dict):
                return None
            sh = data.get("sh")
            return sh if isinstance(sh, list) else []
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None


def build_tiers(sh: list) -> list | str:
    """上游 sh -> 阶梯表；任何一档解析失败返回 'UNPARSED'。"""
    # 特例：单档且 time 为空 = 不分持有期的固定费率（如老 ETF 0.15%）
    if len(sh) == 1 and not (sh[0].get("time") or "").strip():
        rate = parse_rate(sh[0].get("rate") or "")
        if rate is None:
            return "UNPARSED"
        return [{"max_days": None, "rate": rate}]
    tiers = []
    for t in sh:
        rate = parse_rate(t.get("rate") or "")
        if rate is None:
            return "UNPARSED"
        upper = parse_tier_upper(t.get("time") or "")
        if upper == "UNPARSED":
            return "UNPARSED"
        tiers.append({"max_days": upper, "rate": rate})
    # 校验：升序、None 只能在最后
    bounded = [t["max_days"] for t in tiers if t["max_days"] is not None]
    if bounded != sorted(bounded):
        return "UNPARSED"
    if any(t["max_days"] is None for t in tiers[:-1]):
        return "UNPARSED"
    return tiers


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry_run = "--dry-run" in sys.argv

    con = sqlite3.connect(DB_PATH)
    if args:
        codes = args
    else:
        codes = [r[0] for r in con.execute("SELECT fund_code FROM fund_info ORDER BY fund_code")]

    # 备份现值（幂等：只建一次）
    if not dry_run:
        con.execute("CREATE TABLE IF NOT EXISTS fund_info_redeem_backup AS "
                    "SELECT fund_code, redeem_fee_json, datetime('now','localtime') AS backed_at "
                    "FROM fund_info")
        con.commit()

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    stats = {"updated": 0, "empty_sh": [], "fetch_fail": [], "unparsed": []}
    for i, code in enumerate(codes, 1):
        sh = fetch_sh(code)
        if sh is None:
            stats["fetch_fail"].append(code)
        elif not sh:
            stats["empty_sh"].append(code)   # 上游无赎回费阶梯（多为场内 ETF），不写、保持原值
        else:
            tiers = build_tiers(sh)
            if tiers == "UNPARSED":
                stats["unparsed"].append((code, json.dumps(sh, ensure_ascii=False)))
            else:
                payload = json.dumps(tiers, ensure_ascii=False)
                if dry_run:
                    print(f"[dry-run] {code} -> {payload}")
                else:
                    con.execute("UPDATE fund_info SET redeem_fee_json=?, updated_at=? WHERE fund_code=?",
                                (payload, now, code))
                    con.commit()
                stats["updated"] += 1
        if i % 50 == 0 or i == len(codes):
            print(f"进度 {i}/{len(codes)}  updated={stats['updated']} "
                  f"empty_sh={len(stats['empty_sh'])} fail={len(stats['fetch_fail'])} "
                  f"unparsed={len(stats['unparsed'])}", flush=True)
        time.sleep(SLEEP)

    print("\n=== 汇总 ===")
    print(f"成功更新: {stats['updated']}")
    print(f"上游无阶梯(保持原值): {len(stats['empty_sh'])} -> {stats['empty_sh']}")
    print(f"抓取失败: {len(stats['fetch_fail'])} -> {stats['fetch_fail']}")
    if stats["unparsed"]:
        print(f"解析失败 {len(stats['unparsed'])} 条:")
        for code, raw in stats["unparsed"]:
            print(f"  {code}: {raw}")
    con.close()


if __name__ == "__main__":
    main()
