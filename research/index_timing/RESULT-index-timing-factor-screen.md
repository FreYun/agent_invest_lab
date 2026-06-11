# index-products 择时因子全景筛选结论（2026-06-09 收尾）

承接 2026-06-07 那条**未收尾**的研究线：给 `strategies/index-products/` 一篮子指数，逐个找一个
经 validation 验证的择时量化因子。本文件把当时只跑了脚本、没落盘的结论补全并定级。

- 复现脚本：本目录 `green_growth.py` `green_validate.py` `reval_index.py` `screen_donchian_idx.py`
  `screen_d2.py` `green_probe_sz50.py`
- 引擎：vibe-trading（`/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python`），
  统计三关 = bootstrap Sharpe 95%CI + walk-forward 5 窗 + 因子 Calmar vs buyhold
- 数据：指数全周期（多数 2014/2018 起）优于 ETF 短样本——ETF 短样本会造假绿灯（见双创50）
- ⚠️ 标注坑：`screen_d2.py` 曾把「绿色电力」误标成 931151（实为**中证光伏产业**），真正的
  **中证绿色电力是 931897**，漏测后已补（结论 ❌）。指数中文名↔代码务必核对，别凭名字想当然
- 成本 5bp 单边、T+1（`sig.shift(1)`）

## 一句话结论

**donchian_60 趋势突破只在「高波动 + 强趋势」的成长制造指数（半导体、光伏）上是绿灯；
在大盘价值、宽基、低波动/震荡行业上一律跑输 buyhold。大盘价值（上证50/红利/银行/白酒）
只能用估值锚 erp_vix（黄灯），不能用趋势择时。** 这是 sse50.md 论断「大盘蓝筹趋势择时无效」
的对称、全市场验证。

## 二、全景结论表（donchian_60，全周期指数）

判定口径：✅绿灯 = 因子 Cal>BH 且 CI 下界>0 且 wf≥4/5；🟢近绿 = 因子>BH 且 CI 下界>0 但 wf=3/5；
🟡 = 因子>BH 但统计没过；❌ = 因子 ≤ buyhold。

| 指数 | 代码 | 因子Cal(BH) | DD改善 | bootstrap CI / wf | 判定 |
|---|---|---|---|---|---|
| 半导体设备 | 931743.CSI | 0.85 (0.55) | −34% vs −62% | CI[0.28,1.70] wf4/5 | ✅绿灯（最强） |
| 中证光伏产业 | 931151.CSI | 0.24 (0.11) | −55% vs −69% | CI[0.09,1.17] wf4/5 | ✅绿灯（翻转~23/年偏高）|
| 中证新能源(深) | 399808.SZ | 0.27 (0.13) | −46% vs −69% | CI[0.06,1.15] wf4/5 | ✅绿灯 |
| 中证半导 | 931865.CSI | 0.44 (0.41) | +11pp | CI[0.17,1.44] wf3/5 | 🟢近绿 |
| 中证新能源 | 930997.CSI | 0.39 (0.11) | +36pp | CI[−0.14,1.20] wf3/5 | 🟡（Cal大赢但时间不稳）|
| 中证内地新能源 | 000941.CSI | 0.16 (0.11) | −52% vs −70% | CI[−0.10,1.02] wf5/5 | 🟢近绿（CI含0但wf满分）|
| 中证绿色电力 | 931897.CSI | 0.05 (0.09) | ≈ | CI[−0.46,0.80] wf4/5 | ❌（光伏≠绿电，绿电含水/核电趋势弱）|
| 双创50 | 931643.CSI | 0.14 (0.29) | — | CI[−0.31,1.15] wf3/5 | ❌（ETF短样本假绿灯）|
| 科创50 | 000688.SH | 0.01 (0.14) | — | CI[−0.69,0.94] wf3/5 | ❌ |
| 创业板指 | 399006.SZ | 0.11 (0.14) | +9pp | CI[−0.16,0.94] wf3/5 | ❌（改用 trail_dd）|
| 创新药 | 931152.CSI | 0.04 (−0.03) | +9pp | wf2/5 | 🟡弱 |
| 中证消费 | 000932.SH | 0.17 (0.12) | +19pp | wf3/5 | 🟡 |
| 细分化工 | 000813.CSI | 0.11 (0.07) | +2pp | wf3/5 | 🟡 |
| 中证500 / 军工 / 白酒 / 券商 / 银行 / 煤炭 / 国证芯片 / 医疗 | — | ≤BH | — | wf2-3/5 | ❌ |
| 云计算 / 动漫 / 电池 / 电力 / 通信 / A500 / 800消费 / 电网 / 机器人 / 军工龙头 / 自由现金流 | — | ≤BH | — | — | ❌ |

## 三、价格因子之外：成长指数的 trail_dd（降回撤型黄灯）

donchian 不灵的成长指数，trail_dd 能做「降回撤但不增收益」的黄灯：

- 创业板 trail_dd15/20：robustness 全档 OOS dCal +0.16~+0.34（稳），但长样本(2014-26)validation
  Cal 0.14≈BH 0.15、CI[−0.13,1.00]含0、wf3/5 → 只降回撤不增收益，黄灯。
