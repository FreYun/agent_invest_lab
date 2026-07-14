"""scout/valuation.py — 周期股估值带机械层引擎 (做T参考).

链路: 商品价中枢 -> 季度盈利区间 -> 年化EPS区间 -> PIT动态PE分位带 -> 价格带.
设计: docs/superpowers/specs/2026-07-10-cyclical-valuation-band-design.md
锚点优先级: 手动(valuation-targets.json override) > agent(valuation_agent_view
保鲜期内, 由 valuation_anchor_writer.py 钳制落库) > 机械自动.
数据源: brze 代理(sge_daily/fut_daily/income, 禁系统代理) + 本地 market.db
(daily_basic/daily/earnings_events). 单位: 利润=亿元, total_mv=万元.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_PATH = os.path.join(HERE, "valuation-targets.json")
TUSHARE_HTTP_URL = os.getenv("TUSHARE_HTTP_URL", "https://tu.brze.top")
DEFAULT_TIMEOUT = int(os.getenv("TUSHARE_TIMEOUT", "60"))

RESEARCH_MCP_URL = os.getenv("RESEARCH_MCP_URL",
                             "http://research-mcp.jijinmima.cn/mcp")
CONSENSUS_UNIT_DIV = 1e8   # 预期值单位换算到亿(Task7 Step1 探针实测: 元单位, 改这一处)
CONSENSUS_STALE_DAYS = 10  # 预期序列断更超 N 交易日 → 研判上下文标注陈旧

TARGET_DEFAULTS = {
    "driver_override": None,   # {"center":..,"low":..,"high":..} 手动商品价锚
    "earnings_override": None, # {"2026Q3":[lo,hi]} 手动单季区间(亿)
    "pe_window": 250,          # PE 分位回看窗口(交易日)
    "pe_lo_q": 0.10,           # 带下沿分位
    "pe_hi_q": 0.90,           # 带上沿分位
    "rule_tolerance": 0.10,    # 规则法 ±δ
    "earnings_model": "rule",  # commodity_pe 机械估: rule(g规则法) | revenue_margin(营收×净利率)
    "agent_ttl_days": 10,      # agent 建议保鲜期(交易日, 日历日近似换算)
    "research_industries": [], # agent 上下文匹配 research_note 的行业关键词
    "model": None,             # {"kind":"commodity_pe","brief":"…"} 按股定制估值模型
    "enabled": True,           # False=停用: 机械层/触发器都跳过, 数据保留
    ".openclaw": "",
}
MIN_HISTORY = 120        # PIT PE 序列最少样本, 不足不出带
COMMODITY_REGIME_FRAC = 0.4  # commodity_pe 长期带: 只保留 年化盈利>=FRAC×当前锚中点 的可比regime日,
#                              剔盈利塌陷期畸高PE(周期股 PE 与 E 反向, 塌陷日 E→0 使 PE 冲天)
REGIME_MIN_HISTORY = 40  # regime 过滤后比率样本下限(深周期短史如赣锋滤后仅~69天), 仅作用 commodity_pe
CLAMP_PROFIT_LO = 0.5    # agent 盈利钳制: 规则区间 [0.5x, 1.5x]
CLAMP_PROFIT_HI = 1.5
CLAMP_ANCHOR_LO = 0.7    # 新模型锚净利钳制: 机械锚 [0.7x, 1.3x](pe_band/consensus_pe)
CLAMP_ANCHOR_HI = 1.3
CLAMP_PE_QLO = 0.05      # agent PE 钳制: 历史 [5,95] 分位
CLAMP_PE_QHI = 0.95
CLAMP_DRIVER_PAD = 0.10  # agent 商品价钳制: 近120日范围 ±10%
ST_PE_WINDOW = 60        # 短期带 PE 回看窗口(交易日)
ST_PE_LO_Q, ST_PE_HI_Q = 0.20, 0.80   # 短期带振荡分位
CLAMP_ST_WIDTH_LO = 0.03  # 短期半宽钳制笼 [3%, 15%]
CLAMP_ST_WIDTH_HI = 0.15
CLAMP_ST_CENTER_PAD = 0.08  # 短期中枢修正钳 机械值 ±8%
BASKET_MAX_COMPONENTS = 4   # 篮子成分数上限(下限 2)
BASKET_WEIGHT_TOL = 0.01    # 权重和容差: abs(sum-1)<=tol 通过后内存归一化
BASKET_MIN_DAYS = 60        # 篮子交集交易日下限(撑起基期窗20+series_range窗60语义)
BASKET_BASE_DAYS = 20       # 基期窗默认长度(交集最早N个交易日均价), 配置 base_days 可覆盖
BASKET_BASE_VALUE = 100.0   # 指数基期值(量级100: round(x,2)下精度0.01%, clamp/展示更自然)

# 估值模型分发表: kind -> runner. 具体 runner 定义在文件后部,
# 模块加载末尾注册 (提前声明空表, 使 load_targets 的 kind 校验不依赖定义顺序).
MODEL_RUNNERS: dict = {}

# ---------- 配置 ----------

def validate_driver(ts_code: str, driver: dict) -> dict:
    """driver 配置校验(单一事实源: load_targets/writer onboard/admin enable 共用).

    kind=sge|fut: symbol 必填, 原样返回。
    kind=basket: components 2-4 个, 每成分 kind∈(sge,fut)(禁嵌套)/symbol 非空不重复/
    label 非空/weight 正数; 权重和限 1±BASKET_WEIGHT_TOL; 顶层 label 必填
    (api_targets/页面直接用)。返回副本, 权重已归一化(和恰为1, 文件原样不动)。
    不合法抛 ValueError。
    """
    if not isinstance(driver, dict):
        raise ValueError(f"{ts_code}: driver 必须是 dict")
    kind = driver.get("kind")
    if kind in ("sge", "fut"):
        if not driver.get("symbol"):
            raise ValueError(f"{ts_code}: driver.symbol 必填")
        return driver
    if kind != "basket":
        raise ValueError(f"{ts_code}: driver.kind 只支持 sge/fut/basket: {kind!r}")
    if not driver.get("label"):
        raise ValueError(f"{ts_code}: basket 顶层 label 必填(页面/上下文直接展示)")
    comps = driver.get("components")
    if not isinstance(comps, list) or not 2 <= len(comps) <= BASKET_MAX_COMPONENTS:
        raise ValueError(f"{ts_code}: basket components 须为 2-{BASKET_MAX_COMPONENTS} 个成分")
    seen = set()
    for c in comps:
        if not isinstance(c, dict) or c.get("kind") not in ("sge", "fut"):
            raise ValueError(f"{ts_code}: 成分 kind 只支持 sge/fut(禁嵌套 basket): "
                             f"{(c or {}).get('kind')!r}")
        if not c.get("symbol"):
            raise ValueError(f"{ts_code}: 成分 symbol 必填")
        if c["symbol"] in seen:
            raise ValueError(f"{ts_code}: 成分 symbol 重复: {c['symbol']}")
        seen.add(c["symbol"])
        if not c.get("label"):
            raise ValueError(f"{ts_code}: 成分 label 必填: {c['symbol']}")
        w = c.get("weight")
        if not isinstance(w, (int, float)) or w <= 0:
            raise ValueError(f"{ts_code}: 成分 weight 须为正数: {c['symbol']}={w!r}")
    total = sum(c["weight"] for c in comps)
    if abs(total - 1) > BASKET_WEIGHT_TOL:
        raise ValueError(f"{ts_code}: basket 权重和须为 1±{BASKET_WEIGHT_TOL}: {total}")
    out = dict(driver)
    out["components"] = [{**c, "weight": c["weight"] / total} for c in comps]
    return out


def load_targets(path: str = TARGETS_PATH) -> dict:
    """读标的配置, 缺省字段补 TARGET_DEFAULTS; 缺 name 抛 ValueError;
    model.kind 未实现时也抛 ValueError. driver 是 commodity_pe 专属:
    其余模型禁带(防语义混淆), 归一化为 None."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for ts_code, cfg in raw.items():
        merged = dict(TARGET_DEFAULTS)
        merged.update(cfg)
        if "name" not in merged:
            raise ValueError(f"{ts_code}: 配置缺 name 字段")
        # 估值模型分发: 缺省补 commodity_pe, 未实现 kind 提前报错
        model = merged.get("model") or {}
        kind = model.get("kind", "commodity_pe")
        if kind not in MODEL_RUNNERS:
            raise ValueError(f"{ts_code}: 未实现的估值模型 kind={kind} "
                             f"(可选: {sorted(MODEL_RUNNERS)})")
        merged["model"] = {"kind": kind, "brief": model.get("brief", "")}
        merged["enabled"] = bool(merged.get("enabled", True))
        # earnings_model 是 commodity_pe 专属机械估法; 非法值报错, 非商品模型归一 rule
        em = merged.get("earnings_model") or "rule"
        if em not in ("rule", "revenue_margin"):
            raise ValueError(f"{ts_code}: 非法 earnings_model={em} (rule|revenue_margin)")
        if em == "revenue_margin" and kind != "commodity_pe":
            raise ValueError(f"{ts_code}: earnings_model=revenue_margin 仅 commodity_pe 可用")
        merged["earnings_model"] = em if kind == "commodity_pe" else "rule"
        # driver 是 commodity_pe 专属: 其余模型禁带(防语义混淆), 归一化为 None
        if kind == "commodity_pe":
            if not merged.get("driver"):
                raise ValueError(f"{ts_code}: commodity_pe 必须配置 driver")
            merged["driver"] = validate_driver(ts_code, merged["driver"])
        else:
            if merged.get("driver"):
                raise ValueError(f"{ts_code}: {kind} 不接受 driver 配置(商品驱动专属)")
            merged["driver"] = None
        out[ts_code] = merged
    return out


