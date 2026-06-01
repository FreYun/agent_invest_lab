# 行业自身申赎因子横截面扫描

日期：2026-05-29。

## 当前结论

已完整验证且本地有缓存的行业/主题只有两类：

| 行业/主题 | 指数 | 自身申赎是否有用 | 定级 | 关键证据 |
|---|---|---|---|---|
| 创新药 | 931152 中证创新药产业 | 有结构，但不够生产化 | 黄灯 | contrarian 方向正确；72% 参数配置在 4 个价格代理上击败 buyhold Calmar；median dCalmar +0.51；但有效样本约 15 个月、月度胜率 41%-53%、H2 稳定性不足 |
| 有色金属 | 000819 / 930708 / 399395 | 不可用 | 红灯 | contrarian 配置 3/3 beat 仅 2%，2/3 beat 仅 15%；median dCalmar -0.92；512400 上标准配置 Sharpe 0.82 vs buyhold 1.49 |

所以，按现有本地证据回答：**行业自己的申赎数据里，只有创新药有可用的情绪 hint；有色自己的申赎已经证伪。当前没有任何行业自身申赎达到绿灯生产因子。**

注意区分：有色能用的是全市场 `market_retail_contrarian_20_90` 迁移过去的仓位闸门，不是有色自己的申赎。

## 横截面扫描准备

脚本：`scan_industry_sr.py`。

候选池来自 `data/被动指数型基金池（权益黄金）.xlsx` + `data/fund.db` 中的 `fund_nav`，筛选条件：

- `基金分类` 包含 `指数型-股票`
- `主题(近一年)` 非 `全市场`
- 有 `指数代码`
- 代表基金 NAV 覆盖不少于 300 条
- 每个指数选择最新规模最大的代表基金

已生成候选池：`data/industry_universe.csv`，共 208 个非全市场主题/行业代表指数。

## 当前阻塞

`fund_index_subscription_redemption` 上游在 2026-05-29 对已知可用代码也返回 502：

- 931152 创新药：502
- 000819 有色：502
- 399975 证券公司：502
- 930653 食品饮料：502

因此本轮无法可靠拉取其它 206 个行业指数的历史申赎。脚本已经支持断点续传，但真实拉取默认禁用；后续只能在运维确认的窗口内、小批量、显式请求数上限地跑。

## 复现/继续扫描

```bash
cd /home/rooot/agent_invest_lab/research/industry_sr_scan
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python scan_industry_sr.py --build-universe

# 上游恢复且运维批准后再跑；每次必须显式限量，例如先探 5 个请求
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python scan_industry_sr.py --fetch --batch-size 1 --sleep 2.0 --confirm-live-fetch --max-requests 5
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python scan_industry_sr.py --backtest
```

输出文件：

- `data/industry_universe.csv`：208 个候选行业/主题指数及代表基金
- `data/industry_sr_daily.csv`：拉取成功后的行业自身申赎历史
- `data/industry_sr_errors.csv`：接口失败日志
- `data/industry_sr_sweep_full.csv`：全参数回测明细
- `data/industry_sr_summary.csv`：按行业/指数聚合后的红黄绿分级