- 科创50 trail_dd15：Cal 0.01 vs BH 0.08 跑输 → 毙。

## 四、宽基（大盘价值）：erp_vix 黄灯，趋势因子毙

上证50 `green_probe_sz50.py` 收尾确认（与已注册 `sse50_erp_vix_timing` 一致）：

- erp_vix：OOS(24-26) Cal 1.02 vs BH 0.74、DD −9% vs −14%；全周期 Cal 0.18 vs BH 0.03；
  bootstrap CI[−0.36,1.01] p+84%（含0）→ **黄灯/风险管理 overlay**，定级正确。
- donchian/dd_ladder/trail_dd 在上证50 **OOS 全部跑输**（donchian_60 dCal−0.14）→ 趋势择时无效，
  印证 sse50.md。

## 五、对各 index-products .md 的行动建议

| md | 现状 | 建议 |
|---|---|---|
| sse50.md | erp_vix 已写、因子已注册 | 无需动 ✅ |
| zz1000.md | erp_vix + donchian + dd_ladder 已写 | 无需动 ✅ |
| semiconductor.md | donchian 已写（target 931743） | 本次确认绿灯，无需动 ✅ |
| new-energy.md | 纯定性框架 | **已写入**光伏 931151 + 中证新能源(深)399808 的 donchian_60 作「趋势择时辅助」（绿灯，翻转偏高需 5bp 把关）。注意母指数中证新能源 930997 只黄灯、绿色电力 931897 donchian 跑输，仅光伏/新能源(深)子板块能用 |
| shuangchuang.md | 纯定性框架 | donchian 已被双创50/科创50 长样本证伪，**保持纯定性**，勿包装 |
| innodrug / baijiu / nonferrous / dividend.md | 纯定性框架 | donchian 全毙，**保持纯定性**，勿再挖 donchian |
| hs300.md | 估值定性 + erp_vix 因子备选 | 无需动 |

## 六、经济逻辑（为什么 donchian 只在半导体/光伏 work）

趋势突破系统需要标的具备**强趋势性 + 高波动**：半导体、光伏是典型产能/技术驱动的成长制造，
单边行情持续、突破有效，donchian 低翻转拿住主升、熊市离场降回撤。反观：
- 大盘价值（上证50/红利/银行/白酒）：波动温吞、均值回归，趋势系统被假突破反复打脸（whipsaw）。
- 宽基（500/A500）：成分分散互相抵消，无清晰趋势。
- 震荡行业（军工/券商/医疗）：政策/事件驱动、无持续趋势。
→ 这些标的的择时只能靠估值锚（erp_vix）或纯定性多维框架，不能靠趋势因子。

## 七、别重挖清单（负结果存档）

- donchian 在以下指数已严格证伪，**勿重挖**：中证500、中证A500、军工(399967/931066)、券商、银行、
  白酒、煤炭、国证芯片、中证医疗、创新药、中证消费、云计算、动漫游戏、CS电池、电力、通信、
  800消费、电网设备、机器人、自由现金流、科创50、双创50、创业板指、**中证绿色电力931897**。

## 八、新能源6子板块全因子族复扫（2026-06-09，推翻"只测 donchian"的局限）

脚本 `screen_ne6_allfactors.py`：6 指数 × local_sweep **全 44 因子** IS(起点–2021)/OOS(2022–2026) + validation。
**重要更正**：本会话早先只测了 `donchian_60` + `trail_dd15` 两个因子就下结论，**不全面**；全扫 44 因子后多个子板块翻案。

| 指数 | 最佳绿灯因子(OOS) | 关键数字 | 备注 |
|---|---|---|---|
| 光伏 931151 | **mom_60 动量** | OOS Cal 0.45 vs BH −0.17(dCal +0.62)、CI[0.09,1.20]、wf4/5、flips32 | 动量 > donchian；trail_dd10/15、chand 也绿 |
| 电网 931994 | chand_k3 / trail_dd10 / dd_ladder | OOS Cal 0.49 vs 0.34、wf4/5 | donchian 也绿，chandelier 更优 |
| 电池 931719 | **chand_k2 / trail_dd10 / dd_ladder_7_15** | OOS Cal 0.62 vs −0.08(dCal +0.69)、CI[0.43,1.51]、**wf5/5** | 翻案：donchian❌ 但 DD 止损族强绿灯 |
| 新能车 399976 | **chand_k3 / bearonly_150 / trail_dd10 / dd_ladder** | OOS Cal 0.31 vs −0.09(dCal +0.40)、wf4/5 | 翻案：非"趋势无效"，DD 止损族 + 熊市规避绿灯 |
| 绿电 931897 | 无 | 无因子 IS+OOS 同时击败 BH | 确认公用事业无趋势 edge |
| 电力 H30199 | 仅 vol_target（伪绿） | flips **348–669 次/年** | 高翻转，5bp 成本下不可用，实质无干净绿灯 |

