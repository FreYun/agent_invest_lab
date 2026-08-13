#!/usr/bin/env /usr/bin/python3.12
"""深研课题打标 + 与准确性交叉分析。"""
import os, json, re, sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import analyze_belief_accuracy as A

TOPICS = json.load(open("/tmp/dbot_topics.json"))

RE = {
    "timing":    re.compile(r"筑底|反弹|入场|建仓|加仓|减仓|清仓|止跌|拐点|见底|止盈|反转|反弹信号|止跌信号|抄底|是否见顶"),
    "direction": re.compile(r"看多|看空|多空|上涨|下跌|涨还是跌|反弹.*持续|会否继续|后续走势|概率"),
    "short":     re.compile(r"短期|次日|明日|本周|1[-~]?[357]日|三天|五日|一周内|3-5日|5日内"),
    "mid":       re.compile(r"1[-~][23]周|2[-~]4周|中期|1个月|一个月|3个月|三个月"),
    "long":      re.compile(r"半年|6个月|一年|1年|长期|长线|三年"),
    "competing": re.compile(r"competing explanations|竞争观点|一方面.*另一方面|多空双方|双方论据"),
    "macro_only_hit": re.compile(r"产业格局|长期景气|渗透率|供给出清|技术路径"),
    "position_ref":   re.compile(r"当前|自 ?20\d\d|回撤|PE|MA|温度|VIX|分位"),
}

def tag(t):
    tags = {k: bool(RE[k].search(t)) for k in ("timing","direction","competing")}
    tags["macro_only"] = bool(RE["macro_only_hit"].search(t)) and not bool(RE["position_ref"].search(t))
    for h_key, h_lab in [("short","短"),("mid","中"),("long","长")]:
        if RE[h_key].search(t):
            tags["horizon"] = h_lab; break
    else:
        tags["horizon"] = "未提"
    return tags

def main():
    all_topics = []; idx = defaultdict(list)
    for bot, items in TOPICS.items():
        for date, topic in items:
            tg = tag(topic); all_topics.append((bot, date, topic, tg))
            idx[(bot, date)].append((topic, tg))
    n = len(all_topics)
    print(f"# 深研课题特征打标\n共 {n} 条 start_research 课题\n")
    cnt = Counter(); hor = Counter()
    for _, _, _, tg in all_topics:
        for k in ("timing","direction","competing","macro_only"):
            if tg[k]: cnt[k] += 1
        hor[tg["horizon"]] += 1
    print("| 维度 | 命中 | 占比 |")
    print("|---|---:|---:|")
    for k in ("timing","direction","competing","macro_only"):
        print(f"| {k} | {cnt[k]} | {cnt[k]/n*100:.1f}% |")
    print("\n| horizon 提示 | 数量 | 占比 |")
    print("|---|---:|---:|")
    for h in ("短","中","长","未提"):
        print(f"| {h} | {hor[h]} | {hor[h]/n*100:.1f}% |")

    print("\n## 分 bot")
    print("| bot | 课题 | timing% | direction% | competing% | 短 | 中 | 长 | 未提 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    per = defaultdict(list)
    for bot, date, topic, tg in all_topics: per[bot].append(tg)
    for bot in ("bot5d","bot10d","bot16d","bot18d","bot20d"):
        tgs = per[bot]; m = len(tgs)
        if not m: continue
        pt = sum(1 for t in tgs if t["timing"])/m*100
        pd = sum(1 for t in tgs if t["direction"])/m*100
        pc = sum(1 for t in tgs if t["competing"])/m*100
        hs = Counter(t["horizon"] for t in tgs)
        print(f"| {bot} | {m} | {pt:.1f}% | {pd:.1f}% | {pc:.1f}% | {hs['短']} | {hs['中']} | {hs['长']} | {hs['未提']} |")

    records, _ = A.analyze()
    rec_deep = [r for r in records if r[7]]
    day_h = {(bot, date): items[0][1]["horizon"] for (bot, date), items in idx.items()}
    day_c = {(bot, date): items[0][1]["competing"] for (bot, date), items in idx.items()}
    bucket = defaultdict(list)
    for bot, date, h, pu, fret, y, fund, deep in rec_deep:
        bucket[(day_h.get((bot, date), "未提"), h)].append((pu, y))
    print("\n## 课题 horizon × 预测 horizon 命中率(仅深研日)\n")
    print("| 课题 horizon | t+1 命中 | t+5 命中 | t+20 命中 |")
    print("|---|---|---|---|")
    for dh in ("短","中","长","未提"):
        row = f"| {dh} "
        for fh in (1,5,20):
            items = bucket.get((dh, fh), [])
            called = [(pu,y) for pu,y in items if pu != 0.5]
            if called:
                hit = sum(1 for pu,y in called if (pu>0.5)==(y==1))/len(called)
                row += f"| {hit*100:.1f}%(n={len(called)}) "
            else:
                row += "| —(0) "
        print(row + "|")

    print("\n## competing_explanations 有 vs 无(仅深研日)\n")
    print("| competing | t+1 | t+5 | t+20 |")
    print("|---|---|---|---|")
    for flag in (True, False):
        buc = defaultdict(list)
        for bot, date, h, pu, fret, y, fund, deep in rec_deep:
            if day_c.get((bot, date)) is flag:
                buc[h].append((pu, y))
        row = f"| {flag} "
        for fh in (1,5,20):
            items = buc.get(fh, [])
            called = [(pu,y) for pu,y in items if pu != 0.5]
            if called:
                hit = sum(1 for pu,y in called if (pu>0.5)==(y==1))/len(called)
                row += f"| {hit*100:.1f}%(n={len(called)}) "
            else:
                row += "| —(0) "
        print(row + "|")

    print("\n## 抽样：深研日 t+5 看空(p_up<0.5)但实际上涨 的课题 前 6 条\n")
    misses = []
    for bot, date, h, pu, fret, y, fund, deep in rec_deep:
        if h != 5 or pu >= 0.5 or y != 1: continue
        if (bot, date) not in idx: continue
        topic = idx[(bot, date)][0][0]
        misses.append((bot, date, pu, fret, topic))
    misses.sort(key=lambda x: x[2])
    for bot, date, pu, fret, topic in misses[:6]:
        print(f"- **{bot} {date}** p_up={pu} fwd_ret={fret*100:+.2f}%")
        print(f"  > {topic[:240].strip()}...\n")

if __name__ == "__main__":
    main()