# ---------- 季度工具 ----------

_Q_END = {"1": "0331", "2": "0630", "3": "0930", "4": "1231"}
_Q_START = {"1": "0101", "2": "0401", "3": "0701", "4": "1001"}


def quarter_key(yyyymmdd: str) -> str:
    """'20260331' -> '2026Q1' (按月份归季)."""
    return f"{yyyymmdd[:4]}Q{(int(yyyymmdd[4:6]) + 2) // 3}"


def quarter_bounds(qkey: str) -> tuple[str, str]:
    y, q = qkey.split("Q")
    return y + _Q_START[q], y + _Q_END[q]


def prev_quarter(qkey: str) -> str:
    y, q = qkey.split("Q")
    return f"{int(y) - 1}Q4" if q == "1" else f"{y}Q{int(q) - 1}"


def year_quarters(year: str) -> list[str]:
    return [f"{year}Q{i}" for i in (1, 2, 3, 4)]


# ---------- 取数 (brze 代理, 禁系统代理) ----------

def _load_token() -> str:
    tok = os.getenv("TUSHARE_TOKEN")
    if tok:
        return tok.strip()
    tf = os.path.expanduser("~/.openclaw/.tushare-token")
    if os.path.exists(tf):
        with open(tf, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    raise RuntimeError("未找到 tushare token: 设 TUSHARE_TOKEN 或写 ~/.openclaw/.tushare-token")


def fetch_tushare(api_name: str, params: dict, fields: str,
                  timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """POST brze /{api_name}. 返回 list[dict]; code!=0 抛 RuntimeError."""
    body = json.dumps({"api_name": api_name, "token": _load_token(),
                       "params": params, "fields": fields}).encode("utf-8")
    req = urllib.request.Request(
        f"{TUSHARE_HTTP_URL}/{api_name}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if out.get("code") != 0:
        raise RuntimeError(f"tushare {api_name}: {out.get('msg')}")
    data = out["data"]
    return [dict(zip(data["fields"], it)) for it in data["items"]]


def _rmcp_rpc(payload: dict, timeout: int = 120):
    """research-mcp JSON-RPC over SSE. 公网域名, 清代理直连; 失败直接抛不重试."""
    req = urllib.request.Request(
        RESEARCH_MCP_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw = opener.open(req, timeout=timeout).read().decode("utf-8")
    for line in raw.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


def _consensus_rows(resp, bare: str) -> list:
    """兼容 {data:{code:{columns,data}}} 与 {columns,data} 两种返回形状."""
    if not isinstance(resp, dict):
        return []
    d = resp.get("data")
    if isinstance(d, dict):
        blk = d.get(bare) or {}
        return blk.get("data") or []
    if isinstance(d, list):
        return d
    return []


def fetch_consensus(bare_code: str, rpc=_rmcp_rpc) -> dict:
    """research-mcp 一致预期: TTM净利500天日度序列 + 最新FY1/FY2 旁证.

    返回 {"series": [(yyyymmdd, profit_yi)升序], "fy": {...}|None}.
    序列为空/远程异常抛 RuntimeError(调用方决定缓存兜底); 绝不重试(Doris 铁律)。
    坑: 代码必须裸数字, 带 .SH 后缀静默返回空。
    """
    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "valuation", "version": "1"}}})

    def tool(name, args, rid):
        r = rpc({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                 "params": {"name": name, "arguments": args}})
        res = (r or {}).get("result") or {}
        sc = res.get("structuredContent")
        if sc is not None:
            return sc
        cont = res.get("content") or []
        if cont and cont[0].get("type") == "text":
            return json.loads(cont[0]["text"])
        return res

    g = tool("get_stock_consensus_growth",
             {"stock_code": bare_code, "field_name": "par_net_profit"}, 2)
    series = sorted(
        (str(r[0]).replace("-", ""), round(r[3] / CONSENSUS_UNIT_DIV, 2))
        for r in _consensus_rows(g, bare_code)
        if r and len(r) >= 4 and r[3])
    if not series:
        raise RuntimeError(f"一致预期序列为空: {bare_code}")
    fy = None
    try:                                 # FY 旁证 fail-soft, 序列才是硬依赖
        s = tool("get_stock_consensus", {"stock_code": bare_code}, 3)
        net = [r for r in _consensus_rows(s, bare_code)
               if r and len(r) >= 5 and str(r[2]) == "001014"]
        if net:
            last = max(net, key=lambda r: str(r[0]))
            fy = {"date": str(last[0]).replace("-", ""),
                  "fy1_yi": round(last[3] / CONSENSUS_UNIT_DIV, 2) if last[3] else None,
                  "fy2_yi": round(last[4] / CONSENSUS_UNIT_DIV, 2) if last[4] else None}
    except Exception:
        fy = None
    return {"series": series, "fy": fy}


def fetch_income_singles(ts_code: str, start_date: str = "20200101",
                         fetch=fetch_tushare) -> list[dict]:
    """income 累计净利/营收 -> 单季差分. 升序
    {'quarter','end_date','ann_date','profit_yi','revenue_yi','margin'}.

    PIT 口径: 每报告期取最早公告(去重, 忽略后续更正); 非Q1单季可知时点 =
    max(本季ann, 上季ann) — 两份累计都公告了差值才可知. 上季累计缺失则跳过该季.
    revenue_yi/margin 供 revenue_margin 模型用; 缺 revenue 置 None(不影响 profit,
    也让老 fixture/无营收标的透明回退 rule).
    """
    rows = fetch("income", {"ts_code": ts_code, "start_date": start_date},
                 "end_date,ann_date,report_type,n_income_attr_p,revenue")
    first: dict[str, tuple] = {}   # end_date -> (最早ann, 累计净利亿, 累计营收亿|None)
    for r in rows:
        if r.get("n_income_attr_p") is None:
            continue
        if str(r.get("report_type") or "1") != "1":
            continue
        ed, ann = r["end_date"], r.get("ann_date") or "99999999"
        if ed not in first or ann < first[ed][0]:
            rev = r.get("revenue")
            first[ed] = (ann, r["n_income_attr_p"] / 1e8,
                         rev / 1e8 if rev is not None else None)
    singles = []
    for ed in sorted(first):
        q = quarter_key(ed)
        ann, cum, cum_rev = first[ed]
        if q.endswith("Q1"):
            profit, rev = cum, cum_rev
        else:
            _, prev_end = quarter_bounds(prev_quarter(q))
            if prev_end not in first:
                continue
            p_ann, p_cum, p_rev = first[prev_end]
            profit = cum - p_cum
            rev = (cum_rev - p_rev if cum_rev is not None and p_rev is not None
                   else None)
            ann = max(ann, p_ann)
        margin = round(profit / rev, 4) if (rev and rev > 0) else None
        singles.append({"quarter": q, "end_date": ed, "ann_date": ann,
                        "profit_yi": round(profit, 2),
                        "revenue_yi": round(rev, 2) if rev is not None else None,
                        "margin": margin})
    return singles


def fetch_driver_series(driver: dict, start_date: str, end_date: str,
                        fetch=fetch_tushare) -> list[tuple[str, float]]:
    """驱动商品日线收盘, 升序 (yyyymmdd, close). kind: sge|fut."""
    api = "sge_daily" if driver["kind"] == "sge" else "fut_daily"
    rows = fetch(api, {"ts_code": driver["symbol"],
                       "start_date": start_date, "end_date": end_date},
                 "trade_date,close")
    return sorted((r["trade_date"], r["close"]) for r in rows if r.get("close"))


