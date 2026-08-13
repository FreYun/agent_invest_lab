#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 12 张经验卡的「什么时候适用」从中文翻译成可求值的谓词。

**这一步为什么危险。** 谓词写宽了，卡上会挂一堆不相干的先例，底率被稀释成噪音；
写窄了，卡永远没有先例，看起来像"这条经验没用"。两种错都不会报错——它们只会
安静地产出一个看起来很像样的数字。所以本模块的每条谓词都必须过一道自测：

    **谓词必须命中它自己那张卡声明的来源案例。**

一条连自己出处都框不住的谓词，一定是写错了。`--selftest` 把这件事做成硬门禁。

**三态，不是两态。** 每条谓词返回 True / False / None：
    True  = 确定适用
    False = 确定不适用
    None  = **无法判定**（缺轴、缺文档、坐标不全）
把 None 折叠进 False 等于把"没查过"记成"查过且不符合"——这正是伪零缺陷当初
骗过所有人的方式（`equity_weight` 缺失被写成 0）。所以三态一路保留到边表。

**不可表达的卡不降级。** 有 5 张卡的条件依赖当前坐标系里根本没有的轴
（持仓角色、相对强弱排名、流动性闸门、正文自述、叙述目标）。它们被标成
`unexpressible` 并写明**缺哪根轴**，不建自动边、也不降级这张卡。
「卡没有先例」和「卡是错的」是两回事。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import situation_eval_v02 as ev  # noqa: E402

PREDICATE_VERSION = "card_pred_v0.1"


class Subject:
    """一个待判定对象：案例或回测日。

    prv / nxt 是同一 (bot,run) 的前一 / 后一个交易日，供序列型谓词用。
    案例的 prv/nxt 取自日级表——这正是 P0.5 扩量换来的能力：
    在只有 121 个案例的世界里，"次日反转"这种形状根本没法判。
    """

    __slots__ = ("kind", "sid", "bot", "run", "date", "co", "case", "prv", "nxt")

    def __init__(self, kind, sid, bot, run, date, co, case=None, prv=None, nxt=None):
        self.kind, self.sid, self.bot, self.run, self.date = kind, sid, bot, run, date
        self.co, self.case, self.prv, self.nxt = co, case, prv, nxt

    def g(self, key, default=None):
        v = self.co.get(key, default)
        return default if v is None else v

    def has(self, *keys):
        """坐标里这些键是否都拿到了非 None 值——用来把「无法判定」与「不适用」分开。"""
        return all(self.co.get(k) is not None for k in keys)


def B(**kw):
    """basis：命中时把参与判定的坐标值原样记下来，供前端下钻。"""
    return kw


# ----------------------------------------------------------------- 处境类谓词

def p_3eaa(s: Subject):
    """防御状态的退出条件在防御仓位内不可达 → 吸收态。

    只有 bot102 写了解除条件（20 日回撤回到 2% 以内）。其余 3 个 bot 只写了
    进入、没写解除，机械上永远解除不了——但那是**文档缺口**，不是"该卡不适用"，
    故返回 None（无法判定）而不是 False。这与 B4 是同一件事。
    """
    if not s.has("defensive_state"):
        return None, B(reason="defensive_state 未知")
    if s.co["defensive_state"] != "on":
        return False, B(defensive_state=s.co["defensive_state"])
    cfg = ev.PERSONA_DD_LINES.get(s.bot) or {}
    rel = cfg.get("release_pp")
    if rel is None:
        return None, B(reason=f"{s.bot} 文档未写解除条件，无法判定是否卡在吸收态",
                       defensive_state="on", 缺口="release_pp")
    if not s.has("dd20_pp"):
        return None, B(reason="dd20_pp 未知")
    dd = abs(s.co["dd20_pp"])
    stuck = dd > rel
    return stuck, B(defensive_state="on", dd20_pp=s.co["dd20_pp"],
                    release_pp=rel, 距解除还差=round(dd - rel, 2))


