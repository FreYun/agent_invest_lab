#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""res_reports 历史回填 —— 用 qwen3.5-plus 为四个宏观研判室按「周度」补历史报告。

四个研判室(skill 真身平时实时浏览器取数；历史区间不能浏览器→改用 PIT 安全源)：
  res1 market-strategy-ops      → report_type=market_strategy      stance: risk_on|risk_off|neutral
  res2 policy-analysis-ops      → report_type=policy_analysis      stance: tighten|ease|neutral
  res4 intl-relations-ops       → report_type=intl_relations       stance: escalate|deescalate|stable
  res5 cross-market-linkage-ops → report_type=cross_market_linkage stance: strong_linkage|decoupling|mixed

数据流：
  交易日 T(每 ISO 周第一个交易日) × 4 类型
    ① simworld:18078 research_search(PIT, simulated_datetime=T 15:00:00) 取资讯/研报
       + 各域行情/宏观 PIT 工具(best-effort, 失败即略) → 定量+资讯快照
    ② 按该研判室框架拼 prompt(结论行/3-5要点/板块顺逆风/策略含义 + stance 枚举)
    ③ 调 qwen3.5-plus(openai 兼容, base_url/key 读自 ~/.openclaw/openclaw.json) → 报告md + structured_json
    ④ 落 fund.db 的 res_reports(复用 _shared/market_report_db.py.add_report，纯 append)
  幂等：(report_type, as_of_date) 已存在则跳过(除非 --force) → 中断可续跑。
  PIT 口径：每份报告截至当日 15:00 A股收盘(research_search simulated_datetime 与 prompt 均写明)。

simworld 是单 worker，**全程串行**调用(并发会打爆上游，记忆有载)；qwen 调用同样串行(简单稳)。

用法(在 agent_invest_lab/ 下)：
  /usr/bin/python3.12 scripts/res-reports-backfill.py --from 2025-01-01 --to 2026-06-17 --freq weekly
  可选：--types market_strategy,policy_analysis  --limit 2  --dry-run  --force  --model qwen3.5-plus
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from typing import Any, Optional

import requests

# ── 路径常量 ────────────────────────────────────────────────────────────────
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO, "data", "fund.db")
CALENDAR = os.path.join(REPO, "world", "runtime", "calendar.json")
OPENCLAW_JSON = "/home/rooot/.openclaw/openclaw.json"
MARKET_REPORT_DB_PY = "/home/rooot/.openclaw/workspace/skills/armor/_shared/market_report_db.py"
SIMWORLD_URL = os.environ.get("SIMWORLD_MCP_URL", "http://127.0.0.1:18078/mcp")
CACHE_DIR = os.path.join(REPO, "runtime", "res-reports-backfill-cache")
NEWS_WINDOW_DAYS = int(os.environ.get("RES_NEWS_WINDOW_DAYS", "60"))  # 资讯召回窗口(T 前 N 天)

# ── 落库模块(复用 skill 真身那套 res_reports 语义) ────────────────────────────
os.environ.setdefault("MARKET_REPORT_DB", DB_PATH)
_spec = importlib.util.spec_from_file_location("market_report_db", MARKET_REPORT_DB_PY)
mrdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrdb)  # type: ignore

