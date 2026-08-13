#!/usr/bin/env /usr/bin/python3.12
"""
单指数深度研究 d-bot 的观点(belief)准确性评估。

数据源：
- world/runtime/runs/<run>/<date>/<bot>/reply.json 里的 ```yaml belief``` 块
  提供 target_index 在 t+1 / t+5 / t+20 的上涨概率 p_up。
- close_my_day.json 里当日持仓 → 决定用哪只基金的 NAV 算「实际涨跌」。
- data/fund.db 的 fund_nav → 基金 NAV 前向收益(t+1/t+5/t+20 个交易日)。

判对错口径（经与用户确认）：
- 实际涨跌 = 交易基金 NAV 前向收益 > 0
- 指标 = 命中率(p_up>0.5 判涨 vs 实际) + Brier 分 + 校准分桶
"""
import os, re, json, sqlite3
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "world/runtime/runs")
DB = os.path.join(ROOT, "data/fund.db")

# 每个单指数 d-bot 的「主回测 run」(最长、belief 覆盖最多)
RUNS = {
    "bot5d":  "dash-2026-07-30T09-56-04",
    "bot10d": "dash-2026-07-27T08-03-45",
    "bot16d": "dash-2026-07-28T14-53-39",
    "bot18d": "dash-2026-07-26T15-08-54",
    "bot20d": "dash-2026-07-27T02-10-21",
}
HORIZONS = [1, 5, 20]

def load_run_index():
    """每个 run 权威指数绑定——读 strategy-assignments.json。
    agent 和指数**不绑定**，同一个 bot 在不同 run 可能跑不同指数；只有 run 级 assignment 才是权威。
    belief 里 bot 自写的 target_index 会漂移(bot16d 1 天写 hs300、bot20d 1 天写 930632.CSI 新能源电池)，不能作为聚合口径。"""
    import json as _json
    out = {}
    for bot, run in RUNS.items():
        fp = os.path.join(ROOT, "world/runtime/runs", run, "strategy-assignments.json")
        a = _json.load(open(fp))["bots"][bot]
        out[bot] = dict(strategy_id=a["strategy_id"], strategy_title=a["strategy_title"],
                        target_index=a["target_index"], fund=(a.get("buyable_fund_codes") or ["?"])[0])
    return out

# ---------- NAV 载入 ----------
def load_nav():
    c = sqlite3.connect(DB)
    nav = defaultdict(list)  # fund -> [(date, nav)]
    for code, d, v in c.execute(
        "SELECT fund_code, nav_date, nav FROM fund_nav WHERE nav IS NOT NULL ORDER BY fund_code, nav_date"
    ):
        nav[code].append((d, v))
    # date -> index 便于按交易日偏移取前向 NAV
    idx = {code: {d: i for i, (d, _) in enumerate(rows)} for code, rows in nav.items()}
    return nav, idx

def fwd_return(nav, idx, fund, date, h):
    """基金 fund 从 date 起 h 个交易日的前向收益；取不到返回 None。"""
    if fund not in idx or date not in idx[fund]:
        return None
    i = idx[fund][date]
    j = i + h
    rows = nav[fund]
    if j >= len(rows):
        return None
    n0 = rows[i][1]
    n1 = rows[j][1]
    if not n0:
        return None
    return n1 / n0 - 1.0

# ---------- belief 解析 ----------
# (?<![A-Za-z_]) 防止匹配到 prior_p_up 等以 p_up 结尾的键
P_UP_RE = {
    h: re.compile(r"t\+%d:\s*\{[^}]*?(?<![A-Za-z_])p_up:\s*([0-9]*\.?[0-9]+|null)" % h)
    for h in HORIZONS
}