def p_42f1(s: Subject):
    """已越过/紧贴自己文档的回撤闸门，却仍在执行"必须建仓"类铁律。

    主判据 `persona_no_add_flag == '疑似违规'`：越过了一条明写"禁止新增"的线，
    当日仍加仓。之所以只能报"疑似"——快照只有总权益，分不出增强仓/主攻仓子桶。

    次判据"临近闸门仍加仓"（gap ≤ 0.5pp 且当日加仓）覆盖尚未越线的那一侧。

    ⚠ 与待拍板清单里那个"0 实例"的关系：那处用的是**更严的**形式化
    （越线 **且** 卡在引擎卖出承诺窗口里），实测 0 例，结论不变、仍标"无本地先例"。
    本谓词实现的是**卡自己的 claim**（临近组合闸门时建仓铁律应让位），
    它的来源案例 07-21 正是这个 claim 的实例。两种形式化并存，不互相取消。
    """
    if not s.has("persona_rule_state"):
        return None, B(reason="persona 规则状态未知")
    flag = s.co.get("persona_no_add_flag")
    if flag == "疑似违规":
        return True, B(判据="越线后仍加仓", persona_no_add_flag=flag,
                       persona_derisk_stage=s.co.get("persona_derisk_stage"),
                       dd20_pp=s.co.get("dd20_pp"),
                       decision_type=s.co.get("decision_type"))
    gap = s.co.get("persona_gate_gap_pp")
    if gap is not None and gap <= 0.5 and s.co.get("decision_type") == "加仓":
        return True, B(判据="距下一条线≤0.5pp 仍加仓", persona_gate_gap_pp=gap,
                       decision_type="加仓", dd20_pp=s.co.get("dd20_pp"))
    if s.co.get("persona_rule_state") == "无文档线":
        return None, B(reason=f"{s.bot} 无成文回撤分档线，无闸门可临近")
    return False, B(persona_no_add_flag=flag, persona_gate_gap_pp=gap,
                    decision_type=s.co.get("decision_type"))


def p_64e5(s: Subject):
    """趋势仍深度破位时，从低仓一次性提到基准仓。"""
    need = ("trend_state", "decision_type", "equity_bucket", "dd_alltime_pp")
    if not s.has(*need):
        return None, B(reason="缺 " + ",".join(k for k in need if s.co.get(k) is None))
    hit = (s.co["trend_state"] in ("破位", "中期回调")
           and s.co["decision_type"] == "加仓"
           and s.co["equity_bucket"] in ("空仓", "轻仓")
           and s.co["dd_alltime_pp"] <= -10.0)
    return hit, B(trend_state=s.co["trend_state"], decision_type=s.co["decision_type"],
                  equity_bucket=s.co["equity_bucket"], equity_weight=s.co.get("equity_weight"),
                  dd_alltime_pp=s.co["dd_alltime_pp"])


def p_9c2b(s: Subject):
    """长期趋势已破位，却仍按单一偏离阈值加仓（缺趋势/动量二次确认）。"""
    need = ("trend_state", "decision_type")
    if not s.has(*need):
        return None, B(reason="缺 " + ",".join(k for k in need if s.co.get(k) is None))
    hit = s.co["trend_state"] == "破位" and s.co["decision_type"] == "加仓"
    return hit, B(trend_state=s.co["trend_state"], decision_type=s.co["decision_type"],
                  stress_bucket=s.co.get("stress_bucket"),
                  equity_weight=s.co.get("equity_weight"),
                  dist_ma60_pct=s.co.get("dist_ma60_pct"))


def p_b449(s: Subject):
    """三个都能被叫做"账户回撤"的数彼此分歧到足以改变闸门结论。

    卡的 claim 是：累计收益 / 当前距峰值回撤 / 历史最大回撤 不得统称"账户回撤"。
    机械化：这三个数两两之差 ≥ 3pp 时，"取哪个"就决定了闸门响不响——
    此时口径歧义是**实质性的**而非学究式的。
    """
    need = ("dd20_pp", "dd_alltime_pp", "cumulative_return_pct")
    if not s.has(*need):
        return None, B(reason="缺 " + ",".join(k for k in need if s.co.get(k) is None))
    a, b, c = s.co["dd20_pp"], s.co["dd_alltime_pp"], s.co["cumulative_return_pct"]
    spread = max(abs(a - b), abs(b - c), abs(a - c))
    return spread >= 3.0, B(dd20_pp=a, dd_alltime_pp=b, cumulative_return_pct=c,
                            最大分歧pp=round(spread, 2), 门槛pp=3.0)


def p_2149(s: Subject):
    """单日阈值触发带来完整仓位步长，次日阈值回落即反向撤销。

    序列型：D 日在尖峰行情里加仓，D+1 日减仓。只有日级坐标铺满之后才判得了——
    121 个案例是稀疏采样的，相邻两天大多不同时在库里。
    """
    if s.nxt is None:
        return None, B(reason="没有次一交易日坐标（run 末日或日级表未覆盖）")
    need = ("decision_type", "spike_bucket")
    if not s.has(*need):
        return None, B(reason="缺 " + ",".join(k for k in need if s.co.get(k) is None))
    if s.nxt.get("decision_type") is None:
        return None, B(reason="次日 decision_type 未知")
    hit = (s.co["decision_type"] == "加仓"
           and s.co["spike_bucket"] in ("大尖峰", "小尖峰")
           and s.nxt["decision_type"] == "减仓")
    return hit, B(当日=s.co["decision_type"], 次日=s.nxt["decision_type"],
                  spike_bucket=s.co["spike_bucket"], vol_bucket=s.co.get("vol_bucket"),
                  次日日期=s.nxt.get("__date"))


