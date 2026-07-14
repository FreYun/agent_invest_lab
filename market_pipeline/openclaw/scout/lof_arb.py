"""LOF 场内外套利测算 + 盘中计算.

聚焦 QDII+商品 LOF: 折溢价率 -> 分溢价/折价方向算扣费后净套利 -> 可操作性分级.
纯测算函数无 DB/网络副作用; compute_live() 负责拉实时价并落库.
"""
from __future__ import annotations

import signal
from datetime import datetime
from typing import Any, Optional

import scout_db

COMMISSION_PCT = 0.05      # 场内佣金 万2.5 双边
DEFAULT_REDEEM_FEE = 1.5   # 赎回费默认兜底 %
OPP_THRESHOLD = 1.5        # 净套利 ≥ -> 机会区
WATCH_THRESHOLD = 3.0      # |折溢价| ≥ -> 关注区


def premium_pct(price: Optional[float], nav: Optional[float]) -> Optional[float]:
    """折溢价率 %; 正=溢价, 负=折价. price/nav 缺失或非正返回 None(跳过该只)."""
    if price is None or price <= 0 or nav is None or nav <= 0:
        return None
    return (price - nav) / nav * 100.0


def net_arb(premium: float, purchase_fee: float, redeem_fee: float) -> dict:
    """分方向净套利 %. 溢价(premium>0)只算 net_premium; 折价(premium<0)只算 net_discount."""
    net_premium = net_discount = None
    pf = purchase_fee if purchase_fee is not None else 0.0
    rf = redeem_fee if redeem_fee is not None else DEFAULT_REDEEM_FEE
    if premium > 0:
        # 场外申购 -> 场内卖出
        net_premium = premium - pf - COMMISSION_PCT
    elif premium < 0:
        # 场内买入 -> 场外赎回
        net_discount = (-premium) - rf - COMMISSION_PCT
    return {"net_premium": net_premium, "net_discount": net_discount}


def classify(premium: Optional[float], net_premium: Optional[float],
             net_discount: Optional[float], purchasable: Optional[int],
             max_amt: Optional[float], redeemable: Optional[int]) -> str:
    """机会/关注/none 分级.
    溢价套利要能申购(purchasable 且限额>0); 折价套利要能赎回(redeemable).
    暂停申购/暂停赎回的票即便折溢价大也只算 watch(风险预警), 不算可落地机会."""
    if premium is None:
        return "none"
    # 机会区: 对应方向净套利达门槛且该方向可操作
    if (premium > 0 and net_premium is not None and net_premium >= OPP_THRESHOLD
            and purchasable and (max_amt or 0) > 0):
        return "opp"
    if (premium < 0 and net_discount is not None and net_discount >= OPP_THRESHOLD
            and redeemable):
        return "opp"
    if abs(premium) >= WATCH_THRESHOLD:
        return "watch"
    return "none"


def _realtime_quote_guarded(ts, ts_codes_csv: str, seconds: int = 15):
    """带 SIGALRM 硬超时的 realtime_quote (主线程, 同 collect.py 防 urllib3 半开挂死)."""
    def _h(signum, frame):
        raise TimeoutError("realtime_quote 超时")
    old = signal.signal(signal.SIGALRM, _h)
    signal.alarm(seconds)
    try:
        return ts.realtime_quote(ts_code=ts_codes_csv)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def fetch_live_prices(ts_codes: list[str], ts=None) -> dict[str, float]:
    """tushare realtime_quote 点名取场内实时价; 返回 {ts_code: PRICE}.
    `ts` 为 collect.py 已 set_token 的 tushare 模块; 分批 50 个; 单批失败跳过."""
    out: dict[str, float] = {}
    if ts is None:  # 独立运行兜底: 自建 token(collect 帧内会传入, 不走这里)
        import sys
        sys.path.insert(0, "/home/rooot/agent_invest_lab/market_pipeline/openclaw/workspace-bot11/scripts")
        from config import TUSHARE_TOKEN_OFFICIAL
        import tushare as _ts
        _ts.set_token(TUSHARE_TOKEN_OFFICIAL)
        ts = _ts
    for i in range(0, len(ts_codes), 50):
        chunk = ts_codes[i:i + 50]
        try:
            q = _realtime_quote_guarded(ts, ",".join(chunk))
            q.columns = q.columns.str.upper()  # 列名大小写归一(tushare 版本间不稳)
            for _, r in q.iterrows():
                code = r.get("TS_CODE")
                price = r.get("PRICE")
                if not code or price is None:
                    continue
                try:
                    out[str(code)] = float(price)
                except (ValueError, TypeError):
                    continue  # 停牌/无报价(""/"--")单只跳过, 不株连整批
        except Exception as e:
            print(f"  [lof-price] 批 {i//50} 跳过: {str(e)[:80]}")
    return out


def compute_live(c, now: datetime, ts=None, db_path: str = scout_db.DB_PATH) -> int:
    """盘中: 读 universe/nav/rate, 拉实时价, 算折溢价+净套利, upsert lof_arbitrage_live.
    复用调用方传入的连接 c 与已 set_token 的 ts; 返回写入条数. 写完自动 commit."""
    universe = list(c.execute(
        "SELECT lof_code, code6 FROM lof_universe WHERE in_universe=1"))
    if not universe:
        return 0
    # 最新净值: 每只取 nav_date 最大的一行
    nav_map = {}
    for r in c.execute(
            "SELECT lof_code, nav_date, unit_nav FROM lof_nav WHERE (lof_code, nav_date) "
            "IN (SELECT lof_code, MAX(nav_date) FROM lof_nav GROUP BY lof_code)"):
        nav_map[r["lof_code"]] = (r["unit_nav"], r["nav_date"])
    rate_map = {r["lof_code"]: dict(r) for r in c.execute("SELECT * FROM lof_rate")}

    prices = fetch_live_prices([u["lof_code"] for u in universe], ts)
    snap = now.strftime("%Y-%m-%d %H:%M:%S")
    trade_date = now.strftime("%Y-%m-%d")
    n = 0
    for u in universe:
        code = u["lof_code"]
        price = prices.get(code)
        nav, nav_date = nav_map.get(code, (None, None))
        prem = premium_pct(price, nav)
        if prem is None:
            continue
        rate = rate_map.get(code, {})
        arb = net_arb(prem, rate.get("purchase_fee_pct"),
                      rate.get("redeem_fee_pct"))
        act = classify(prem, arb["net_premium"], arb["net_discount"],
                       rate.get("purchasable"), rate.get("max_purchase_amt"),
                       rate.get("redeemable"))
        c.execute(
            "INSERT OR REPLACE INTO lof_arbitrage_live(trade_date, lof_code, "
            "snapshot_time, price, nav, nav_date, premium_pct, net_premium_arb, "
            "net_discount_arb, actionable) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (trade_date, code, snap, price, nav, nav_date, prem,
             arb["net_premium"], arb["net_discount"], act))
        n += 1
    c.commit()
    return n
