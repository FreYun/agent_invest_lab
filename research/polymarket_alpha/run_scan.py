import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd
from align import load_signal, align
from data_targets import load_target
from ic_scan import spearman_ic, quantile_monotonicity, detrend

HYPOS = {
  "H1_rate_recession": (["fed_no_cut_2026","fed_hike_2026","recession_2026"],
                        ["000300.SH","000001.SH","QQQ","SPY"]),
  "H2_geo_gold":       (["taiwan_risk_2026","iran_regime_fall","hormuz_normal_2026"],
                        ["518880","000300.SH"]),
  "H3_crypto_growth":  (["btc_dip_50k_2026","eth_dip_500_2026"],
                        ["399006.SZ","000688.SH","QQQ"]),
}
HORIZONS = [5, 10, 20]
US = {"QQQ","SPY","513100","513500"}

rows = []
for hypo,(factors,targets) in HYPOS.items():
    for f in factors:
        sig = load_signal(f)
        for t in targets:
            try: price = load_target(t)
            except FileNotFoundError: continue
            lag = 1 if t in US else 0
            for h in HORIZONS:
                base = align(sig, price, h, extra_lag=lag)
                # level 版
                rows.append([hypo,f,t,h,"level",spearman_ic(base),quantile_monotonicity(base),len(base)])
                # diff(去趋势) 版: 对 sig 去趋势后重新对齐
                dsig = detrend(sig, 5)
                dbase = align(dsig, price, h, extra_lag=lag)
                rows.append([hypo,f,t,h,"diff",spearman_ic(dbase),quantile_monotonicity(dbase),len(dbase)])

df = pd.DataFrame(rows, columns=["hypo","factor","target","horizon","mode","ic","qmono","n"])
df.to_csv("/home/rooot/agent_invest_lab/research/polymarket_alpha/summary.csv", index=False)

# robustness 判据(仅 diff 版): 每个 factor 跨 targets IC 同号占比
def struct_flag(g):
    d = g[g["mode"]=="diff"].dropna(subset=["ic"])
    if len(d) < 3: return "样本不足"
    pos = (d["ic"] > 0).mean()
    same = max(pos, 1-pos)
    med = d["ic"].abs().median()
    return "有结构(hint)" if same >= 0.75 and med >= 0.1 else "无结构/噪声"
flags = df.groupby("factor").apply(struct_flag)

with open("/home/rooot/agent_invest_lab/research/polymarket_alpha/report.md","w") as fp:
    fp.write("# Polymarket 信号发现 · 结果报告\n\n")
    fp.write(f"- as-of: 2026-07-03  source: polymarket.db/macro_factor + market.db\n")
    fp.write("- 全部为**黄灯/hint 级**(样本<2年,单一regime),不得升格生产因子\n\n")
    fp.write("## 各因子结构判定(以 diff 去趋势版为准)\n\n")
    for f,flag in flags.items():
        fp.write(f"- `{f}`: {flag}\n")
    fp.write("\n## 完整明细见 summary.csv\n")
    fp.write("\n## 诚实盲区\n- level 版 IC 多为趋势假象,已实测去趋势后大幅衰减,故判定只采 diff 版\n")
    fp.write("- 1年单一regime,跨regime未验证\n- 尾部/加密合约流动性低,定价可能偏离真实概率\n")
print("done", len(df), "rows")
