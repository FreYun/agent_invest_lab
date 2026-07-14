"""S3 Markdown render helpers."""

from __future__ import annotations


def render_md(payload: dict) -> str:
    lines = [f"# S3 首板接力候选清单 — {payload['date']}", ""]
    regime = payload.get("regime_input", {})
    lines += [
        "## 上游 Regime",
        "",
        f"- **regime**: {regime.get('regime_name', regime.get('code', '?'))} (score={regime.get('score', '?')})",
        f"- **confidence**: {regime.get('confidence', '?')}",
        f"- **base 单票上限**: {regime.get('position_limit_single_base', '?')}",
        "",
    ]
    gate = payload.get("regime_gate") or {}
    if gate:
        gate_label = "✅ 开启" if gate.get("allowed") else "⚠️ 关闭"
        lines += [
            "## Regime Gate (Advisory)",
            "",
            f"- **状态**: {gate_label}",
        ]
        if gate.get("reason"):
            lines.append(f"- **原因**: {gate['reason']}")
        if gate.get("playbook_mode"):
            lines.append(f"- **playbook mode**: {gate['playbook_mode']}")
        if not gate.get("allowed"):
            lines.append("- _候选仍照常生成, 是否实盘请由 agent 综合判断._")
        lines.append("")
    if payload.get("skipped_reason"):
        lines += ["## 本日跳过", "", f"**原因**: {payload['skipped_reason']}", ""]
        return "\n".join(lines)
    stats = payload.get("stats") or {}
    lines += [
        "## 选股漏斗",
        "",
        "| 阶段 | 数量 |",
        "|---|---|",
        f"| 涨停池 | {stats.get('universe_size', 0)} |",
        f"| 首板池 | {stats.get('first_board_pool_size', 0)} |",
        f"| 信号通过 | {stats.get('passed_count', 0)} |",
        "",
    ]
    candidates = payload.get("candidates", [])
    if not candidates:
        lines += ["## 候选清单", "", "**今日 0 只 candidate**。", ""]
        return "\n".join(lines)
    lines += [f"## 候选清单 ({len(candidates)} 只)", ""]
    for i, c in enumerate(candidates, 1):
        s = c["signal"]
        lines += [
            f"### {i}. {c['code']} {c['name']} ({c['industry']})",
            "",
            f"- 行业 rank={s.get('sector_rank')} 涨停数={s.get('sector_limit_count')}",
            f"- 首封={s.get('first_seal_time')} 炸板={s.get('blast_count')} 换手={s.get('turnover_rate'):.2f}%",
            f"- 入场区: {c['entry']['zone_low']:.2f} ~ {c['entry']['zone_high']:.2f} ({c['entry']['rule']})",
            f"- 止损: {c['stop_loss']['price']:.2f} ({c['stop_loss']['rule']})",
            f"- 建议仓位: {c['position_pct']*100:.2f}% `{c['position_calc']}`",
            "",
        ]
    return "\n".join(lines)


def render_verification_md(t1_date: str, t_date: str, results: list, mode: str = "backtest") -> str:
    lines = [f"# S3 T+1 验证 — {t1_date} (T 日 = {t_date}) [mode={mode}]", ""]
    if not results:
        lines.append("**T 日无 candidate 或 T+1 数据缺失。**")
        return "\n".join(lines)
    counts = {}
    pnls = []
    for r in results:
        v = r["verification"]
        counts[v["status"]] = counts.get(v["status"], 0) + 1
        if v.get("pnl_pct") is not None:
            pnls.append(v["pnl_pct"])
    lines += ["## 状态统计", ""]
    for status, n in sorted(counts.items()):
        lines.append(f"- **{status}**: {n}")
    lines.append("")
    if pnls:
        wins = sum(1 for p in pnls if p > 0)
        lines += [
            "## 盈亏汇总",
            "",
            f"- 触发: **{len(pnls)}/{len(results)}**",
            f"- 胜率: **{wins}/{len(pnls)} = {wins*100/len(pnls):.0f}%**",
            f"- 平均盈亏: **{sum(pnls)/len(pnls):+.2f}%**",
            "",
        ]
    lines += ["## 明细", ""]
    for r in results:
        c = r["candidate"]
        v = r["verification"]
        title = f"### {c['code']} {c['name']} — {v['status']}"
        if v.get("pnl_pct") is not None:
            title += f" ({v['pnl_pct']:+.2f}%)"
        lines += [title, "", f"- {v.get('note')}", ""]
    return "\n".join(lines)
