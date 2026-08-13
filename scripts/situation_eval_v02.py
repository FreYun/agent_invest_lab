#!/usr/bin/env python3
"""处境坐标系 v0.2 求值器（P0.3 修订版）

纯机械求值，无 LLM。对经验库中的历史案例批量计算处境坐标并落 situation_vectors 表。

数据源（全部只读）：
  - data/market.db      行情正本：index_daily(000300.SH)、board_trend_daily、mkt_gvix_daily
  - data/fund.db        账户正本：fund_bot_daily_snapshots
  - scripts/v5_mainline_plan.py  信号维正本（直接 import 复用，禁止重实现以免口径漂移）

口径纪律：
  - 日期：库内 YYYY-MM-DD，market.db 为 YYYYMMDD，转换集中在 _ymd()
  - index_daily.pct_chg 存在 NULL，收益一律由 close 自算
  - 缺数维度显式写 null 并在 coords_json.__missing 里记原因，不用 0 或默认值冒充

v0.1 → v0.2 的实质改动（起因：v0.1「本舰队没有硬回撤闸门」的结论被证伪）
  1. 账户维从单回撤轴改为双回撤轴 + persona 控仓线：
     - dd_alltime_pp  ：run 内历史峰值回撤（引擎 crashSignal 用的就是这个口径）
     - dd20_pp        ：近 20 交易日滚动峰值回撤（persona METHODOLOGY 用的是这个口径）
     两者互不包含：引擎档位按前者，persona 控仓线按后者。
  2. 新增 persona_derisk_stage / persona_gate_gap_pp / defensive_state，
     数值来自各 bot 自己 METHODOLOGY.md 的 `### 回撤控仓` 节（逐字摘录见 PERSONA_DD_LINES）。
  3. 引擎回撤钥匙降级为注记（engine_key_enabled / engine_dd_level）——实勘 8 个案例 run，
     其中 5 个 run（109/121 案例）该功能整段关闭。它是"能不能卖"的解锁键，
     不是"该不该减"的命令，且在本案例集里近乎常量，不入 cell。
  4. 新增 persona_line_obeyed：越线当日实际动作是否与自己写的规则一致。
  5. 修 v0.1 三个缺陷：分格 NA 冒充、决策类型静默误判、落库无痕覆盖。

v0.2 → v0.2.1（起因：独立验证判 FAIL，两条 claim 被证伪 + 一个未申报的数据缺陷）
  1. engine_dd_level 在钥匙关闭时改写 None（原写 0）。原实现让"测量结果是 0 档"（8 例）
     与"根本没测量"（109 例）在同一列上不可分，任何 group by 该列而不同时读
     engine_key_enabled 的消费方都会把两者混进同一个分母。文档承诺"绝不写 0"，
     代码却在写 0——这是文档与实现相反，不是口径分歧。
  2. 新增 class_sum_ok()：equity_weight 不是原始字段，是从 holdings_json[].asset_class
     分类汇总来的；该字段大量为空串时汇总结果为 0，列上与"真实空仓"无法区分。
     实测全库 44,428 / 76,843 = 57.8% 的行四类权重之和 != 1（按本文件的
     CLASS_SUM_TOL=0.02，as-of 2026-08-11 凌晨），本案例集命中 45 例。
     ⚠ 报这个数必须连容差一起报：换容差比例就变（tol 0.1→57.2% / 0.02→57.8% /
     1e-4→57.9% / 严格相等→59.3%）。"约 58%" 单独出现是欠定义的。
     ⚠ 本文件此前两处写"全库绝对数持续漂移，故只写比例"——那是错的，已删。
     不合格行数在 tol=0.02 下九个半小时纹丝不动（凌晨 44,428，13:13 钉快照仍 44,428）；
     当时看到的 44,494 是我复测时换成了更严的容差，不是源漂移。真正漂的只有总行数
     （76,843→76,872，纯追加且追加的都是合格行）。据此可判定：此前写的"76842"是笔误，
     正确值 76,843 = 32,415 + 44,428。详见 06 §八之二 第三个更正块。
     ⚠ 复测本量请直接调用本文件的 class_sum_ok()，不要另写比较，否则拿到的是另一个量。
     本案例集的 45 例是冻结集上的计数，不漂移，且在 tol=0.1/0.02/1e-4 三档下同为 45
     （仅"严格相等"下涨到 51，那 6 例是浮点尾数）。
     四类权重之和是否为 1 是唯一一行内可查的判别不变量。不满足时 equity_* 一律写 None。
  3. equity_change_pp 的差分基准（前一交易日）也必须过同一个不变量，
     否则会产出 bot101 2026-07-10 那种 action_count=0 却记 +53.97pp"加仓"的幻影。
  4. 关于"引擎回撤钥匙整段历史只转过 1 次"——该说法已证伪，不再出现在任何注释里。
     实测全舰队 47 条 account-drawdown 触发记录，横跨 16 个 run、7 个 bot
     （bot20d 22 / bot105gr 11 / bot105g 5 / bot16d 4 / bot18d 3 / bot10d 1 / bot105d 1），
     其中 31 条 reason 只有 account-drawdown 一项；去掉同 run 内重复轨迹后为 37 个 (bot,日期) 对。
     ⚠⚠ as-of 2026-08-11T12:11:10+08:00 —— 这是活动目录上的流量指标，不是存量事实。
     同日 11:36 测得 46/15/21/36，两次都对：dash-2026-08-10T06-25-24 在两次统计之间
     仍在运行并写入了新的一条（行内时间戳 2026-08-11T03:53:26Z）。引用此数必须带 as-of，
     要求可复现必须先钉只读快照，否则复算方与原测方永远对不上而且双方都没错。
     该事实不改变本求值器的行为（案例集内 109/121 仍是功能关闭），仅作口径更正。
     ⚠ 计数陷阱：含匹配的 run.log 里有 2 个被 grep 判为二进制——成因是 NUL 字节，
     不是编码非法（实测 nul=True / invalid_utf8=False，记错成因会让人去查错的东西）。
     不加 -a 时 grep 只报"匹配到二进制文件"而不输出行，逐行统计会静默漏掉 15 条。
     统计 run.log 一律用 grep -a；"文件数对得上"不等于"行数对得上"；
     同一份报告里同一个量的两个口径必须显式相减对账。
"""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/agent_invest_lab")
MARKET_DB = os.path.join(ROOT, "data", "market.db")
FUND_DB = os.path.join(ROOT, "data", "fund.db")
SCRIPTS = os.path.join(ROOT, "scripts")

EVALUATOR_VERSION = "situation_eval_v0.2.1"
BENCHMARK = "000300.SH"

# 坐标不全时的保留字面量。v0.1 用 str(x or "NA") 把缺数轴悄悄拼成 'NA|NA' 这种
# 看起来像真格子的 cell_id；扩到 backtest_day 后会有 501/1111 天落进去。
INCOMPLETE_CELL = "__坐标不全__"

sys.path.insert(0, SCRIPTS)
import v5_mainline_plan as v5  # noqa: E402  信号维正本

# 只有 multi_equity 族跑 v5 主线状态机；其余 persona 的信号维标 null。
#
# ⚠ 这个门是「族标签门」，而正确的门是「PIT 门」：案发日该 bot 有没有装
#   bots/<bot>/skills/market-mainline。在当前冻结的 121 例上两者逐例同答
#   （实测 PIT 违规 0、漏覆盖 0），但那是巧合：
#     · bot105d 族标签是 multi_equity_high_v2，不在本集合里，故其 8 例被 null。
#       该 bot 其实装了 market-mainline，只是落地日 2026-07-21 晚于这 8 例的
#       最后一天 2026-07-13 —— 所以 null 恰好也是 PIT 正确的。
#     · bot102 dash run（2025-01~02）族标签为空，同样被 null；该 bot 装 skill
#       是 2026-06-11，也晚于案例日 —— 同样恰好正确。
#   两个方向的失效都要防：bot105d 在 2026-07-21 之后的新案例本应有信号维，
#   本门会继续错误地 null；而若有人把 multi_equity_high_v2 塞进本集合来"提高
#   覆盖率"，那 8 个旧案例会被追认上当天看不见的 regime —— 那是未来信息违规。
#   改法见 03 §六 第 5 项（待裁决，不影响已落库的 121 例任何一格）。
V5_FAMILIES = {"multi_equity_high", "multi_equity_medium", "multi_equity_low"}


# ---------------------------------------------------------------- persona 控仓线
#
# 逐字来源：bots/<bot>/METHODOLOGY.md 的 `### 回撤控仓` 节。
# 峰值口径由文档自身给定："账户从近 20 个交易日峰值回撤" —— 与引擎的历史峰值不是一回事。
# lines 按阈值升序；stage = 已越过的线数。
# release_pp：解除 defensive review 的回撤水平（bot102 是唯一写了解除条件的）。