# ── 各研判室规格(框架来自对应 skill 的 *-framework.md，浓缩进 system prompt) ──
SPECS: dict[str, dict[str, Any]] = {
    "market_strategy": {
        "res": "res1",
        "name": "宏观策略研究室(market-strategy-ops)",
        "stance": ["risk_on", "risk_off", "neutral"],
        "stance_hint": "risk_on=进攻/风险偏好上行，risk_off=防守/避险，neutral=均衡",
        "framework": (
            "研判**整个市场**的 regime(不是某行业)，套六维：\n"
            "  趋势方向(主要指数均线/动量→牛/熊/震荡)、风险偏好(股债比/信用利差/避险资产→risk_on/off)、\n"
            "  波动率(VIX/A股波动/量能→平静/紧张/恐慌)、流动性(利率政策/北向/两融/成交额→宽松/中性/收紧)、\n"
            "  风格轮动(大小盘·成长/价值·红利→占优风格)、板块资金(主线/拥挤板块/资金流向)。\n"
            "收敛成一个 regime 标签 + 一句话定调；每维给数值依据，无数据的维度显式标「数据缺失」。"
        ),
        "queries": ["A股 大盘 行情 走势 指数", "市场 风险偏好 北向 成交量 流动性", "板块 风格 轮动 资金 主线"],
        "search_type": "news",
        "quant": [
            ("market_index_quote", {"market": "cn", "symbols": ["沪深300", "上证综指", "创业板指", "中证1000"]}),
            ("option_vix", {"type_codes": ["000300", "000016"]}),
            ("macro_data", {"region": "cn", "categories": ["usdcny", "pmi"]}),
        ],
    },
    "policy_analysis": {
        "res": "res2",
        "name": "政策研究室(policy-analysis-ops)",
        "stance": ["tighten", "ease", "neutral"],
        "stance_hint": "tighten=收紧，ease=宽松，neutral=中性/结构性",
        "framework": (
            "研判**政策本身**(不是某行业产业链)，套五维：\n"
            "  类型(货币/财政/产业/监管)、力度(相对预期：超预期/符合/低于)、方向(宽松/收紧/结构性定向)、\n"
            "  受益受损板块(到行业/板块层面，要给传导理由)、时滞(短期情绪冲击 vs 中期落地兑现)。\n"
            "收敛成一句定调；最细到板块，不点个股。基于权威发布，拿不到原文标「待核实原文」，绝不臆测条款数字。"
        ),
        "queries": ["央行 货币政策 降准 降息 LPR 公开市场", "财政部 专项债 财政政策 减税", "发改委 产业政策 监管 国常会 政治局会议"],
        "search_type": "all",
        "quant": [
            ("macro_data", {"region": "cn", "categories": ["m2", "afre", "cpi"]}),
            ("bond_yield_curve", {"curve_type": "cn", "maturities": ["1Y", "10Y"]}),
        ],
    },
    "intl_relations": {
        "res": "res4",
        "name": "国际关系研究室(intl-relations-ops)",
        "stance": ["escalate", "deescalate", "stable"],
        "stance_hint": "escalate=升级，deescalate=缓和，stable=僵持/平稳",
        "framework": (
            "研判**国际关系/地缘态势**(不止中美)，套四维 + 方向判断：\n"
            "  大国关系轴(中美/中欧/中俄/中日韩/中印/美欧)、地缘冲突(俄乌/中东/台海南海)、\n"
            "  贸易与供应链(关税/出口管制/制裁/关键资源 油气稀土锂粮食)、全球秩序(阵营化/去美元化/多边机制)。\n"
            "给方向判断(升级/缓和/僵持)及强度、指明主轴、受影响板块顺逆风、对中国资产整体风险偏好的影响。\n"
            "信源分级：官方>权威媒体>传闻；传闻必标「未证实」，绝不当事实下结论。"
        ),
        "queries": ["中美关系 贸易 科技 关税 出口管制 制裁", "俄乌 中东 地缘 冲突 局势", "稀土 供应链 全球 秩序 去美元化"],
        "search_type": "news",
        "quant": [
            ("commodity_market", {"market_type": "spot", "symbols": ["黄金"]}),
        ],
    },
    "cross_market_linkage": {
        "res": "res5",
        "name": "中美市场联动研究室(cross-market-linkage-ops)",
        "stance": ["strong_linkage", "decoupling", "mixed"],
        "stance_hint": "strong_linkage=强联动，decoupling=背离/脱钩，mixed=弱联动/混合",
        "framework": (
            "研判**中美跨市场联动**(不是某行业)，套五维：\n"
            "  传导链(美股 费半/纳指→A股科技、隔夜情绪→开盘缺口)、相关性(近窗口 A股vs美股/中概 相关系数·beta)、\n"
            "  领先-滞后(谁领先谁、滞后几日)、汇率与利差(美元/人民币·中美利差对北向与风险偏好的牵引)、\n"
            "  背离信号(A股与美股/中概明显背离=重点信号)。\n"
            "给联动状态判断(强正联动/弱联动/背离)及主导机制(资金通道/情绪传染/基本面)，落到对 A股的指示意义。\n"
            "强调「相关≠因果」，说清机制并标注不确定性。"
        ),
        "queries": ["美股 纳斯达克 标普 隔夜 收盘", "中概股 金龙指数 ADR 人民币 汇率", "中美利差 国债收益率 北向资金"],
        "search_type": "news",
        "quant": [
            ("market_index_quote", {"market": "us", "symbols": ["SPX.GI", "NDX.GI", "DJIA.GI"]}),
            ("market_index_quote", {"market": "cn", "symbols": ["沪深300", "科创50"]}),
            ("option_vix", {"type_codes": ["000300"]}),
            ("bond_yield_curve", {"curve_type": "cn", "maturities": ["10Y"]}),
        ],
    },
}