# ----------------------------------------------------------------- 过程类谓词

_DATE_RE = re.compile(r"(20\d{2})[-/年]?(\d{1,2})[-/月]?(\d{1,2})")


def _dates_in(text: str):
    out = []
    for y, m, d in _DATE_RE.findall(text or ""):
        try:
            out.append("%04d-%02d-%02d" % (int(y), int(m), int(d)))
        except ValueError:
            continue
    return out


def p_477b(s: Subject):
    """注入案例的分块标题里出现了晚于决策日的日期 → 未来信息泄漏。

    ⚠ 本卡原先声明的判据是"source_verified=1 但 pit_verified=0"。实测
    **121/121 全部如此**——这三列在本库里是常量，没有任何判别力，
    拿它当证据等于没有证据。故改用卡自己 claim 里那句可机械化的部分：
    「每个注入报告的标题日期…都必须不晚于决策日」。

    只扫 `sections_json` 的 heading，不扫 body：正文出现未来日期可能是合法的
    前瞻表述（"下周一将…"），标题日期不是。宁可漏，不可脏。
    """
    if s.case is None:
        return None, B(reason="回测日没有注入报告分块，本卡只对案例适用")
    try:
        secs = json.loads(s.case["sections_json"] or "[]")
    except json.JSONDecodeError as e:
        return None, B(reason=f"sections_json 解析失败：{e}")
    bad = []
    for sec in secs if isinstance(secs, list) else []:
        if not isinstance(sec, dict):
            continue
        head = sec.get("heading") or ""
        for d in _dates_in(head):
            if d > s.date:
                bad.append({"heading": head[:60], "日期": d})
    if bad:
        return True, B(判据="分块标题日期晚于决策日", trade_date=s.date, 命中=bad)
    return False, B(trade_date=s.date, 扫描分块数=len(secs) if isinstance(secs, list) else 0)


# ----------------------------------------------------------------- 不可表达的卡

UNEXPRESSIBLE = {
    "lesson_16c13ce7b4ee593d30b3": dict(
        needs=["正文自述动作 vs 账本动作 的比对轴"],
        why="卡的判据是「最终回复自称维持不动，账本却有同日 5 笔减仓」。"
            "坐标系里有 action_count，但没有「正文声称做了什么」这一侧，"
            "两者对不上才是本卡的处境。缺的不是数据而是一根新轴。"
            "注：其来源案例 orders 非空，所以「有动作无订单」这个便宜的替代判据"
            "连自己的出处都框不住，已弃用。"),
    "lesson_65aabd451af05757661f": dict(
        needs=["叙述目标权重轴（正文声称的目标 vs 成交后实际权重）"],
        why="卡的判据是「正文称减至约 17%，成交后实际 19.14%」。"
            "成交后权重坐标里有，正文叙述的目标没有。"),
    "lesson_7d3a5c69b0f2469ea418": dict(
        needs=["执行故障/工具失败轴", "未执行动作留痕"],
        why="卡的处境是「已确认的风险切换因工具故障未完成」。"
            "execution_verified 在本库 121/121 恒为 1，无判别力；"
            "run.log 里的工具失败没有进坐标系。"),
    "lesson_9c2b_placeholder": dict(needs=[], why=""),  # 占位，见下方 registry 覆盖
    "lesson_ac775eb3bb8894c15c56": dict(
        needs=["流动性指标轴", "风险闸门触发/恢复状态轴"],
        why="卡的处境是「流动性跌破硬阈值 → 降险；单日回到阈值上方但未满足连续确认 → 不恢复」。"
            "坐标系里没有流动性轴，也没有「闸门此刻处于降险态还是恢复观察态」。"
            "engine_key_enabled 只说钥匙开没开，不是这个。"),
    "lesson_cec8ce1a44805ce07e50": dict(
        needs=["持仓角色轴（核心/卫星）", "相对强弱排名轴"],
        why="卡的判据是「卫星的相对强弱或排名退出预设范围」。"
            "坐标系只有总权益与四类权重，分不出哪一笔是卫星，也没有排名。"),
}
UNEXPRESSIBLE.pop("lesson_9c2b_placeholder")


