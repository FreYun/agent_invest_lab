#!/usr/bin/env python3
"""快速验证：纳指执行卡式规则 vs 买入持有，用 513100 NAV（2013-2026，含2022熊市）。
规则内核（设计稿）：
  - 常态：底色仓位 base；急跌阶梯：自252日高回撤>=d1 加a1，>=d2 再加a2（合计到100%）
  - regime 破坏 = 收盘 < MA200 连续 K 日 且 MA200 较 20 日前下行 → 降到 defensive
  - 修复 = 收盘 > MA200 连续 5 日 → 回 base
  - 创252日新高 → 重置弹药回 base
对比变体：无 MA200 斜率过滤（只要 K 日在 MA200 下就破坏）。
"""
import sqlite3

con = sqlite3.connect('/home/ubuntu/rooot/agent_invest_lab/data/fund.db')
rows = con.execute(
    "SELECT nav_date, nav FROM fund_nav WHERE fund_code='513100' AND nav IS NOT NULL ORDER BY nav_date"
).fetchall()
dates = [r[0] for r in rows]
nav = [r[1] for r in rows]
n = len(nav)
print(f"NAV rows: {n}, {dates[0]} .. {dates[-1]}")

# 预计算 MA200 / 252日高
ma200 = [None]*n
hi252 = [None]*n
s = 0.0
for i in range(n):
    s += nav[i]
    if i >= 200:
        s -= nav[i-200]
    if i >= 199:
        ma200[i] = s/200
    lo = max(0, i-251)
    hi252[i] = max(nav[lo:i+1])

def simulate(start, end, base=0.85, defensive=0.30, d1=0.08, a1=0.075, d2=0.15, a2=0.075,
             K=10, slope_filter=True, label=""):
    i0 = next(i for i,d in enumerate(dates) if d >= start)
    i1 = max(i for i,d in enumerate(dates) if d <= end)
    cash, units = None, None
    w = None
    below = 0; above = 0
    state = 'normal'
    added = 0.0
    equity = 1.0
    weights_hist = []
    eq_hist = []
    trades = 0
    target = base
    for i in range(i0, i1+1):
        if i > i0:
            ret = nav[i]/nav[i-1] - 1
            equity *= (1 + w*ret)
        # 信号（收盘后，次日生效——这里简化为当日收盘调仓，对比口径一致即可）
        if ma200[i] is None:
            w = base if w is None else w
            eq_hist.append(equity); weights_hist.append(w); continue
        below = below+1 if nav[i] < ma200[i] else 0
        above = above+1 if nav[i] > ma200[i] else 0
        slope_down = ma200[i-20] is not None and ma200[i] < ma200[i-20]
        dd = nav[i]/hi252[i] - 1
        if state == 'normal':
            broken = below >= K and (slope_down or not slope_filter)
            if broken:
                state = 'broken'; target = defensive; added = 0
            else:
                add = (a1 if dd <= -d1 else 0) + (a2 if dd <= -d2 else 0)
                if dd >= -0.001:  # 新高附近重置
                    add = 0
                target = min(1.0, base + add)
        else:  # broken
            if above >= 5:
                state = 'normal'; target = base; added = 0
        if w is None: w = target
        if abs(target - w) > 0.001:
            trades += 1
            w = target
        eq_hist.append(equity); weights_hist.append(w)
    # 指标
    peak = 0; maxdd = 0
    for e in eq_hist:
        peak = max(peak, e)
        maxdd = min(maxdd, e/peak - 1)
    years = (i1 - i0)/244
    cagr = eq_hist[-1]**(1/years) - 1 if years > 0.5 else eq_hist[-1]-1
    calmar = cagr/abs(maxdd) if maxdd < 0 else float('inf')
    print(f"{label:38s} 总收益 {eq_hist[-1]-1:+8.1%}  年化 {cagr:+7.1%}  最大回撤 {maxdd:7.1%}  卡玛 {calmar:5.2f}  调仓 {trades:3d} 次")
    return eq_hist

def buyhold(start, end, label=""):
    i0 = next(i for i,d in enumerate(dates) if d >= start)
    i1 = max(i for i,d in enumerate(dates) if d <= end)
    eq = [nav[i]/nav[i0] for i in range(i0, i1+1)]
    peak=0; maxdd=0
    for e in eq:
        peak=max(peak,e); maxdd=min(maxdd, e/peak-1)
    years=(i1-i0)/244
    cagr = eq[-1]**(1/years)-1 if years>0.5 else eq[-1]-1
    print(f"{label:38s} 总收益 {eq[-1]-1:+8.1%}  年化 {cagr:+7.1%}  最大回撤 {maxdd:7.1%}  卡玛 {cagr/abs(maxdd):5.2f}")

for (s,e,tag) in [("2025-01-02","2026-06-01","bot11回测窗口"),
                  ("2021-01-01","2023-12-29","含2022熊市"),
                  ("2014-06-01","2026-06-01","12年全样本")]:
    print(f"\n=== {tag} ({s} → {e}) ===")
    buyhold(s,e,"买入持有")
    simulate(s,e,slope_filter=True, label="执行卡（MA200破坏需斜率向下确认）")
    simulate(s,e,slope_filter=False,label="对照：无斜率过滤（纯10日破MA200）")
    simulate(s,e,slope_filter=True, defensive=0.15, label="变体：防守仓15%")
    simulate(s,e,slope_filter=True, base=0.90, label="变体：底色90%")