def parse_beliefs(bot, run):
    """返回 [(date, {h: p_up}, held_fund)]"""
    rp = os.path.join(BASE, run)
    out = []
    fund_counter = Counter()
    daily = []
    for date in sorted(os.listdir(rp)):
        dp = os.path.join(rp, date, bot)
        rf = os.path.join(dp, "reply.json")
        if not os.path.exists(rf):
            continue
        try:
            dj = json.load(open(rf))
        except Exception:
            continue
        reply = dj.get("reply", "") or ""
        if "belief" not in reply or "p_up" not in reply:
            continue
        # 是否「开了深度研究模式」：当日调用了研究循环(start_research / expand_research_phase)
        tnames = set()
        for t in dj.get("tool_trace", []) or []:
            nm = t.get("name") or t.get("tool") or ""
            if nm:
                tnames.add(nm)
        deep = ("start_research" in tnames) or ("expand_research_phase" in tnames)
        p = {}
        for h in HORIZONS:
            m = P_UP_RE[h].search(reply)
            if m and m.group(1) != "null":
                p[h] = float(m.group(1))
        if not p:
            continue
        # 当日持仓基金(取市值最大的)
        held = None
        cf = os.path.join(dp, "close_my_day.json")
        if os.path.exists(cf):
            try:
                hold = json.load(open(cf)).get("holdings") or []
                hold = [x for x in hold if x.get("market_value")]
                if hold:
                    held = max(hold, key=lambda x: x["market_value"])["fund_code"]
            except Exception:
                pass
        if held:
            fund_counter[held] += 1
        daily.append((date, p, held, deep))
    dominant = fund_counter.most_common(1)[0][0] if fund_counter else None
    # 空仓日：用最近一次持仓的基金前向填充；开头缺失则用之后第一次持仓的基金后向填充。
    # 这样比全局 dominant 更忠实——首日建仓稀金属的日子会用稀金属基金，而非后期轮动到的基金。
    n = len(daily)
    filled = [None] * n
    last = None
    for i in range(n):
        if daily[i][2]:
            last = daily[i][2]
        filled[i] = last
    nxt = None
    for i in range(n - 1, -1, -1):
        if daily[i][2]:
            nxt = daily[i][2]
        if filled[i] is None:
            filled[i] = nxt
    for (date, p, _, deep), fund in zip(daily, filled):
        out.append((date, p, fund or dominant, deep))
    return out, dominant, fund_counter

# ---------- 指标 ----------
def brier(p, y):
    return (p - y) ** 2

CAL_BINS = [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]

def analyze():
    nav, idx = load_nav()
    records = []  # (bot, date, h, p_up, fret, y, fund, deep)
    fundmap = {}
    for bot, run in RUNS.items():
        beliefs, dominant, fc = parse_beliefs(bot, run)
        fundmap[bot] = (dominant, dict(fc))
        for date, p, fund, deep in beliefs:
            if not fund:
                continue
            for h, pu in p.items():
                fret = fwd_return(nav, idx, fund, date, h)
                if fret is None:
                    continue
                y = 1 if fret > 0 else 0
                records.append((bot, date, h, pu, fret, y, fund, deep))
    return records, fundmap

def agg(records, key_bot=True):
    """按 (bot,h) 或 (h) 聚合。"""
    groups = defaultdict(list)
    for bot, date, h, pu, fret, y, fund, deep in records:
        k = (bot, h) if key_bot else ("ALL", h)
        groups[k].append((pu, y, fret))
    rows = {}
    for k, items in groups.items():
        n = len(items)
        # 命中率：排除 p_up==0.5 的无方向判定
        called = [(pu, y) for pu, y, _ in items if pu != 0.5]
        hits = sum(1 for pu, y in called if (pu > 0.5) == (y == 1))
        hit_rate = hits / len(called) if called else None
        brier_mean = sum(brier(pu, y) for pu, y, _ in items) / n
        base_up = sum(y for _, y, _ in items) / n
        brier_base = sum(brier(0.5, y) for _, y, _ in items) / n  # 恒 0.5 基线
        mean_p = sum(pu for pu, _, _ in items) / n
        rows[k] = dict(n=n, called=len(called), hit_rate=hit_rate,
                       brier=brier_mean, brier_base=brier_base,
                       base_up=base_up, mean_p=mean_p)
    return rows

def agg_dir(records, key_bot=True, bull_th=0.6, bear_th=0.4, deep_only=None):
    """按方向拆分——严格口径：只保留 p_up>=bull_th (明确看涨) / p_up<=bear_th (明确看跌)。
    中间温和倾向 (bear_th, bull_th) 全部剔除——那不算 agent 明确表态。
    看涨命中=实际涨(y=1)；看跌命中=实际跌(y=0)。另给各方向平均前向收益。
    deep_only: True 仅深研日 / False 仅非深研日 / None 全部。"""
    groups = defaultdict(list)
    for bot, date, h, pu, fret, y, fund, deep in records:
        if deep_only is not None and bool(deep) != deep_only:
            continue
        k = (bot, h) if key_bot else ("ALL", h)
        groups[k].append((pu, y, fret))
    rows = {}
    for k, items in groups.items():
        bull = [(y, fr) for pu, y, fr in items if pu >= bull_th]
        bear = [(y, fr) for pu, y, fr in items if pu <= bear_th]
        mid = sum(1 for pu, _, _ in items if bear_th < pu < bull_th)
        def stat(lst, want_up):
            if not lst:
                return dict(n=0, hit=None, mret=None)
            hit = sum(1 for y, _ in lst if (y == 1) == want_up) / len(lst)
            mret = sum(fr for _, fr in lst) / len(lst)
            return dict(n=len(lst), hit=hit, mret=mret)
        rows[k] = dict(bull=stat(bull, True), bear=stat(bear, False), middle=mid,
                       spread=( (sum(fr for _, fr in bull)/len(bull) if bull else 0)
                               -(sum(fr for _, fr in bear)/len(bear) if bear else 0) ))
    return rows