# 每条线的 req 描述该线【要求账户处于什么状态】，而不是要求当天做什么动作：
#   band       (下限%, 上限%) 总权益应落入的区间；文档没给区间则 None
#   no_add     该线是否禁止新增仓位
#   no_add_scope 禁止范围（增强仓/主攻仓/卫星仓）——快照只有总权益，分不出子桶，
#                故 no_add 命中只能报"疑似"，不能判定违规
#   relative   文档用的是相对表述（如"降一档风险预算"），机械不可校验

PERSONA_DD_LINES = {
    "bot101": {
        "peak_window": 20,
        "lines": [
            (10.0, "降一档风险预算", {"band": None, "no_add": False, "relative": "降一档（90→70→50）"}),
            (15.0, "强制 defensive review，当日不得新增主攻仓",
             {"band": None, "no_add": True, "no_add_scope": "主攻仓"}),
        ],
        "defensive_entry_pp": 15.0,
        "release_pp": None,
        "source": "bots/bot101/METHODOLOGY.md:194-200",
        "note": "文档另述 10% 以内是正常波动区间、不要频繁降仓；底线为回撤逼近 20%",
    },
    "bot105d": {
        "peak_window": 20,
        "lines": [
            (10.0, "降一档风险预算", {"band": None, "no_add": False, "relative": "降一档（90→70→50）"}),
            (15.0, "强制 defensive review，当日不得新增主攻仓",
             {"band": None, "no_add": True, "no_add_scope": "主攻仓"}),
        ],
        "defensive_entry_pp": 15.0,
        "release_pp": None,
        "source": "bots/bot105d/METHODOLOGY.md:194-200（与 bot101 逐字相同）",
        "note": "同 bot101",
    },
    "bot102": {
        "peak_window": 20,
        "lines": [
            (3.0, "增强仓减半，禁止新增增强仓",
             {"band": None, "no_add": True, "no_add_scope": "增强仓", "relative": "增强仓减半"}),
            (4.0, "总权益降到 35%-45%", {"band": (35.0, 45.0), "no_add": False}),
            (5.0, "强制 defensive review，总权益 0%-20%，当日不得新增增强仓",
             {"band": (0.0, 20.0), "no_add": True, "no_add_scope": "增强仓"}),
        ],
        "defensive_entry_pp": 5.0,
        "release_pp": 2.0,
        "source": "bots/bot102/METHODOLOGY.md:471-479；AGENTS.md:58,146（「5% 回撤是硬闸门」）",
        "note": "本舰队唯一写了解除条件、也是唯一把控仓要求写成可校验总权益区间的 bot",
    },
    "bot103": {
        "peak_window": 20,
        "lines": [
            (2.5, "降一档风险预算，卫星仓优先清零",
             {"band": None, "no_add": False, "relative": "降一档 + 卫星清零"}),
            (5.0, "强制 defensive review，当日不得新增卫星仓，核心仓降到 risk_off 区间",
             {"band": None, "no_add": True, "no_add_scope": "卫星仓", "relative": "核心仓降到 risk_off"}),
        ],
        "defensive_entry_pp": 5.0,
        "release_pp": None,
        "source": "bots/bot103/METHODOLOGY.md:438-444；MEMORY.md:25",
        "note": "文档称账户净值回撤是最高优先级闸门",
    },
    "bot10d": {
        "peak_window": 20,
        "lines": [],
        "defensive_entry_pp": None,
        "release_pp": None,
        "tolerance_pp": 10.0,
        "source": "bots/bot10d/USER.md:11",
        "note": "只有一句容忍线「约 -10%，这条线是经历过更大回撤之后给的」——是容忍不是分档规则，不入 stage",
    },
    "bot18d": {
        "peak_window": 20, "lines": [], "defensive_entry_pp": None, "release_pp": None,
        "source": "bots/bot18d/（无 `### 回撤控仓` 节）",
        "note": "persona 文档无任何数值回撤规则；其 world yaml 设 crash_trigger_drawdown_pct: 8，风控在引擎侧",
    },
    "bot20d": {
        "peak_window": 20, "lines": [], "defensive_entry_pp": None, "release_pp": None,
        "source": "bots/bot20d/（无 `### 回撤控仓` 节）",
        "note": "同 bot18d",
    },
}


# ---------------------------------------------------------------- 引擎回撤钥匙
#
# world/src/run.ts:1560-1569 —— accountDrawdownPct 取 performance.summary.current_drawdown_pct
# （历史峰值口径），阈值 config.crashTriggerDrawdownPct 默认 8；越档时置
# unlockedByForcedRiskControl，作用是【解开 min_hold 卖出承诺锁】，不是命令减仓。
# config.ts:534 的解析链：account_drawdown_trigger_enabled ?? crash_trigger_enabled
#                          ?? (deep_research_mode === 'agent-triggered')
#
# 下表逐 run 实勘（证据为 runtime/runs/<run>/run.log 与 state.json，非推断）。
ENGINE_KEY = {
    "oos-bot101-daily": (
        False, 8.0,
        "run.log 11 条 deep-research-check 全为 account_dd=n/a%；state.json "
        "deep_research_drawdown_level_by_bot={'bot101':0}、commitments={}",
    ),
    "oos-bot102-daily": (
        False, 8.0,
        "run.log 11 条 deep-research-check 全为 account_dd=n/a%；state.json level={'bot102':0}、commitments={}",
    ),
    "oos-bot103-daily": (
        False, 8.0,
        "run.log 11 条 deep-research-check 全为 account_dd=n/a%；state.json level={'bot103':0}、commitments={}",
    ),
    "dash-bot102-2025-01-02-2025-11-24": (
        False, 8.0,
        "run.log 1203 行内 0 条 deep-research 行；state.json 无 deep_research_drawdown_* 字段（该功能尚未进代码）",
    ),
    "bot105d-weekly-rsloop-deepall-20260721r2": (
        False, 8.0,
        "run.log 91 条 deep-research 行均无 account_dd 字段；state.json 无 deep_research_drawdown_* 字段",
    ),
    "dash-2026-08-02T05-56-09": (
        True, 8.0,
        "bot10d：381/382 条 deep-research-check 带数值 account_dd（1 条 n/a）；最深 -4.30%，越 8% 天数 0",
    ),
    "dash-2026-07-27T01-10-07": (
        True, 8.0,
        "bot20d：352/352 条带数值 account_dd；最深 -13.58%，越 8% 共 24 天；"
        "reason 含 account-drawdown 的有 6 天（03-20/03-27/04-07/06-09/06-29/07-06），其中 1 天为唯一原因",
    ),
    "dash-2026-08-03T01-59-26": (
        True, 8.0,
        "bot18d：385/386 条带数值 account_dd（1 条 n/a）；最深 -5.99%，越 8% 天数 0；account_dd_level 320 条全为 0",
    ),
}
ENGINE_DEFAULT_THRESHOLD = 8.0


def _ymd(d: str) -> str:
    return d.replace("-", "")


def _dashed(d: str) -> str:
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


CLASS_SUM_TOL = 0.02

# ------------------------------------------------------------ 阈值口径开关（B1 / B2）
#
# 这两个是【待拍板项】，不是我该替人选的。默认值 = v0.2 已落库的口径，
# 换默认值会改变已交付的结论，因此默认必须保持 rounded/dd20 不变。
#
# THRESHOLD_MODE  阈值比较用哪个精度（B1）
#   "rounded" 先 round 到 2 位再跟阈值比（v0.2 现状）。副作用：真实回撤 -4.9998%
#             会被 round 成 -5.00 从而判为"越过 5% 线"——差 0.0002pp。而这一例
#             恰好是全库唯一一例干净的"违反自订风控"实锤。
#   "raw"     用原始浮点值比，只有展示才 round。理由：判定阈值不该由展示精度决定。
#
# DEEPEST_CALIBER 每个 bot 最深那条线的峰值口径（B2）
#   PERSONA_DD_LINES 里 9 条线中有 5 条文档明写"近 20 个交易日"，另外 4 条
#   （bot101 15%、bot102 5%、bot103 5%、bot105d 15%——恰好是每个 bot 最深的那条）
#   只写了"从峰值"，没说是哪个峰值。
#   "dd20"    一律按 20 日滚动峰值读（v0.2 现状）。
#   "alltime" 最深那条按 run 内历史峰值读（更容易触发，符合"最后一道线"的本意）。
#
# 两者都不影响 cell_id —— cell = stress_bucket|dd20_bucket，与控仓线判定无关。
# 这一点由 scripts/caliber_matrix.py 逐例实测，不靠推断。
#
# ASOF_CALIBER    账户维取哪个时点（B3）
#   "close"        取 D 日收盘快照（v0.2 现状）。问题：D 收盘快照是【决策的结果】，
#                  拿它当处境轴用等于拿答案当题目；判"有没有越自己的线"时更严重，
#                  等于用 bot 当时看不到的数去判它违规。
#   "predecision"  处境轴取 D-1 收盘（bot 决策时实际拿到的账户状态），
#                  但"是否达标"仍用 D 收盘权益（合规看的是收在哪，不是起在哪）。
#
# 为什么认为 bot 看到的是 D-1：全库 121 例里有 58 例在决策文里自报了回撤，
# 其中 28 例与 D-1 收盘的某个口径逐位吻合（容差 0.011pp），只有 1 例与 D 收盘吻合。
# 最刺眼的一例 bot102 2026-07-21：自报 -4.80% 并写明"账户回撤≥5% ❌ 未触发"，
# -4.80% 正是 D-1（07-20）收盘的 -4.7978%；而我们判它违规用的是 D 收盘的 -4.9998%，
# 且还要先 round 成 -5.00 才越线。详见 scripts/caliber_matrix.py。
THRESHOLD_MODE = "rounded"
DEEPEST_CALIBER = "dd20"
ASOF_CALIBER = "close"