# ============ 第二轮：深熊保险丝设计 ============
def simulate2(start, end, base=0.90, defensive=0.50, ladder=True,
              d1=0.08, a1=0.05, d2=0.15, a2=0.05,
              fuse_dd=0.18, K=10, label=""):
    """deep-fuse: 破坏 = 收盘<MA200 连续K日 且 MA200斜率向下 且 自252日高回撤>=fuse_dd"""
    i0 = next(i for i,d in enumerate(dates) if d >= start)
    i1 = max(i for i,d in enumerate(dates) if d <= end)
    w = None; below = 0; above = 0; state='normal'; equity=1.0
    eq_hist=[]; trades=0
    for i in range(i0, i1+1):
        if i > i0:
            equity *= (1 + w*(nav[i]/nav[i-1]-1))
        if ma200[i] is None:
            w = base if w is None else w
            eq_hist.append(equity); continue
        below = below+1 if nav[i] < ma200[i] else 0
        above = above+1 if nav[i] > ma200[i] else 0
        slope_down = ma200[i-20] is not None and ma200[i] < ma200[i-20]
        dd = nav[i]/hi252[i] - 1
        if state=='normal':
            if below>=K and slope_down and dd <= -fuse_dd:
                state='broken'; target=defensive
            else:
                add = ((a1 if dd<=-d1 else 0)+(a2 if dd<=-d2 else 0)) if ladder else 0
                if dd >= -0.001: add = 0
                target = min(1.0, base+add)
        else:
            if above>=5:
                state='normal'; target=base
            else:
                target = defensive
        if w is None: w = target
        if abs(target-w)>0.001: trades+=1; w=target
        eq_hist.append(equity)
    peak=0; maxdd=0
    for e in eq_hist:
        peak=max(peak,e); maxdd=min(maxdd,e/peak-1)
    years=(i1-i0)/244
    cagr = eq_hist[-1]**(1/years)-1 if years>0.5 else eq_hist[-1]-1
    print(f"{label:44s} 总收益 {eq_hist[-1]-1:+8.1%}  年化 {cagr:+7.1%}  最大回撤 {maxdd:7.1%}  卡玛 {cagr/abs(maxdd):5.2f}  调仓 {trades:3d} 次")

print("\n\n############ 第二轮：深熊保险丝 ############")
for (s,e,tag) in [("2025-01-02","2026-06-01","bot11回测窗口"),
                  ("2021-01-01","2023-12-29","含2022熊市"),
                  ("2014-06-01","2026-06-01","12年全样本")]:
    print(f"\n=== {tag} ({s} → {e}) ===")
    buyhold(s,e,"买入持有")
    simulate2(s,e,ladder=False,label="A 仅保险丝(-18%+MA200双确认→50%)")
    simulate2(s,e,ladder=True, label="B 保险丝+急跌阶梯(90→100)")
    simulate2(s,e,ladder=True,base=0.85,a1=0.075,a2=0.075,label="C 同B但底色85弹药15")
    simulate2(s,e,ladder=True,fuse_dd=0.22,label="D 同B保险丝阈值-22%")
    simulate2(s,e,ladder=True,defensive=0.30,label="E 同B防守仓30%")

# ============ 第三轮：保险丝 + LLM regime 判别（用已知联储紧缩期做判别正确的代理）============
TIGHTENING = [("2022-01-01","2022-12-31")]  # 代理：LLM 判别为"利率/盈利双杀"的时期
def in_tight(d):
    return any(a <= d <= b for a,b in TIGHTENING)

def simulate3(start, end, base=0.90, defensive=0.50, dip=0.10,
              fuse_dd=0.18, K=10, judge=True, label=""):
    i0 = next(i for i,d in enumerate(dates) if d >= start)
    i1 = max(i for i,d in enumerate(dates) if d <= end)
    w=None; below=0; above=0; state='normal'; equity=1.0; eq_hist=[]; trades=0
    for i in range(i0, i1+1):
        if i > i0:
            equity *= (1 + w*(nav[i]/nav[i-1]-1))
        if ma200[i] is None:
            w = base if w is None else w; eq_hist.append(equity); continue
        below = below+1 if nav[i] < ma200[i] else 0
        above = above+1 if nav[i] > ma200[i] else 0
        slope_down = ma200[i-20] is not None and ma200[i] < ma200[i-20]
        dd = nav[i]/hi252[i] - 1
        if state=='normal':
            fuse = below>=K and slope_down and dd <= -fuse_dd
            if fuse and (not judge or in_tight(dates[i])):
                state='broken'; target=defensive
            else:
                target = 1.0 if (dd <= -dip) else base
                if dd >= -0.001: target = base
        else:
            if above>=5: state='normal'; target=base
            else: target=defensive
        if w is None: w=target
        if abs(target-w)>0.001: trades+=1; w=target
        eq_hist.append(equity)
    peak=0; maxdd=0
    for e in eq_hist:
        peak=max(peak,e); maxdd=min(maxdd,e/peak-1)
    years=(i1-i0)/244
    cagr = eq_hist[-1]**(1/years)-1 if years>0.5 else eq_hist[-1]-1
    print(f"{label:46s} 总收益 {eq_hist[-1]-1:+8.1%}  年化 {cagr:+7.1%}  最大回撤 {maxdd:7.1%}  卡玛 {cagr/abs(maxdd):5.2f}  调仓 {trades:3d} 次")

print("\n\n############ 第三轮：保险丝+判别（上限/下限）############")
for (s,e,tag) in [("2025-01-02","2026-06-01","bot11回测窗口"),
                  ("2021-01-01","2023-12-29","含2022熊市"),
                  ("2014-06-01","2026-06-01","12年全样本")]:
    print(f"\n=== {tag} ({s} → {e}) ===")
    buyhold(s,e,"买入持有")
    simulate3(s,e,judge=True, label="上限：判别正确(2022砍/2025不砍)+急跌满仓")
    simulate3(s,e,judge=False,label="下限：判别全错=每次保险丝都砍")