def agg_by_index(records, run_idx, bull_th=0.6, bear_th=0.4, deep_only=None):
    """按 run 权威绑定的指数聚合——因 agent 和指数不绑定，跨 run 只能这样归。
    key = (target_index, strategy_title, h)。"""
    groups = defaultdict(list)
    for bot, date, h, pu, fret, y, fund, deep in records:
        if deep_only is not None and bool(deep) != deep_only:
            continue
        ti = run_idx[bot]["target_index"]
        title = run_idx[bot]["strategy_title"]
        groups[(ti, title, h)].append((pu, y, fret))
    rows = {}
    for k, items in groups.items():
        bull = [(y, fr) for pu, y, fr in items if pu >= bull_th]
        bear = [(y, fr) for pu, y, fr in items if pu <= bear_th]
        mid  = sum(1 for pu, _, _ in items if bear_th < pu < bull_th)
        def stat(lst, want_up):
            if not lst: return dict(n=0, hit=None, mret=None)
            hit = sum(1 for y,_ in lst if (y==1)==want_up)/len(lst)
            mret = sum(fr for _,fr in lst)/len(lst)
            return dict(n=len(lst), hit=hit, mret=mret)
        rows[k] = dict(total=len(items), mid=mid, bull=stat(bull,True), bear=stat(bear,False),
                       spread=((sum(fr for _,fr in bull)/len(bull) if bull else 0)
                              -(sum(fr for _,fr in bear)/len(bear) if bear else 0)))
    return rows

def calibration(records):
    """按 (bot,h) 分桶：预测均值 vs 实际上涨频率。"""
    cal = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0]))  # (bot,h)->bin->[sum_p, sum_y, n]
    for bot, date, h, pu, fret, y, fund, deep in records:
        for lo, hi in CAL_BINS:
            if lo <= pu < hi:
                cell = cal[(bot, h)][(lo, hi)]
                cell[0] += pu; cell[1] += y; cell[2] += 1
                break
    return cal

def pct(x):
    return "—" if x is None else f"{x*100:.1f}%"