def class_sum_ok(row) -> tuple:
    """四类权重之和是否等于 1。

    `fund_bot_daily_snapshots.equity_weight` 不是原始字段，是从
    `holdings_json[].asset_class` 分类汇总出来的。该字段在大量行上是空串，
    分类器归不到任何一类，汇总结果就是 0 —— 从列上看和"真实空仓"完全一样。
    权重和是否为 1 是唯一能把两者分开的、一行就能查的不变量。
    """
    s = sum(float(row[k] or 0.0) for k in ("equity_weight", "bond_weight", "gold_weight", "cash_weight"))
    return abs(s - 1.0) <= CLASS_SUM_TOL, s


def _dd_window(navs, i: int, win: int) -> float:
    """navs[i] 相对最近 `win` 个交易日峰值的回撤，返回非负 pp 值。

    精度由 THRESHOLD_MODE 决定，且【必须】与 account_axes 里的越线判定同源，
    否则同一条 5.0 的线会一处判越、一处判未越：0.9500/1.0-1 的浮点结果是
    -4.999999999999996，round 到 2 位就变成 -5.00。这个差异不是学术的——
    全库唯一一例"违反自订风控"实锤就压在这 0.0002pp 上。
    """
    w = navs[max(0, i - win + 1) : i + 1] if win else navs[: i + 1]
    pk = max(v for _, v in w)
    if pk <= 0:
        return 0.0
    raw = abs(min(0.0, (w[-1][1] / pk - 1) * 100))
    return raw if THRESHOLD_MODE == "raw" else round(raw, 2)


def dd20_at(navs, i: int) -> float:
    """近 20 交易日滚动峰值口径（各 persona 文档 `### 回撤控仓` 的口径）。"""
    return _dd_window(navs, i, 20)


def ddall_at(navs, i: int) -> float:
    """run 内历史峰值口径（引擎 crashSignal 的口径）。B2 选 alltime 时用它。"""
    return _dd_window(navs, i, 0)


def ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 市场维

def market_axes(mc: sqlite3.Connection, date_ymd: str) -> dict:
    """基准指数的趋势 / 波动 / 下跌速度 / 宽度。全部 trade_date<=D，PIT 安全。"""
    rows = mc.execute(
        "SELECT trade_date, close FROM index_daily WHERE ts_code=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 500",
        (BENCHMARK, date_ymd),
    ).fetchall()
    out: dict = {}
    missing: dict = {}

    if not rows or rows[0]["trade_date"] != date_ymd:
        missing["market"] = f"index_daily 无 {BENCHMARK} 于 {date_ymd} 的行"
        return {"axes": {}, "missing": missing}

    closes = [float(r["close"]) for r in rows]  # closes[0] = 当日
    close = closes[0]

    # --- 趋势状态
    ma60 = sum(closes[:60]) / 60 if len(closes) >= 60 else None
    ma250 = sum(closes[:250]) / 250 if len(closes) >= 250 else None
    out["dist_ma60_pct"] = round((close / ma60 - 1) * 100, 2) if ma60 else None
    out["dist_ma250_pct"] = round((close / ma250 - 1) * 100, 2) if ma250 else None
    if ma60 is None or ma250 is None:
        out["trend_state"] = None
        missing["trend_state"] = "均线窗口不足"
    elif close >= ma60 and close >= ma250:
        out["trend_state"] = "完好"
    elif close < ma60 and close >= ma250:
        out["trend_state"] = "中期回调"
    elif close < ma250 and out["dist_ma250_pct"] > -5:
        out["trend_state"] = "破位"
    else:
        out["trend_state"] = "深度破位"

    # --- 波动分位：20 日已实现波动率在过去 250 日同measure中的分位（普适，全史可得）
    def rvol(offset: int):
        seg = closes[offset : offset + 21]
        if len(seg) < 21:
            return None
        rets = [math.log(seg[i] / seg[i + 1]) for i in range(20)]
        m = sum(rets) / 20
        var = sum((x - m) ** 2 for x in rets) / 19
        return math.sqrt(var * 252)

    today_rv = rvol(0)
    hist = [v for v in (rvol(i) for i in range(0, 250)) if v is not None]
    if today_rv is not None and len(hist) >= 60:
        pctl = sum(1 for v in hist if v <= today_rv) / len(hist)
        out["rvol20_annual_pct"] = round(today_rv * 100, 2)
        out["vol_pctl"] = round(pctl, 3)
        out["vol_bucket"] = (
            "极端" if pctl >= 0.95 else "高" if pctl >= 0.8 else "中" if pctl >= 0.5 else "低"
        )
    else:
        out["vol_pctl"] = out["vol_bucket"] = None
        missing["vol_pctl"] = "已实现波动率历史窗口不足"

    # --- GVIX 分位（次要轴，2025-08-28 起才有；缺则 null，不回退不冒充）
    g = mc.execute(
        "SELECT percentile_1y FROM mkt_gvix_daily WHERE trade_date=?", (date_ymd,)
    ).fetchone()
    out["gvix_pctl_1y"] = round(float(g["percentile_1y"]), 3) if g and g["percentile_1y"] is not None else None
    if out["gvix_pctl_1y"] is None:
        missing["gvix_pctl_1y"] = "mkt_gvix_daily 覆盖不到该日（2025-08-28 起）"

    # --- 近期走势与急跌尖峰（拆成两轴）
    #     v0.1 教训：原 drop_speed = max(-单日最深, -5日累计/2) 退化成二值——任何 5 天里
    #     总有一天下跌，-worst_day 恒为正。改为：cum5 定"方向与幅度"（含上涨），
    #     worst_day 单独作"尖峰"轴（保留 2026-07-17 单日 -3.6% 被反抽抹平的教训）。
    if len(closes) >= 6:
        daily = [(closes[i] / closes[i + 1] - 1) * 100 for i in range(5)]
        worst_day = min(daily)
        cum5 = (closes[0] / closes[5] - 1) * 100
        out["worst_day_5d_pct"] = round(worst_day, 2)
        out["cum5_pct"] = round(cum5, 2)
        out["move5_bucket"] = (
            "上涨" if cum5 > 1.5 else "横盘" if cum5 >= -1.5 else "回调" if cum5 >= -4
            else "急跌" if cum5 >= -8 else "崩盘"
        )
        out["spike_bucket"] = (
            "无尖峰" if worst_day > -1.5 else "小尖峰" if worst_day > -3
            else "大尖峰" if worst_day > -5 else "极端尖峰"
        )
    else:
        out["move5_bucket"] = out["spike_bucket"] = None
        missing["move5"] = "近 5 日行情不足"

    # --- 宽度：板块 above_ma60 占比（board_trend_daily 自 2024-01-30）
    #     v0.1 用 sum(CASE WHEN above_ma60=1 THEN 1 ELSE 0 END) 把 NULL 当成"未站上"，
    #     等于用未知冒充已知。改为把 NULL 排除出分母，并在 NULL 比例过高时整轴标缺。
    b = mc.execute(
        "SELECT count(*) n_all, "
        "       sum(CASE WHEN above_ma60 IS NULL THEN 1 ELSE 0 END) n_null, "
        "       sum(CASE WHEN above_ma60=1 THEN 1 ELSE 0 END) n_up "
        "FROM board_trend_daily WHERE trade_date=?",
        (date_ymd,),
    ).fetchone()
    n_all = (b["n_all"] or 0) if b else 0
    n_null = (b["n_null"] or 0) if b else 0
    n_known = n_all - n_null
    if n_known >= 10 and n_null / max(n_all, 1) <= 0.2:
        frac = (b["n_up"] or 0) / n_known
        out["breadth_above_ma60"] = round(frac, 3)
        out["breadth_n_known"] = n_known
        out["breadth_bucket"] = (
            "崩塌" if frac < 0.2 else "弱" if frac < 0.4 else "中" if frac < 0.6 else "强"
        )
    else:
        out["breadth_above_ma60"] = out["breadth_bucket"] = None
        missing["breadth"] = (
            f"board_trend_daily 该日可用板块不足（总 {n_all} 条、above_ma60 为 NULL {n_null} 条）"
        )

    # --- 市场压力档（复合轴）
    #     直接交叉 trend×move5×breadth×vol 会格子爆炸（47 格 /121 例，18 个孤格）。
    #     改为先合成一个 0-10 的压力分再分档：格子数受控，且每档语义可读。
    pts = 0
    known = True
    for axis, table in (
        ("trend_state", {"完好": 0, "中期回调": 1, "破位": 2, "深度破位": 3}),
        ("move5_bucket", {"上涨": 0, "横盘": 0, "回调": 1, "急跌": 2, "崩盘": 3}),
        ("breadth_bucket", {"强": 0, "中": 0, "弱": 1, "崩塌": 2}),
        ("vol_bucket", {"低": 0, "中": 0, "高": 1, "极端": 2}),
    ):
        v = out.get(axis)
        if v is None:
            known = False
            break
        pts += table[v]
    if known:
        out["stress_score"] = pts
        out["stress_bucket"] = (
            "平静" if pts <= 1 else "偏紧" if pts <= 3 else "紧张" if pts <= 5 else "极端"
        )
    else:
        out["stress_score"] = out["stress_bucket"] = None
        missing["stress"] = "组成轴缺数，压力档不可求"

    return {"axes": out, "missing": missing}


