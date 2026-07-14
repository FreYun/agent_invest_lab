"""valuation_agent — 估值锚点 agent(小奶龙 bot11) 触发器.

每日盘后 cron 调用(在 valuation.py 机械刷新之后). 逐标的判断触发条件,
命中才打包上下文 POST research-loop :18890/api/chat 让 bot11 执行
valuation-anchor skill; bot 的结论经 valuation_anchor_writer.py 落库.
不满足条件零成本跳过. 结果判定看数据层(valuation_agent_view 是否新增行),
不轻信 HTTP 200(教训: research_loop_api_chat_cron_path).

二期: 深度触发未命中时, 每交易日跑 daily_st 轻量情绪研判(修短期带中枢/半宽,
TTL 2 交易日); 无 st baseline 跳过。

exit: 0 成功/无触发跳过; 1 HTTP/连接错; 2 chat 200 但落库验证失败.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import valuation  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENDPOINT = "http://localhost:18890/api/chat"
BOT_ID = "bot11"
HTTP_TIMEOUT_SEC = 1800          # bot 要拉一致预期+研判, 给 30min
ST_HTTP_TIMEOUT_SEC = 600        # 轻量研判, 10min 足够
ONBOARD_HTTP_TIMEOUT_SEC = 2700  # 建模走深度研究(start_research 循环上限30min)+落草案, 给 45min
DRIVER_SHIFT_PCT = 0.03          # 5日均价偏离中枢阈值
WEEKLY_DAYS = 7
CONSENSUS_SHIFT_PCT = 0.05      # 一致预期净利5日变动阈值(consensus_pe 深度触发)

SKILL_PATH = os.path.expanduser(
    "~/.openclaw/workspace/skills/research/valuation-anchor/SKILL.md")

JUDGE_ITEMS = {
    "commodity_pe": "四项研判: 商品价锚 / 盈利区间校准 / PE带适配 / "
                    "短期做T带(st_center_adj/st_width_pct)",
    "pe_band": '三项研判: 盈利锚校准(TTM净利明显失真时 profit_adj={"anchor":[lo,hi]}, '
               "亿, 钳机械锚0.7~1.3倍) / PE带适配 / 短期做T带",
    "pb_band": "两项研判: PB带适配 / 短期做T带(BPS锚是事实值不可修, 不要给 profit_adj)",
    "consensus_pe": "三项研判: 预期锚校准(你的判断与一致预期明显分歧时 "
                    'profit_adj={"anchor":[lo,hi]}, 亿) / 前瞻PE带适配 / 短期做T带',
}

PROMPT_TMPL = """执行估值锚点研判任务(标的 {name} {ts_code}):

1. Read {skill_path} 并严格按其流程执行。
2. 本次触发原因: {triggers}。
3. 下方是机械层现状与 scout 研究上下文; 结合 research-mcp 一致预期(自行调用)
   按该标的模型做研判: {judge_items}。
4. 结论(哪怕判断"无需修正")必须经
   `python3 {writer} --reviewer {bot} --json '<payload>'` 落库,
   payload schema 见 SKILL.md; trigger 字段填 "{trigger0}"。
5. 完成后简短汇报修正了什么、核心依据。

--- 机械层与 scout 研究上下文 ---
{context}"""

ST_PROMPT_TMPL = """执行短期做T带每日轻量研判(标的 {name} {ts_code}):

你是估值带系统的"短期情绪研判层"。机械层已算出短期带 baseline(近60日估值比率
振荡), 你的任务只有两个数: 结合市场情绪与技术面, 判断今天合适的短期**中枢**与**半宽**。
1. 中枢 st_center_adj: 默认维持机械值(填 null); 只有情绪/事件使定价重心明显偏移才修,
   会被钳制在机械中枢 ±8% 内。
2. 半宽 st_width_pct: 默认维持机械值(填 null); 情绪亢奋或恐慌(振幅放大/涨跌停增多)
   → 放宽, 缩量平静 → 收窄; 会被钳制在 [0.03, 0.15]。