SYSTEM_TMPL = """你是「{name}」的资深研究员，正在为历史某交易日补写一份**研判 brief**。
{framework}

写作要求(硬性)：
- 全文 ≤1800 字中文，研报式长段落/重复总判断/套话一律删，要紧凑可读。
- 报告骨架四段：
  ① 结论行：一句话结论 + stance 标签 + 关键依据/出处
  ② 关键要点 3-5 条(每条 ≤1 行，带方向)
  ③ 板块顺逆风(紧凑表或要点)
  ④ 策略含义 / 对A股指示意义 + 1-2 个风险与需盯的观察信号
- stance 只能取其一：{stance}（{stance_hint}）。
- **绝不编造**点位/涨跌幅/利差/VIX/宏观读数/政策条款。给定快照里没有的数字一律不写；某维度无依据就显式写「数据缺失」。
- 只用下方提供的 PIT 快照(截至该日 15:00 A股收盘可见的资讯与数据)，不得引入此日期之后的信息。

只输出一个 ```json 代码块，结构：
```json
{{"report_md": "<报告全文 markdown>", "structured": {{"headline": "一句话结论", "stance": "<枚举之一>", "key_points": ["..."], "sectors": ["..."], "tags": ["..."]}}}}
```"""

USER_TMPL = """【世界日 T】{date}（PIT 截止：{date} 15:00 A股收盘）

【资讯 / 研报快照(research_search, PIT)】
{news}

【定量快照(行情/宏观 PIT，缺失项已略)】
{quant}

请据此产出 {date} 当日的研判 brief，按系统要求只输出那个 json 代码块。"""


# ── 日历 / 周度决策日 ─────────────────────────────────────────────────────────
def load_trading_days(d_from: str, d_to: str) -> list[str]:
    days = json.load(open(CALENDAR, encoding="utf-8"))["trading_days"]
    return sorted(d for d in days if d_from <= d <= d_to)


def iso_week_monday(d: str) -> str:
    x = dt.date.fromisoformat(d)
    return (x - dt.timedelta(days=x.weekday())).isoformat()


def decision_days(dates: list[str], freq: str) -> list[str]:
    if freq == "daily":
        return dates
    out, last = [], ""
    for d in dates:
        key = iso_week_monday(d) if freq == "weekly" else d[:7]  # weekly=ISO周一; monthly=YYYY-MM
        if key != last:
            out.append(d)
            last = key
    return out


# ── qwen 凭证 ────────────────────────────────────────────────────────────────
def load_qwen_creds(model: str) -> tuple[str, str, str]:
    cfg = json.load(open(OPENCLAW_JSON, encoding="utf-8"))
    for prov in cfg.get("models", {}).get("providers", {}).values():
        ids = {m.get("id") for m in prov.get("models", [])}
        if model in ids and prov.get("baseUrl") and prov.get("apiKey"):
            return prov["baseUrl"].rstrip("/"), prov["apiKey"], model
    raise SystemExit(f"openclaw.json 里找不到含 {model} 的 provider(baseUrl+apiKey)")


def call_qwen(base_url: str, api_key: str, model: str, system: str, user: str, retries: int = 3) -> str:
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    last = ""
    for i in range(retries):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=240)
            if r.status_code != 200:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(3 * (i + 1))
                continue
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"qwen 调用失败(重试 {retries} 次): {last}")