# hit_means：谓词为真时，这条边到底在说什么。**必须逐卡声明，不许从谓词形状反推。**
#   违反 —— 主体做了卡明确反对的事（边上带"违反"，是负面先例）
#   处境 —— 主体处在卡描述的那种局面里，本身不含褒贬（"回撤口径分歧"就属于这类：
#           三个口径分叉是客观状态，不是谁做错了）
#   缺陷 —— 命中说明的是系统/文档的缺陷，不是 bot 的选择（防御态出不来是没写解除条件）
#   流程 —— 命中说明的是产出物本身的流程问题（标题日期越界是写作时点问题）
# 之所以要声明：把"命中=违反"当默认值，会让"回撤口径分歧"那 1,246 天全部变成
# 1,246 次违规——一个纯粹由默认值制造出来的结论。
REGISTRY = {
    "lesson_3eaa916ad5a24da5b35b": dict(kind="situation", fn=p_3eaa,
                                        slug="防御态吸收态", hit_means="缺陷"),
    "lesson_42f13bc09a7e4a93b105": dict(kind="situation", fn=p_42f1,
                                        slug="闸门边缘仍建仓", hit_means="违反"),
    "lesson_64e5df1855c414503f92": dict(kind="situation", fn=p_64e5,
                                        slug="破位时一次性提仓", hit_means="违反"),
    "lesson_9c2b816a5de04f7691e3": dict(kind="situation", fn=p_9c2b,
                                        slug="破位仍按单一阈值加仓", hit_means="违反"),
    "lesson_b4491a6c4ca739476386": dict(kind="situation", fn=p_b449,
                                        slug="回撤口径分歧", hit_means="处境"),
    "lesson_2149049dbace8ef48b12": dict(kind="sequence", fn=p_2149,
                                        slug="次日反转", hit_means="违反"),
    "lesson_477b3db8cbd36945f168": dict(kind="process", fn=p_477b,
                                        slug="标题日期越界", hit_means="流程"),
}

# 自测的例外：谓词没命中某条 curation_evidence 来源，但确有正当理由。
# **每条例外必须写明理由，且理由必须是可反驳的**——这张表是给复核人看的，
# 不是给我自己开脱的。空着比编一条好。
SELFTEST_EXCEPTIONS = {
    ("lesson_2149049dbace8ef48b12", "case_893f84c25617a2f034f9"):
        "这张卡讲的是「D 日凭短暂阈值一次性上仓位」。两条来源里 case_30c48 是 D 日"
        "（2026-07-21 目标 40%→60%，谓词命中），case_893f84 是 D+1 日"
        "（2026-07-22 阈值回落、目标退回 40%）——它是这次行为的**后果**，不是第二次"
        "同类行为。谓词锚在 D 日、对 D+1 判 False，是它该有的行为；若为了"
        "「命中全部来源」把 D+1 也框进来，等于把因和果都算成因，此后底率的分子会翻倍。"
        "该案例以 role=后果 的边入库，来源是卡的出处声明而非机械匹配。",
}
for cid, meta in UNEXPRESSIBLE.items():
    REGISTRY[cid] = dict(kind="unexpressible", fn=None, slug="(不可表达)", **meta)


# ----------------------------------------------------------------- 装载

def load_subjects(lib: sqlite3.Connection):
    cases = {r["case_id"]: r for r in lib.execute("SELECT * FROM experience_cases")}
    rows = lib.execute(
        "SELECT subject_type, subject_id, trade_date, coords_json, cell_id FROM situation_vectors"
    ).fetchall()
    by_key = {}
    parsed = []
    for r in rows:
        co = json.loads(r["coords_json"])
        co["__date"] = r["trade_date"]
        co["__cell_id"] = r["cell_id"]
        bot = co.get("__bot_id")
        run = co.get("__run_id")
        parsed.append((r, co, bot, run))
        if r["subject_type"] == "backtest_day":
            by_key[(bot, run, r["trade_date"])] = co
    # 同一 (bot,run) 的交易日序列，用于取前后日
    seq = {}
    for (bot, run, d) in by_key:
        seq.setdefault((bot, run), []).append(d)
    for k in seq:
        seq[k].sort()
    idx = {k: {d: i for i, d in enumerate(v)} for k, v in seq.items()}

    subs = []
    for r, co, bot, run, in parsed:
        prv = nxt = None
        key = (bot, run)
        if key in idx and r["trade_date"] in idx[key]:
            i = idx[key][r["trade_date"]]
            days = seq[key]
            if i > 0:
                prv = by_key[(bot, run, days[i - 1])]
            if i + 1 < len(days):
                nxt = by_key[(bot, run, days[i + 1])]
        subs.append(Subject(r["subject_type"], r["subject_id"], bot, run, r["trade_date"],
                            co, cases.get(r["subject_id"]), prv, nxt))
    return subs, cases