def build_driver_series(driver: dict, start_date: str, end_date: str,
                        fetch=fetch_tushare):
    """驱动序列统一入口: 单 driver -> (series, None); basket -> (指数序列, basket_meta).

    basket 合成(基期归一加权指数, 量纲无关):
    - 逐成分取序列, 任一成分为空抛 RuntimeError(宁整带 stale 不静默丢成分致权重漂移);
    - 交易日取交集(自然处理停牌/日历错配, 不插值), 交集<BASKET_MIN_DAYS 抛错;
    - 基期 = 交集最早 base_days(默认20) 个交易日各成分均价 base_i;
    - idx(d) = BASKET_BASE_VALUE × Σ w_i × price_i(d)/base_i。
    已知取舍: daily_basic 深度回填使取数窗口前移会令基期漂移(指数绝对值跳变);
    g因子/driver_shift/带子全是比值不受影响, agent 锚 TTL+clamp±10% 双保险,
    basket_meta 落 base_start/base 供审计, 不引入 pin 基期日复杂度。
    basket_meta.components 各成分带真实价格(base/close/center20/avg5),
    供页面与 agent 理解无量纲指数背后的真实行情。
    """
    if driver.get("kind") != "basket":
        return fetch_driver_series(driver, start_date, end_date, fetch), None
    comps = driver["components"]
    series_list = []
    for comp in comps:
        s = fetch_driver_series(comp, start_date, end_date, fetch)
        if not s:
            raise RuntimeError(f"篮子成分 {comp['symbol']} 价格序列为空")
        series_list.append(s)
    dates = sorted(set.intersection(*[{d for d, _ in s} for s in series_list]))
    if len(dates) < BASKET_MIN_DAYS:
        raise RuntimeError(f"篮子成分交集交易日不足: {len(dates)}<{BASKET_MIN_DAYS}")
    base_days = int(driver.get("base_days") or BASKET_BASE_DAYS)
    base_dates = dates[:base_days]
    maps = [dict(s) for s in series_list]
    meta_comps = []
    bases = []
    for comp, m, s in zip(comps, maps, series_list):
        base = sum(m[d] for d in base_dates) / len(base_dates)
        bases.append(base)
        meta_comps.append({
            "kind": comp["kind"], "symbol": comp["symbol"],
            "label": comp["label"], "weight": round(comp["weight"], 4),
            "base": round(base, 2), "close": s[-1][1],
            "center20": rolling_center(s, end_date, 20),
            "avg5": rolling_center(s, end_date, 5),
        })
    idx = [(d, round(BASKET_BASE_VALUE * sum(
        comp["weight"] * m[d] / base
        for comp, m, base in zip(comps, maps, bases)), 4)) for d in dates]
    meta = {"base_start": base_dates[0], "base_days": len(base_dates),
            "n_days": len(dates), "components": meta_comps}
    return idx, meta


# ---------- 序列辅助 ----------

def rolling_center(series, asof: str, n: int = 20):
    """截至 asof 的近 n 个有价日均值; 无数据 None."""
    vals = [v for d, v in series if d <= asof][-n:]
    return round(sum(vals) / len(vals), 2) if vals else None


def series_range(series, asof: str, n: int = 60):
    vals = [v for d, v in series if d <= asof][-n:]
    return (min(vals), max(vals)) if vals else (None, None)


def quarter_avg(series, qkey: str):
    s, e = quarter_bounds(qkey)
    vals = [v for d, v in series if s <= d <= e]
    return sum(vals) / len(vals) if vals else None


# ---------- 季度盈利区间 ----------

def rule_estimate(singles, gold, asof: str, tolerance: float, driver_center=None):
    """规则法单季估计: [P_lo×g×(1-δ), P_hi×g×(1+δ)].

    P_lo/P_hi = 截至 asof 已知的最近两个单季净利的低/高者;
    g = 商品价中枢(生效值或近20日均) ÷ 参照两季各自季均价的均值.
    金价横盘 g≈1 即"大概率不超过前两季". 返回 (lo, hi, basis) 或 None(样本<2).
    """
    known = [s for s in singles if s["ann_date"] <= asof]
    if len(known) < 2:
        return None
    last2 = known[-2:]
    p_lo = min(s["profit_yi"] for s in last2)
    p_hi = max(s["profit_yi"] for s in last2)
    refs = [quarter_avg(gold, s["quarter"]) for s in last2]
    refs = [r for r in refs if r]
    center = driver_center if driver_center is not None else rolling_center(gold, asof)
    g = round(center / (sum(refs) / len(refs)), 4) if (center and refs) else 1.0
    lo, hi = p_lo * g * (1 - tolerance), p_hi * g * (1 + tolerance)
    if lo > hi:                       # 负利润时乘子反向, 保证 lo<=hi
        lo, hi = hi, lo
    basis = {"method": "rule", "ref_quarters": [s["quarter"] for s in last2],
             "p_lo": p_lo, "p_hi": p_hi, "g": g, "tolerance": tolerance}
    return round(lo, 2), round(hi, 2), basis


def revenue_margin_estimate(singles, gold, asof: str, tolerance: float,
                            driver_center=None, *, smooth_k: int = 4,
                            margin_lookback: int = 4):
    """营收×净利率单季估计: 隐含量(营收÷价) × 当前价 × 净利率.

    抓住 rule_estimate 漏掉的经营杠杆(净利率随价/规模扩张; g 规则法只缩放净利).
    PIT: 只用 ann_date<=asof. 隐含量=revenue_yi/季均价(销量代理; 篮子driver则为
    每指数点营收, 量纲自洽因同一价乘回), 近 smooth_k 季均值消噪. 区间 =
    隐含量 × 价[lo,hi](近60日) × 净利率[lo,hi](近margin_lookback季) × (1±tol).

    返回 (lo, hi, basis) 或 None(→调用方回退 rule): 已知季<2 / 有效(营收+价)季<
    smooth_k / 当前价缺 / min 净利率<0(穿零深周期让保守 rule 兜底).
    """
    known = [s for s in singles if s["ann_date"] <= asof]
    if len(known) < 2:
        return None
    vols = []                      # 隐含量 = 单季营收 ÷ 该季商品季均价
    for s in known:
        rev, pavg = s.get("revenue_yi"), quarter_avg(gold, s["quarter"])
        if rev is not None and pavg:
            vols.append(rev / pavg)
    if len(vols) < smooth_k:
        return None
    vol_smooth = sum(vols[-smooth_k:]) / smooth_k
    margins = [s["margin"] for s in known[-margin_lookback:]
               if s.get("margin") is not None]
    if not margins:
        return None
    m_lo, m_hi = min(margins), max(margins)
    if m_lo < 0:                   # 穿越盈亏零点 → 交给保守 rule
        return None
    center = (driver_center if driver_center is not None
              else rolling_center(gold, asof, 20))
    plo, phi = series_range(gold, asof, 60)
    if not center or plo is None or phi is None:
        return None
    lo = vol_smooth * plo * m_lo * (1 - tolerance)
    hi = vol_smooth * phi * m_hi * (1 + tolerance)
    if lo > hi:                    # margin/价反向时保证 lo<=hi
        lo, hi = hi, lo
    basis = {"method": "revenue_margin",
             "ref_quarters": [s["quarter"] for s in known[-smooth_k:]],
             "vol_smooth": round(vol_smooth, 4), "price_center": center,
             "price_range": [plo, phi], "margin_lo": m_lo, "margin_hi": m_hi,
             "tolerance": tolerance}
    return round(lo, 2), round(hi, 2), basis


def load_events(c, ts_code: str) -> list[dict]:
    """earnings_events(预告/快报, 累计口径万元) -> 累计区间(亿). 升序按 ann_date.

    返回全量记录, PIT 过滤(ann_date<=asof)由调用方负责."""
    out = []
    for r in c.execute(
        "SELECT ann_date, end_date, net_profit_min, net_profit_max "
        "FROM earnings_events WHERE ts_code=? AND end_date IS NOT NULL "
        "AND net_profit_min IS NOT NULL ORDER BY ann_date", (ts_code,)):
        lo = r["net_profit_min"] / 1e4
        hi = (r["net_profit_max"] / 1e4) if r["net_profit_max"] is not None else lo
        out.append({"quarter": quarter_key(r["end_date"]), "ann_date": r["ann_date"],
                    "cum_lo_yi": round(lo, 2), "cum_hi_yi": round(hi, 2)})
    return out