def parse_qwen_json(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise ValueError("qwen 输出无 json 代码块")
    obj = json.loads(m.group(1))
    if not obj.get("report_md", "").strip():
        raise ValueError("report_md 为空")
    return obj


# ── simworld PIT 取数(MCP streamable-http，全程串行) ──────────────────────────
class Simworld:
    def __init__(self, session):
        self.s = session

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        res = await self.s.call_tool(tool, args)
        txt = ""
        for c in res.content:
            if getattr(c, "type", "") == "text":
                txt += c.text
        try:
            return json.loads(txt)
        except Exception:  # noqa: BLE001
            return {"_raw": txt[:2000]}


def _items_from_search(j: Any) -> list[str]:
    """从 research_search 返回里抽 [日期] 摘要 文本行。data 形如 {items:[{INFOCODE,SUMMARY,...}]}。"""
    if not isinstance(j, dict):
        return []
    rows = j.get("data") or j.get("results") or j.get("items") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("data") or rows.get("list") or []
    # search_type='all' 时 items 是 {'news':[...], 'research':[...]} 字典 → 摊平成一个列表
    if isinstance(rows, dict):
        flat = []
        for v in rows.values():
            if isinstance(v, list):
                flat.extend(v)
        rows = flat
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        summ = r.get("SUMMARY") or r.get("CONTENT") or r.get("TITLE") or ""
        code = str(r.get("INFOCODE") or "")
        date = code[:8] if code[:8].isdigit() else (r.get("SHOWTIME", "") or "")[:10]
        # SUMMARY 上游常是 "['...']" 的 list-repr 字符串，去壳成纯文本
        summ = re.sub(r"\s+", " ", str(summ)).strip()
        summ = re.sub(r"^\[['\"]|['\"]\]$", "", summ).strip()
        if summ:
            out.append(f"[{date}] {summ[:280]}")
    return out


def _compact_quant(tool: str, j: Any) -> Optional[str]:
    """把行情/宏观工具返回压成短行：每个 item 取其内嵌序列的最后一条(最接近 T 的 PIT 值)。"""
    if not isinstance(j, dict) or j.get("success") is False or "error" in j:
        return None
    items = j.get("items") or j.get("data") or j.get("results") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or items.get("list") or []
    if not isinstance(items, list) or not items:
        return None
    lines = []
    for it in items[:6]:
        if not isinstance(it, dict):
            lines.append(str(it)[:120])
            continue
        label = (it.get("指数标识") or it.get("标的代码") or it.get("期限") or it.get("category")
                 or it.get("品种") or it.get("名称") or "")
        series = next((v for v in it.values() if isinstance(v, list) and v and isinstance(v[0], dict)), None)
        payload = series[-1] if series else {k: v for k, v in it.items() if not isinstance(v, (list, dict))}
        lines.append(f"{label}={json.dumps(payload, ensure_ascii=False)}")
    return f"- {tool}: " + " ; ".join(lines) if lines else None


async def gather_context(date: str, spec: dict[str, Any], retries: int = 3) -> tuple[str, str]:
    """每个任务开一条独立 simworld 会话(贯穿全程的长连接会被上游 reset → httpx.ReadError 掀全局)。
    会话级重试：连接断只重试本任务，失败则抛出由上层 per-task try 接住、计 fail、续跑。"""
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession
    last = None
    for attempt in range(retries):
        try:
            async with streamablehttp_client(SIMWORLD_URL) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return await _gather(Simworld(session), date, spec)
        except Exception as e:  # noqa: BLE001  (连接级错误：ReadError/超时/握手失败)
            last = e
            if attempt < retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"simworld 会话连续失败 {retries} 次: {type(last).__name__}: {last}")


async def _gather(sw: Simworld, date: str, spec: dict[str, Any]) -> tuple[str, str]:
    sdt = f"{date} 15:00:00"
    # 新闻窗口收窄到 T 前 NEWS_WINDOW_DAYS 天：默认窗口[T-365,T]按相关性 rerank 会混入偏旧新闻，
    # 周度 brief 要反映当期 → 显式收窄(end 缺省=T)，让召回贴近 T。
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=NEWS_WINDOW_DAYS)).isoformat()
    # 资讯：逐 query 串行(单 worker)，去重，限量
    news, seen = [], set()
    for q in spec["queries"]:
        try:
            j = await sw.call("research_search", {
                "query": q, "simulated_datetime": sdt,
                "search_type": spec["search_type"], "top_k": 8,
                "start_date": start, "end_date": date,
            })
            for line in _items_from_search(j):
                h = hashlib.md5(line[:120].encode()).hexdigest()
                if h not in seen:
                    seen.add(h)
                    news.append(line)
        except Exception as e:  # noqa: BLE001
            news.append(f"(query「{q}」取数异常: {type(e).__name__})")
    news = news[:14] or ["(本期无可用资讯，按数据缺失处理)"]

    # 定量：best-effort，失败/空即略
    quant = []
    for tool, base in spec["quant"]:
        try:
            j = await sw.call(tool, {**base, "simulated_datetime": sdt})
            line = _compact_quant(tool, j)
            if line:
                quant.append(line)
        except Exception:  # noqa: BLE001
            continue
    quant_txt = "\n".join(quant) if quant else "(本期定量快照不可用/缺失)"
    return "\n".join(f"- {n}" for n in news), quant_txt