**规律修正**：电池/新能车不是"趋势择时整体失效"，而是 **donchian 趋势突破族失效、但 DD-first 回撤止损族（trail_dd / dd_ladder / chandelier）是绿灯**——它们"高波动 + 趋势反复但有大级别回撤"，追突破被 whipsaw，而"回撤止损 + 均线再入"能避开 2022–23 大跌段。光伏则是**动量(mom_60)最优**。

**OHLC 注意**：simworld 指数 quote 只有 close、无 high/low，chandelier 的 ATR 用 close 代理偏紧、结果偏乐观，需真 OHLC（ETF akshare）复核；但 **trail_dd10 / dd_ladder / mom_60 是纯 close、绿灯可靠**，family 结论稳。

**待办**：据此修正 `battery.md` / `nev.md`（补 DD-first 绿灯，删除"趋势失效→纯定性"的误判）、`solar.md`（补 mom_60 动量优于 donchian）。
- 大盘价值（上证50 已测）趋势/突破/dd_ladder/trail_dd 全部 OOS 跑输，**勿重挖**。

## 九、TMT/主题 9 指数全因子族复扫（2026-06-11，为 7 份新 methodology 做的）

脚本 `screen_7idx_allfactors.py`（数据探查 `probe_7idx.py`）：9 指数 × 全 44 因子
IS(起点–2021)/OOS(2022–2026.06) + validation（bootstrap 600、wf 5 窗、5bp、T+1）。
**§七"勿重挖清单"再次部分翻案**——donchian 在通信/云计算/动漫/国证芯片确实❌（不矛盾），
但 DD-止损族 / 动量族在这些指数上大面积绿灯（与 §八 新能源教训同构）。
注意：simworld 指数 quote 全部无 high/low，chand_* / donch*_trail 的 ATR 用 close 代理
**偏乐观、仅参考**；下表只列**纯 close 因子**（trail_dd / dd_ladder / mom / donchian /
bearonly / deepdd / rsi 均纯 close，可靠）。

| 指数 | 代码 | 样本 | 最佳纯close绿灯(OOS) | 关键数字 | 备注 |
|---|---|---|---|---|---|
| 通信设备 | 931160.CSI | 11.5y | **trail_dd10_p120** | OOS Cal 1.74 vs BH 1.04、DD −22% vs −39%、CI[0.09,1.23]、wf4/5 | dd_ladder_7_15 / trail_dd15 也绿，**DD-止损整族绿灯** |
| 通信技术 | 931144.CSI | 10.4y | **donchian_60** + mom_20 | donchian: dCal+0.06 但 IS_dCal+0.19、flips 仅10、CI[0.15,1.32] wf4/5；mom_20 wf5/5 | 趋势突破+动量两族正交都绿 |
| CS人工智 | 930713.CSI | 10.8y | mom_20（🟢近绿） | OOS Cal 0.61 vs 0.31、CI[−0.02,1.18] 含0、p+97%、wf5/5 | **唯一赢家、无族佐证 → 只能黄灯用** |
| 创业板人工智能 | 970070.SZ | 7.4y | dl_bearonly_150 | OOS Cal 1.31 vs 0.82、CI[0.30,1.80]、wf5/5 | 备选口径 |
| 科创芯片 | 000685.SH | 6.4y(IS<3y) | **无** | IS 是 2020–21 单边牛（BH IS_cal 0.90），任何择时都输 IS | **纯定性，勿包装**；探索性记 regime_lever OOS dCal+0.41 |
| 国证芯片 | 980017.SZ | 14.4y | mom_20 / rsi_panic_re_entry | mom_20: IS_dCal+0.48 OOS+0.20 CI[0.39,1.47] wf4/5；rsi: wf5/5 exp0.73 | donch120_trail exp 仅 0.09，不实用 |
| 中证半导 | 931865.CSI | 9.4y | **rsi_panic_re_entry** + mom_20 | rsi: OOS Cal 0.59 vs 0.43、CI[0.15,1.47] wf4/5；donchian_60 降级🟡(wf3/5) | §二"近绿"复核后改 rsi/mom 更优 |
| 云计算 | 930851.CSI | 13.9y | **mom_20** | OOS Cal 0.89 vs 0.21（dCal+0.68）、DD −25% vs −52%、CI[0.28,1.36]、**wf5/5** | bearonly/deepdd 也绿；§七翻案 |
| 动漫游戏 | 930901.CSI | 13.4y | **dl_deepdd15_p120** | OOS Cal 0.55 vs **−0.02**、DD −26% vs −58%、CI[0.05,1.14]、wf4/5 | trail_dd10_p60/deepdd20 也绿，DD 族级；§七翻案 |

**选择偏差提示**：单指数 44 选 1 必然 inflate；上表凡"整族绿灯"（通信设备 DD 族、动漫游戏 DD 族、
云计算多族）有族级鲁棒性背书；**单因子赢家（930713 mom_20）只能当黄灯/第二意见写进 methodology**。
产物：7 份 methodology 见 `strategies/index-products/`（telecom-equipment / telecom-tech / ai /
chip / semiconductor-industry / cloud / game）。