# ---------------------------------------------------------------- 账户维

def account_axes(fc: sqlite3.Connection, bot_id: str, run_id: str, date: str) -> dict:
    """双回撤轴 + persona 控仓线 + 引擎钥匙注记。

    两个回撤量并存，互不包含：
      dd_alltime_pp —— run 内历史峰值回撤。引擎 crashSignal 用的就是这个
                       （run.ts:1560 取 performance.summary.current_drawdown_pct）。
      dd20_pp       —— 近 20 交易日滚动峰值回撤。各 bot METHODOLOGY 的
                       `### 回撤控仓` 全部按这个口径写规则。
    v0.1 只有前者，导致 persona 控仓线在坐标里不可见。
    """
    out: dict = {}
    missing: dict = {}
    rows = fc.execute(
        "SELECT trade_date, net_value, equity_weight, bond_weight, gold_weight, cash_weight, "
        "       daily_return_pct, cumulative_return_pct "
        "FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? AND trade_date<=? "
        "ORDER BY trade_date",
        (bot_id, run_id, date),
    ).fetchall()
    if not rows or rows[-1]["trade_date"] != date:
        missing["account"] = f"fund_bot_daily_snapshots 无 {bot_id}/{run_id}/{date}"
        return {"axes": out, "missing": missing}

    # ---- B3：处境按哪个时点取。row_post 恒为 D 收盘；rows 可能被截到 D-1。
    row_post = rows[-1]
    if ASOF_CALIBER == "predecision":
        if len(rows) < 2:
            missing["account_asof"] = (
                f"predecision 口径需要 D-1 快照，但 {bot_id}/{run_id} 在 {date} 是 run 首日"
            )
            return {"axes": out, "missing": missing}
        rows = rows[:-1]
    last = rows[-1]
    out["situation_asof_date"] = last["trade_date"]
    out["__asof_caliber"] = ASOF_CALIBER
    if last["net_value"] is None:
        # v0.1 用 navs[-1] 取"最后一个非空 net_value"，当日缺值时会静默拿昨天的净值冒充今天。
        missing["drawdown"] = f"{date} 当日 net_value 为 NULL，回撤不可求（不用前值冒充）"
        navs = None
    else:
        navs = [(r["trade_date"], float(r["net_value"])) for r in rows if r["net_value"] is not None]

    cfg = PERSONA_DD_LINES.get(bot_id)

    if navs:
        cur = navs[-1][1]
        peak_all = max(v for _, v in navs)
        if peak_all <= 0:
            missing["drawdown"] = f"run 内历史峰值净值 {peak_all} 非正，回撤不可求"
        else:
            dd_all = (cur / peak_all - 1) * 100
            out["dd_alltime_pp"] = round(dd_all, 2)
            a = abs(dd_all)
            out["dd_bucket"] = "无" if a < 1 else "浅" if a < 3 else "中" if a < 6 else "深"

            # --- 20 交易日滚动峰值（persona 文档口径）
            win = navs[-20:]
            peak20 = max(v for _, v in win)
            out["dd20_window_days"] = len(win)
            if peak20 <= 0:
                missing["dd20"] = f"20 日窗口峰值 {peak20} 非正"
            else:
                dd20 = (cur / peak20 - 1) * 100
                out["dd20_pp"] = round(dd20 if dd20 < 0 else 0.0, 2)
                b = abs(out["dd20_pp"])
                out["dd20_bucket"] = "无" if b < 1 else "浅" if b < 3 else "中" if b < 6 else "深"
                if len(win) < 20:
                    missing["dd20_window"] = (
                        f"run 内仅 {len(win)} 个交易日，20 日窗口未满，峰值取现有全部"
                    )

            # --- 引擎回撤钥匙（注记，不入 cell）
            enabled, thr, evidence = ENGINE_KEY.get(
                run_id, (None, ENGINE_DEFAULT_THRESHOLD, "该 run 未实勘")
            )
            out["engine_dd_threshold_pp"] = thr
            if enabled is None:
                out["engine_key_enabled"] = out["engine_dd_level"] = None
                missing["engine_key"] = f"run {run_id} 的引擎回撤触发开关未实勘，不猜"
            else:
                out["engine_key_enabled"] = enabled
                # v0.2.1 修正：钥匙关闭时必须写 None，不能写 0。
                # v0.2 初版写 0 并只在 __missing 里记原因，于是 engine_dd_level=0 同时表示
                # 「测量了、结果是 0 档」（8 例）与「根本没测量」（109 例）两件事——任何按该列
                # group by 而没同时读 engine_key_enabled 的消费方都会把两者混成一个分母。
                # 文档承诺"绝不写 0"，代码却在写 0，独立验证判 FAIL，这里改成真的不写。
                out["engine_dd_level"] = int(math.floor(a / thr)) if enabled else None
                out["__engine_key_evidence"] = evidence
                if not enabled:
                    missing["engine_dd_level"] = (
                        f"该 run 引擎回撤触发关闭，档位未被测量（不写 0，写 0 会与真实 0 档混淆）："
                        f"{evidence[:60]}"
                    )

            # --- persona 控仓线（dd20 口径）
            if cfg is None:
                out["persona_derisk_stage"] = out["persona_stage_bucket"] = None
                out["persona_gate_gap_pp"] = None
                missing["persona_lines"] = f"{bot_id} 未在 PERSONA_DD_LINES 中登记"
            elif not cfg["lines"]:
                out["persona_derisk_stage"] = None
                out["persona_stage_bucket"] = "无文档线"
                out["persona_gate_gap_pp"] = None
                missing["persona_lines"] = (
                    f"{bot_id} 的 persona 文档无分档回撤规则（{cfg['note'][:40]}）"
                )
            elif "dd20_pp" in out:
                # B1/B2：逐线选峰值口径与比较精度。deepest_pp 之外的线永远按 dd20。
                d20_raw = abs(min(0.0, dd20))
                dall_raw = a
                deepest_pp = max(t[0] for t in cfg["lines"])

                def _cmp_val(line_pp, _d20=d20_raw, _dall=dall_raw, _deep=deepest_pp):
                    base = _dall if (DEEPEST_CALIBER == "alltime" and line_pp == _deep) else _d20
                    return base if THRESHOLD_MODE == "raw" else round(base, 2)

                d = d20_raw if THRESHOLD_MODE == "raw" else round(d20_raw, 2)
                crossed = [t for t in cfg["lines"] if _cmp_val(t[0]) >= t[0]]
                stage = len(crossed)
                out["persona_derisk_stage"] = stage
                out["persona_stage_bucket"] = "线内" if stage == 0 else f"已越{stage}线"
                out["persona_lines_pp"] = [t[0] for t in cfg["lines"]]
                out["persona_crossed_label"] = crossed[-1][1] if crossed else None
                nxt = [t[0] for t in cfg["lines"] if _cmp_val(t[0]) < t[0]]
                # 这才是原设计里的 dist_to_dd_gate_pp：距下一条自己写的线还有多远。
                # 用该线自己的口径值算距离，否则 alltime 口径下距最深线的距离会算错。
                out["persona_gate_gap_pp"] = round(nxt[0] - _cmp_val(nxt[0]), 2) if nxt else None
                out["persona_line_source"] = cfg["source"]

                # 控仓线是阶梯不是叠加：越过更深的线【取代】更浅的线的区间要求
                # （bot102 的 4% 线要求 35-45%、5% 线要求 0-20%，两者求交是空集）。
                # 故 band 取【最深一条给了区间的已越线】；no_add 则是并集（任一条禁止即禁止）。
                req_band = None
                req_band_from = None
                no_add = False
                scopes = []
                rels = []
                for pp, lab, req in crossed:
                    if req.get("band") is not None:
                        req_band = req["band"]
                        req_band_from = pp
                    if req.get("no_add"):
                        no_add = True
                        scopes.append(req.get("no_add_scope") or "任意")
                    if req.get("relative"):
                        rels.append(req["relative"])
                out["persona_req_band"] = list(req_band) if req_band else None
                out["persona_req_band_from_line_pp"] = req_band_from
                out["persona_req_no_add"] = no_add
                out["persona_req_no_add_scope"] = "/".join(dict.fromkeys(scopes)) or None
                out["persona_req_relative"] = "/".join(dict.fromkeys(rels)) or None

                # 首次越线以来第几天（同一段回撤内）——lesson_3eaa 关心的"防御状态待了多久"
                since = 0
                for i in range(len(navs) - 1, -1, -1):
                    if dd20_at(navs, i) >= cfg["lines"][0][0]:
                        since += 1
                    else:
                        break
                out["days_since_first_cross"] = since

                entry = cfg.get("defensive_entry_pp")
                if entry is None:
                    out["defensive_state"] = None
                    missing["defensive_state"] = f"{bot_id} 文档未定义 defensive review"
                else:
                    # bot102 是滞回状态机（≥5% 进入，修复到 <2% 解除），必须沿时间推演，
                    # 不能拿当日 dd20 直接判。其余 bot 文档只写了进入、没写解除——
                    # 这正是 lesson_3eaa 指出的缺陷，此处如实标注而非替它补一个退出条件。
                    rel = cfg.get("release_pp")
                    state = False
                    on_since = None
                    for i in range(len(navs)):
                        # entry 恒等于该 bot 最深的那条线，故跟随 DEEPEST_CALIBER；
                        # release（仅 bot102 有）文档明写"20 日回撤回到 2% 以内"，不跟随。
                        d_i = ddall_at(navs, i) if DEEPEST_CALIBER == "alltime" else dd20_at(navs, i)
                        if d_i >= entry:
                            if not state:
                                on_since = navs[i][0]
                            state = True
                        elif state and rel is not None and d_i < rel:
                            state = False
                            on_since = None
                    out["defensive_state"] = "on" if state else "off"
                    out["defensive_on_since"] = on_since
                    if rel is None:
                        missing["defensive_exit"] = (
                            f"{bot_id} 文档只写了进入条件（≥{entry}%）未写解除条件，"
                            f"状态一旦置 on 无法机械解除"
                        )
            else:
                out["persona_derisk_stage"] = out["persona_stage_bucket"] = None
                out["persona_gate_gap_pp"] = None

    out["run_days_elapsed"] = len(rows)

    # 合规判定用 D 收盘权益（合规看的是收在哪），处境轴用 last（可能是 D-1）
    post_ok, post_sum = class_sum_ok(row_post)
    out["equity_weight_post"] = (round(float(row_post["equity_weight"]) * 100, 2)
                                 if post_ok and row_post["equity_weight"] is not None else None)
    if not post_ok:
        missing["equity_weight_post"] = (
            f"D 收盘四类权重之和={post_sum:.4f}≠1，收盘权益不可信"
        )

    ew = last["equity_weight"]
    cls_ok, cls_sum = class_sum_ok(last)
    if ew is None:
        out["equity_weight"] = out["equity_bucket"] = None
        out["equity_change_pp"] = out["equity_move"] = None
        missing["equity_weight"] = "快照缺 equity_weight"
    elif not cls_ok:
        # v0.2.1 新增。fund.db 的 equity_weight 是由 holdings_json[].asset_class 分类汇总出来的，
        # 而该字段在大量行上是空串 —— 分类器一条都没归到，汇总结果就是 0。实测全库 57.8% 的行
        # 四类权重之和 != 1（按 CLASS_SUM_TOL=0.02；换容差此比例会变，见模块 docstring），
        # 其中 121 案例命中 45 例（该 45 在 0.1/0.02/1e-4 三档下同值）：bot101 2026-07-09 真实持仓
        # 54.92% 却记 equity_weight=0，次日 asset_class 补齐后跳到 53.97%，坐标上会凭空多出一次
        # +53.97pp 的"加仓"，而当天 action_count=0、bot 什么都没做。
        # 权重和 != 1 是个一行就能查的不变量，v0.2 初版没查 —— 现在查，查不过就整组标 null。
        out["equity_weight"] = out["equity_bucket"] = None
        out["equity_change_pp"] = out["equity_move"] = None
        missing["equity_weight"] = (
            f"四类权重之和={cls_sum:.4f}≠1，说明 holdings_json[].asset_class 未分类完全，"
            f"equity_weight 由分类汇总而来因而不可信（非真实空仓）"
        )
    else:
        ew = float(ew) * 100
        out["equity_weight"] = round(ew, 2)
        out["equity_bucket"] = (
            "空仓" if ew < 5 else "轻仓" if ew < 40 else "中仓" if ew < 70 else "重仓"
        )
        # 仓位日变化：比 decision_type 更硬的"是否真的减了"证据，直接来自快照正本
        prev = rows[-2] if len(rows) >= 2 else None
        prev_ew = prev["equity_weight"] if prev is not None else None
        prev_ok = class_sum_ok(prev)[0] if prev is not None else False
        if prev_ew is None:
            out["equity_change_pp"] = out["equity_move"] = None
            missing["equity_change"] = "run 内无前一交易日快照"
        elif not prev_ok:
            # 前一日分类不全 → 差分的被减数是假的，差出来的"加仓/降仓"是幻影，不出这个轴
            out["equity_change_pp"] = out["equity_move"] = None
            missing["equity_change"] = (
                f"前一交易日 {prev['trade_date']} 四类权重之和={class_sum_ok(prev)[1]:.4f}≠1，"
                f"差分基准不可信"
            )
        else:
            out["equity_change_pp"] = round(ew - float(prev_ew) * 100, 2)
            out["equity_move"] = (
                "大幅降仓" if out["equity_change_pp"] <= -10 else
                "降仓" if out["equity_change_pp"] <= -2 else
                "加仓" if out["equity_change_pp"] >= 2 else "基本不变"
            )
    out["daily_return_pct"] = last["daily_return_pct"]
    out["cumulative_return_pct"] = last["cumulative_return_pct"]
    return {"axes": out, "missing": missing}