def source_case_ids(lib: sqlite3.Connection):
    out = {}
    for r in lib.execute("SELECT card_id, decision_context_json, title "
                         "FROM experience_card_versions WHERE status='candidate'"):
        ctx = json.loads(r["decision_context_json"])
        ids = ctx.get("source_case_ids") or (
            [ctx["source_case_id"]] if "source_case_id" in ctx else [])
        out[r["card_id"]] = (ids, r["title"])
    return out


def source_roles(lib: sqlite3.Connection):
    """{(card_id, case_id): source_role}。

    库里已经记了每条来源是以什么身份被引用的（curation_evidence 是"卡由它提炼而来"，
    execution_follow_through / later_observation 是"它记录的是这件事的后续"）。
    自测把这两类分开看：谓词框不住一条**后续**案例是正常的——那条案例本来就不是
    卡所描述的那个局面，它是那个局面的下一帧。
    """
    out: dict = {}
    for r in lib.execute("SELECT card_id, case_id, source_role FROM experience_card_sources"):
        out[(r["card_id"], r["case_id"])] = r["source_role"]
    return out


# 这些 role 表示"来源案例记录的是后续/后果"，谓词不命中它们不算缺陷
CONSEQUENCE_ROLES = {"execution_follow_through", "later_observation",
                     "outcome_limitation", "execution_attribution_gap"}


def evaluate_all(subs):
    """返回 {card_id: {subject_id: (verdict, basis)}}"""
    res = {}
    for cid, meta in REGISTRY.items():
        if meta["kind"] == "unexpressible":
            continue
        d = {}
        for s in subs:
            try:
                v, basis = meta["fn"](s)
            except Exception as e:                      # noqa: BLE001
                v, basis = None, {"reason": f"谓词抛异常：{type(e).__name__}: {e}"}
            d[s.sid] = (v, basis)
        res[cid] = d
    return res


def selftest(lib, subs, res) -> int:
    """两道闸，第二道是这轮新加的、也是真正有牙齿的那一道。

    闸一（旧）：每条谓词至少命中一条自己的来源案例。它只能挡住写反了的谓词。
    闸二（新）：谓词必须命中**全部** role=curation_evidence 的来源案例；漏一条就要在
                SELFTEST_EXCEPTIONS 里有一条写明理由的例外，否则 FAIL。
                role 属于"后续/后果"类的来源漏掉不算数——那类案例记的是下一帧。

    为什么要加闸二：闸一是"≥1 命中"，一条把 121 例全判 True 的谓词也能过。
    闸二把标准从"沾边"提到"框得住出处"，同时逼我把每一次放宽都写成一条可被反驳的理由。
    """
    srcs = source_case_ids(lib)
    roles = source_roles(lib)
    print(f"== {PREDICATE_VERSION} 自测 ==")
    print("   闸一：每条谓词 ≥1 命中自身来源")
    print("   闸二：必须命中全部 curation_evidence 来源，例外须在代码里写明理由\n")
    fails, gate2_fails, waived = [], [], []
    for cid, meta in sorted(REGISTRY.items()):
        ids, title = srcs.get(cid, ([], "?"))
        short = cid[7:15]
        if meta["kind"] == "unexpressible":
            print(f"  [不可表达] {short}  {title[5:40]:40s} 缺轴：{'、'.join(meta['needs'])}")
            continue
        d = res[cid]
        hits = [i for i in ids if d.get(i, (None,))[0] is True]
        miss = [i for i in ids if d.get(i, (None,))[0] is not True]
        ok = len(hits) >= 1
        n_ev = sum(1 for i in ids if roles.get((cid, i)) == "curation_evidence")
        print(f"  [{meta['kind']:9s}] {short}  {meta['slug']:14s} 命中={meta['hit_means']:2s} "
              f"来源 {len(ids)} 例（其中提炼源 {n_ev}）命中 {len(hits)}  {'✓' if ok else '✗'}")
        for i in miss:
            v = d.get(i, (None, {}))
            role = roles.get((cid, i), "(未标注)")
            tag = "后续案例，不该命中" if role in CONSEQUENCE_ROLES else "提炼源"
            print(f"        未命中 {i[:22]}  role={role}（{tag}）判定={v[0]!r}")
            print(f"                 依据 {json.dumps(v[1], ensure_ascii=False)[:120]}")
            if role == "curation_evidence":
                why = SELFTEST_EXCEPTIONS.get((cid, i))
                if why:
                    waived.append((cid, i, why))
                    print(f"                 → 已声明例外：{why[:70]}……")
                else:
                    gate2_fails.append((cid, i))
                    print("                 → ✗ 闸二：提炼源未命中且无声明例外")
        if not ok:
            fails.append(cid)
    print()
    if waived:
        print(f"-- 闸二例外 {len(waived)} 条（每条都要能被复核人反驳）--")
        for cid, i, why in waived:
            print(f"   {cid[7:15]} / {i[:22]}")
            for ln in _wrap(why, 92):
                print(f"      {ln}")
        print()
    if fails or gate2_fails:
        if fails:
            print(f"✗ 闸一失败：{len(fails)} 条谓词框不住自己的出处 -> {[c[7:15] for c in fails]}")
            print("  （一条连来源案例都命中不了的谓词，一定是写错了，不得落库）")
        if gate2_fails:
            print(f"✗ 闸二失败：{len(gate2_fails)} 条提炼源未命中且未声明例外：")
            for cid, i in gate2_fails:
                print(f"     {cid[7:15]} / {i[:22]}")
        return 1
    print(f"✓ 闸一、闸二均通过（例外 {len(waived)} 条已声明）\n")
    return 0


