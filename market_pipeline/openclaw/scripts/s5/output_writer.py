"""S5 MD 渲染 — 从 DB payload 生成人可读 Markdown。

仅 render.py (on-demand CLI) 使用。日常选股/验证不再生成 MD 文件。
"""

from __future__ import annotations


def render_md(payload: dict) -> str:
    """把 select 的 payload 渲染成人可读的 Markdown。"""
    date = payload["date"]
    candidates = payload.get("candidates", [])
    skipped = payload.get("skipped_reason")
    stats = payload.get("stats")
    regime_input = payload.get("regime_input", {})

    lines = []
    lines.append(f"# S5 龙回头候选清单 — {date}")
    lines.append("")

    lines.append("## 上游 Regime")
    lines.append("")
    lines.append(f"- **regime**: {regime_input.get('regime_name', regime_input.get('code', '?'))} (score={regime_input.get('score', '?')})")
    lines.append(f"- **confidence**: {regime_input.get('confidence', '?')}")
    lines.append(f"- **switched**: {regime_input.get('switched', False)}")
    lines.append(f"- **emergency_switch**: {regime_input.get('emergency_switch', False)}")
    lines.append(f"- **base 单票上限**: {regime_input.get('position_limit_single_base', '?')}")
    lines.append("")

    gate = payload.get("regime_gate") or {}
    if gate:
        gate_label = "✅ 开启" if gate.get("allowed") else "⚠️ 关闭"
        lines.append("## Regime Gate (Advisory)")
        lines.append("")
        lines.append(f"- **状态**: {gate_label}")
        if gate.get("reason"):
            lines.append(f"- **原因**: {gate['reason']}")
        if not gate.get("allowed"):
            lines.append("- _候选仍照常生成, 是否实盘请由 agent 综合判断._")
        lines.append("")

    if skipped:
        lines.append("## ⏭️ 本日跳过")
        lines.append("")
        lines.append(f"**原因**: {skipped}")
        lines.append("")
        return "\n".join(lines)

    if stats:
        lines.append("## 选股漏斗")
        lines.append("")
        lines.append("| 阶段 | 数量 |")
        lines.append("|---|---|")
        lines.append(f"| 候选股池 (universe) | {stats.get('universe_size', 0)} |")
        lines.append(f"| 龙头池 (≥2 连板) | {stats.get('dragon_pool_size', 0)} |")
        lines.append(f"| 信号通过 (passed) | {stats.get('passed_count', 0)} |")
        lines.append("")

    if not candidates:
        lines.append("## 候选清单")
        lines.append("")
        lines.append("**今日 0 只 candidate**。可能原因: 无前期龙头进入冷却 / 无足够大反包 / 行情偏弱无热门题材。")
        lines.append("")
        rejects = payload.get("reject_samples", [])
        if rejects:
            lines.append("### 拒因抽样 (前 5 只最接近的)")
            lines.append("")
            for r in rejects[:5]:
                lines.append(f"- **{r['code']} {r['name']}** — {r['reject_reason']}")
            lines.append("")
        return "\n".join(lines)

    lines.append(f"## 候选清单 ({len(candidates)} 只)")
    lines.append("")
    for i, c in enumerate(candidates, 1):
        lines.append(f"### {i}. {c['code']} {c['name']} ({c['industry']})")
        lines.append("")
        peak = c["dragon_peak"]
        cd = c["cooldown"]
        rb = c["rebound"]
        lines.append(f"- **龙头日**: {peak['date']} 收 {peak['close']:.2f}, 最高 {peak['max_streak']} 板")
        lines.append(f"- **冷却**: {cd['days']} 个交易日, 阶段跌幅 {cd['drop_pct']:.2f}%")
        lines.append(f"- **反包**: T 日涨 {rb['t_pct']:.2f}%, 收 {rb['t_close']:.2f}, T-1 最高 {rb['t1_high']:.2f}")
        lines.append("")
        e = c["entry"]
        sl = c["stop_loss"]
        t1 = c["target_1"]
        t2 = c["target_2"]
        lines.append(f"- **入场区**: {e['zone_low']:.2f} ~ {e['zone_high']:.2f} ({e['rule']})")
        lines.append(f"- **止损**: {sl['price']:.2f} ({sl['rule']})")
        lines.append(f"- **止盈 1**: {t1['price']:.2f} (+{t1['pct']:.0f}%)")
        lines.append(f"- **止盈 2**: {t2['price']:.2f} (+{t2['pct']:.0f}%)")
        lines.append(f"- **建议仓位**: {c['position_pct'] * 100:.2f}%")
        lines.append(f"  - 计算: `{c['position_calc']}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 本清单仅供参考, 实盘前请人工复核。T+1 开盘 5-10 分钟跑 `verify.py` 检查实际开盘是否触发买点。")
    lines.append("")
    return "\n".join(lines)