3. 轻量任务: 不调 research-mcp、不做深度研究, 只基于下方上下文判断, 几分钟内完成。
4. 结论(哪怕全维持机械值)必须落库:
   python3 {writer} --reviewer {bot} --json '<payload>'
   payload: {{"ts_code":"{ts_code}","trigger":"daily_st","confidence":0.6,
   "rationale":"技术面: …\\n情绪面: …\\n结论: …","st_center_adj":null或数值,"st_width_pct":null或数值}}
   rationale 三段式(段间用换行符 \\n 分隔): 『技术面:』『情绪面:』『结论:』各起一段,
   每段 1-2 句且必须引用上下文里的具体数据(乖离/振幅/涨停数/regime), 总量 100-200 字
   —— 研究部要在页面上看懂你"看到什么→判断什么"。
5. 完成后一句话汇报。

--- 上下文 ---
{context}"""

ONBOARD_PROMPT_TMPL = """执行新标的建模研判(标的 {name} {ts_code}):

研究部想把该标的接入估值带系统, 请你起草"估值建模草案"。这是该标的的**首次建模**,
不是轻量研判——必须先唤起深度研究模式再下结论:
1. 先调用 start_research 工具启动一轮深度研究(硬性要求, 不许跳过直接落草案):
   topic 填 "{name}({ts_code}) 盈利驱动与估值建模: 主营与盈利结构、盈利由什么
   商品/变量驱动、驱动价与盈利的映射方式、季节性与特殊因素、适合的估值尺子"。
   该调用同步阻塞至研究完成, 返回的 brief(conclusion/key_evidence/sources)
   就是本次建模的事实基础; 之后不要对同一主题重复调用。
   **空 brief 兜底铁律**: 若 start_research 返回空/未完成(conclusion.confidence
   =unidentified、key_evidence 为空、或提示"未完成 output 阶段"), 绝不因此中止,
   也不要反复重跑研究或在 expand/list_mcp/discover 里空转——立即改用本地数据
   (market.db daily_basic 的 pe_ttm/pb 历史分位、财报)与自身行业知识完成建模,
   仍走完第 3-5 步。无论研究成败, **调 writer 落草案是本任务不可省略的强制终点**,
   研究空跑时也要落一份基于本地数据的草案(applicable 据实判定, rationale 注明
   "深度研究未产出有效结论, 依本地数据+知识建模")。
3. 基于深度研究 brief, 从模型池为该股选择估值模型(单选):
   - commodity_pe 商品驱动周期股: 盈利由可日频取数的商品价驱动(kind=sge 上海金 /
     kind=fut 期货主力 / kind=basket 2-4成分加权篮子), 单商品能解释约60%以上
     利润波动优先单 driver。仅此模型需要 driver 字段。
   - pe_band 盈利稳定型: 盈利可预期、PE-TTM 有意义(消费/白电/医药白马等)。
   - pb_band 金融/重资产: 盈利失真或强周期表内资产定价(银行/券商/保险/地产)。
   - consensus_pe 成长/预期驱动: 市场按前瞻盈利定价(科技/新产业), **必须先自查
     该股有卖方一致预期覆盖**(research-mcp get_stock_consensus_growth, 裸代码)。
   - 都不合适(亏损且无预期覆盖等) -> applicable=false, rationale 说明理由。
   两可标的(如券商牌照+互联网流量混合体)必须在 rationale 写明选型取舍。
4. 给出估值方法论 brief(>=50字: 盈利驱动/估值尺子/做T特征)、research_industries
   (2-4个行业关键词)、.openclaw(接入注意)。
5. 结论必须经 writer 落库(草案不直接生效, 研究部确认后才启用):
   python3 {writer} --reviewer {bot} --onboard --json '<payload>'
   payload 形状(非 commodity_pe **不要带 driver 字段**):
   {{"ts_code":"{ts_code}","applicable":true/false,
   "model":{{"kind":"commodity_pe|pe_band|pb_band|consensus_pe","brief":"..."}},
   "driver":{{"kind":"sge|fut","symbol":"...","label":"..."}},
   "research_industries":["..."],".openclaw":"...","rationale":"建模分析全文"}}
   多商品驱动时 driver 改为篮子形状(成分 2-4 个, 每成分 kind 仍限 sge|fut):
   "driver":{{"kind":"basket","label":"铜金篮子(指数, 基期=100)","components":[
   {{"kind":"fut","symbol":"CU.SHF","label":"沪铜主力(元/吨)","weight":0.5}},
   {{"kind":"sge","symbol":"Au99.99","label":"上海金(元/克)","weight":0.5}}]}}