def build_year_quarters(year: str, singles, events, gold, asof: str, tolerance: float,
                        overrides=None, agent_adj=None, driver_center=None, *,
                        earnings_model="rule"):
    """当年 4 季盈利区间, 全部只用截至 asof 可知的信息(PIT).

    优先级: actual(income差分) > forecast(累计预告差分, 要求当年更早各季全 actual)
            > 手动 overrides > agent_adj > 机械估(earnings_model).
    earnings_model: 'rule'(默认, g规则法) | 'revenue_margin'(营收×净利率, 缺数据自动回退rule).
    返回 [{'quarter','status','lo','hi','basis'}] 或 None(机械估样本不足).
    """
    overrides, agent_adj = overrides or {}, agent_adj or {}
    actual = {s["quarter"]: s for s in singles if s["ann_date"] <= asof}
    ev_pit = [e for e in events if e["ann_date"] <= asof]
    out = []
    for q in year_quarters(year):
        if q in actual:
            p = actual[q]["profit_yi"]
            out.append({"quarter": q, "status": "actual", "lo": p, "hi": p,
                        "basis": {"ann_date": actual[q]["ann_date"]}})
            continue
        evs = [e for e in ev_pit if e["quarter"] == q]
        if evs and all(x["status"] == "actual" for x in out):
            e = max(evs, key=lambda x: x["ann_date"])   # 同季多份预告取最新
            base_lo = sum(x["lo"] for x in out)
            base_hi = sum(x["hi"] for x in out)
            out.append({"quarter": q, "status": "forecast",
                        "lo": round(e["cum_lo_yi"] - base_lo, 2),
                        "hi": round(e["cum_hi_yi"] - base_hi, 2),
                        "basis": {"ann_date": e["ann_date"],
                                  "cum": [e["cum_lo_yi"], e["cum_hi_yi"]]}})
            continue
        if q in overrides:
            lo, hi = overrides[q]
            out.append({"quarter": q, "status": "estimated", "lo": lo, "hi": hi,
                        "basis": {"method": "manual"}})
            continue
        if q in agent_adj:
            lo, hi = agent_adj[q]
            out.append({"quarter": q, "status": "estimated", "lo": lo, "hi": hi,
                        "basis": {"method": "agent"}})
            continue
        est = None
        if earnings_model == "revenue_margin":
            est = revenue_margin_estimate(singles, gold, asof, tolerance,
                                          driver_center)
        if est is None:                       # 未选中或数据不足 → 回退 rule
            est = rule_estimate(singles, gold, asof, tolerance, driver_center)
        if est is None:
            return None
        lo, hi, basis = est
        out.append({"quarter": q, "status": "estimated", "lo": lo, "hi": hi,
                    "basis": basis})
    return out


def actual_forecast_quarters(singles, events, asof: str, year: str) -> list[dict]:
    """当年"已披露实际季(income差分) + 业绩预告季(累计预告差分)", 不含估计季.

    模型无关的业绩明细(展示用): 非商品模型没有商品驱动逐季推, 只呈现事实
    (实际)与公司指引(预告), 绝不外推/猜测。遇到既非 actual 又非可差分 forecast
    的季即停(未来季留白)。与 build_year_quarters 的前两分支同口径。
    """
    actual = {s["quarter"]: s for s in singles if s["ann_date"] <= asof}
    ev_pit = [e for e in events if e["ann_date"] <= asof]
    out = []
    for q in year_quarters(year):
        if q in actual:
            p = actual[q]["profit_yi"]
            out.append({"quarter": q, "status": "actual", "lo": p, "hi": p,
                        "basis": {"ann_date": actual[q]["ann_date"]}})
            continue
        evs = [e for e in ev_pit if e["quarter"] == q]
        if evs and all(x["status"] == "actual" for x in out):
            e = max(evs, key=lambda x: x["ann_date"])
            base_lo, base_hi = sum(x["lo"] for x in out), sum(x["hi"] for x in out)
            out.append({"quarter": q, "status": "forecast",
                        "lo": round(e["cum_lo_yi"] - base_lo, 2),
                        "hi": round(e["cum_hi_yi"] - base_hi, 2),
                        "basis": {"ann_date": e["ann_date"],
                                  "cum": [e["cum_lo_yi"], e["cum_hi_yi"]]}})
            continue
        break                       # 未披露/不可差分 → 停, 不猜估计季
    return out


def _write_display_quarters(c, ts_code: str, asof: str, fetch) -> None:
    """非商品模型: 落 实际单季(income) + 当年业绩预告季(events) 到 valuation_quarter_est
    (统一业绩明细展示用)。fail-soft: 取数失败不阻断带子。"""
    try:
        singles = fetch_income_singles(ts_code, fetch=fetch)
    except Exception as e:                          # noqa: BLE001
        print(f"[valuation] {ts_code} 业绩明细取数失败(跳过): {e}", file=sys.stderr)
        return
    if not singles:
        return
    events = load_events(c, ts_code)
    forecast = [q for q in actual_forecast_quarters(singles, events, asof, asof[:4])
                if q["status"] == "forecast"]
    _upsert_quarters(c, ts_code, singles, forecast)


# ---------- PIT 动态 PE 与分位带 ----------

def pit_pe_series(basics, singles, events, gold, tolerance: float, min_annual=None,
                  *, earnings_model="rule"):
    """时点正确的动态 PE 序列(无未来函数, PE带分位的基础).

    basics: [(trade_date, close, total_mv万元)] 升序.
    每日 d: 当年四季区间只用 ann_date<=d 的财报/预告 + 截至 d 的商品价;
    年化盈利取区间中点; pe = total_mv / (annual_mid亿 × 1e4万元).

    min_annual(亿): 非 None 时剔除估计年化盈利<该值的日子——周期股盈利塌陷期
    E→0 会使 PE 畸高, 这些日的 PE 与当前盈利水平不可比, 混入会污染分位带上沿。
    """
    out = []
    for td, _close, total_mv in basics:
        qs = build_year_quarters(td[:4], singles, events, gold, td, tolerance,
                                 earnings_model=earnings_model)
        if not qs or not total_mv:
            continue
        annual_mid = sum((q["lo"] + q["hi"]) / 2 for q in qs)
        if annual_mid <= 0:
            continue
        if min_annual is not None and annual_mid < min_annual:
            continue
        out.append((td, round(total_mv / (annual_mid * 1e4), 3)))
    return out


def quantile(vals, q: float):
    """线性插值分位. 空列表 None."""
    s = sorted(vals)
    if not s:
        return None
    pos = (len(s) - 1) * q
    i = int(pos)
    if i + 1 >= len(s):
        return float(s[-1])
    return round(s[i] + (s[i + 1] - s[i]) * (pos - i), 4)


def regress_gold_profit(singles, gold, n: int = 12):
    """近 n 季 '季均商品价 vs 单季净利' OLS 旁证(只展示不入带). 样本<6 返回 None.

    前提: singles 已按季度升序(fetch_income_singles 的返回即是)。
    """
    pts = [(quarter_avg(gold, s["quarter"]), s["profit_yi"]) for s in singles[-n:]]
    pts = [(x, y) for x, y in pts if x]
    if len(pts) < 6:
        return None
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    if sxx == 0:
        return None
    syy = sum((y - my) ** 2 for _, y in pts)
    r2 = (sxy * sxy) / (sxx * syy) if syy else None
    return {"beta": round(sxy / sxx, 4),
            "r2": round(r2, 3) if r2 is not None else None, "n": len(pts)}


def tech_facts(c, ts_code: str, asof: str):
    """技术面事实(喂 agent 上下文与页面; 只算客观事实不作判断).

    均线/20日乖离/20日高低位/距60日高/5·10日动量/量比/近5日日均振幅.
    缺 daily 表或样本 <20 返回 None(fail-soft, 不阻断带子计算)."""
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT trade_date, high, low, close, pre_close, vol FROM daily "
            "WHERE ts_code=? AND trade_date<=? AND close IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 60", (ts_code, asof))][::-1]
    except sqlite3.OperationalError:
        return None
    if len(rows) < 20:
        return None
    closes = [r["close"] for r in rows]
    close = closes[-1]

    def ma(n):
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

    lo20 = min((r["low"] for r in rows[-20:] if r["low"] is not None), default=None)
    hi20 = max((r["high"] for r in rows[-20:] if r["high"] is not None), default=None)
    hi60 = max((r["high"] for r in rows if r["high"] is not None), default=None)
    amp5 = [(r["high"] - r["low"]) / r["pre_close"]
            for r in rows[-5:] if r["high"] and r["low"] and r["pre_close"]]
    vols5 = [r["vol"] for r in rows[-5:] if r["vol"]]
    vols20 = [r["vol"] for r in rows[-20:] if r["vol"]]
    return {
        "close": close,
        "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma60": ma(60),
        "bias20": round(close / ma(20) - 1, 4) if ma(20) else None,
        "pos20": (round((close - lo20) / (hi20 - lo20), 4)
                  if lo20 is not None and hi20 is not None and hi20 > lo20 else None),
        "dist_hi60": round(close / hi60 - 1, 4) if hi60 else None,
        "ret5": round(close / closes[-6] - 1, 4) if len(closes) >= 6 else None,
        "ret10": round(close / closes[-11] - 1, 4) if len(closes) >= 11 else None,
        "vol_ratio5_20": (round(sum(vols5) / len(vols5)
                                / (sum(vols20) / len(vols20)), 3)
                          if vols5 and vols20 else None),
        "amp5_avg": round(sum(amp5) / len(amp5), 4) if amp5 else None,
    }


# ---------- agent 建议 sanity 钳制 (writer 与引擎共用) ----------