def _wrap(s: str, w: int):
    """按显示宽度折行（中文按 2 格算），只为把例外理由排得能读。"""
    out, cur, n = [], "", 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2000 else 1
        if n + cw > w:
            out.append(cur)
            cur, n = "", 0
        cur += ch
        n += cw
    if cur:
        out.append(cur)
    return out


def scan(subs, res):
    print("== 全库扫描（案例 / 回测日 分开统计）==\n")
    print(f"  {'卡':10s} {'谓词':14s} {'案例命中':>8s} {'案例不适用':>10s} {'案例无法判定':>12s}"
          f" {'日级命中':>8s} {'日级不适用':>10s} {'日级无法判定':>12s}")
    for cid, meta in sorted(REGISTRY.items()):
        if meta["kind"] == "unexpressible":
            continue
        d = res[cid]
        c = Counter()
        for s in subs:
            v = d[s.sid][0]
            c[(s.kind, v)] += 1
        print(f"  {cid[7:15]:10s} {meta['slug']:14s} "
              f"{c[('case', True)]:8d} {c[('case', False)]:10d} {c[('case', None)]:12d}"
              f" {c[('backtest_day', True)]:8d} {c[('backtest_day', False)]:10d}"
              f" {c[('backtest_day', None)]:12d}")
        tot_case = c[('case', True)] + c[('case', False)] + c[('case', None)]
        tot_day = c[('backtest_day', True)] + c[('backtest_day', False)] + c[('backtest_day', None)]
        assert tot_case + tot_day == len(subs), (cid, tot_case, tot_day, len(subs))
    n_case = sum(1 for s in subs if s.kind == "case")
    n_day = sum(1 for s in subs if s.kind == "backtest_day")
    print(f"\n  恒等式：每行三态之和必须 = 该类总数（案例 {n_case} / 日级 {n_day}），已逐行断言通过")
    print(f"  不可表达 {sum(1 for m in REGISTRY.values() if m['kind']=='unexpressible')} 张，"
          f"可表达 {sum(1 for m in REGISTRY.values() if m['kind']!='unexpressible')} 张，"
          f"合计 {len(REGISTRY)} 张（卡总数 12：{len(REGISTRY)==12}）\n")


DDL_EDGES = """
-- 判定全表：三态逐主体留痕。**不是只存命中**——只存命中就分不清
-- 「查过，不符合」和「没法查」，那正是伪零缺陷的同一个形状。
-- 底率层的分母要从这张表来：分母 = verdict IS NOT NULL 的行数。
CREATE TABLE IF NOT EXISTS experience_card_verdicts (
  card_id TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  verdict INTEGER,                       -- 1 命中 / 0 不适用 / NULL 无法判定
  basis_json TEXT NOT NULL CHECK (json_valid(basis_json)),
  bot_id TEXT, run_id TEXT, trade_date TEXT, cell_id TEXT,
  predicate_version TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (card_id, subject_id, predicate_version)
);
CREATE INDEX IF NOT EXISTS idx_verdict_card ON experience_card_verdicts(card_id, verdict);
CREATE INDEX IF NOT EXISTS idx_verdict_cell ON experience_card_verdicts(cell_id, card_id);
CREATE INDEX IF NOT EXISTS idx_verdict_date ON experience_card_verdicts(trade_date);

CREATE TABLE IF NOT EXISTS experience_edges (
  edge_id TEXT PRIMARY KEY,
  edge_type TEXT NOT NULL,
  src_kind TEXT NOT NULL,
  src_id TEXT NOT NULL,
  dst_kind TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  role TEXT NOT NULL,
  basis TEXT NOT NULL,                   -- mechanical_predicate | card_provenance
  basis_json TEXT NOT NULL CHECK (json_valid(basis_json)),
  bot_id TEXT, run_id TEXT, trade_date TEXT, cell_id TEXT,
  predicate_version TEXT,
  evaluator_version TEXT,
  computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON experience_edges(dst_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_edge_src ON experience_edges(src_kind, src_id);
CREATE INDEX IF NOT EXISTS idx_edge_type ON experience_edges(edge_type, role);
"""