--- 参考资料 ---
{context}"""


def check_triggers(c, ts_code: str, cfg: dict, today: str) -> list[str]:
    """today=YYYYMMDD. 返回命中的触发原因列表(可多个). 基线只看深度行.

    earnings: 商品模型看 quarter_est 状态迁移; 其余模型看 earnings_events 新公告。
    driver_shift 商品专属(meta.driver 缺失时自然跳过); consensus_pe 另有
    consensus_shift(一致预期净利5日变动超阈值)。
    """
    out = []
    kind = (cfg.get("model") or {}).get("kind", "commodity_pe")
    last = c.execute(
        "SELECT MAX(run_at) FROM valuation_agent_view WHERE ts_code=? "
        "AND trigger!='daily_st'", (ts_code,)).fetchone()[0]
    today_dt = datetime.strptime(today, "%Y%m%d")
    if not last or (today_dt - datetime.strptime(last[:10], "%Y-%m-%d")).days >= WEEKLY_DAYS:
        out.append("weekly")
    if last:
        if kind == "commodity_pe":
            n = c.execute(
                "SELECT COUNT(*) FROM valuation_quarter_est WHERE ts_code=? "
                "AND status IN ('actual','forecast') AND updated_at>?",
                (ts_code, last)).fetchone()[0]
        else:
            n = c.execute(
                "SELECT COUNT(*) FROM earnings_events WHERE ts_code=? "
                "AND ann_date>?", (ts_code, last[:10].replace("-", ""))).fetchone()[0]
        if n:
            out.append("earnings")
    band = c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                     "ORDER BY trade_date DESC LIMIT 1", (ts_code,)).fetchone()
    if band:
        meta = json.loads(band["meta_json"] or "{}")
        avg5 = (meta.get("driver") or {}).get("avg5")
        if (avg5 and band["driver_center"]
                and abs(avg5 / band["driver_center"] - 1) > DRIVER_SHIFT_PCT):
            out.append("driver_shift")
        if (band["close"] is not None and band["price_lo"] is not None
                and band["price_hi"] is not None
                and not band["price_lo"] <= band["close"] <= band["price_hi"]):
            out.append("band_breach")
    if kind == "consensus_pe":
        rows = c.execute(
            "SELECT profit_yi FROM consensus_profit_daily WHERE ts_code=? "
            "AND profit_yi IS NOT NULL ORDER BY trade_date DESC LIMIT 6",
            (ts_code,)).fetchall()
        if len(rows) == 6 and rows[5]["profit_yi"]:
            chg = rows[0]["profit_yi"] / rows[5]["profit_yi"] - 1
            if abs(chg) > CONSENSUS_SHIFT_PCT:
                out.append("consensus_shift")
    return out


def _band_block(c, ts_code: str) -> str:
    band = c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                     "ORDER BY trade_date DESC LIMIT 1", (ts_code,)).fetchone()
    if not band:
        return "【机械层】尚无带子数据(valuation.py 未跑或历史不足)。"
    meta = json.loads(band["meta_json"] or "{}")
    kind = meta.get("model_kind", "commodity_pe")
    drv = meta.get("driver") or {}
    reg = meta.get("regression")
    lines = [
        f"【机械层现状 {band['trade_date']} · 模型 {kind}】",
    ]
    # 商品价锚行: 仅 commodity_pe 且 driver_center 非空时输出
    if band["driver_center"] is not None:
        lines.append(
            f"- 商品价锚({drv.get('label','')}): 中枢 {band['driver_center']} "
            f"区间 [{band['driver_low']}, {band['driver_high']}] 来源={band['driver_source']}; "
            f"自动中枢={drv.get('auto_center')} 近5日均={drv.get('avg5')}")
    # 各模型专属锚行
    anchor = meta.get("anchor") or {}
    if kind == "pe_band":
        lines.append(f"- 锚: TTM EPS {anchor.get('eps_ttm')} 元/股 "
                     f"(TTM净利 {anchor.get('profit_ttm_yi')} 亿, "
                     f"当前PE-TTM {anchor.get('pe_ttm')})")
    elif kind == "pb_band":
        lines.append(f"- 锚: BPS {anchor.get('bps')} 元/股 "
                     f"(当前PB {anchor.get('pb')}; 净资产为事实值, 锚不可修)")
    elif kind == "consensus_pe":
        fy = anchor.get("fy") or {}
        lines.append(f"- 锚: 一致预期EPS {anchor.get('eps_fwd')} 元/股 "
                     f"(TTM一致预期净利 {anchor.get('profit_consensus_yi')} 亿 "
                     f"@{anchor.get('consensus_date')}, "
                     f"30日预期变动 {_pct(anchor.get('chg30'))})")
        if fy:
            lines.append(f"- 一致预期旁证: FY1 {fy.get('fy1_yi')} 亿 / "
                         f"FY2 {fy.get('fy2_yi')} 亿")
        if (anchor.get("lag_days") or 0) > 10:
            lines.append(f"- ⚠ 预期数据陈旧: 已断更 {anchor['lag_days']} 交易日, "
                         f"降低对预期锚的信任")
    # 年化净利行: 仅非空时输出, label 按 kind
    if band["profit_year_lo"] is not None:
        if kind == "consensus_pe":
            profit_label = "一致预期净利"
        elif kind == "pe_band":
            profit_label = "TTM净利"
        else:
            profit_label = "年化归母净利区间"
        lines.append(f"- {profit_label}: [{band['profit_year_lo']}, {band['profit_year_hi']}] 亿 "
                     f"(来源={band['earnings_source']})")
    # PE带行 label 按 kind
    if kind == "pb_band":
        pe_label = "PB带"
    elif kind == "pe_band":
        pe_label = "PE-TTM带"
    elif kind == "consensus_pe":
        pe_label = "前瞻PE带"
    else:
        pe_label = "PE带"
    lines.append(f"- {pe_label}: [{band['pe_lo']}, {band['pe_hi']}] 来源={band['pe_source']} "
                 f"(分位原始值 {meta.get('pe_quantile_raw')}, 样本 {meta.get('pe_n')})")
    lines.append(f"- 价格带: [{band['price_lo']}, {band['price_hi']}], 现价 {band['close']}, "
                 f"带内位置 {band['band_pos']}")
    # 篮子/回归段: 非商品 meta 无这些键自然跳过
    bk = drv.get("basket")
    if bk:
        lines.append("- 篮子成分(真实价): " + "; ".join(
            f"{c_.get('label','')} 权重{round((c_.get('weight') or 0)*100)}% "
            f"现价{c_.get('close')} 20日均{c_.get('center20')} 5日均{c_.get('avg5')}"
            for c_ in bk.get("components", [])))
        lines.append(f"- 注意: 该标的商品价锚为加权篮子指数(基期=100, 基期窗起点 "
                     f"{bk.get('base_start')}), driver_center/low/high 修正必须按"
                     f"指数量纲给值(现值≈{band['driver_center']}), 不要填任何真实商品价。")
    if reg:
        lines.append(f"- 驱动价({drv.get('label') or '商品价'})回归旁证: "
                     f"beta={reg['beta']} 亿/单位价, r2={reg['r2']}, "
                     f"n={reg['n']} (单变量, 仅参考)")
    # 当年季度明细段: 仅 commodity_pe
    if kind == "commodity_pe":
        qs = c.execute("SELECT quarter, status, profit_lo, profit_hi FROM "
                       "valuation_quarter_est WHERE ts_code=? AND quarter LIKE ? "
                       "ORDER BY quarter", (ts_code, band["trade_date"][:4] + "%")).fetchall()
        if qs:
            lines.append("- 当年季度明细(亿): " + "; ".join(
                f"{r['quarter']}={r['status']}[{r['profit_lo']},{r['profit_hi']}]"
                for r in qs))
    return "\n".join(lines)


def _reviews_block(c, ts_code: str, cfg: dict) -> str:
    bare = scout_db.bare(ts_code)
    lines = []
    rows = c.execute(
        "SELECT trade_date, reviewer, logic_stars, action_stars, summary "
        "FROM intraday_review WHERE code=? ORDER BY trade_date DESC, "
        "reviewed_at DESC LIMIT 10", (bare,)).fetchall()
    if rows:
        lines.append("【scout 个股点评(近10条)】")
        lines += [f"- {r['trade_date']} {r['reviewer']} 逻辑{r['logic_stars']}星/"
                  f"操作{r['action_stars']}星: {(r['summary'] or '')[:120]}" for r in rows]
    kws = cfg.get("research_industries") or []
    if kws:
        cond = " OR ".join("board_name LIKE ?" for _ in kws)
        rows = c.execute(
            f"SELECT trade_date, reviewer, board_name, continuation_stars, summary "
            f"FROM intraday_board_review WHERE ({cond}) AND trade_date>=date('now','-7 day') "
            f"ORDER BY trade_date DESC LIMIT 6",
            [f"%{k}%" for k in kws]).fetchall()
        if rows:
            lines.append("【scout 板块点评(近7天, 行业关键词命中)】")
            lines += [f"- {r['trade_date']} {r['reviewer']} {r['board_name']} "
                      f"持续{r['continuation_stars']}星: {(r['summary'] or '')[:120]}"
                      for r in rows]
    return "\n".join(lines)


def _overlay_block(ts_code: str) -> str:
    """res 行业研究立场(fail-soft: 任何异常返回空串, 不阻断)."""
    try:
        import research_overlay
        entries = research_overlay.compute(codes=[ts_code])
        return research_overlay.render_prompt_block(
            entries, "【res 行业研究立场】")
    except Exception as e:
        print(f"  WARN research_overlay 失败(忽略): {e!r}", file=sys.stderr)
        return ""


def _model_block(cfg: dict) -> str:
    """注入估值方法论简介(由研究部在 targets 配置中确认)."""
    brief = (cfg.get("model") or {}).get("brief")
    return f"【该标的估值方法论(研究部确认)】\n{brief}" if brief else ""


def _pct(v):
    """百分比格式化, None 显示 —."""
    return "—" if v is None else f"{v * 100:+.1f}%"


def _st_block(c, ts_code: str) -> str:
    """短期做T带现状块(st_center 为 None 时返回空串)."""
    band = c.execute("SELECT * FROM valuation_band_daily WHERE ts_code=? "
                     "ORDER BY trade_date DESC LIMIT 1", (ts_code,)).fetchone()
    if not band or band["st_center"] is None:
        return ""
    m = (json.loads(band["meta_json"] or "{}").get("st")) or {}
    return (f"【短期做T带现状 {band['trade_date']}】\n"
            f"- 生效: 中枢 {band['st_center']} 带 [{band['st_lo']}, {band['st_hi']}] "
            f"来源={band['st_source']} 位置={band['st_pos']}\n"
            f"- 机械 baseline: 中枢 {m.get('center_mech')} 半宽 {m.get('width_mech')} "
            f"(60日PE 中位 {m.get('pe_med60')}, "
            f"20/80分位 [{m.get('pe_q20')}, {m.get('pe_q80')}])")


def _tech_block(c, ts_code: str) -> str:
    """技术面事实块(meta_json 无 tech 键时返回空串)."""
    band = c.execute("SELECT meta_json FROM valuation_band_daily WHERE ts_code=? "
                     "ORDER BY trade_date DESC LIMIT 1", (ts_code,)).fetchone()
    t = (json.loads(band["meta_json"] or "{}") if band else {}).get("tech")
    if not t:
        return ""
    return ("【技术面事实】\n"
            f"- 现价 {t.get('close')} | MA5 {t.get('ma5')} MA10 {t.get('ma10')} "
            f"MA20 {t.get('ma20')} MA60 {t.get('ma60')} "
            f"(20日乖离 {_pct(t.get('bias20'))})\n"
            f"- 20日高低位位置 {t.get('pos20')} | 距60日高 {_pct(t.get('dist_hi60'))}\n"
            f"- 动量: 5日 {_pct(t.get('ret5'))} 10日 {_pct(t.get('ret10'))} | "
            f"量比(5/20日均量) {t.get('vol_ratio5_20')} | "
            f"近5日日均振幅 {_pct(t.get('amp5_avg'))}")


def _sentiment_block(c) -> str:
    """全市场情绪(regime 表由 21:00 pipeline 维护; 缺表/空表 fail-soft 返回空)."""
    try:
        cls = c.execute("SELECT trade_date, regime_name, total_score, confidence "
                        "FROM regime_classify_daily "
                        "ORDER BY trade_date DESC LIMIT 1").fetchone()
        raw = c.execute("SELECT trade_date, sentiment_index, limit_up_count, "
                        "limit_down_count, advance_decline_ratio, total_amount_yi "
                        "FROM regime_raw_daily "
                        "ORDER BY trade_date DESC LIMIT 1").fetchone()
    except Exception:
        return ""
    if not cls and not raw:
        return ""
    lines = ["【市场情绪】"]
    if cls:
        lines.append(f"- regime: {cls['regime_name']} (score={cls['total_score']}, "
                     f"conf={cls['confidence']}, {cls['trade_date']})")
    if raw:
        lines.append(f"- 情绪指数 {raw['sentiment_index']} | 涨停 {raw['limit_up_count']} "
                     f"跌停 {raw['limit_down_count']} | 涨跌比 "
                     f"{raw['advance_decline_ratio']} | 两市成交 "
                     f"{raw['total_amount_yi']} 亿 ({raw['trade_date']})")
    return "\n".join(lines)


def build_context(c, ts_code: str, cfg: dict) -> str:
    """深度研判上下文: 方法论 + 机械层 + 短期带 + 技术面 + 情绪 + scout 点评 + 上次观点."""
    parts = [_model_block(cfg), _band_block(c, ts_code), _st_block(c, ts_code),
             _tech_block(c, ts_code), _sentiment_block(c),
             _reviews_block(c, ts_code, cfg), _overlay_block(ts_code)]
    # 深度连续性只看深度行
    last = c.execute("SELECT run_at, rationale, valid_until FROM valuation_agent_view "
                     "WHERE ts_code=? AND trigger!='daily_st' "
                     "ORDER BY run_at DESC LIMIT 1", (ts_code,)).fetchone()
    if last:
        parts.append(f"【你上次的锚点观点 {last['run_at']} (生效至{last['valid_until']})】\n"
                     f"{(last['rationale'] or '')[:300]}")
    return "\n\n".join(p for p in parts if p)


def build_st_context(c, ts_code: str, cfg: dict) -> str:
    """轻量研判上下文: 方法论 + 短期带现状 + 技术面 + 情绪 + 上次 st 观点."""
    parts = [_model_block(cfg), _st_block(c, ts_code), _tech_block(c, ts_code),
             _sentiment_block(c)]
    last = c.execute(
        "SELECT run_at, rationale, st_center_adj, st_width_pct, valid_until "
        "FROM valuation_agent_view WHERE ts_code=? AND trigger='daily_st' "
        "ORDER BY run_at DESC LIMIT 1", (ts_code,)).fetchone()
    if last:
        parts.append(f"【你上次的短期研判 {last['run_at']} (生效至{last['valid_until']})】\n"
                     f"中枢修正={last['st_center_adj']} 半宽={last['st_width_pct']}\n"
                     f"{(last['rationale'] or '')[:200]}")
    return "\n\n".join(p for p in parts if p)


def build_onboard_context(c, ts_code: str, name: str) -> str:
    """建模研判上下文: 标的名 + 概念板块 + 近60日量价 + res 行业研究. 全 fail-soft."""
    bare = scout_db.bare(ts_code)
    parts = [f"【标的】{name} {ts_code}"]
    try:
        rows = c.execute(
            "SELECT DISTINCT board_name FROM concept_board_daily WHERE board_code IN "
            "(SELECT board_code FROM stock_concept_map WHERE code=? AND "
            "snapshot_date=(SELECT MAX(snapshot_date) FROM stock_concept_map)) "
            "LIMIT 20", (bare,)).fetchall()
        if rows:
            parts.append("【所属概念板块】" + "、".join(r["board_name"] for r in rows))
    except Exception:
        pass
    try:
        rows = c.execute("SELECT close, pct_chg, amount FROM daily WHERE ts_code=? "
                         "ORDER BY trade_date DESC LIMIT 60", (ts_code,)).fetchall()
        closes = [r["close"] for r in rows if r["close"] is not None]
        if closes:
            cum = 1.0
            for r in rows:
                if r["pct_chg"] is not None:
                    cum *= 1 + r["pct_chg"] / 100
            amts = [r["amount"] for r in rows if r["amount"]]
            avg_amt_yi = sum(amts) / len(amts) / 1e5 if amts else None  # 千元->亿
            parts.append(f"【近60日量价】现价 {closes[0]} 区间 "
                         f"[{min(closes)}, {max(closes)}] "
                         f"区间涨跌 {(cum - 1) * 100:+.1f}%"
                         + (f" 日均成交额 {avg_amt_yi:.1f} 亿" if avg_amt_yi else ""))
    except Exception:
        pass
    ov = _overlay_block(ts_code)
    if ov:
        parts.append(ov)
    return "\n\n".join(parts)


def _post_chat(session_key: str, message: str, sender_name: str,
               timeout: int) -> int:
    """公共 HTTP 发消息到 research-loop, 返回 0(成功)/1(失败)."""
    payload = {
        "bot_id": BOT_ID, "session_key": session_key, "message": message,
        "channel": "cron",
        "metadata": {"sender_id": "cron", "sender_name": sender_name},
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, ""
    except Exception as e:
        print(f"[valuation-agent] {sender_name} CONN_ERROR {e}", file=sys.stderr)
        return 1
    elapsed = time.time() - started
    print(f"[valuation-agent] {sender_name} status={status} elapsed={elapsed:.0f}s "
          f"body_head={body[:160]!r}")
    return 0 if status == 200 else 1


def _call_agent(ts_code: str, cfg: dict, triggers: list[str], context: str) -> int:
    """深度研判: 调 valuation-anchor skill, 30min 超时."""
    today = datetime.now().date().isoformat()
    writer = os.path.join(HERE, "valuation_anchor_writer.py")
    kind = (cfg.get("model") or {}).get("kind", "commodity_pe")
    judge_items = JUDGE_ITEMS.get(kind, JUDGE_ITEMS["commodity_pe"])
    msg = PROMPT_TMPL.format(
        name=cfg["name"], ts_code=ts_code, skill_path=SKILL_PATH,
        triggers=",".join(triggers), writer=writer, bot=BOT_ID,
        trigger0=triggers[0], context=context, judge_items=judge_items)
    return _post_chat(f"agent:{BOT_ID}:valuation-anchor-{today}", msg,
                      "valuation-anchor", HTTP_TIMEOUT_SEC)


def _call_st_agent(ts_code: str, cfg: dict, context: str) -> int:
    """轻量 daily_st 研判: 只判短期带中枢/半宽, 10min 超时."""
    today = datetime.now().date().isoformat()
    writer = os.path.join(HERE, "valuation_anchor_writer.py")
    msg = ST_PROMPT_TMPL.format(name=cfg["name"], ts_code=ts_code,
                                writer=writer, bot=BOT_ID, context=context)
    return _post_chat(f"agent:{BOT_ID}:valuation-st-{today}", msg,
                      "valuation-st", ST_HTTP_TIMEOUT_SEC)


def _call_onboard_agent(ts_code: str, name: str, context: str) -> int:
    """建模研判派发(首次建模走深度研究), 45min 超时."""
    today = datetime.now().date().isoformat()
    writer = os.path.join(HERE, "valuation_anchor_writer.py")
    msg = ONBOARD_PROMPT_TMPL.format(name=name, ts_code=ts_code,
                                     skill_path=SKILL_PATH, writer=writer,
                                     bot=BOT_ID, context=context)
    return _post_chat(f"agent:{BOT_ID}:valuation-onboard-{today}", msg,
                      "valuation-onboard", ONBOARD_HTTP_TIMEOUT_SEC)


def _mark_draft_failed(db, ts_code: str, err: str) -> None:
    c = scout_db.conn(db)
    try:
        c.execute("UPDATE valuation_target_draft SET status='failed', error=?, "
                  "updated_at=? WHERE ts_code=? AND status='drafting'",
                  (err, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ts_code))
        c.commit()
    finally:
        c.close()


def run_onboard(ts_code: str, db, dry_run: bool = False) -> int:
    """加股建模研判: 前置 draft 行 status=drafting(由 server add_target 落).
    成败看 draft 表状态迁移(drafting -> draft_ready/unsupported), 不信 HTTP 200."""
    c = scout_db.conn(db)
    d = c.execute("SELECT name, status FROM valuation_target_draft WHERE ts_code=?",
                  (ts_code,)).fetchone()
    if not d or d["status"] != "drafting":
        print(f"[valuation-onboard] {ts_code} 无 drafting 草案行"
              f"(状态={d['status'] if d else '缺'}), 需先经页面发起", file=sys.stderr)
        c.close()
        return 1
    context = build_onboard_context(c, ts_code, d["name"])
    c.close()
    if dry_run:
        print(context)
        return 0
    r = _call_onboard_agent(ts_code, d["name"], context)
    if r:
        _mark_draft_failed(db, ts_code, "chat 请求失败(:18890 连接/HTTP错)")
        return 1
    c = scout_db.conn(db)
    status = c.execute("SELECT status FROM valuation_target_draft WHERE ts_code=?",
                       (ts_code,)).fetchone()["status"]
    c.close()
    if status == "drafting":
        _mark_draft_failed(db, ts_code, "chat 200 但草案未落库(bot 没调 writer --onboard?)")
        print(f"[valuation-onboard] {ts_code} WARN: 草案未落库", file=sys.stderr)
        return 2
    print(f"[valuation-onboard] {ts_code} OK status={status}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="估值锚点 agent 触发器")
    ap.add_argument("--db", help="覆盖 DB 路径(测试用)")
    ap.add_argument("--dry-run", action="store_true", help="只打印触发判断与上下文")
    ap.add_argument("--force", action="store_true", help="无视触发条件必跑(强制深度)")
    ap.add_argument("--onboard", metavar="TS_CODE",
                    help="加股建模研判模式: 对该代码跑一次建模草案派发")
    a = ap.parse_args()
    db = a.db or scout_db.DB_PATH
    if a.onboard:
        return run_onboard(a.onboard.strip().upper(), db, dry_run=a.dry_run)
    targets = valuation.load_targets()
    today = datetime.now().strftime("%Y%m%d")
    rc = 0
    for ts_code, cfg in targets.items():
        if not cfg.get("enabled", True):
            print(f"[valuation-agent] {ts_code} 已停用, 跳过")
            continue
        c = scout_db.conn(db)
        triggers = check_triggers(c, ts_code, cfg, today)
        if a.force and not triggers:
            triggers = ["manual"]
        # 双轨判断: 深度触发命中走深度, 否则走 daily_st 轻量研判
        mode = "deep" if triggers else "st"
        if mode == "deep":
            context = build_context(c, ts_code, cfg)
        else:
            context = build_st_context(c, ts_code, cfg)
            if "【短期做T带现状" not in context:
                print(f"[valuation-agent] {ts_code} 无短期带 baseline, 跳过 daily_st")
                c.close()
                continue
        before = c.execute("SELECT COUNT(*) FROM valuation_agent_view WHERE ts_code=?",
                           (ts_code,)).fetchone()[0]
        c.close()
        print(f"[valuation-agent] {ts_code} 模式={mode} 触发: {triggers or ['daily_st']}")
        if a.dry_run:
            print(context)
            continue
        r = (_call_agent(ts_code, cfg, triggers, context) if mode == "deep"
             else _call_st_agent(ts_code, cfg, context))
        if r:
            rc = max(rc, r)
            continue
        # 数据层验证: chat 200 不算数, 必须真的落了新行
        c = scout_db.conn(db)
        after = c.execute("SELECT COUNT(*) FROM valuation_agent_view WHERE ts_code=?",
                          (ts_code,)).fetchone()[0]
        c.close()
        if after <= before:
            print(f"[valuation-agent] {ts_code} WARN: chat 完成但 "
                  f"valuation_agent_view 未新增行(bot 没调 writer?)", file=sys.stderr)
            rc = max(rc, 2)
            continue
        # 新锚点已落库 -> 立即重算该标的带子, 消除"次日机械层才吸收"的时序延迟
        # (机械层 21:35 先跑, 本触发器 22:20 后跑, 修正原本要等 T+1 才进模型)。
        # fail-soft: 重算异常/失败不影响 agent 本身的成败, 修正仍在库, 退回次日兜底。
        try:
            vrc = valuation.run_daily(db, only=ts_code)
            print(f"[valuation-agent] {ts_code} 带子即时重算 rc={vrc}")
        except Exception as e:
            print(f"[valuation-agent] {ts_code} WARN: 即时重算异常 {e!r}",
                  file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