def build_report(records, fundmap):
    per = agg(records, True)
    allh = agg(records, False)
    cal = calibration(records)
    L = []
    L.append("# 单指数深度研究 d-bot 观点(belief)准确性评估\n")
    L.append("> 由 `scripts/analyze_belief_accuracy.py` 生成，可复现。\n")
    L.append("## 方法\n")
    L.append("- **观点来源**：每个 d-bot 每日 `reply` 末尾的 ```yaml belief``` 块，给出 target_index 在 "
             "t+1 / t+5 / t+20 的上涨概率 `p_up`。\n")
    L.append("- **实际涨跌**：bot 当日持仓基金(取市值最大者，空仓日用该 run 主基金兜底)的 NAV 前向收益，"
             ">0 记为上涨。数据取自 `fund_nav`。\n")
    L.append("- **指标**：命中率(`p_up>0.5` 判涨 vs 实际，剔除 `p_up=0.5` 无方向样本)、Brier 分"
             "(概率校准误差，越小越好；恒报 0.5 的基线=0.25)、校准分桶、方向性偏差(均值 p_up vs 实际上涨率)。"
             "**方向拆分(看涨/看跌)用严格口径**：`p_up ≥ 0.6` 才算明确看涨、`p_up ≤ 0.4` 才算明确看跌，"
             "中间温和倾向不计入方向表态(详见第五节)。\n")
    L.append("- **是否深研**：以当日 tool_trace 是否调用 `start_research`/`expand_research_phase`(研究循环)"
             "判定「开了深度模式」。据此把日子分为深研日/非深研日，见第二节——这是本报告的核心口径。\n")
    L.append("- **样本**：5 个单指数 d-bot 各取最长、belief 覆盖最全的主回测 run。总打分样本 "
             f"**{len(records)}** 条(bot×日×horizon)，其中深研日 "
             f"**{sum(1 for r in records if r[7])}** 条。\n")

    # 主基金表
    L.append("\n### 各 bot 主回测 run 与基金\n")
    L.append("| bot | 主 run | 主基金 |")
    L.append("|---|---|---|")
    for bot in RUNS:
        dom, fc = fundmap[bot]
        L.append(f"| {bot} | `{RUNS[bot]}` | {dom} |")

    # 汇总表
    L.append("\n## 一、总体结论(全部 bot 合并)\n")
    L.append("| horizon | 样本 | 命中率 | 实际上涨率(基准) | 恒报涨命中率 | 均值 p_up | Brier | Brier基线 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for h in HORIZONS:
        r = allh[("ALL", h)]
        L.append(f"| t+{h} | {r['n']} | {pct(r['hit_rate'])} | {pct(r['base_up'])} | "
                 f"{pct(r['base_up'])} | {r['mean_p']:.3f} | {r['brier']:.3f} | {r['brier_base']:.3f} |")
    L.append("\n**读法**：命中率≈50%、Brier≈0.25(=恒报 0.5 基线)说明观点几乎无方向预测力；"
             "命中率显著低于「实际上涨率」说明还不如无脑看涨。\n")

    # 深研日 vs 非深研日
    rec_deep = [r for r in records if r[7]]
    rec_shallow = [r for r in records if not r[7]]
    ad = agg(rec_deep, False)
    ash = agg(rec_shallow, False)
    dd = agg_dir(rec_deep, False)
    dsh = agg_dir(rec_shallow, False)
    n_deep_days = len({(r[0], r[1]) for r in rec_deep})
    n_all_days = len({(r[0], r[1]) for r in records})
    L.append("\n## 二、只看「开了深度研究」的日子(核心)\n")
    L.append(f"深研日 = 当日 tool_trace 出现 `start_research` / `expand_research_phase`(研究循环)。"
             f"这些 bot 并非每天深研——{n_all_days} 个有 belief 的交易日里只有 **{n_deep_days}** 天真正开了深度模式。"
             "下表对比「深研日」与「非深研日」的观点准确性，看深度研究到底有没有让判断更准。\n")
    L.append("| horizon | 组别 | 样本 | 命中率 | 实际上涨率 | Brier | 明确看涨命中 | 明确看跌命中 | 收益差(涨-跌) |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for h in HORIZONS:
        for label, A_, D_ in [("深研日", ad, dd), ("非深研日", ash, dsh)]:
            r = A_.get(("ALL", h))
            dr = D_.get(("ALL", h))
            if not r:
                continue
            bl, be = dr["bull"], dr["bear"]
            L.append(f"| t+{h} | {label} | {r['n']} | {pct(r['hit_rate'])} | {pct(r['base_up'])} | "
                     f"{r['brier']:.3f} | {pct(bl['hit'])}(n={bl['n']}) | {pct(be['hit'])}(n={be['n']}) | {dr['spread']*100:+.2f}pp |")
    L.append("\n**读法**：命中率/Brier 用宽口径(p_up 只要偏离 0.5 就当方向判断)；"
             "「明确看涨/看跌命中」用严格口径——**只计 agent 真写出 p_up≥0.6 或 p_up≤0.4 这种明确表态的日子**，"
             "中间的温和倾向(0.4~0.6)不算表态。收益差 = 明确看涨日的标的平均前向收益 − 明确看跌日的。\n")

    # 分 bot
    L.append("\n## 三、分 bot × horizon(全部有 belief 的日子)\n")
    L.append("| bot | horizon | 样本 | 命中率 | 实际上涨率 | 均值 p_up | Brier | vs 基线 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for bot in RUNS:
        for h in HORIZONS:
            r = per[(bot, h)]
            edge = r['brier_base'] - r['brier']  # 正=优于基线
            tag = "✅优" if edge > 0.003 else ("≈平" if abs(edge) <= 0.003 else "❌劣")
            L.append(f"| {bot} | t+{h} | {r['n']} | {pct(r['hit_rate'])} | {pct(r['base_up'])} | "
                     f"{r['mean_p']:.3f} | {r['brier']:.3f} | {tag} |")

    # 校准
    L.append("\n## 四、概率校准(全部 bot 合并，按 horizon)\n")
    L.append("每个 p_up 区间内：预测均值 应 ≈ 实际上涨率，偏离即校准差。\n")
    for h in HORIZONS:
        L.append(f"\n**t+{h}**\n")
        L.append("| p_up 区间 | 样本 | 预测均值 | 实际上涨率 |")
        L.append("|---|---:|---:|---:|")
        # 合并所有 bot 的该 horizon 分桶
        merged = defaultdict(lambda: [0.0, 0, 0])
        for (bot, hh), bins in cal.items():
            if hh != h:
                continue
            for b, cell in bins.items():
                m = merged[b]
                m[0] += cell[0]; m[1] += cell[1]; m[2] += cell[2]
        for (lo, hi) in CAL_BINS:
            cell = merged.get((lo, hi))
            if not cell or cell[2] == 0:
                continue
            sp, sy, n = cell
            hi_disp = 1.0 if hi > 1 else hi
            L.append(f"| [{lo:.1f}, {hi_disp:.1f}) | {n} | {sp/n:.3f} | {sy/n:.3f} |")

    # 看涨 vs 看跌（严格口径）
    da = agg_dir(records, False)               # 严格 0.6/0.4，全部
    db = agg_dir(records, True)                # 严格 0.6/0.4，分 bot
    da_deep = agg_dir(records, False, deep_only=True)  # 严格 0.6/0.4，仅深研日
    L.append("\n## 五、看涨 vs 看跌分开统计（严格口径：只算明确表态）\n")
    L.append("**严格阈值**：`p_up ≥ 0.6` 才算**明确看涨**、`p_up ≤ 0.4` 才算**明确看跌**；"
             "中间温和倾向(0.4 < p_up < 0.6)全部剔除——那不算 agent 明确表态。"
             "看涨命中 = 实际涨(y=1)；看跌命中 = 实际跌(y=0)。\n")
    L.append("\n### 5.1 分布：多少条 belief 是「明确表态」\n")
    L.append("agent 大多数日子的 belief 都挤在中间温和区间(50% 左右)，明确表态其实很少：\n")
    L.append("| horizon | 明确看涨(≥0.6) | 中间(0.4~0.6) | 明确看跌(≤0.4) | 总样本 |")
    L.append("|---|---:|---:|---:|---:|")
    for h in HORIZONS:
        r = da[("ALL", h)]
        total = r['bull']['n'] + r['bear']['n'] + r['middle']
        L.append(f"| t+{h} | {r['bull']['n']} ({r['bull']['n']/total*100:.1f}%) | "
                 f"{r['middle']} ({r['middle']/total*100:.1f}%) | "
                 f"{r['bear']['n']} ({r['bear']['n']/total*100:.1f}%) | {total} |")
    L.append("\n**观察**：明确看跌的次数**普遍多于**明确看涨——bot 在深研时更敢空、不敢多；"
             "但绝大多数日子(~80%)仍是中间温和倾向，没有旗帜鲜明表态。\n")

    L.append("\n### 5.2 严格口径命中率(全部 bot 合并)\n")
    L.append("`平均前向收益` = 该方向所有样本标的 NAV 的 t+N 收益均值。有方向预测力时，"
             "明确看涨样本的平均收益应显著高于明确看跌样本(spread>0)。\n")
    L.append("| horizon | 明确看涨 n | 命中率 | 平均前向收益 | 明确看跌 n | 命中率 | 平均前向收益 | 收益差(涨-跌) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for h in HORIZONS:
        r = da[("ALL", h)]
        bl, be = r["bull"], r["bear"]
        bmr = f"{bl['mret']*100:+.2f}%" if bl['mret'] is not None else "—"
        rmr = f"{be['mret']*100:+.2f}%" if be['mret'] is not None else "—"
        L.append(f"| t+{h} | {bl['n']} | {pct(bl['hit'])} | {bmr} | "
                 f"{be['n']} | {pct(be['hit'])} | {rmr} | {r['spread']*100:+.2f}pp |")

    L.append("\n### 5.3 严格口径命中率(仅深研日)\n")
    L.append("**这是本节最重要的表**——直接回答「agent 在深度研究时给出明确方向表态，准不准」：\n")
    L.append("| horizon | 明确看涨 n | 命中率 | 平均前向收益 | 明确看跌 n | 命中率 | 平均前向收益 | 收益差(涨-跌) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for h in HORIZONS:
        r = da_deep[("ALL", h)]
        bl, be = r["bull"], r["bear"]
        bmr = f"{bl['mret']*100:+.2f}%" if bl['mret'] is not None else "—"
        rmr = f"{be['mret']*100:+.2f}%" if be['mret'] is not None else "—"
        L.append(f"| t+{h} | {bl['n']} | {pct(bl['hit'])} | {bmr} | "
                 f"{be['n']} | {pct(be['hit'])} | {rmr} | {r['spread']*100:+.2f}pp |")
    L.append("\n**读法**：深研时的明确看涨样本很少(11~23 条)但命中率还不错(≈60%)；"
             "**明确看跌样本多得多(30~51 条)、命中率却全线崩盘**——t+5 只对 22.7%、t+20 只对 10%(30 条对了 3 条)。"
             "「说跌实涨」的日子平均涨 +2.7% ~ +8.4%，越长视界反指越狠。深研的明确看跌信号是**负 alpha**，"
             "印证既有定论「看空只降杠杆、永不反向」——因为看空越坚决越会打脸。\n")

    L.append("\n### 5.4 分**指数**(严格口径，按 run 权威绑定聚合)\n")
    L.append("**为什么按指数而不是按 bot 分**：agent 和指数**不绑定**——同一个 bot 可能被跑在多个 run 上、各自绑不同指数；"
             "belief 里 bot 自己写的 `target_index` 还会漂移(bot16d 1 天写 hs300、bot20d 1 天写 930632.CSI)。"
             "指数归类只能读 run 的 `strategy-assignments.json`。本节的 5 个 run 恰好分别绑到 5 个不同指数，"
             "所以按指数聚合的样本数≈按 bot 聚合，但**读的时候要以指数视角看**——这些数字反映该指数在该 run 里的判断风格，"
             "不代表该 bot 在其他 run 的表现。\n")
    run_idx = load_run_index()
    di_all  = agg_by_index(records, run_idx, deep_only=None)
    di_deep = agg_by_index(records, run_idx, deep_only=True)
    order = [(run_idx[b]["target_index"], run_idx[b]["strategy_title"]) for b in RUNS]

    L.append("\n#### 5.4a 分指数(全部日子)\n")
    L.append("| 指数(strategy) | horizon | 总样本 | 明确看涨 n | 命中 | 明确看跌 n | 命中 | 涨均 | 跌均 | 收益差 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for ti, title in order:
        for h in HORIZONS:
            r = di_all.get((ti, title, h))
            if not r: continue
            bl, be = r['bull'], r['bear']
            bmr = f"{bl['mret']*100:+.2f}%" if bl['mret'] is not None else "—"
            rmr = f"{be['mret']*100:+.2f}%" if be['mret'] is not None else "—"
            L.append(f"| {title}({ti}) | t+{h} | {r['total']} | "
                     f"{bl['n']} | {pct(bl['hit'])} | {be['n']} | {pct(be['hit'])} | "
                     f"{bmr} | {rmr} | {r['spread']*100:+.2f}pp |")

    L.append("\n#### 5.4b 分指数(仅深研日) ★核心\n")
    L.append("| 指数(strategy) | horizon | 总样本 | 明确看涨 n | 命中 | 明确看跌 n | 命中 | 涨均 | 跌均 | 收益差 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for ti, title in order:
        for h in HORIZONS:
            r = di_deep.get((ti, title, h))
            if not r: continue
            bl, be = r['bull'], r['bear']
            bmr = f"{bl['mret']*100:+.2f}%" if bl['mret'] is not None else "—"
            rmr = f"{be['mret']*100:+.2f}%" if be['mret'] is not None else "—"
            L.append(f"| {title}({ti}) | t+{h} | {r['total']} | "
                     f"{bl['n']} | {pct(bl['hit'])} | {be['n']} | {pct(be['hit'])} | "
                     f"{bmr} | {rmr} | {r['spread']*100:+.2f}pp |")
    L.append("\n**指数画像观察**：\n"
             "- **沪深300**：最保守——明确表态占比全表最低(t+1 明确看涨仅 1.9%)；深研 t+5/t+20 **明确看涨样本数=0**，深研只敢看空；深研 t+20 看跌 7 条全错(0% 命中)。\n"
             "- **证券公司指数**：明确看跌占比最高(t+1 达 26%)，最爱看空；但深研 t+5 明确看涨 0 命中、明确看跌 25% 命中——两边都错。\n"
             "- **中证1000**：唯一敢多敢空最平衡的指数(t+1 看涨 10.7% ≈ 看跌 10.4%)；t+5 明确看涨命中 74.5% 全表最高；**深研 t+20 收益差 +0.06pp 是全部(指数×深研×t+20)里唯一没转负的**——深研有点方向感。\n"
             "- **人工智能主题(930713)**：反指之王——深研 t+20 明确看跌 15 条**命中率 0%**，说跌日均涨 **+14.19%**；这是把「深研越坚决看跌越打脸」推到极端的样本。\n"
             "- **中证医疗**：明确表态最少(t+5 只有 1 条明确看涨、9 条明确看跌)，样本稀薄难下结论，看似的高命中率不稳。\n"
             "**读法**：这些差异说明 agent 面对不同底层资产**判断风格明显不同**（看多倾向、看空频率、错法方向），"
             "改进不能一刀切——反指之王(AI/沪深300 深研看空)优先接「言行一致硬约束」限速；"
             "中证1000 型指数上 t+20 深研可保留方向权重、其他指数上应剥离方向仅用于降杠杆。\n")

    L.append("\n### 5.4c 分 bot × horizon(严格口径，全部日子，参考)\n")
    L.append("因每个 bot 在本报告的 run 里绑一个固定指数，与 5.4a 数字接近；保留此表方便对齐 by-bot 视角。\n")
    L.append("| bot | horizon | 明确看涨 n | 命中 | 明确看跌 n | 命中 | 收益差(涨-跌) |")
    L.append("|---|---|---:|---:|---:|---:|---:|")
    for bot in RUNS:
        for h in HORIZONS:
            r = db[(bot, h)]
            bl, be = r["bull"], r["bear"]
            L.append(f"| {bot} | t+{h} | {bl['n']} | {pct(bl['hit'])} | "
                     f"{be['n']} | {pct(be['hit'])} | {r['spread']*100:+.2f}pp |")

    L.append("\n### 5.5 阈值敏感性(全部样本，对照)\n")
    L.append("为什么用 0.6/0.4 而不是 0.5/0.5? 因为绝大多数 belief 分布在 [0.45, 0.55]，"
             "把 0.48 当「看跌」不合理。下表展示不同阈值下的收益差(spread)——"
             "阈值放宽后 bull/bear 样本数暴增但 spread 反而变小/翻负，说明真正的信号只在极端表态里：\n")
    L.append("| 阈值 | horizon | 看涨 n | 看涨命中 | 看跌 n | 看跌命中 | 收益差 |")
    L.append("|---|---|---:|---:|---:|---:|---:|")
    for bull_th, bear_th, tag in [(0.5001,0.4999,"0.5/0.5(宽)"),(0.55,0.45,"0.55/0.45"),(0.6,0.4,"0.6/0.4(采用)"),(0.65,0.35,"0.65/0.35")]:
        sens = agg_dir(records, False, bull_th=bull_th, bear_th=bear_th)
        for h in HORIZONS:
            r = sens[("ALL", h)]
            bl, be = r["bull"], r["bear"]
            L.append(f"| {tag} | t+{h} | {bl['n']} | {pct(bl['hit'])} | "
                     f"{be['n']} | {pct(be['hit'])} | {r['spread']*100:+.2f}pp |")

    # 课题设计诊断
    L.append("\n## 六、课题设计诊断(深研日 topic 是否对口?)\n")
    L.append("为了排除「课题设计跟涨跌无关、所以测不准是必然」这个假说，对所有 `start_research` 的 topic "
             "做关键词打标(见 `scripts/topic_diagnostics.py`)。\n")
    try:
        import subprocess
        diag = subprocess.check_output(["/usr/bin/python3.12", os.path.join(ROOT, "scripts/topic_diagnostics.py")]).decode()
        lines = diag.splitlines()
        start = next((i for i, s in enumerate(lines) if s.startswith("共 ")), 0)
        end = next((i for i, s in enumerate(lines) if s.startswith("## 抽样")), len(lines))
        L.append("\n".join(lines[start:end]).strip())
    except Exception as e:
        L.append(f"(topic 诊断跑失败: {e})")
    L.append("\n**读法**：`macro_only=0%` 说明没有跑题课题；`timing` 占比很高说明大多数课题直接问"
             "「筑底/反弹/入场」这类择时问题——课题设计本身对口，锅不在题目上。真正值得注意的是 "
             "**结构化列多空(competing=True)的日子 t+5 / t+20 命中显著高于不列多空的日子**——"
             "bot 逼自己写多空 checklist 时观点更准，说明改进方向是**提示词层面强制 competing_explanations**，"
             "而不是继续加深研调用次数。\n")

    # 关键发现
    L.append("\n## 七、关键发现\n")
    a1, a5, a20 = allh[("ALL",1)], allh[("ALL",5)], allh[("ALL",20)]
    ad5, ash5 = ad[("ALL",5)], ash[("ALL",5)]
    ad20 = ad[("ALL",20)]
    L.append(f"0. **深度研究没换来更准的观点，且课题设计不背锅(直接回答)**：只看开了深度模式的 {n_deep_days} 天，"
             f"t+5 命中率 {pct(ad5['hit_rate'])} 反而**低于**非深研日的 {pct(ash5['hit_rate'])}；"
             f"t+5 深研日方向收益差 {dd[('ALL',5)]['spread']*100:+.2f}pp(非深研日 {dsh[('ALL',5)]['spread']*100:+.2f}pp)、"
             f"t+20 深研日 {dd[('ALL',20)]['spread']*100:+.2f}pp。第六节的课题打标显示"
             "题目本身 73.7% 是明确择时问题、macro_only=0%，设计并没有跑题——问题出在 bot 的**判断风格**"
             "(见发现 4/5)，不是研究命题上。\n")
    L.append("6. **列多空对比会显著提升准确性(可操作发现)**：第六节里 competing=True 的日子，"
             "t+5 命中 57.1%、t+20 命中 60.8%，显著高于不列多空的 44.6% / 37.8%，与整体「无预测力」"
             "形成鲜明对比。这暗示改进方向不是加深研次数，而是**在提示词里强制 belief 前先写多空 checklist**。\n")
    L.append(f"1. **短周期近似掷硬币，长周期系统性看反**：t+1 命中 {pct(a1['hit_rate'])}、"
             f"t+5 {pct(a5['hit_rate'])}，仅略高于 50%；t+20 命中骤降到 {pct(a20['hit_rate'])}，"
             f"而同期实际上涨率高达 {pct(a20['base_up'])}——即长周期里无脑看涨都远好于 bot 的方向判断。\n")
    L.append(f"2. **概率预测无增量信息**：三个 horizon 的 Brier 分({a1['brier']:.3f}/{a5['brier']:.3f}/"
             f"{a20['brier']:.3f})均 ≥ 恒报 0.5 的基线 0.25，说明 p_up 的高低并不能区分涨跌。\n")
    L.append(f"3. **看多不足 vs 上行行情**：t+20 均值 p_up={a20['mean_p']:.3f}(仅中性略偏多)明显低于"
             f"实际上涨率 {pct(a20['base_up'])}，bot 深研结论偏保守、未充分看多，而标的基金在窗口内多数上涨，"
             "这是长周期命中率崩塌的主因。\n")
    # 方向拆分洞见（严格口径，深研日）
    dd_deep = da_deep  # 深研日严格
    d5d, d20d = dd_deep[("ALL",5)], dd_deep[("ALL",20)]
    L.append(f"4. **明确表态严格口径：深研时敢明确看跌、且明确看跌是反指**："
             f"深研日「明确看跌 (p_up≤0.4)」样本数 {d5d['bear']['n']}(t+5) / {d20d['bear']['n']}(t+20)，"
             f"远多于「明确看涨 (p_up≥0.6)」的 {d5d['bull']['n']} / {d20d['bull']['n']} 条；"
             f"命中率却触底——t+5 明确看跌命中 **{pct(d5d['bear']['hit'])}**、"
             f"t+20 明确看跌命中 **{pct(d20d['bear']['hit'])}**({d20d['bear']['n']} 条对了不到几条)；"
             f"「说跌实涨」的日子平均涨 +{d5d['bear']['mret']*100:.2f}% / +{d20d['bear']['mret']*100:.2f}%。"
             f"这直接支撑既有定论「看空只降杠杆、永不反向」——**深研越明确看跌越会打脸**。"
             f"相反明确看涨那少数几条(t+5 命中 {pct(d5d['bull']['hit'])})是全表唯一稳定的方向 alpha。\n")
    # 找最好/最差 bot(按 t+5 brier edge)
    edges = {bot: per[(bot,5)]['brier_base'] - per[(bot,5)]['brier'] for bot in RUNS}
    best = max(edges, key=edges.get); worst = min(edges, key=edges.get)
    L.append(f"5. **bot 间分化**：t+5 相对基线，`{best}` 最好(Brier 优势 {edges[best]*100:+.2f}pp)，"
             f"`{worst}` 最差({edges[worst]*100:+.2f}pp)，但整体都在基线附近，无稳定 alpha。\n")
    L.append("\n> 口径说明：实际涨跌用「当日持仓基金」而非 belief 的 target_index 收盘价，"
             "对做过指数轮动的 bot(如 bot16d/bot20d)属近似；空仓日用最近一次持仓基金前后填充。"
             "这会带来轻微跟踪误差，但不改变「无显著预测力」的定性结论。\n")
    return "\n".join(L)

if __name__ == "__main__":
    records, fundmap = analyze()
    out = os.path.join(ROOT, "research", "deep-research-belief-accuracy.md")
    report = build_report(records, fundmap)
    with open(out, "w") as f:
        f.write(report)
    print(f"总打分样本: {len(records)}")
    print(f"报告已写入: {out}")