# ── 落库(幂等) ───────────────────────────────────────────────────────────────
def already_done(report_type: str, date: str) -> bool:
    c = mrdb.conn()
    try:
        row = c.execute(
            "SELECT 1 FROM res_reports WHERE report_type=? AND as_of_date=? LIMIT 1",
            (report_type, date)).fetchone()
        return row is not None
    finally:
        c.close()


def save_report(report_type: str, date: str, obj: dict[str, Any], res: str) -> None:
    structured = obj.get("structured", {})
    run_id = f"{res}-{date.replace('-', '')}-backfill"
    c = mrdb.conn()
    try:
        mrdb.add_report(c, report_type, date, obj["report_md"],
                        structured_json=structured, scope="global", agent_run_id=run_id)
        c.commit()
    finally:
        c.close()


# ── 缓存(避免 qwen 失败重跑时再捶 simworld) ───────────────────────────────────
def cache_path(report_type: str, date: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{report_type}_{date}.json")


# ── 主流程 ───────────────────────────────────────────────────────────────────
async def run(args) -> int:
    base_url, api_key, model = load_qwen_creds(args.model)
    tdays = load_trading_days(args.d_from, args.d_to)
    if not tdays:
        print("窗口内无交易日", file=sys.stderr)
        return 2
    days = decision_days(tdays, args.freq)
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    for t in types:
        if t not in SPECS:
            print(f"未知 report_type: {t}", file=sys.stderr)
            return 2

    tasks = [(d, t) for d in days for t in types]
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"窗口 {tdays[0]}..{tdays[-1]} → {len(days)} 个{args.freq}决策日 × {len(types)} 类型 = {len(tasks)} 个任务"
          f"（模型 {model}，DB {DB_PATH}）", flush=True)

    done = skip = fail = 0
    for n, (date, rtype) in enumerate(tasks, 1):
        spec = SPECS[rtype]
        tag = f"[{n}/{len(tasks)}] {rtype} {date}"
        if not args.force and already_done(rtype, date):
            skip += 1
            print(f"{tag} ⏭  已存在，跳过", flush=True)
            continue
        try:
            cp = cache_path(rtype, date)
            if os.path.exists(cp) and not args.force:
                ctx = json.load(open(cp, encoding="utf-8"))
                news_txt, quant_txt = ctx["news"], ctx["quant"]
            else:
                news_txt, quant_txt = await gather_context(date, spec)
                json.dump({"news": news_txt, "quant": quant_txt}, open(cp, "w", encoding="utf-8"),
                          ensure_ascii=False)
            system = SYSTEM_TMPL.format(name=spec["name"], framework=spec["framework"],
                                        stance=" / ".join(spec["stance"]), stance_hint=spec["stance_hint"])
            user = USER_TMPL.format(date=date, news=news_txt, quant=quant_txt)
            if args.dry_run:
                print(f"{tag} 🔎 dry-run：资讯{news_txt.count(chr(10))+1}条 / quant {len(quant_txt)}字 — 不调qwen不落库", flush=True)
                done += 1
                continue
            raw = call_qwen(base_url, api_key, model, system, user)
            obj = parse_qwen_json(raw)
            st = obj.get("structured", {}).get("stance")
            if st not in spec["stance"]:
                obj.setdefault("structured", {})["stance"] = "neutral" if "neutral" in spec["stance"] else spec["stance"][-1]
            save_report(rtype, date, obj, spec["res"])
            done += 1
            print(f"{tag} ✅ {obj.get('structured', {}).get('headline', '')[:50]}", flush=True)
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"{tag} ❌ {type(e).__name__}: {e}", flush=True)

    print(f"\n完成：生成 {done} / 跳过 {skip} / 失败 {fail}（共 {len(tasks)}）", flush=True)
    return 0 if fail == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="d_from", default="2025-01-01")
    p.add_argument("--to", dest="d_to", default="2026-06-17")
    p.add_argument("--freq", choices=["daily", "weekly", "monthly"], default="weekly")
    p.add_argument("--types", default=",".join(SPECS.keys()))
    p.add_argument("--model", default="qwen3.5-plus")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