# ---------------------------------------------------------------- 信号维

_regime_cache: dict = {}


def _regime_name(mc: sqlite3.Connection, date_ymd: str):
    if date_ymd in _regime_cache:
        return _regime_cache[date_ymd]
    try:
        top15 = v5.top_boards(mc, date_ymd, limit=15)
        r = v5.regime(mc, date_ymd, top15)
        val = (r["name"], r["hs300_vs_ma120"], r["concentration"]["dominant_count"], None)
    except Exception as e:  # 数据不足等
        val = (None, None, None, f"{type(e).__name__}: {e}"[:120])
    _regime_cache[date_ymd] = val
    return val


def signal_axes(mc: sqlite3.Connection, strategy_family: str, date_ymd: str) -> dict:
    """v5 主线状态机。仅 multi_equity 族适用；其余 persona 标 null。

    ⚠ "其余 persona 用别的信号体系" 这个说法只对 12/46 例成立（bot10d/bot18d/bot20d
    从未装 market-mainline）。另 34 例（bot102 dash 26 + bot105d 8）所属 bot 是用
    v5 主线的，它们标 null 的真实原因是案发日早于该 bot 的 skill 落地日 —— 是 PIT
    边界不是体系边界。详见 V5_FAMILIES 处的注释与 03 §二之二 的更正块。

    信号年龄 = 同 regime 名连续维持的交易日数（向前走，上限 250 日）。
    """
    out: dict = {}
    missing: dict = {}
    if strategy_family not in V5_FAMILIES:
        missing["signal"] = f"persona 家族 {strategy_family} 不使用 v5 主线信号，v0 不求值"
        return {"axes": out, "missing": missing}

    name, dist120, dom, err = _regime_name(mc, date_ymd)
    if name is None:
        missing["signal"] = f"v5 regime 求值失败：{err or '板块或指数数据不足'}"
        return {"axes": out, "missing": missing}

    out["regime_name"] = name
    direction = {"抱主线·v4": "进攻", "无主线·宽基": "中性", "防御·红利": "防御"}.get(name)
    out["signal_direction"] = direction
    if direction is None:
        # v0.1 用 dict.get 把 "数据不足" 悄悄映射成 None，与"不适用"混为一谈
        missing["signal_direction"] = f"v5 regime 名 {name!r} 无对应方向（多为数据不足档）"
    out["hs300_vs_ma120_pct"] = round(dist120 * 100, 2) if dist120 is not None else None
    out["concentration_dominant_count"] = dom

    days = mc.execute(
        "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code=? AND trade_date<? "
        "ORDER BY trade_date DESC LIMIT 250",
        (BENCHMARK, date_ymd),
    ).fetchall()
    age = 1
    for d in days:
        prev = _regime_name(mc, d["trade_date"])[0]
        if prev != name:
            break
        age += 1
    out["signal_age_days"] = age
    out["signal_age_bucket"] = "D0-1" if age <= 1 else "D2-5" if age <= 5 else "D6-20" if age <= 20 else "D20+"
    return {"axes": out, "missing": missing}


# ---------------------------------------------------------------- 决策类型维