def _edge_id(*parts) -> str:
    return "e_" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20]


def build_edges(lib, subs, res, now: str) -> int:
    """写判定全表 + INSTANTIATES 边。

    两种依据严格分开，**不许混在一列里**：
      mechanical_predicate —— 谓词在坐标上算出来的，可重算、可反驳；
      card_provenance     —— 卡自己的出处声明（谁提炼自哪一例、哪一例是后续），
                             是人写下的，机器没有独立验证过。
    混在一起的后果：前端上一条"某案例 → 某卡"的连线，读者无法知道它是
    "算出来的"还是"抄卡片自述的"，而这两者的可信度完全不同。
    """
    lib.executescript(DDL_EDGES)
    by_sid = {s.sid: s for s in subs}
    lib.execute("DELETE FROM experience_card_verdicts WHERE predicate_version=?",
                (PREDICATE_VERSION,))
    lib.execute("DELETE FROM experience_edges WHERE predicate_version=? AND basis=?",
                (PREDICATE_VERSION, "mechanical_predicate"))
    lib.execute("DELETE FROM experience_edges WHERE basis=?", ("card_provenance",))

    nv = Counter()
    ne = Counter()
    for cid, d in res.items():
        meta = REGISTRY[cid]
        for sid, (v, basis) in d.items():
            s = by_sid[sid]
            cell = s.co.get("__cell_id") or None
            lib.execute(
                "INSERT OR REPLACE INTO experience_card_verdicts "
                "(card_id, subject_kind, subject_id, verdict, basis_json, bot_id, run_id,"
                " trade_date, cell_id, predicate_version, evaluator_version, computed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, s.kind, sid, None if v is None else int(v),
                 json.dumps(basis, ensure_ascii=False), s.bot, s.run, s.date, cell,
                 PREDICATE_VERSION, ev.EVALUATOR_VERSION, now))
            nv[("命中" if v else ("不适用" if v is False else "无法判定"))] += 1
            if v is not True:
                continue
            eid = _edge_id("INSTANTIATES", sid, cid, meta["hit_means"], PREDICATE_VERSION)
            lib.execute(
                "INSERT OR REPLACE INTO experience_edges "
                "(edge_id, edge_type, src_kind, src_id, dst_kind, dst_id, role, basis,"
                " basis_json, bot_id, run_id, trade_date, cell_id, predicate_version,"
                " evaluator_version, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, "INSTANTIATES", s.kind, sid, "card", cid, meta["hit_means"],
                 "mechanical_predicate", json.dumps(basis, ensure_ascii=False),
                 s.bot, s.run, s.date, cell, PREDICATE_VERSION, ev.EVALUATOR_VERSION, now))
            ne[(s.kind, meta["hit_means"])] += 1

    # 出处边：卡自己声明的来源案例。谓词已机械命中的不重复发边（那条已是
    # mechanical_predicate），只发谓词没命中的那些——它们正是"后续/后果"类。
    roles = source_roles(lib)
    srcs = source_case_ids(lib)
    np_ = Counter()
    for cid, (ids, _t) in srcs.items():
        meta = REGISTRY.get(cid)
        for case_id in ids:
            role_raw = roles.get((cid, case_id), "(未标注)")
            hit = bool(meta and meta["kind"] != "unexpressible"
                       and res.get(cid, {}).get(case_id, (None,))[0] is True)
            if hit:
                continue
            role = "后果" if role_raw in CONSEQUENCE_ROLES else "出处"
            s = by_sid.get(case_id)
            eid = _edge_id("CITED_BY", case_id, cid, role)
            lib.execute(
                "INSERT OR REPLACE INTO experience_edges "
                "(edge_id, edge_type, src_kind, src_id, dst_kind, dst_id, role, basis,"
                " basis_json, bot_id, run_id, trade_date, cell_id, predicate_version,"
                " evaluator_version, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, "CITED_BY", "case", case_id, "card", cid, role, "card_provenance",
                 json.dumps({"source_role": role_raw,
                             "说明": "此边来自卡片自述的出处，机器未独立验证"},
                            ensure_ascii=False),
                 s.bot if s else None, s.run if s else None, s.date if s else None,
                 (s.co.get("__cell_id") if s else None), None, None, now))
            np_[role] += 1
    lib.commit()

    print("== 落库：判定全表 + 关系边 ==")
    print(f"   判定行 {sum(nv.values())} = " + " + ".join(f"{k} {v}" for k, v in nv.most_common()))
    n_expr = sum(1 for m in REGISTRY.values() if m["kind"] != "unexpressible")
    print(f"   校验：可表达卡 {n_expr} × 主体 {len(subs)} = {n_expr*len(subs)}"
          f"   （与判定行数一致：{n_expr*len(subs) == sum(nv.values())}）")
    print(f"   INSTANTIATES 边 {sum(ne.values())}（依据=谓词，可重算）：")
    for (kind, role), n in sorted(ne.items()):
        print(f"      {kind:13s} role={role:4s} {n:5d}")
    print(f"   CITED_BY 边 {sum(np_.values())}（依据=卡片自述出处，机器未独立验证）：")
    for role, n in sorted(np_.items()):
        print(f"      role={role:4s} {n:5d}")
    tot = lib.execute("SELECT count(*) c FROM experience_edges").fetchone()["c"]
    print(f"   边表总行 {tot}   （{sum(ne.values())} + {sum(np_.values())} = "
          f"{sum(ne.values())+sum(np_.values())}，一致：{tot == sum(ne.values())+sum(np_.values())}）\n")
    return 0