def render_verification_md(t1_date: str, t_date: str, results: list, mode: str = "live") -> str:
    lines = []
    lines.append(f"# S5 T+1 验证 — {t1_date} (T 日 = {t_date}) [mode={mode}]")
    lines.append("")

    if not results:
        lines.append("**T 日无 candidate 或 T+1 数据缺失。**")
        return "\n".join(lines)

    counts = {}
    for r in results:
        counts[r["verification"]["status"]] = counts.get(r["verification"]["status"], 0) + 1

    lines.append("## 状态统计")
    lines.append("")
    for status, n in sorted(counts.items()):
        lines.append(f"- **{status}**: {n}")
    lines.append("")

    if mode == "backtest":
        triggered = [r for r in results if r["verification"].get("pnl_pct") is not None]
        if triggered:
            pnls = [r["verification"]["pnl_pct"] for r in triggered]
            avg = sum(pnls) / len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            lines.append("## 盈亏汇总")
            lines.append("")
            lines.append(f"- 触发: **{len(triggered)}/{len(results)}**")
            lines.append(f"- 胜率 (pnl>0): **{wins}/{len(triggered)} = {wins*100/len(triggered):.0f}%**")
            lines.append(f"- 平均盈亏: **{avg:+.2f}%**")
            lines.append(f"- 最高盈: {max(pnls):+.2f}%")
            lines.append(f"- 最大亏: {min(pnls):+.2f}%")
            lines.append("")

    lines.append("## 明细")
    lines.append("")
    for r in results:
        c = r["candidate"]
        v = r["verification"]
        emoji = {
            "triggered": "✅", "triggered_late": "🟡", "gap_up_skip": "⚠️",
            "stop_hit": "❌", "wait": "⏳", "no_data": "🚫", "parse_error": "🚫",
            "triggered_at_open": "✅", "triggered_intraday": "🟡",
            "gap_down_skip": "⚠️", "hit_target_1": "🎯", "hit_target_2": "🎯🎯",
            "close_hold": "📦",
        }.get(v["status"], "?")
        title = f"### {emoji} {c['code']} {c['name']} — {v['status']}"
        if v.get("pnl_pct") is not None:
            title += f" ({v['pnl_pct']:+.2f}%)"
        lines.append(title)
        lines.append("")
        lines.append(f"- 入场区: {c['entry']['zone_low']:.2f} ~ {c['entry']['zone_high']:.2f}")
        lines.append(f"- 止损: {c['stop_loss']['price']:.2f}")
        lines.append(f"- 止盈 1/2: {c['target_1']['price']:.2f} / {c['target_2']['price']:.2f}")
        if mode == "backtest":
            if v.get("t1_open") is not None:
                lines.append(f"- T+1 OHLC: O={v['t1_open']:.2f} H={v['t1_high']:.2f} L={v['t1_low']:.2f} C={v['t1_close']:.2f}")
            if v.get("entry_price") is not None:
                lines.append(f"- 入场价: {v['entry_price']:.2f}")
                lines.append(f"- 退出价: {v['exit_price']:.2f} ({v.get('exit_reason')})")
        else:
            if v.get("open_price") is not None:
                lines.append(f"- T+1 开盘: {v['open_price']:.2f}")
            if v.get("current") is not None:
                lines.append(f"- 当前: {v['current']:.2f}")
        lines.append(f"- **{v['note']}**")
        lines.append("")

    return "\n".join(lines)