# 实测词汇表：fund_bot_actions.action_type 只有 ADD / REDUCE 两个值。
# v0.1 用 else→'换仓' 兜底，任何未知/畸形输入都会被悄悄叫作换仓。
ACTION_VOCAB = {"ADD": "加仓", "REDUCE": "减仓", "SWITCH": "换仓"}


def decision_axes(actions_json: str) -> dict:
    out: dict = {}
    missing: dict = {}
    try:
        actions = json.loads(actions_json or "[]")
    except json.JSONDecodeError as e:
        return {"axes": {"decision_type": None},
                "missing": {"decision_type": f"actual_actions_json 解析失败：{e}"}}

    if actions is None:
        actions = []
    if not isinstance(actions, list):
        return {"axes": {"decision_type": None, "action_count": None},
                "missing": {"decision_type": f"actual_actions_json 顶层不是数组而是 {type(actions).__name__}"}}

    out["action_count"] = len(actions)
    bad = [a for a in actions if not isinstance(a, dict)]
    if bad:
        return {"axes": {"decision_type": None, "action_count": len(actions)},
                "missing": {"decision_type": f"{len(bad)}/{len(actions)} 个动作元素不是对象"}}

    raw = [a.get("action_type") for a in actions]
    unknown = sorted({r for r in raw if r not in ACTION_VOCAB})
    if unknown:
        out["decision_type"] = None
        missing["decision_type"] = f"含词汇表外的 action_type {unknown}（已知仅 {sorted(ACTION_VOCAB)}）"
        return {"axes": out, "missing": missing}

    types = {ACTION_VOCAB[r] for r in raw}
    if not types:
        out["decision_type"] = "不动"
    elif len(types) == 1:
        out["decision_type"] = next(iter(types))
    else:
        out["decision_type"] = "换仓"
    return {"axes": out, "missing": missing}


# ---------------------------------------------------------------- 遵守判定

def compliance_axis(coords: dict) -> dict:
    """越线后账户【是否处于】规则要求的状态。

    v0.2 初版按"当日动作"判遵守，被 fund.db 正本证伪：bot101 在 2026-07-20 把权益从
    49.6% 一次砍到 4.2%（完全照它自己的规则做了），随后 14 天维持在 4% 附近不动——
    按动作判会把这 14 天全记成"越线未减"，而它们恰恰是遵守后的稳态。
    遵守是一个【状态】，不是一个【动作】。

    因此分两类判定，且只在文档写得可校验时才判：
      band 类   —— 文档给了总权益区间（全舰队只有 bot102 写了）→ 拿快照权益直接比；
      no_add 类 —— 文档禁止新增某类仓位 → 当日加仓即触发。但快照只有总权益、
                   分不出增强仓/主攻仓/卫星仓，故只能报"疑似"，不判定违规。
      relative 类（"降一档""卫星清零"）→ 机械不可校验，如实标注为不可校验。
    """
    out = {"persona_band_conform": None, "persona_no_add_flag": None, "persona_rule_state": None}
    stage = coords.get("persona_derisk_stage")
    if coords.get("persona_stage_bucket") == "无文档线":
        out["persona_rule_state"] = "无文档线"
        return out
    if stage is None:
        return out
    if stage == 0:
        out["persona_rule_state"] = "未越线"
        return out

    # 达标判定恒用 D 收盘权益：规则要求的是"降到某区间"，看的是收在哪不是起在哪。
    # close 口径下 equity_weight_post 与 equity_weight 同值，此改动对现状无影响。
    ew = coords.get("equity_weight_post")
    if ew is None:
        ew = coords.get("equity_weight")
    band = coords.get("persona_req_band")
    if band is None:
        out["persona_band_conform"] = "文档未给可校验区间"
    elif ew is None:
        out["persona_band_conform"] = None
    elif ew > band[1]:
        out["persona_band_conform"] = "超上限"
        out["persona_band_excess_pp"] = round(ew - band[1], 2)
    elif ew < band[0]:
        # 降风险规则里有约束力的是上限；低于下限是比要求更保守，不算违规
        out["persona_band_conform"] = "低于下限(更保守)"
        out["persona_band_excess_pp"] = round(ew - band[0], 2)
    else:
        out["persona_band_conform"] = "达标"
        out["persona_band_excess_pp"] = 0.0

    if coords.get("persona_req_no_add") and coords.get("decision_type") == "加仓":
        scope = coords.get("persona_req_no_add_scope")
        out["persona_no_add_flag"] = (
            "疑似违规" if scope and scope != "任意" else "违规"
        )
        out["persona_no_add_note"] = (
            f"该线禁止新增{scope}，当日为加仓；快照只有总权益，无法确认加的是不是{scope}"
            if scope and scope != "任意" else "该线禁止新增任何仓位，当日为加仓"
        )
    elif coords.get("persona_req_no_add"):
        out["persona_no_add_flag"] = "未加仓"

    # 复合状态，供 cell 与简报使用
    if out["persona_band_conform"] == "超上限":
        out["persona_rule_state"] = "越线未到位"
    elif out["persona_band_conform"] in ("达标", "低于下限(更保守)"):
        out["persona_rule_state"] = "越线已到位"
    elif out["persona_no_add_flag"] in ("违规", "疑似违规"):
        out["persona_rule_state"] = "越线加仓"
    else:
        out["persona_rule_state"] = "越线·规则不可校验"
    return out


# ---------------------------------------------------------------- 装配

CELL_CANDIDATES = {
    "A_趋势×回撤": ["trend_state", "dd_bucket"],
    "B_市场3轴": ["trend_state", "move5_bucket", "breadth_bucket"],
    "C_市场2+账户1": ["trend_state", "move5_bucket", "dd_bucket"],
    "D_四轴交叉": ["trend_state", "vol_bucket", "move5_bucket", "dd_bucket"],
    "E_压力×历史回撤": ["stress_bucket", "dd_bucket"],
    "F_压力×回撤×仓位": ["stress_bucket", "dd_bucket", "equity_bucket"],
    # v0.2 新增：把账户维换成 persona 自己的口径
    "G_压力×20日回撤": ["stress_bucket", "dd20_bucket"],
    "H_压力×越线档": ["stress_bucket", "persona_stage_bucket"],
    "I_压力×20日回撤×越线档": ["stress_bucket", "dd20_bucket", "persona_stage_bucket"],
    "J_压力×规则状态": ["stress_bucket", "persona_rule_state"],
}
# v0.2 改用 G：与 E 同形，但账户轴换成各 persona 文档实际使用的 20 日滚动峰值口径。
# 未选 H/J（越线档/规则状态）作 cell 轴的原因：它们是 persona 相对量，会把 bot102 的
# 5% 与 bot101 的 15% 塞进同一格，而 cell 的用途是让不同 persona 在【同一处境】相遇、
# 好算底率；persona 相对量应走 SCOPED_TO 边与谓词，不进 cell。
CELL_AXES = CELL_CANDIDATES["G_压力×20日回撤"]


def cell_of(coords: dict, axes) -> str:
    """任一轴缺数 → 整个 cell 判为坐标不全，绝不用 'NA' 冒充成一个格子。"""
    vals = [coords.get(a) for a in axes]
    if any(v is None for v in vals):
        return INCOMPLETE_CELL
    return "|".join(str(v) for v in vals)


def cell_id_of(coords: dict) -> str:
    return cell_of(coords, CELL_AXES)


REPORT_AXES = [
    "trend_state", "vol_bucket", "move5_bucket", "spike_bucket", "breadth_bucket",
    "stress_bucket", "dd_bucket", "dd20_bucket", "persona_stage_bucket", "defensive_state",
    "engine_key_enabled", "engine_dd_level", "equity_bucket", "equity_move",
    "signal_direction", "signal_age_bucket", "decision_type",
    "persona_band_conform", "persona_no_add_flag", "persona_rule_state",
]


def evaluate_case(mc, fc, case: sqlite3.Row, family: str) -> dict:
    date = case["trade_date"]
    ymd = _ymd(date)
    parts = [
        market_axes(mc, ymd),
        account_axes(fc, case["bot_id"], case["run_id"], date),
        signal_axes(mc, family, ymd),
        decision_axes(case["actual_actions_json"]),
    ]
    coords: dict = {}
    missing: dict = {}
    for p in parts:
        coords.update(p["axes"])
        missing.update(p["missing"])
    coords.update(compliance_axis(coords))
    coords["__missing"] = missing
    coords["__persona_family"] = family
    return coords


def coords_sha(coords: dict) -> str:
    payload = {k: v for k, v in coords.items() if not k.startswith("__")}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


# v0.2.1 新增。coords_sha 只覆盖语义坐标（cell 归属与统计都只依赖它），
# 但 __missing 的原因串、__engine_key_evidence 这些注记完全不进 sha，
# 于是"只改了注记"的重跑会被判成"未变"并 continue —— 旧注记留在库里，
# 且不产生任何存档痕迹。本次补丁改的正是 __missing 的措辞，
# 这个洞会把本次改动本身藏掉。故另算注记摘要，两者任一变化都重写并存档。
ANNOT_SKIP = ("__coords_sha", "__annot_sha")