def clamp_agent(payload: dict, ctx: dict):
    """钳制 agent 锚点建议, 防离谱(教训: 投顾机械收口).

    ctx = valuation_band_daily.meta_json['clamp_ctx'].
    返回 (钳后 payload, clamped 记录 dict). 规则:
    - 商品价三值限 [driver_min120×0.9, driver_max120×1.1]
    - profit_adj 各季限规则法区间的 [0.5x, 1.5x]; 非 estimated 季直接丢弃
    - profit_adj.anchor 限机械锚净利 [0.7x, 1.3x](非商品模型); ctx 缺基准则丢弃
    - pe_lo_adj/pe_hi_adj 限 [pe_p05, pe_p95]
    - st_width_pct 绝对钳 [CLAMP_ST_WIDTH_LO, CLAMP_ST_WIDTH_HI]
    - st_center_adj 限机械值 ±CLAMP_ST_CENTER_PAD; 缺机械基准则置 None
    """
    p, clamped = dict(payload), {}
    lo120, hi120 = ctx.get("driver_min120"), ctx.get("driver_max120")
    if lo120 is not None and hi120 is not None:
        b_lo, b_hi = lo120 * (1 - CLAMP_DRIVER_PAD), hi120 * (1 + CLAMP_DRIVER_PAD)
        for k in ("driver_center", "driver_low", "driver_high"):
            v = p.get(k)
            if v is not None and not b_lo <= v <= b_hi:
                clamped[k] = v
                p[k] = round(min(max(v, b_lo), b_hi), 2)
    rq = ctx.get("rule_quarters") or {}
    anchor_mech = ctx.get("anchor_profit_mech")
    kept = {}
    for q, pair in (p.get("profit_adj") or {}).items():
        if q == "anchor":
            if not anchor_mech:
                clamped["profit_adj.anchor"] = "dropped(该模型无锚修正基准)"
                continue
            b_lo = min(CLAMP_ANCHOR_LO * anchor_mech, CLAMP_ANCHOR_HI * anchor_mech)
            b_hi = max(CLAMP_ANCHOR_LO * anchor_mech, CLAMP_ANCHOR_HI * anchor_mech)
        elif q not in rq:
            clamped[f"profit_adj.{q}"] = "dropped(非estimated季)"
            continue
        else:
            r_lo, r_hi = rq[q]
            b_lo = min(CLAMP_PROFIT_LO * r_lo, CLAMP_PROFIT_HI * r_lo)   # 兼容负值
            b_hi = max(CLAMP_PROFIT_LO * r_hi, CLAMP_PROFIT_HI * r_hi)
        lo, hi = pair
        lo2 = round(min(max(lo, b_lo), b_hi), 2)
        hi2 = round(min(max(hi, b_lo), b_hi), 2)
        if hi2 < lo2:
            lo2, hi2 = hi2, lo2
        if [lo2, hi2] != [lo, hi]:
            clamped[f"profit_adj.{q}"] = pair
        kept[q] = [lo2, hi2]
    p["profit_adj"] = kept or None
    p05, p95 = ctx.get("pe_p05"), ctx.get("pe_p95")
    if p05 is not None and p95 is not None:
        for k in ("pe_lo_adj", "pe_hi_adj"):
            v = p.get(k)
            if v is not None and not p05 <= v <= p95:
                clamped[k] = v
                p[k] = round(min(max(v, p05), p95), 3)
    # 短期带宽钳制: 绝对笼 [3%, 15%]
    v = p.get("st_width_pct")
    if v is not None and not CLAMP_ST_WIDTH_LO <= v <= CLAMP_ST_WIDTH_HI:
        clamped["st_width_pct"] = v
        p["st_width_pct"] = round(min(max(v, CLAMP_ST_WIDTH_LO),
                                      CLAMP_ST_WIDTH_HI), 4)
    # 短期中枢修正钳: 机械值 ±8%; 缺机械基准则丢弃(置 None)
    v = p.get("st_center_adj")
    if v is not None:
        mech = ctx.get("st_center_mech")
        if not mech:
            clamped["st_center_adj"] = "dropped(缺st机械基准)"
            p["st_center_adj"] = None
        else:
            b_lo = mech * (1 - CLAMP_ST_CENTER_PAD)
            b_hi = mech * (1 + CLAMP_ST_CENTER_PAD)
            if not b_lo <= v <= b_hi:
                clamped["st_center_adj"] = v
                p["st_center_adj"] = round(min(max(v, b_lo), b_hi), 2)
    return p, clamped


# ---------- 有效锚点 + 每日落库 ----------

def latest_agent_view(c, ts_code: str, today: str):
    """保鲜期内最新'深度研判'行(排除 daily_st 轻量行, 其长期锚点字段恒空)."""
    r = c.execute(
        "SELECT * FROM valuation_agent_view WHERE ts_code=? AND valid_until>=? "
        "AND trigger!='daily_st' ORDER BY run_at DESC LIMIT 1",
        (ts_code, today)).fetchone()
    return dict(r) if r else None


def latest_st_view(c, ts_code: str, today: str):
    """保鲜期内最新含短期带修正的行(daily_st 或深度研判均可)."""
    r = c.execute(
        "SELECT * FROM valuation_agent_view WHERE ts_code=? AND valid_until>=? "
        "AND (st_center_adj IS NOT NULL OR st_width_pct IS NOT NULL) "
        "ORDER BY run_at DESC LIMIT 1", (ts_code, today)).fetchone()
    return dict(r) if r else None