def power(subs, res):
    """判别力：一张在 77% 的日子上都为真的卡，命中它几乎不携带信息。

    这一栏是给前端用的——没有它，读者会把"命中 1,246 天"读成"这条经验很重要"，
    而它其实说明的是相反的事。
    """
    print("== 判别力（只看日级，分母 = 能判定的日子）==\n")
    print(f"  {'卡':10s} {'谓词':14s} {'可判定':>7s} {'命中':>6s} {'命中率':>8s}  读法")
    for cid, meta in sorted(REGISTRY.items()):
        if meta["kind"] == "unexpressible":
            continue
        d = res[cid]
        t = sum(1 for s in subs if s.kind == "backtest_day" and d[s.sid][0] is True)
        f = sum(1 for s in subs if s.kind == "backtest_day" and d[s.sid][0] is False)
        if t + f == 0:
            print(f"  {cid[7:15]:10s} {meta['slug']:14s} {0:7d} {0:6d} {'—':>8s}  "
                  f"日级全部无法判定，这张卡只能靠案例")
            continue
        r = t / (t + f)
        if t == 0:
            read = "日级零先例：地图外"
        elif t < 5:
            read = f"只有 {t} 个日级先例，任何比例都是噪音"
        elif r > 0.5:
            read = f"{r*100:.0f}% 的日子都为真——处境是常态，命中几乎不携带信息"
        elif r < 0.05:
            read = "罕见处境，命中携带信息量大，但样本薄"
        else:
            read = "可用作分层依据"
        print(f"  {cid[7:15]:10s} {meta['slug']:14s} {t+f:7d} {t:6d} {r*100:7.1f}%  {read}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-db", required=True)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--power", action="store_true", help="只打判别力表")
    ap.add_argument("--build-edges", action="store_true",
                    help="写判定全表与关系边；自测不过则拒绝写")
    args = ap.parse_args()
    if os.path.basename(args.library_db) == "experience-library-v7.db":
        print("!! 拒绝写生产库 experience-library-v7.db；请指向 P1 副本", file=sys.stderr)
        return 2
    lib = sqlite3.connect(args.library_db)
    lib.row_factory = sqlite3.Row
    subs, _ = load_subjects(lib)
    res = evaluate_all(subs)
    rc = 0
    if args.selftest or args.build_edges or not (args.scan or args.power):
        rc = selftest(lib, subs, res)
    if args.scan:
        scan(subs, res)
    if args.power or args.scan:
        power(subs, res)
    if args.build_edges:
        if rc != 0:
            print("!! 自测未通过，拒绝落库（谓词错了，边就是错的）", file=sys.stderr)
            return 2
        build_edges(lib, subs, res, datetime.now(timezone.utc).isoformat())
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
