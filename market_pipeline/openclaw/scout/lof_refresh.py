"""LOF 盘后刷新: 全集圈定 + 净值 + 费率/限额/时滞.

供盘后 cron 调用; 串行单进程. 数据源:
  - tushare(:18065) fund_basic(全集) / fund_nav(净值兜底)
  - eastmoney lsjz  净值主源(真实单位净值 DWJZ, 不前向填充)
  - ttjj(:18077)    fund_rate
失败的单只跳过, 不中断整批.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

import requests

import scout_db
import _tushare_client as tushare
import _ttjj_client as ttjj

DEFAULT_REDEEM_FEE = 1.5

# eastmoney 历史净值: 权威单位净值源, 只列真实披露日(不像 ttjj/brze 会前向填充或滞后)
_EM_LSJZ = "http://api.fund.eastmoney.com/f10/lsjz"
_EM_HEADERS = {"Referer": "http://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0"}


def _pct(s: str):
    """'0.15%' -> 0.15; 非百分比(如 '1000元/笔')返回 None."""
    if not isinstance(s, str):
        return None
    m = re.match(r"^\s*([\d.]+)\s*%\s*$", s)
    return float(m.group(1)) if m else None


def parse_fund_rate(lof_code: str, raw: dict) -> dict | None:
    """把 ttjj fund_rate 响应解析成 lof_rate 行 dict; 失败返回 None."""
    if not raw or not raw.get("success"):
        return None
    d = raw.get("data") or {}
    limits = d.get("交易限制") or {}
    timing = d.get("确认时效") or {}
    fees = (d.get("费率明细") or {}).get("申购费率") or []
    # 首个百分比档的优惠费率
    pf = None
    for item in fees:
        v = _pct(item.get("优惠费率标准"))
        if v is not None:
            pf = v
            break
    max_amt = limits.get("最大申购限额_元")
    available = bool(d.get("是否可用"))
    purchasable = 1 if (available and (max_amt or 0) > 0) else 0

    def _int(x):
        return int(x) if isinstance(x, (int, float)) else None

    return {
        "lof_code": lof_code,
        "purchase_fee_pct": pf,
        "redeem_fee_pct": DEFAULT_REDEEM_FEE,
        "redeem_fee_source": "default",
        "max_purchase_amt": max_amt,
        "purchase_confirm_days": _int(timing.get("申购确认天数_工作日")),
        "redeem_confirm_days": _int(timing.get("赎回确认天数_工作日")),
        "redeem_arrival_days": _int(timing.get("赎回到账天数_工作日")),
        "purchasable": purchasable,
        "cached_at": datetime.now().isoformat(),
    }


# 商品关键词 -> 子主题; QDII(海外)关键词 -> 子主题
# 注意: 不收 "有色"(A股有色板块股票基金,非商品期货)、不收裸 "科技"(会扫进A股科技LOF)
_COMMODITY = {"原油": "原油", "石油": "原油", "油气": "原油", "黄金": "黄金",
              "白银": "白银", "商品": "商品", "豆粕": "商品"}
_QDII = {"纳指": "纳指", "纳斯达克": "纳指", "标普": "标普", "道琼斯": "美股",
         "美国": "美股", "海外": "海外", "亚太": "亚太", "德国": "德国",
         "日经": "日本", "恒生": "港股", "中概": "中概",
         "芯片": "芯片", "全球": "全球"}


def classify_lof(ts_code: str, name: str):
    """按名称关键词分类; 返回 (category, sub_theme) 或 None(非QDII/商品/或ETF/REIT)."""
    nm = name or ""
    code = ts_code.split(".")[0]
    # 排除 ETF(名称含 ETF 或代码段 159/51/56/58) 与 公募基础设施REIT(封闭式,不可自由申赎,
    # 净套利模型不适用; 全在代码段 180/508)。按代码而非名称排 REIT, 以免误伤
    # 投海外房产的开放式 QDII(如 160140 美国REIT精选LOF, 可申赎)。
    if ("ETF" in nm.upper()
            or code[:3] in ("159", "180", "508")
            or code[:2] in ("51", "56", "58")):
        return None
    for kw, theme in _COMMODITY.items():
        if kw in nm:
            return ("商品", theme)
    for kw, theme in _QDII.items():
        if kw in nm:
            return ("QDII", theme)
    return None


def build_universe(db_path: str = scout_db.DB_PATH) -> int:
    """从 fund_basic 自动圈 QDII+商品 LOF, upsert lof_universe(保留人工 manual 行).
    返回自动圈定条数."""
    r = tushare.call("fund_basic", market="E", status="L")
    rows = r.get("rows") or []
    if not rows:
        print("[lof-refresh] build_universe: fund_basic 返回空, 跳过全批")
        return 0
    now = datetime.now().isoformat()
    c = scout_db.conn(db_path)
    n = 0
    for x in rows:
        ts_code = x.get("ts_code")
        name = x.get("name")
        if not ts_code:
            continue
        cls = classify_lof(ts_code, name)
        if not cls:
            continue
        category, sub_theme = cls
        # 不覆盖人工 in_universe/source: 已存在则只刷 name/category/sub_theme
        c.execute(
            "INSERT INTO lof_universe(lof_code, code6, name, category, sub_theme, "
            "in_universe, source, updated_at) VALUES (?,?,?,?,?,1,'auto',?) "
            "ON CONFLICT(lof_code) DO UPDATE SET code6=excluded.code6, "
            "name=excluded.name, category=excluded.category, "
            "sub_theme=excluded.sub_theme, updated_at=excluded.updated_at",
            (ts_code, ts_code.split(".")[0], name, category, sub_theme, now))
        n += 1
    c.commit()
    c.close()
    return n


def _em_latest_nav(code6, session):
    """eastmoney lsjz 取最新真实单位净值(DWJZ). 返回 (nav_date 'YYYY-MM-DD', unit_nav) 或 None.

    lsjz 只列真实披露日, 不前向填充(ttjj 的"复权净值"会把 QDII 未披露日补成上一日,
    造出假的 T+1 净值; brze 代理则结构性停在 ~T-3). DWJZ 是申赎单位净值, 已正确处理
    分红(非复权), 故 QDII/分红基金都准. 这是 LOF 折溢价测算的正确净值口径."""
    r = session.get(_EM_LSJZ,
                    params={"fundCode": code6, "pageIndex": 1, "pageSize": 10},
                    headers=_EM_HEADERS, timeout=15,
                    proxies={"http": None, "https": None})
    lst = (r.json().get("Data") or {}).get("LSJZList") or []
    valid = [x for x in lst if x.get("DWJZ") not in (None, "", "--")
             and len(str(x.get("FSRQ") or "")) == 10]
    if not valid:
        return None
    top = max(valid, key=lambda x: x["FSRQ"])
    try:
        return top["FSRQ"], float(top["DWJZ"])
    except (ValueError, TypeError):
        return None


def _brze_latest_nav(ts_code, b_start, b_end):
    """brze 代理 fund_nav 单只兜底(eastmoney 不可达时). 返回 (nav_date, unit_nav) 或 None.
    brze 单位净值口径正确但数据停在 ~T-3, 仅作回退."""
    r = tushare.call("fund_nav", ts_code=ts_code, start_date=b_start, end_date=b_end)
    rows = r.get("rows") or []
    if not rows:
        rows = (tushare.call("fund_nav", ts_code=ts_code).get("rows") or [])
    if not rows:
        return None
    top = max(rows, key=lambda x: str(x.get("nav_date") or ""))
    raw_date = str(top.get("nav_date") or "")
    if top.get("unit_nav") is None or len(raw_date) != 8:
        return None
    try:
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}", float(top["unit_nav"])
    except (ValueError, TypeError):
        return None


def refresh_nav(db_path: str = scout_db.DB_PATH) -> int:
    """对 in_universe=1 的每只 LOF 拉最新单位净值, 串行单进程.

    主源 eastmoney lsjz(真实单位净值 DWJZ): QDII/商品/分红基金口径都正确, 只列真实
    披露日(不造假 T+1). eastmoney 不可达时单只回退 brze 代理(偏旧但口径正确).
    注意: QDII 净值本就滞后(海外收盘 + T+1/T+2 披露), 故部分只能到 T-2/T-3, 属正常;
    不应像旧逻辑那样用复权填充/锚点滚补硬造出当日净值.
    """
    c = scout_db.conn(db_path)
    universe = [(r["lof_code"], r["code6"]) for r in c.execute(
        "SELECT lof_code, code6 FROM lof_universe WHERE in_universe=1")]
    now = datetime.now().isoformat()
    b_end = datetime.now().strftime("%Y%m%d")
    b_start = (datetime.now() - timedelta(days=20)).strftime("%Y%m%d")
    session = requests.Session()

    n = 0
    for ts_code, code6 in universe:
        try:
            res = None
            try:
                res = _em_latest_nav(code6, session)
            except Exception as e:
                print(f"  [lof-nav] {code6} eastmoney 失败, 退回 brze: {str(e)[:60]}")
            if res is None:  # eastmoney 无数据/异常 -> brze 兜底
                res = _brze_latest_nav(ts_code, b_start, b_end)
            if res is None:
                continue
            nav_date, unit_nav = res
            c.execute(
                "INSERT OR REPLACE INTO lof_nav(lof_code, nav_date, unit_nav, "
                "ann_date, cached_at) VALUES (?,?,?,?,?)",
                (ts_code, nav_date, unit_nav, None, now))
            n += 1
            time.sleep(0.15)  # 礼貌限速, 避免 eastmoney 连请触发风控
        except Exception as e:
            print(f"  [lof-nav] {ts_code} 跳过: {str(e)[:80]}")
    c.commit()
    c.close()
    return n


def _purchasable(status):
    """申购状态原文 -> 可申购 1/0/None. 暂停申购/封闭期/认购期 -> 0; 开放申购/限大额 -> 1."""
    if not status:
        return None
    return 0 if (status == "暂停申购" or "封闭" in status or "认购" in status) else 1


def _redeemable(status):
    """赎回状态原文 -> 可赎回 1/0/None. 暂停/封闭 -> 0; 开放赎回 -> 1."""
    if not status:
        return None
    return 0 if ("暂停" in status or "封闭" in status) else 1


def refresh_rate(db_path: str = scout_db.DB_PATH) -> int:
    """对 in_universe=1 的每只 LOF 拉 ttjj fund_rate(限额/时效/申购费)串行,
    并批量拉 fund_basic_info 的权威申赎状态 + 最高赎回费率覆盖(暂停申购则不可套利)."""
    c = scout_db.conn(db_path)
    universe = [(r["lof_code"], r["code6"]) for r in c.execute(
        "SELECT lof_code, code6 FROM lof_universe WHERE in_universe=1")]
    # 批量申赎状态(fund_basic_info 支持 fund_codes 数组, universe 规模小, 一次取完)
    status_by_code = {}
    try:
        br = ttjj.call("fund_basic_info", fund_codes=[c6 for _, c6 in universe])
        for it in ((br.get("data") or {}).get("items") or br.get("items") or []):
            status_by_code[str(it.get("基金代码") or "")] = it
    except Exception as e:
        print(f"  [lof-rate] fund_basic_info 批量失败(退回限额推断): {str(e)[:80]}")
    now = datetime.now().isoformat()
    n = 0
    for ts_code, code6 in universe:
        try:
            raw = ttjj.call("fund_rate", fund_code=code6)
            row = parse_fund_rate(ts_code, raw)
            if not row:
                continue
            # fund_basic_info 权威申赎状态覆盖 fund_rate 的限额推断
            it = status_by_code.get(code6) or {}
            sg, sh, rf = it.get("申购状态"), it.get("赎回状态"), it.get("最高赎回费率")
            purchasable = _purchasable(sg) if sg else row["purchasable"]
            redeemable = _redeemable(sh)
            redeem_fee = rf if rf is not None else row["redeem_fee_pct"]
            redeem_src = "ttjj" if rf is not None else row["redeem_fee_source"]
            c.execute(
                "INSERT OR REPLACE INTO lof_rate(lof_code, purchase_fee_pct, "
                "redeem_fee_pct, redeem_fee_source, max_purchase_amt, "
                "purchase_confirm_days, redeem_confirm_days, redeem_arrival_days, "
                "purchasable, redeemable, purchase_status, redeem_status, cached_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["lof_code"], row["purchase_fee_pct"], redeem_fee, redeem_src,
                 row["max_purchase_amt"], row["purchase_confirm_days"],
                 row["redeem_confirm_days"], row["redeem_arrival_days"],
                 purchasable, redeemable, sg, sh, now))
            n += 1
        except Exception as e:
            print(f"  [lof-rate] {ts_code} 跳过: {str(e)[:80]}")
    c.commit()
    c.close()
    return n


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", action="store_true", help="重圈全集")
    ap.add_argument("--nav", action="store_true", help="刷净值")
    ap.add_argument("--rate", action="store_true", help="刷费率")
    ap.add_argument("--all", action="store_true", help="全部")
    a = ap.parse_args()
    scout_db.init_schema()
    if a.all or a.universe:
        print(f"[lof-refresh] universe: {build_universe()} 只")
    if a.all or a.nav:
        print(f"[lof-refresh] nav: {refresh_nav()} 只")
    if a.all or a.rate:
        print(f"[lof-refresh] rate: {refresh_rate()} 只")


if __name__ == "__main__":
    main()