def annot_sha(coords: dict) -> str:
    payload = {
        k: v for k, v in coords.items()
        if k.startswith("__") and k not in ANNOT_SKIP
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


DDL = """
CREATE TABLE IF NOT EXISTS situation_vectors (
  situation_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('case','backtest_day')),
  subject_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  coords_json TEXT NOT NULL CHECK (json_valid(coords_json)),
  cell_id TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  data_vintage TEXT NOT NULL,
  computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_situation_cell ON situation_vectors(cell_id);
CREATE INDEX IF NOT EXISTS idx_situation_subject ON situation_vectors(subject_type, subject_id);

-- v0.2：重述留痕。v0.1 的 INSERT OR REPLACE 会把旧坐标无声抹掉，
-- 而 data_vintage 只记行情版本、不反映坐标本身的变化。
CREATE TABLE IF NOT EXISTS situation_vectors_history (
  history_id INTEGER PRIMARY KEY AUTOINCREMENT,
  situation_id TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  coords_json TEXT NOT NULL,
  cell_id TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  data_vintage TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  superseded_at TEXT NOT NULL,
  superseded_by_sha TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sv_hist_subject ON situation_vectors_history(subject_type, subject_id);
"""


def persist(lib: sqlite3.Connection, results, vintage: str, now: str) -> dict:
    lib.executescript(DDL)
    stats = Counter()
    for cid, d, co, cell in results:
        sid = f"sv_{EVALUATOR_VERSION}_{cid}"
        sha = coords_sha(co)
        ash = annot_sha(co)
        old = lib.execute(
            "SELECT * FROM situation_vectors WHERE situation_id=?", (sid,)
        ).fetchone()
        if old is not None:
            try:
                old_co = json.loads(old["coords_json"])
                old_sha, old_ash = coords_sha(old_co), annot_sha(old_co)
            except json.JSONDecodeError:
                old_sha = old_ash = "unparseable"
            if old_sha == sha and old_ash == ash:
                stats["未变"] += 1
                continue
            if old_sha == sha:
                stats["仅注记变更(旧值已存档)"] += 1
            else:
                stats["重述(旧值已存档)"] += 1
            lib.execute(
                "INSERT INTO situation_vectors_history "
                "(situation_id, subject_type, subject_id, trade_date, coords_json, cell_id, "
                " evaluator_version, data_vintage, computed_at, superseded_at, superseded_by_sha) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (old["situation_id"], old["subject_type"], old["subject_id"], old["trade_date"],
                 old["coords_json"], old["cell_id"], old["evaluator_version"], old["data_vintage"],
                 old["computed_at"], now, sha),
            )
        else:
            stats["新增"] += 1
        co = dict(co, __coords_sha=sha, __annot_sha=ash)
        lib.execute(
            "INSERT OR REPLACE INTO situation_vectors "
            "(situation_id, subject_type, subject_id, trade_date, coords_json, cell_id, "
            " evaluator_version, data_vintage, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "case", cid, d, json.dumps(co, ensure_ascii=False), cell,
             EVALUATOR_VERSION, vintage, now),
        )

        # 旧版本行归档后移出 live 表：同一 subject 只保留一行"现行坐标"，
        # 否则任何 WHERE subject_id=? 的消费方都会拿到两行不同版本的坐标。
        for old_v in lib.execute(
            "SELECT * FROM situation_vectors WHERE subject_type='case' AND subject_id=? "
            "AND evaluator_version<>?", (cid, EVALUATOR_VERSION)
        ).fetchall():
            lib.execute(
                "INSERT INTO situation_vectors_history "
                "(situation_id, subject_type, subject_id, trade_date, coords_json, cell_id, "
                " evaluator_version, data_vintage, computed_at, superseded_at, superseded_by_sha) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (old_v["situation_id"], old_v["subject_type"], old_v["subject_id"], old_v["trade_date"],
                 old_v["coords_json"], old_v["cell_id"], old_v["evaluator_version"], old_v["data_vintage"],
                 old_v["computed_at"], now, sha),
            )
            lib.execute("DELETE FROM situation_vectors WHERE situation_id=?", (old_v["situation_id"],))
            stats[f"旧版本 {old_v['evaluator_version']} 已归档"] += 1
    lib.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True)
    ap.add_argument("--market-db", default=MARKET_DB)
    ap.add_argument("--fund-db", default=FUND_DB)
    ap.add_argument("--dry-run", action="store_true", help="只算不写，打印直方图")
    ap.add_argument("--threshold-mode", choices=("rounded", "raw"), default=THRESHOLD_MODE,
                    help="B1：阈值比较用展示精度(rounded, 现状)还是原始值(raw)")
    ap.add_argument("--deepest-caliber", choices=("dd20", "alltime"), default=DEEPEST_CALIBER,
                    help="B2：每个 bot 最深那条线按 20 日峰值(dd20, 现状)还是历史峰值(alltime)")
    ap.add_argument("--asof-caliber", choices=("close", "predecision"), default=ASOF_CALIBER,
                    help="B3：处境取 D 收盘(close, 现状)还是 D-1 收盘(predecision)")
    args = ap.parse_args()

    globals()["THRESHOLD_MODE"] = args.threshold_mode
    globals()["DEEPEST_CALIBER"] = args.deepest_caliber
    globals()["ASOF_CALIBER"] = args.asof_caliber
    if (args.threshold_mode, args.deepest_caliber, args.asof_caliber) != ("rounded", "dd20", "close"):
        print(f"!! 非默认口径运行：threshold_mode={args.threshold_mode} "
              f"deepest_caliber={args.deepest_caliber}；此结果与已交付的 v0.2 结论不可直接比较\n")

    mc = ro(args.market_db)
    fc = ro(args.fund_db)
    lib = sqlite3.connect(args.library_db)
    lib.row_factory = sqlite3.Row

    # 按 (bot_id, run_id) 取 family——bot102 有两份 persona 快照（dash run 的 family 为空，
    # oos run 为 multi_equity_medium），只按 bot_id 键会让后写的那份覆盖前一份。
    families = {}
    fallback = {}
    for r in lib.execute("SELECT bot_id, run_id, strategy_family FROM agent_persona_snapshots"):
        fam = r["strategy_family"] or "unknown"
        families[(r["bot_id"], r["run_id"])] = fam
        if fam != "unknown" or r["bot_id"] not in fallback:
            fallback[r["bot_id"]] = fam

    cases = lib.execute(
        "SELECT case_id, bot_id, run_id, trade_date, actual_actions_json FROM experience_cases ORDER BY trade_date"
    ).fetchall()

    vintage = mc.execute(
        "SELECT max(trade_date) d FROM index_daily WHERE ts_code=?", (BENCHMARK,)
    ).fetchone()["d"]
    now = datetime.now(timezone.utc).isoformat()

    results = []
    for c in cases:
        family = families.get((c["bot_id"], c["run_id"])) or fallback.get(c["bot_id"], "unknown")
        coords = evaluate_case(mc, fc, c, family)
        coords["__bot_id"] = c["bot_id"]
        coords["__run_id"] = c["run_id"]
        results.append((c["case_id"], c["trade_date"], coords, cell_id_of(coords)))

    if not args.dry_run:
        stats = persist(lib, results, vintage, now)
        print("== 落库 ==  " + "  ".join(f"{k}={v}" for k, v in stats.most_common()) + "\n")

    print(f"== 求值器 {EVALUATOR_VERSION} | 行情 vintage {vintage} | 案例 {len(results)} ==\n")

    axis_cov: dict = {}
    for _, _, co, _ in results:
        for a in REPORT_AXES:
            axis_cov.setdefault(a, [0, 0])
            axis_cov[a][1] += 1
            if co.get(a) is not None:
                axis_cov[a][0] += 1
    print("-- 各轴覆盖率 --")
    for a, (ok, tot) in axis_cov.items():
        print(f"  {a:26s} {ok:3d}/{tot}  {ok/tot*100:5.1f}%")

    print("\n-- 候选分格方案对比（供拍板点 1）--")
    print("  注：「≥5例格覆盖率」奖励粗粒度——把所有案例塞进一格也能拿 100%。")
    print("      故并列「最大格占比」与「均匀度」（归一化熵，1=完全均匀，0=全挤一格）作为反向指标。")
    print("      ⚠ 两列分母不同：「占比」分母=可分格案例数（已排除「坐标不全」列那些），")
    print("        「≥5例覆盖」分母恒为全部案例数。只有 F 方案两者不等（76 vs 121），跨列比较时注意。")
    print(f"  {'方案':22s} {'格数':>4s} {'最大格':>5s} {'占比':>6s} {'中位格':>5s} {'孤格':>4s} "
          f"{'坐标不全':>6s} {'≥5例覆盖':>8s} {'均匀度':>6s}")
    for tag, axes in CELL_CANDIDATES.items():
        cells = [cell_of(co, axes) for _, _, co, _ in results]
        n_inc = sum(1 for x in cells if x == INCOMPLETE_CELL)
        c = Counter(x for x in cells if x != INCOMPLETE_CELL)
        if not c:
            print(f"  {tag:22s} {'-':>4s}")
            continue
        sizes = sorted(c.values())
        tot = sum(sizes)
        med = sizes[len(sizes) // 2]
        singles = sum(1 for v in sizes if v == 1)
        cov = sum(v for v in sizes if v >= 5) / len(results)
        ent = -sum((v / tot) * math.log(v / tot) for v in sizes)
        even = ent / math.log(len(sizes)) if len(sizes) > 1 else 0.0
        print(f"  {tag:22s} {len(c):4d} {max(sizes):5d} {max(sizes)/tot*100:5.1f}% {med:5d} {singles:4d} "
              f"{n_inc:6d} {cov*100:7.1f}% {even:6.3f}")

    print("\n-- 分格直方图（正式 cell = %s）--" % "×".join(CELL_AXES))
    cnt = Counter(cell for _, _, _, cell in results)
    for cell, n in cnt.most_common():
        print(f"  {n:3d}  {cell}")
    print(f"  合计 {len(cnt)} 格 / {len(results)} 案例；单案例格 {sum(1 for v in cnt.values() if v==1)} 个")

    # --- v0.2 重点：persona 控仓线的实际穿越情况
    print("\n-- persona 控仓线穿越（dd20 口径，逐 bot）--")
    by_bot: dict = {}
    for _, _, co, _ in results:
        by_bot.setdefault(co.get("__bot_id"), []).append(co)
    print(f"  {'bot':10s} {'案例':>4s} {'线(pp)':16s} {'越≥1线':>6s} {'越≥2线':>6s} {'defensive on':>12s} 规则可校验性")
    for bot, items in sorted(by_bot.items()):
        cfg = PERSONA_DD_LINES.get(bot, {})
        lines = ",".join(str(t[0]) for t in cfg.get("lines", [])) or "无"
        s1 = sum(1 for co in items if (co.get("persona_derisk_stage") or 0) >= 1)
        s2 = sum(1 for co in items if (co.get("persona_derisk_stage") or 0) >= 2)
        dfs = sum(1 for co in items if co.get("defensive_state") == "on")
        has_band = any(t[2].get("band") for t in cfg.get("lines", []))
        kind = "有总权益区间(可校验)" if has_band else ("仅相对/禁新增(不可校验)" if cfg.get("lines") else "—")
        print(f"  {bot:10s} {len(items):4d} {lines:16s} {s1:6d} {s2:6d} {dfs:12d} {kind}")

    print("\n-- 越线后账户是否处于规则要求的状态（状态判定，非动作判定）--")
    c = Counter(co.get("persona_rule_state") for _, _, co, _ in results)
    for k, v in c.most_common():
        print(f"  {str(k):18s} {v:3d}")
    print("  band 达标情况：" + "  ".join(
        f"{k}={v}" for k, v in Counter(co.get("persona_band_conform") for _, _, co, _ in results).most_common()))
    print("  禁新增线命中：" + "  ".join(
        f"{k}={v}" for k, v in Counter(co.get("persona_no_add_flag") for _, _, co, _ in results).most_common()))

    bad = [(cid, d, co) for cid, d, co, _ in results
           if co.get("persona_rule_state") in ("越线未到位", "越线加仓")]
    print(f"\n  越线但状态未达要求的案例 {len(bad)} 个（按 bot/日期）：")
    for cid, d, co in sorted(bad, key=lambda x: (x[2].get("__bot_id"), x[1])):
        print(f"    {d} {co.get('__bot_id'):8s} dd20={co.get('dd20_pp'):>7}pp stage={co.get('persona_derisk_stage')} "
              f"权益={co.get('equity_weight')}% 要求={co.get('persona_req_band')} "
              f"超出={co.get('persona_band_excess_pp')}pp 动作={co.get('decision_type')} "
              f"仓位变化={co.get('equity_change_pp')}pp → {co.get('persona_rule_state')}")

    print("\n-- 越线段的连续性（同一段回撤内的多天不是多个独立事件）--")
    seg: dict = {}
    for cid, d, co, _ in results:
        if (co.get("persona_derisk_stage") or 0) >= 1:
            seg.setdefault(co.get("__bot_id"), []).append((d, co))
    for bot, items in sorted(seg.items()):
        # 只按日期排序：items 是 (日期, coords字典)，裸 sort() 在同 bot 同日两条记录上
        # 会退化成比较 dict 而抛 TypeError。当前 121 例无重复 (bot,date)，但
        # experience_cases 上没有 (bot_id,trade_date) 唯一约束，跨 run 覆盖同一天即触发；
        # 且崩溃点在写库提交之后，会造成"库已写好但退出码 1"的假失败。（第六轮验证发现）
        items.sort(key=lambda x: x[0])
        print(f"  {bot}: 越线案例 {len(items)} 天，{items[0][0]} ~ {items[-1][0]}，"
              f"首次越线以来最长第 {max(co.get('days_since_first_cross') or 0 for _, co in items)} 天；"
              f"权益路径 {items[0][1].get('equity_weight')}% → {items[-1][1].get('equity_weight')}%")

    print("\n-- 引擎回撤钥匙（逐 run）--")
    by_run: dict = {}
    for _, _, co, _ in results:
        by_run.setdefault(co.get("__run_id"), []).append(co)
    for run, items in sorted(by_run.items(), key=lambda kv: -len(kv[1])):
        en = items[0].get("engine_key_enabled")
        lv = Counter(co.get("engine_dd_level") for co in items)
        deepest = min((co.get("dd_alltime_pp") for co in items if co.get("dd_alltime_pp") is not None), default=None)
        print(f"  {run:44s} 案例{len(items):3d}  钥匙={'开' if en else '关' if en is not None else '未实勘'}"
              f"  档位分布={dict(lv)}  最深历史回撤={deepest}pp")

    print("\n-- 两个回撤口径的差（|dd20| - |dd_alltime|，负值=20日窗口更浅）--")
    diffs = [(co.get("dd20_pp"), co.get("dd_alltime_pp"), co.get("__bot_id"))
             for _, _, co, _ in results if co.get("dd20_pp") is not None and co.get("dd_alltime_pp") is not None]
    if diffs:
        gaps = [abs(a) - abs(b) for a, b, _ in diffs]
        same = sum(1 for g in gaps if abs(g) < 0.01)
        print(f"  可比 {len(diffs)} 例；两口径相同 {same} 例；"
              f"差值 中位 {sorted(gaps)[len(gaps)//2]:.2f}pp  最小 {min(gaps):.2f}pp  最大 {max(gaps):.2f}pp")
        bucket_diff = sum(1 for _, _, co, _ in results
                          if co.get("dd_bucket") and co.get("dd20_bucket")
                          and co["dd_bucket"] != co["dd20_bucket"])
        print(f"  两口径落在【不同回撤档】的案例 {bucket_diff} 个 —— v0.1 只有历史峰值口径，这些全被归错档")
        full = sum(1 for _, _, co, _ in results if (co.get("dd20_window_days") or 0) >= 20)
        print(f"  ⚠ 20 日窗口已满的只有 {full}/{len(results)} 例；其余 {len(results)-full} 例 run 内不足 20 个交易日，"
              f"dd20 实际退化为「自 run 起点以来的回撤」，与 dd_alltime 恒等")

    print("\n-- 同日多 bot 检查（市场维应同、账户维应异）--")
    by_date: dict = {}
    for cid, d, co, _ in results:
        by_date.setdefault(d, []).append((co.get("__bot_id"), co))
    multi = {d: v for d, v in by_date.items() if len(v) > 1}
    same_mkt = diff_acct = 0
    for d, items in sorted(multi.items()):
        mkts = {cell_of(co, ["trend_state", "move5_bucket", "breadth_bucket"]) for _, co in items}
        accts = {cell_of(co, ["dd20_bucket", "equity_bucket"]) for _, co in items}
        if len(mkts) == 1:
            same_mkt += 1
        if len(accts) > 1:
            diff_acct += 1
    print(f"  多 bot 交易日 {len(multi)} 天；市场维完全一致 {same_mkt} 天；账户维出现分化 {diff_acct} 天")
    for d, items in sorted(multi.items())[:5]:
        print(f"    {d}: " + "; ".join(
            f"{b}[{cell_of(co,['dd20_bucket','equity_bucket','decision_type'])}]" for b, co in items))

    print("\n-- 各轴取值分布 --")
    for a in REPORT_AXES:
        c = Counter(co.get(a) for _, _, co, _ in results)
        print(f"  {a:26s} " + "  ".join(f"{k}={v}" for k, v in c.most_common()))

    print("\n-- 缺数原因汇总 --")
    mc2 = Counter()
    for _, _, co, _ in results:
        for k, v in co.get("__missing", {}).items():
            mc2[f"{k}: {v[:70]}"] += 1
    for k, v in mc2.most_common():
        print(f"  {v:3d}  {k}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
