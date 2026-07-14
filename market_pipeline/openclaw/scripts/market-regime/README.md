# 市场 Regime 判断器

每日 A 股市场的 regime 分类器。六维打分 → 映射 5 档 regime (强牛/强势震荡/中性震荡/弱势震荡/熊) → 附 `switched` 和 `emergency_switch` 标记, 供下游 skill (如 [s5-dragon-pullback](../s5-dragon-pullback/SKILL.md)) 决策。

> ⚠️ **仓位决策**: regime 的梯度仓位表 (`强牛 90% / 强势 70% / ...`) **已在 11 年样本验证失效**, 不要用。
>
> 已验证的仓位规则是 **v2_entry_only**: 默认 50% 底仓, `switched=1` 且 `regime ∈ {强牛, 强势震荡}` 的那天加仓到 90%。详情 (含 4 次 out-of-sample 验证、失败替代方案、边界) 见 [memory/position-sizing-v2-entry-only.md](memory/position-sizing-v2-entry-only.md)。

---

## 生产链路

每日 16:00 cron 跑 `/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/daily-regime-pipeline.sh`:

```
Step 1  workspace-bot11/scripts/review/daily_review.py
        → 采集并写入 market.db:
          daily / stk_limit / regime_raw_daily / index_daily

Step 2  scripts/backfill/replay.py --rules v2 --start 20150105 --end <today>
        → 从 2015 全量回放 (保证 3 日确认链一致)
        → 直接 INSERT OR REPLACE 写 market.db.regime_classify_daily
```

下游 skill 读 regime 的唯一数据源是 `market.db.regime_classify_daily` (v2 规则), 示例见 [s5-dragon-pullback/scripts/regime_loader.py](../s5-dragon-pullback/scripts/regime_loader.py)。

---

## 输出表 regime_classify_daily

| 字段 | 说明 |
|------|------|
| `trade_date` | YYYY-MM-DD |
| `rules_version` | `v2` (生产) |
| `total_score` | 六维总分 [-12, +12] |
| `score_ma_position` / `score_advance_decline` / `score_sentiment_delta` / `score_sentiment_index` / `score_streak_height` / `score_volume_trend` | 各维度得分 [-2, +2] |
| `regime_code` | `STRONG_BULL` / `STRONG_RANGE` / `NEUTRAL_RANGE` / `WEAK_RANGE` / `BEAR` |
| `regime_name` | 对应中文 |
| `last_regime_code` | 前一交易日 regime |
| `switched` | 今日是否切换 (0/1) |
| `bootstrap` | 是否在开头 3 日 bootstrap 状态 (0/1) |
| `emergency_switch` | 是否触发逃生门 (0/1) |
| `emergency_direction` | `up` / `down` / NULL |
| `emergency_reason` | 逃生门触发原因, 分号分隔 |
| `confidence` | `high` / `medium` / `low` (按缺失维度数降级) |
| `missing_dims` | 缺失的维度列表, 分号分隔 |

playbook 字段 (regime → 推荐战法/禁止战法/仓位上限) **不入库**, 是 regime_code 的派生查表, 消费时 import `scripts/regime_rules.py` 的 `lookup_playbook(regime_code)`。

---

## 手动查询

```bash
# 最近 5 天
python3 -c "
import sqlite3
c = sqlite3.connect('/home/rooot/agent_invest_lab/data/market.db')
c.row_factory = sqlite3.Row
for r in c.execute('''
  SELECT trade_date, regime_name, total_score, switched, emergency_switch, confidence
  FROM regime_classify_daily WHERE rules_version='v2'
  ORDER BY trade_date DESC LIMIT 5
'''): print(dict(r))
"

# 重跑历史 (cron 也是这个命令)
cd scripts/backfill && python3 replay.py --rules v2 --start 20150105 --end 20260416
```

**注意**: replay.py 必须从 `20150105` 起, 否则窗口内前 3 日 bootstrap + MA/量比等派生指标会错, 并覆盖正确行。短窗口会被拒绝 (rc=2)。

---

## 六维打分 / Regime 映射 / 战法清单

详见:
- [references/scoring.md](references/scoring.md) — 六维打分规则与阈值
- [references/mapping.md](references/mapping.md) — regime → 战法推荐/禁止/仓位上限映射
- [references/playbooks.md](references/playbooks.md) — 8 个战法 (S1–S8) 简介
- [references/regime-examples.md](references/regime-examples.md) — 历史案例库

---

## 文件清单 (Phase 2 精简后)

```
scripts/
├── backfill/                  cron 生产链路
│   ├── replay.py              ✅ cron 入口, 写 regime_classify_daily
│   ├── db.py                  共享 DB 连接
│   ├── derive_raw_data.py     从 daily/stk_limit 派生 raw
│   └── rules_v2.py            逃生门 v2 规则
├── scoring.py                 六维打分 + score_all_dims + build_scores_window
├── regime_rules.py            3 日确认 + 逃生门 + regime mapping + lookup_playbook
├── output_writer.py           ClassifyResult + determine_confidence (瘦身后)
├── parser.py                  复盘 MD 解析 (classify 退役后主要服务于 backfill)
├── tushare_client.py          Tushare 封装
└── tests/                     单测

memory/experiments/            归档: 历史实验脚本 + 实验报告 (可删)
```

历史: 2026-04-17 Phase 2 清理, 删除了 `classify.py` 单日分类 CLI + regime-log.md 日志 + MD/JSON 渲染 (output_writer 里的 write_md/write_json/append_log/parse_regime_log), 现在 regime 数据单一来自 replay.py → DB。