def _upsert_quarters(c, ts_code: str, singles, quarters) -> None:
    """单季明细落库. 只在内容变化时更新(updated_at 是 agent earnings 触发器的依据)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = ([{"quarter": s["quarter"], "status": "actual", "lo": s["profit_yi"],
              "hi": s["profit_yi"], "basis": {"ann_date": s["ann_date"]}}
             for s in singles]
            + [{"quarter": q["quarter"], "status": q["status"], "lo": q["lo"],
                "hi": q["hi"], "basis": q["basis"]} for q in quarters])
    for r in rows:
        old = c.execute(
            "SELECT status, profit_lo, profit_hi FROM valuation_quarter_est "
            "WHERE ts_code=? AND quarter=?", (ts_code, r["quarter"])).fetchone()
        if old and (old["status"], old["profit_lo"], old["profit_hi"]) == \
                (r["status"], r["lo"], r["hi"]):
            continue
        c.execute(
            "INSERT OR REPLACE INTO valuation_quarter_est "
            "(ts_code, quarter, status, profit_lo, profit_hi, basis_json, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts_code, r["quarter"], r["status"], r["lo"], r["hi"],
             json.dumps(r["basis"], ensure_ascii=False), now))


def _mark_stale(c, ts_code: str, err: str) -> None:
    """取数/计算失败: 若 daily_basic 已有更新交易日, 把最近带子复制过去并标 stale."""
    last = c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                     "ORDER BY trade_date DESC LIMIT 1", (ts_code,)).fetchone()
    td = c.execute("SELECT MAX(trade_date) FROM daily_basic WHERE ts_code=?",
                   (ts_code,)).fetchone()[0]
    if not last or not td or last["trade_date"] >= td:
        return
    meta = json.loads(last["meta_json"] or "{}")
    meta["stale"] = {"from": last["trade_date"], "error": err[:200]}
    c.execute(
        "INSERT OR REPLACE INTO valuation_band_daily (trade_date, ts_code, "
        "driver_center, driver_low, driver_high, driver_source, profit_year_lo, "
        "profit_year_hi, eps_lo, eps_hi, earnings_source, pe_lo, pe_hi, pe_source, "
        "price_lo, price_hi, close, band_pos, st_center, st_lo, st_hi, st_pos, "
        "st_source, meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (td, ts_code, last["driver_center"], last["driver_low"], last["driver_high"],
         last["driver_source"], last["profit_year_lo"], last["profit_year_hi"],
         last["eps_lo"], last["eps_hi"], last["earnings_source"], last["pe_lo"],
         last["pe_hi"], last["pe_source"], last["price_lo"], last["price_hi"],
         last["close"], last["band_pos"], last["st_center"], last["st_lo"],
         last["st_hi"], last["st_pos"], last["st_source"],
         json.dumps(meta, ensure_ascii=False)))


def _finalize_band(c, ts_code: str, cfg: dict, fetch, basics, ratio_series,
                   agent, anchor_lo: float, anchor_hi: float, *,
                   profit_year=(None, None), earnings_source=None,
                   driver_vals=None, singles=None, extra_meta=None,
                   extra_clamp_ctx=None, min_samples: int = MIN_HISTORY) -> None:
    """模型无关的带子收尾(四模型共用): 比率分位带(agent修正) -> 价格带/band_pos
    -> 短期做T带 -> 市值股息 -> meta -> INSERT valuation_band_daily.

    ratio_series 按各模型口径构造, 落库列名沿用 pe_*; anchor 是每股锚(元/股,
    commodity=EPS区间, pb_band=BPS点值); driver_vals/singles 仅商品模型传。
    min_samples: 比率样本下限, commodity 走 regime 过滤后传 REGIME_MIN_HISTORY。
    """
    today, close, total_mv = basics[-1]
    # 非商品模型: 落 实际+预告 业绩明细(展示用); 商品模型已在 runner 内 _upsert_quarters
    if cfg["model"]["kind"] != "commodity_pe":
        _write_display_quarters(c, ts_code, today, fetch)
    pes = ratio_series
    if len(pes) < min_samples:
        raise RuntimeError(f"估值比率样本不足: {len(pes)}<{min_samples}")
    vals = [p for _, p in pes]
    pe_lo_q, pe_hi_q = quantile(vals, cfg["pe_lo_q"]), quantile(vals, cfg["pe_hi_q"])
    p05, p95 = quantile(vals, CLAMP_PE_QLO), quantile(vals, CLAMP_PE_QHI)
    pe_src, pe_lo, pe_hi = "quantile", pe_lo_q, pe_hi_q
    if agent and (agent.get("pe_lo_adj") is not None
                  or agent.get("pe_hi_adj") is not None):
        pe_src = "agent"
        pe_lo = agent["pe_lo_adj"] if agent.get("pe_lo_adj") is not None else pe_lo_q
        pe_hi = agent["pe_hi_adj"] if agent.get("pe_hi_adj") is not None else pe_hi_q

    # 价格带用锚中点(单锚): 比率分位宽度已表达估值波动, 再与锚区间宽度外积会双重
    # 复合放大; 周期股更把"高PE×高E"这个现实中不同时出现的角当上沿。与短期带同口径。
    anchor_mid = (anchor_lo + anchor_hi) / 2
    price_lo, price_hi = pe_lo * anchor_mid, pe_hi * anchor_mid
    band_pos = ((close - price_lo) / (price_hi - price_lo)
                if price_hi > price_lo else None)

    # 短期做T带: 近60日比率振荡 baseline; agent 每日情绪研判可修中枢/半宽
    st_vals = [v for _, v in pes[-ST_PE_WINDOW:]]
    st_pe_med = quantile(st_vals, 0.5)
    st_q20 = quantile(st_vals, ST_PE_LO_Q)
    st_q80 = quantile(st_vals, ST_PE_HI_Q)
    st_center_mech = round(st_pe_med * anchor_mid, 2)
    w = (st_q80 - st_q20) / 2 / st_pe_med if st_pe_med else CLAMP_ST_WIDTH_LO
    st_width_mech = round(min(max(w, CLAMP_ST_WIDTH_LO), CLAMP_ST_WIDTH_HI), 4)
    st_src, st_center, st_width = "auto", st_center_mech, st_width_mech
    stv = latest_st_view(c, ts_code, today)
    if stv:
        st_src = "agent"
        if stv.get("st_center_adj") is not None:
            st_center = stv["st_center_adj"]
        if stv.get("st_width_pct") is not None:
            st_width = stv["st_width_pct"]
    st_lo = round(st_center * (1 - st_width), 2)
    st_hi = round(st_center * (1 + st_width), 2)
    st_pos = round((close - st_lo) / (st_hi - st_lo), 4) if st_hi > st_lo else None

    # 市值与股息: 远端 daily_basic 补 dv_ttm; 支付率段仅商品模型(需 singles+年化区间)
    annual_lo, annual_hi = profit_year
    mv_yi = round(total_mv / 1e4, 1)
    mv_meta = {"total_mv_yi": mv_yi}
    try:
        dvrows = fetch("daily_basic",
                       {"ts_code": ts_code,
                        "start_date": (datetime.strptime(today, "%Y%m%d")
                                       - timedelta(days=30)).strftime("%Y%m%d"),
                        "end_date": today},
                       "trade_date,dv_ttm,dv_ratio,pe_ttm")
        dvrows = sorted((r for r in dvrows if r.get("dv_ttm") is not None),
                        key=lambda r: r["trade_date"])
        mv_meta["dv_ttm"] = dvrows[-1]["dv_ttm"] if dvrows else None
        mv_meta["pe_ttm"] = dvrows[-1].get("pe_ttm") if dvrows else None
        if singles is not None and annual_lo is not None:
            div_by_year: dict[str, float] = {}             # 归属年度 -> 分红总额(亿)
            for r in fetch("dividend", {"ts_code": ts_code},
                           "end_date,div_proc,cash_div_tax,base_share,ann_date"):
                if (r.get("div_proc") == "实施" and r.get("cash_div_tax")
                        and r.get("base_share") and (r.get("ann_date") or "") <= today):
                    y = r["end_date"][:4]
                    div_by_year[y] = (div_by_year.get(y, 0)
                                      + r["cash_div_tax"] * r["base_share"] / 1e4)
            payout = payout_year = div_y = profit_y = None
            for y in sorted(div_by_year, reverse=True):
                qs = [s for s in singles
                      if s["quarter"].startswith(y) and s["ann_date"] <= today]
                if len(qs) == 4:
                    p = sum(s["profit_yi"] for s in qs)
                    if p > 0:
                        payout, payout_year = round(div_by_year[y] / p, 4), y
                        div_y, profit_y = round(div_by_year[y], 1), round(p, 1)
                        break
            fwd_lo = fwd_hi = None
            if payout:
                fwd_lo = round(payout * annual_lo / mv_yi * 100, 2)
                fwd_hi = round(payout * annual_hi / mv_yi * 100, 2)
            mv_meta.update({"payout": payout, "payout_year": payout_year,
                            "div_yi": div_y, "profit_yi": profit_y,
                            "fwd_dv_lo": fwd_lo, "fwd_dv_hi": fwd_hi})
    except Exception as e:
        mv_meta["error"] = str(e)[:200]

    meta = {
        "model_kind": cfg["model"]["kind"],
        "mv": mv_meta,
        "pe_quantile_raw": {"lo": pe_lo_q, "hi": pe_hi_q},
        "st": {"pe_med60": st_pe_med, "pe_q20": st_q20, "pe_q80": st_q80,
               "center_mech": st_center_mech, "width_mech": st_width_mech},
        "tech": tech_facts(c, ts_code, today),
        "clamp_ctx": {"pe_p05": p05, "pe_p95": p95,
                      "st_center_mech": st_center_mech,
                      **(extra_clamp_ctx or {})},
        "agent_run_at": agent["run_at"] if agent else None,
        "pe_n": len(pes),
        **(extra_meta or {}),
    }
    dv = driver_vals or {}
    c.execute(
        "INSERT OR REPLACE INTO valuation_band_daily (trade_date, ts_code, "
        "driver_center, driver_low, driver_high, driver_source, profit_year_lo, "
        "profit_year_hi, eps_lo, eps_hi, earnings_source, pe_lo, pe_hi, pe_source, "
        "price_lo, price_hi, close, band_pos, st_center, st_lo, st_hi, st_pos, "
        "st_source, meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (today, ts_code, dv.get("center"), dv.get("low"), dv.get("high"),
         dv.get("source"), annual_lo, annual_hi,
         round(anchor_lo, 3), round(anchor_hi, 3), earnings_source,
         round(pe_lo, 3), round(pe_hi, 3), pe_src,
         round(price_lo, 2), round(price_hi, 2), close,
         round(band_pos, 4) if band_pos is not None else None,
         round(st_center, 2), st_lo, st_hi, st_pos, st_src,
         json.dumps(meta, ensure_ascii=False)))


def _run_commodity_pe(c, ts_code: str, cfg: dict, fetch) -> None:
    """商品驱动周期股模型(一期链路): 商品价中枢 -> 季度盈利 -> PIT PE分位带 -> 价格带."""
    basics = [(r["trade_date"], r["close"], r["total_mv"]) for r in c.execute(
        "SELECT trade_date, close, total_mv FROM daily_basic WHERE ts_code=? "
        "AND close IS NOT NULL AND total_mv IS NOT NULL ORDER BY trade_date",
        (ts_code,))]
    if len(basics) < MIN_HISTORY:
        raise RuntimeError(f"daily_basic 历史不足: {len(basics)}<{MIN_HISTORY}")
    today = basics[-1][0]
    start = (datetime.strptime(basics[0][0], "%Y%m%d")
             - timedelta(days=900)).strftime("%Y%m%d")
    gold, basket_meta = build_driver_series(cfg["driver"], start, today, fetch)
    if not gold:
        raise RuntimeError("商品价序列为空")
    singles = fetch_income_singles(ts_code, start_date=start, fetch=fetch)
    if len(singles) < 2:
        raise RuntimeError(f"单季净利样本不足: {len(singles)}")
    events = load_events(c, ts_code)
    agent = latest_agent_view(c, ts_code, today)

    # 商品价锚: 手动 > agent > 自动
    auto_center = rolling_center(gold, today)
    auto_lo, auto_hi = series_range(gold, today, 60)
    drv_src, center, dlo, dhi = "auto", auto_center, auto_lo, auto_hi
    if agent and agent.get("driver_center"):
        drv_src, center = "agent", agent["driver_center"]
        dlo = agent.get("driver_low") or auto_lo
        dhi = agent.get("driver_high") or auto_hi
    if cfg.get("driver_override"):
        ov = cfg["driver_override"]
        drv_src, center = "manual", ov["center"]
        dlo, dhi = ov.get("low", auto_lo), ov.get("high", auto_hi)

    # 盈利: 手动/agent 只作用于 estimated 季 (build_year_quarters 内部保证)
    agent_adj = {}
    if agent and agent.get("profit_adj_json"):
        agent_adj = {q: tuple(v) for q, v in
                     json.loads(agent["profit_adj_json"]).items()}
    overrides = {q: tuple(v) for q, v in
                 (cfg.get("earnings_override") or {}).items()}
    em = cfg["earnings_model"]
    quarters = build_year_quarters(today[:4], singles, events, gold, today,
                                   cfg["rule_tolerance"], overrides, agent_adj, center,
                                   earnings_model=em)
    if quarters is None:
        raise RuntimeError("机械估样本不足")
    rule_q = build_year_quarters(today[:4], singles, events, gold, today,
                                 cfg["rule_tolerance"],       # 纯机械版(无override/agent)
                                 earnings_model=em)           # =钳制基准, 随 em 走
    _upsert_quarters(c, ts_code, singles, quarters)

    annual_lo = round(sum(q["lo"] for q in quarters), 2)
    annual_hi = round(sum(q["hi"] for q in quarters), 2)
    close, total_mv = basics[-1][1], basics[-1][2]
    shares = total_mv * 1e4 / close                            # 股数
    eps_lo, eps_hi = annual_lo * 1e8 / shares, annual_hi * 1e8 / shares

    # regime 过滤: 只保留 年化盈利>=FRAC×当前锚中点 的可比日, 剔盈利塌陷期畸高PE
    anchor_mid_yi = (annual_lo + annual_hi) / 2
    pes = pit_pe_series(basics[-cfg["pe_window"]:], singles, events, gold,
                        cfg["rule_tolerance"],
                        min_annual=COMMODITY_REGIME_FRAC * anchor_mid_yi,
                        earnings_model=em)
    if len(pes) < REGIME_MIN_HISTORY:
        raise RuntimeError(f"可比regime PIT PE 样本不足: {len(pes)}<{REGIME_MIN_HISTORY}")

    d120lo, d120hi = series_range(gold, today, 120)
    earnings_source = ("manual" if overrides else "agent" if agent_adj
                       else "revenue_margin" if em == "revenue_margin" else "rule")
    _finalize_band(
        c, ts_code, cfg, fetch, basics, pes, agent,
        eps_lo, eps_hi, profit_year=(annual_lo, annual_hi),
        earnings_source=earnings_source, min_samples=REGIME_MIN_HISTORY,
        driver_vals={"center": center, "low": dlo, "high": dhi, "source": drv_src},
        singles=singles,
        extra_meta={
            "driver": {"auto_center": auto_center, "auto_low": auto_lo,
                       "auto_high": auto_hi, "avg5": rolling_center(gold, today, 5),
                       "label": cfg["driver"].get("label", ""),
                       **({"basket": basket_meta} if basket_meta else {})},
            "regression": regress_gold_profit(singles, gold),
        },
        extra_clamp_ctx={
            "rule_quarters": {q["quarter"]: [q["lo"], q["hi"]]
                              for q in (rule_q or []) if q["status"] == "estimated"},
            "driver_min120": d120lo, "driver_max120": d120hi,
        })


def _basics_ratio(c, ts_code: str, col: str):
    """daily_basic 全序列 basics + 指定列比率序列(NULL/<=0 剔除). 新模型共用取数."""
    rows = c.execute(
        f"SELECT trade_date, close, total_mv, {col} AS ratio FROM daily_basic "
        "WHERE ts_code=? AND close IS NOT NULL AND total_mv IS NOT NULL "
        "ORDER BY trade_date", (ts_code,)).fetchall()
    basics = [(r["trade_date"], r["close"], r["total_mv"]) for r in rows]
    ratio = [(r["trade_date"], r["ratio"]) for r in rows
             if r["ratio"] is not None and r["ratio"] > 0]
    return basics, ratio


def _agent_anchor(agent, total_mv, close):
    """agent 锚修正解包: profit_adj_json['anchor']=[lo,hi](亿, writer 已钳) ->
    (anchor_eps_lo, anchor_eps_hi, profit_lo, profit_hi, 'agent') 或 None(无修正)."""
    if agent and agent.get("profit_adj_json"):
        adj = json.loads(agent["profit_adj_json"])
        pair = adj.get("anchor")
        if pair:
            shares = total_mv * 1e4 / close
            return (pair[0] * 1e8 / shares, pair[1] * 1e8 / shares,
                    pair[0], pair[1], "agent")
    return None


def _run_pe_band(c, ts_code: str, cfg: dict, fetch) -> None:
    """盈利稳定型白马: 本地 pe_ttm 历史分位带 × TTM EPS(全本地零外部依赖)."""
    basics, ratio = _basics_ratio(c, ts_code, "pe_ttm")
    if len(basics) < MIN_HISTORY:
        raise RuntimeError(f"daily_basic 历史不足: {len(basics)}<{MIN_HISTORY}")
    today, close, total_mv = basics[-1]
    ratio = ratio[-cfg["pe_window"]:]
    if not ratio or ratio[-1][0] != today:
        raise RuntimeError("当日 pe_ttm 缺失(亏损或数据未更新), pe_band 不适用")
    pe_today = ratio[-1][1]
    eps = close / pe_today                                # TTM EPS(元/股)
    profit_ttm = round(total_mv / pe_today / 1e4, 2)      # 万元/PE -> 亿
    agent = latest_agent_view(c, ts_code, today)
    adj = _agent_anchor(agent, total_mv, close)
    if adj:
        anchor_lo, anchor_hi, profit_lo, profit_hi, src = adj
    else:
        anchor_lo = anchor_hi = eps
        profit_lo = profit_hi = profit_ttm
        src = "ttm"
    _finalize_band(
        c, ts_code, cfg, fetch, basics, ratio, agent, anchor_lo, anchor_hi,
        profit_year=(profit_lo, profit_hi), earnings_source=src,
        extra_meta={"anchor": {"kind": "ttm_eps", "eps_ttm": round(eps, 3),
                               "pe_ttm": pe_today, "profit_ttm_yi": profit_ttm}},
        extra_clamp_ctx={"anchor_profit_mech": profit_ttm})


def _run_pb_band(c, ts_code: str, cfg: dict, fetch) -> None:
    """金融/重资产: 本地 pb 历史分位带 × BPS. 净资产是事实值, agent 不可修锚
    (clamp_ctx 不放 anchor_profit_mech, writer 钳制自动丢弃 anchor 修正)."""
    basics, ratio = _basics_ratio(c, ts_code, "pb")
    if len(basics) < MIN_HISTORY:
        raise RuntimeError(f"daily_basic 历史不足: {len(basics)}<{MIN_HISTORY}")
    today, close, total_mv = basics[-1]
    ratio = ratio[-cfg["pe_window"]:]
    if not ratio or ratio[-1][0] != today:
        raise RuntimeError("当日 pb 缺失(数据未更新), pb_band 不适用")
    bps = close / ratio[-1][1]
    agent = latest_agent_view(c, ts_code, today)
    _finalize_band(
        c, ts_code, cfg, fetch, basics, ratio, agent, bps, bps,
        extra_meta={"anchor": {"kind": "bps", "bps": round(bps, 3),
                               "pb": ratio[-1][1]}})


def _run_consensus_pe(c, ts_code: str, cfg: dict, fetch,
                      fetch_consensus_fn=None) -> None:
    """成长/科技(预期驱动): 前瞻PE = 市值/一致预期净利 的分位带 × 预期EPS.

    每日先拉远程序列 upsert consensus_profit_daily; 远程失败用缓存出带
    (cache_only 标记), 缓存也空才抛错走 stale 兜底。绝不重试远程。
    """
    fc = fetch_consensus_fn or fetch_consensus
    basics, _ = _basics_ratio(c, ts_code, "pe_ttm")
    if len(basics) < MIN_HISTORY:
        raise RuntimeError(f"daily_basic 历史不足: {len(basics)}<{MIN_HISTORY}")
    today, close, total_mv = basics[-1]
    bare = ts_code.split(".")[0]
    fy, cache_only = None, False
    try:
        data = fc(bare)
        for td, yi in data["series"]:
            c.execute("INSERT OR REPLACE INTO consensus_profit_daily "
                      "(ts_code, trade_date, profit_yi) VALUES (?,?,?)",
                      (ts_code, td, yi))
        fy = data.get("fy")
    except Exception as e:
        cache_only = True
        print(f"[valuation] {ts_code} 一致预期取数失败(用缓存): {e!r}",
              file=sys.stderr)
    cons = {r["trade_date"]: r["profit_yi"] for r in c.execute(
        "SELECT trade_date, profit_yi FROM consensus_profit_daily "
        "WHERE ts_code=? AND profit_yi IS NOT NULL ORDER BY trade_date",
        (ts_code,))}
    if not cons:
        raise RuntimeError("无一致预期数据(远程失败且本地缓存为空)")
    # 前瞻PE(d) = total_mv(d)万 / (预期净利(d)亿 × 1e4); 预期<=0 的日子跳过
    ratio = []
    for td, _cl, mv in basics[-cfg["pe_window"]:]:
        yi = cons.get(td)
        if yi and yi > 0:
            ratio.append((td, round(mv / (yi * 1e4), 3)))
    latest_td = max(cons)
    profit_now = cons[latest_td]
    if profit_now <= 0:
        raise RuntimeError(f"最新一致预期净利非正({profit_now}亿), consensus_pe 不适用")
    lag = sum(1 for td, _cl, _mv in basics if td > latest_td)
    cdates = sorted(cons)
    ci = cdates.index(latest_td)
    chg30 = (round(cons[latest_td] / cons[cdates[ci - 30]] - 1, 4)
             if ci >= 30 and cons[cdates[ci - 30]] else None)
    shares = total_mv * 1e4 / close
    eps = profit_now * 1e8 / shares               # 当前一致预期 EPS(meta 展示用)
    agent = latest_agent_view(c, ts_code, today)
    adj = _agent_anchor(agent, total_mv, close)
    if adj:
        anchor_lo, anchor_hi, profit_lo, profit_hi, src = adj
    elif fy and fy.get("fy1_yi") and fy.get("fy2_yi"):
        # 锚用前瞻区间 [FY1, FY2](不塌成单点), 反映一致预期的年度跨度与不确定性
        profit_lo, profit_hi = sorted((fy["fy1_yi"], fy["fy2_yi"]))
        anchor_lo = profit_lo * 1e8 / shares
        anchor_hi = profit_hi * 1e8 / shares
        src = "consensus_fy"
    else:
        anchor_lo = anchor_hi = eps               # 无 FY 明细(缓存兜底)回退单点
        profit_lo = profit_hi = profit_now
        src = "consensus"
    _finalize_band(
        c, ts_code, cfg, fetch, basics, ratio, agent, anchor_lo, anchor_hi,
        profit_year=(profit_lo, profit_hi), earnings_source=src,
        extra_meta={"anchor": {
            "kind": "consensus_eps", "eps_fwd": round(eps, 3),
            "profit_consensus_yi": profit_now, "consensus_date": latest_td,
            "lag_days": lag, "chg30": chg30, "fy": fy,
            "cache_only": cache_only}},
        extra_clamp_ctx={"anchor_profit_mech": profit_now})


# 注册估值模型 runner
MODEL_RUNNERS["commodity_pe"] = _run_commodity_pe
MODEL_RUNNERS["pe_band"] = _run_pe_band
MODEL_RUNNERS["pb_band"] = _run_pb_band
MODEL_RUNNERS["consensus_pe"] = _run_consensus_pe


def run_daily(db_path=None, only=None, fetch=fetch_tushare,
              targets_path: str = TARGETS_PATH) -> int:
    """逐标的计算落库. 单标的失败 -> 标 stale 继续下一个. 返回 0 全成功 / 1 有失败."""
    db_path = db_path or scout_db.DB_PATH
    targets = load_targets(targets_path)
    c = scout_db.conn(db_path)
    rc = 0
    for ts_code, cfg in targets.items():
        if only and ts_code != only:
            continue
        if not cfg["enabled"]:
            print(f"[valuation] {ts_code} {cfg['name']} 已停用, 跳过")
            continue
        try:
            MODEL_RUNNERS[cfg["model"]["kind"]](c, ts_code, cfg, fetch)
            print(f"[valuation] {ts_code} {cfg['name']} OK")
        except Exception as e:
            rc = 1
            print(f"[valuation] {ts_code} FAIL: {e!r}", file=sys.stderr)
            try:
                _mark_stale(c, ts_code, str(e))
            except Exception as e2:
                print(f"[valuation] {ts_code} stale 标记也失败: {e2!r}", file=sys.stderr)
        c.commit()
    c.close()
    return rc


# ---------- 看板 API (server.py 路由调用, 只读连接) ----------

def _ro(db_path=None):
    c = sqlite3.connect(f"file:{db_path or scout_db.DB_PATH}?mode=ro",
                        uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def api_targets(db_path=None, targets_path: str = TARGETS_PATH) -> dict:
    """标的列表 + 各自最新带子摘要(无带子只回配置)."""
    targets = load_targets(targets_path)
    c = _ro(db_path)
    try:
        rows = []
        for ts_code, cfg in targets.items():
            band = c.execute(
                "SELECT trade_date, close, price_lo, price_hi, band_pos, "
                "st_lo, st_hi, st_pos, st_source, "
                "driver_source, pe_source, earnings_source "
                "FROM valuation_band_daily WHERE ts_code=? "
                "ORDER BY trade_date DESC LIMIT 1", (ts_code,)).fetchone()
            rows.append({"ts_code": ts_code, "name": cfg["name"],
                         "driver_label": (cfg.get("driver") or {}).get("label", ""),
                         ".openclaw": cfg.get(".openclaw", ""),
                         "enabled": cfg["enabled"],
                         **(dict(band) if band else {})})
        return {"targets": rows}
    finally:
        c.close()


def api_band(code: str, days: int = 250, db_path=None,
             targets_path: str = TARGETS_PATH) -> dict:
    """单标的完整数据: 带子序列 + K线 + 最新明细 + 当年季度 + agent 观点."""
    targets = load_targets(targets_path)
    if code not in targets:
        return {"error": f"未配置标的 {code}"}
    c = _ro(db_path)
    try:
        series = [dict(r) for r in c.execute(
            "SELECT trade_date, price_lo, price_hi, close, band_pos, "
            "st_lo, st_hi, st_pos "
            "FROM valuation_band_daily WHERE ts_code=? "
            "ORDER BY trade_date DESC LIMIT ?", (code, days))][::-1]
        latest = c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                           "ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
        latest = dict(latest) if latest else None
        if latest:
            latest["meta"] = json.loads(latest.pop("meta_json") or "{}")
        try:
            kline = [dict(r) for r in c.execute(
                "SELECT trade_date, open, high, low, close FROM daily "
                "WHERE ts_code=? ORDER BY trade_date DESC LIMIT ?",
                (code, days))][::-1]
        except sqlite3.OperationalError:
            kline = []
        # 近8季业绩明细(quarter 字符串按年季字典序=时序); 展示实际/预告(+商品估计)
        quarters = [dict(r) for r in c.execute(
            "SELECT quarter, status, profit_lo, profit_hi, basis_json, updated_at "
            "FROM valuation_quarter_est WHERE ts_code=? "
            "ORDER BY quarter DESC LIMIT 8", (code,))][::-1]
        # 页面 agent 卡展示深度观点, daily_st 轻量行另有短期带来源标识
        agent = c.execute("SELECT * FROM valuation_agent_view WHERE ts_code=? "
                          "AND trigger!='daily_st' "
                          "ORDER BY run_at DESC LIMIT 1", (code,)).fetchone()
        agent = dict(agent) if agent else None
        if agent:
            agent["active"] = bool(latest and agent["valid_until"]
                                   and agent["valid_until"] >= latest["trade_date"])
        # 做T卡片"最新研判": 最新一条 daily_st 行或含短期带修正的深度行。
        # 与 latest_st_view 语义不同: 维持机械值(st 双 null)的 daily_st 行也返回 ——
        # 它不参与带子计算, 但研判理由要给页面看。
        st_agent = c.execute(
            "SELECT * FROM valuation_agent_view WHERE ts_code=? "
            "AND (trigger='daily_st' OR st_center_adj IS NOT NULL "
            "OR st_width_pct IS NOT NULL) "
            "ORDER BY run_at DESC LIMIT 1", (code,)).fetchone()
        st_agent = dict(st_agent) if st_agent else None
        if st_agent:
            st_agent["active"] = bool(
                latest and st_agent["valid_until"]
                and st_agent["valid_until"] >= latest["trade_date"])
        return {"ts_code": code, "config": targets[code], "series": series,
                "latest": latest, "kline": kline, "quarters": quarters,
                "agent": agent, "st_agent": st_agent}
    finally:
        c.close()


def api_agent(code: str, db_path=None) -> dict:
    """agent 观点历史分轨: 深度近20条 + daily_st 轻量近30条(时间线页用)."""
    c = _ro(db_path)
    try:
        deep = [dict(r) for r in c.execute(
            "SELECT * FROM valuation_agent_view WHERE ts_code=? "
            "AND trigger!='daily_st' ORDER BY run_at DESC LIMIT 20", (code,))]
        daily = [dict(r) for r in c.execute(
            "SELECT * FROM valuation_agent_view WHERE ts_code=? "
            "AND trigger='daily_st' ORDER BY run_at DESC LIMIT 30", (code,))]
        return {"deep": deep, "daily": daily}
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="周期股估值带机械层(每日盘后)")
    ap.add_argument("--db", help="覆盖 DB 路径(默认生产 market.db)")
    ap.add_argument("--code", help="只跑单个标的 ts_code")
    ap.add_argument("--targets", help="覆盖配置文件路径")
    a = ap.parse_args()
    db = a.db or scout_db.DB_PATH
    scout_db.init_schema(db)
    return run_daily(db, only=a.code, targets_path=a.targets or TARGETS_PATH)


if __name__ == "__main__":
    sys.exit(main())
