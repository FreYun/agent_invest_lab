"""生成所有关键策略的净值 + 回撤对比 HTML.

输出单文件 HTML (Chart.js CDN, 离线打开需要联网加载 JS).

支持两种底层:
    --underlying hs300    HS300 指数 (默认, 对应 strategy_comparison.html)
    --underlying blended  50% HS300 + 50% CSI1000 每日再平衡合成指数
                         (对应 strategy_comparison_blended.html)
    --underlying both    同时生成两个

对 blended 底层, MA60 是用合成指数的 close 算的(策略看组合自己, 不看外部).
v2 regime 标签不变, 因为 classifier 本来就是全市场维度, 不依赖哪只指数.

包含的策略:
    fullhold             — 基准 (永远满仓)
    ma60_filter          — 3 个月趋势线开关 (两段都稳)
    v2_dynamic_cc        — spec §5 梯度仓位 (信号改善加仓/减弱减仓)
    v2_entry_only_cc     — 切入日 90%, 其他 50%
    v2_entry_held_5d_cc  — 切入后持 5 天 90%, 其他 30%
    v2_bullish_only      — 只在强牛+强势震荡满仓 (反面教材)
    v2_no_bear           — 只躲熊市 (反面教材)

执行模型: next_open (真实 T+1 open 建仓)
PnL 引擎: cc (close-to-close, 真实口径)

用法:
    python3 generate_strategy_chart.py                         # 默认 HS300
    python3 generate_strategy_chart.py --underlying blended   # 50/50 合成
    python3 generate_strategy_chart.py --underlying both      # 两个都跑
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect  # noqa: E402
from ma_trend_filter_backtest import (  # noqa: E402
    build_ma_signals,
    load_hs300_ordered,
    simulate_filter,
    simulate_fullhold,
)
from v2_regime_filter_backtest import (  # noqa: E402
    STRATEGIES as V2_STRATEGIES,
    load_v2_classify,
    simulate as v2_simulate,
)


def load_index_ordered(conn, ts_code: str) -> list[dict]:
    """通用指数加载器 (同 load_hs300_ordered 但参数化 ts_code)."""
    rows = conn.execute(
        "SELECT trade_date, open, close FROM index_daily "
        "WHERE ts_code=? ORDER BY trade_date ASC",
        (ts_code,),
    ).fetchall()
    out = []
    for d, o, c in rows:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        out.append({"date": iso, "open": float(o), "close": float(c)})
    return out


def build_blended_series(hs_rows: list[dict], csi_rows: list[dict],
                         w_hs: float = 0.5, w_csi: float = 0.5) -> list[dict]:
    """构造每日再平衡的合成指数.

    规则:
        NAV 起点 = 1000 (任意值, 只要自洽)
        每日先按上日 close 的 50/50 权重分配, T 日结束再按 T 日 close 重新 50/50 分配

    输出日序列: [{date, open, close}, ...], 和单指数 series 格式完全一致, 可直接喂给
    现有 simulate_filter / simulate_fullhold / v2_simulate 使用.

    其中:
        open[t]  = close[t-1] × (1 + 0.5 × H_overnight + 0.5 × C_overnight)
        close[t] = open[t]    × (1 + 0.5 × H_intraday  + 0.5 × C_intraday)
    """
    hs_by_date = {r["date"]: r for r in hs_rows}
    csi_by_date = {r["date"]: r for r in csi_rows}
    common_dates = sorted(set(hs_by_date.keys()) & set(csi_by_date.keys()))
    if not common_dates:
        raise ValueError("HS300 和 CSI1000 没有共同日期")

    out = []
    prev_close = 1000.0  # 起点 NAV
    prev_date = None

    for date in common_dates:
        h = hs_by_date[date]
        c = csi_by_date[date]
        if prev_date is None:
            # 第一天: 没有隔夜段, open 就是起点, 日内用当日的 close/open - 1
            open_nav = prev_close
            h_intra = h["close"] / h["open"] - 1 if h["open"] > 0 else 0
            c_intra = c["close"] / c["open"] - 1 if c["open"] > 0 else 0
            close_nav = open_nav * (1 + w_hs * h_intra + w_csi * c_intra)
        else:
            h_prev = hs_by_date[prev_date]
            c_prev = csi_by_date[prev_date]
            # 隔夜段: 上日 close → 今日 open
            h_over = h["open"] / h_prev["close"] - 1 if h_prev["close"] > 0 else 0
            c_over = c["open"] / c_prev["close"] - 1 if c_prev["close"] > 0 else 0
            # 日内段: 今日 open → 今日 close
            h_intra = h["close"] / h["open"] - 1 if h["open"] > 0 else 0
            c_intra = c["close"] / c["open"] - 1 if c["open"] > 0 else 0
            open_nav = prev_close * (1 + w_hs * h_over + w_csi * c_over)
            close_nav = open_nav * (1 + w_hs * h_intra + w_csi * c_intra)

        out.append({"date": date, "open": open_nav, "close": close_nav})
        prev_close = close_nav
        prev_date = date
    return out

logger = logging.getLogger("chart")

DEFAULT_START = "2020-01-02"
DEFAULT_END = "2026-04-14"

# 策略 ID → (颜色, 描述, 分组)
STRATEGY_META = {
    "fullhold":           ("#888888", "永远满仓 (基准)", "baseline"),
    "ma60_filter":        ("#e63946", "MA60 趋势过滤器 (价格驱动, 两段稳健)", "winner"),
    "v2_dynamic_cc":      ("#3b82f6", "v2 梯度仓位 (信号改善加仓/减弱减仓, 即 spec §5)", "v2_graded"),
    "v2_entry_only_cc":   ("#10b981", "v2 切入日 90% 其他 50% (full-sample 最佳但存疑)", "v2_graded"),
    "v2_entry_held_5d_cc":("#8b5cf6", "v2 切入后持 5 天 90% 其他 30%", "v2_graded"),
    "v2_bullish_only":    ("#f59e0b", "v2 只在强牛/强势满仓 (反面: 2020-22 惨败)", "v2_binary"),
    "v2_no_bear":         ("#ef4444", "v2 只躲熊市 (反面: DD 比 fullhold 还大)", "v2_binary"),
}


def run_all_strategies(start: str, end: str, underlying: str = "hs300"):
    conn = connect()
    if underlying == "hs300":
        series = load_hs300_ordered(conn)
        underlying_label = "HS300 (000300.SH)"
    elif underlying == "blended":
        hs = load_index_ordered(conn, "000300.SH")
        csi = load_index_ordered(conn, "000852.SH")
        series = build_blended_series(hs, csi, w_hs=0.5, w_csi=0.5)
        underlying_label = "50% HS300 + 50% CSI1000 (每日再平衡)"
    else:
        raise ValueError(f"unknown underlying: {underlying}")
    logger.info(f"底层: {underlying_label}")

    classify = load_v2_classify(conn)

    # 给 series 加 MA 信号
    build_ma_signals(series, 60)

    # 定位区间
    start_idx = None
    end_idx = None
    for i, r in enumerate(series):
        if start_idx is None and r["date"] >= start:
            start_idx = i
        if r["date"] <= end:
            end_idx = i
    if start_idx is None or end_idx is None:
        raise ValueError("样本区间为空")
    logger.info(f"回测 [{series[start_idx]['date']} ~ {series[end_idx]['date']}], "
                f"{end_idx - start_idx + 1} 天")

    results = {}  # strategy_id → [{date, nav, dd}]

    # fullhold
    sim = simulate_fullhold(series, start_idx, end_idx, engine="cc", execution="next_open")
    results["fullhold"] = sim["rows"]

    # MA60
    sim = simulate_filter(series, 60, start_idx, end_idx, engine="cc", execution="next_open")
    results["ma60_filter"] = sim["rows"]

    # v2 策略 (从 v2_regime_filter_backtest.py 引入)
    v2_ids = [
        "v2_dynamic_cc",
        "v2_entry_only_cc",
        "v2_entry_held_5d_cc",
        "v2_bullish_only",
        "v2_no_bear",
    ]
    for sid in v2_ids:
        fn, _desc = V2_STRATEGIES[sid]
        sim = v2_simulate(fn, series, classify, start_idx, end_idx, engine="cc", execution="next_open")
        results[sid] = sim["rows"]

    conn.close()
    return results, underlying_label


def build_html(results: dict, start: str, end: str, underlying_label: str = "HS300") -> str:
    """把 results 转成 HTML."""
    # 统一日期轴 (取第一个策略的 dates)
    first = next(iter(results.values()))
    dates = [r["trade_date"] for r in first]

    # 每个策略的 nav 和 dd 序列
    datasets_nav = []
    datasets_dd = []
    for sid, rows in results.items():
        color, label, _group = STRATEGY_META[sid]
        nav_series = [round(r["cumulative_nav"], 4) for r in rows]
        dd_series = [round(r["max_drawdown_to_date"] * 100, 2) for r in rows]

        # 计算关键指标
        total_ret = (rows[-1]["cumulative_nav"] - 1) * 100
        max_dd = min(r["max_drawdown_to_date"] for r in rows) * 100
        n_days = len(rows)
        years = n_days / 244
        ann_ret = ((1 + total_ret/100) ** (1/years) - 1) * 100 if years > 0 else 0
        ret_dd = abs(total_ret / max_dd) if max_dd != 0 else 0

        display = f"{sid}  |  {label}  |  总回报 {total_ret:+.1f}%  |  DD {max_dd:+.1f}%  |  ret/dd {ret_dd:.2f}"

        datasets_nav.append({
            "label": display,
            "data": nav_series,
            "borderColor": color,
            "backgroundColor": color + "22",
            "borderWidth": 2,
            "tension": 0.0,
            "pointRadius": 0,
            "pointHoverRadius": 4,
        })
        datasets_dd.append({
            "label": sid,
            "data": dd_series,
            "borderColor": color,
            "backgroundColor": color + "22",
            "borderWidth": 1.5,
            "tension": 0.0,
            "pointRadius": 0,
            "pointHoverRadius": 3,
        })

    # 崩盘窗口 annotations (画在 chart 背景)
    crash_windows = [
        ("2020 COVID", "2020-02-01", "2020-04-30"),
        ("2022 三杀", "2021-12-01", "2022-10-31"),
        ("2024-Q1", "2024-01-15", "2024-02-29"),
    ]

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>策略净值对比 — {underlying_label} — {start} ~ {end}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1"></script>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 1400px;
         margin: 20px auto; padding: 0 20px; color: #222; }}
  h1 {{ font-size: 1.4em; }}
  .meta {{ color: #666; font-size: 0.9em; margin-bottom: 1em; }}
  .chart-box {{ margin: 20px 0; padding: 15px; border: 1px solid #ddd;
               border-radius: 6px; background: #fafafa; }}
  .controls {{ margin: 10px 0; }}
  .controls button {{ padding: 6px 12px; margin-right: 8px; cursor: pointer;
                     border: 1px solid #aaa; background: #f0f0f0; border-radius: 3px; }}
  .controls button:hover {{ background: #e0e0e0; }}
  .summary {{ margin: 20px 0; }}
  .summary table {{ border-collapse: collapse; width: 100%; font-size: 0.92em; }}
  .summary th, .summary td {{ padding: 6px 10px; border: 1px solid #ddd; text-align: right; }}
  .summary th {{ background: #f5f5f5; text-align: left; }}
  .summary td:first-child {{ text-align: left; font-family: monospace; }}
  .legend-tip {{ color: #666; font-size: 0.85em; font-style: italic; }}
</style>
</head>
<body>
<h1>策略净值 + 回撤对比 ({underlying_label}, {start} ~ {end})</h1>
<div class="meta">
  <b>底层</b>: {underlying_label}<br>
  <b>口径</b>: close-to-close, T+1 open 执行 (真实可实盘). NAV 起点 = 1.0.<br>
  <b>点击图例中的策略名可以单独隐藏/显示曲线</b>. 鼠标悬停查看任意一天的具体数值.
</div>

<div class="summary">
  <table>
    <thead>
      <tr><th>策略</th><th>总回报</th><th>年化</th><th>最大回撤</th><th>ret/dd</th></tr>
    </thead>
    <tbody id="summary-body"></tbody>
  </table>
</div>

<div class="chart-box">
  <h3 style="margin-top:0">净值曲线 (NAV)</h3>
  <div class="controls">
    <button onclick="toggleYScale('nav')">切换对数/线性</button>
    <button onclick="toggleGroup('v2_binary', false)">隐藏 v2_binary 失败组</button>
    <button onclick="toggleGroup('v2_binary', true)">显示 v2_binary 失败组</button>
  </div>
  <div style="height: 480px;"><canvas id="navChart"></canvas></div>
</div>

<div class="chart-box">
  <h3 style="margin-top:0">回撤曲线 (从历史峰值算)</h3>
  <div style="height: 380px;"><canvas id="ddChart"></canvas></div>
  <p class="legend-tip">回撤 = 当前 NAV / 截至当前的最高 NAV - 1, 永远 ≤ 0. 越接近 0 越好.</p>
</div>

<script>
const dates = {dates_json};
const datasetsNav = {datasets_nav_json};
const datasetsDd = {datasets_dd_json};
const strategyGroups = {groups_json};
const crashWindows = {crash_json};

const crashAnnotations = {{}};
crashWindows.forEach((w, i) => {{
  crashAnnotations['crash_' + i] = {{
    type: 'box',
    xMin: w[1],
    xMax: w[2],
    backgroundColor: 'rgba(180, 180, 180, 0.12)',
    borderColor: 'rgba(180, 180, 180, 0.4)',
    borderWidth: 1,
    label: {{ display: true, content: w[0], position: 'start',
             color: '#666', font: {{ size: 10 }} }}
  }};
}});

const ctxNav = document.getElementById('navChart').getContext('2d');
const navChart = new Chart(ctxNav, {{
  type: 'line',
  data: {{ labels: dates, datasets: datasetsNav }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    scales: {{
      x: {{ type: 'time', time: {{ unit: 'year' }}, grid: {{ color: '#eee' }} }},
      y: {{ type: 'linear', title: {{ display: true, text: 'NAV (起点 = 1.0)' }},
            grid: {{ color: '#eee' }} }}
    }},
    plugins: {{
      legend: {{ position: 'top', labels: {{ font: {{ size: 11 }}, boxWidth: 20 }} }},
      tooltip: {{ callbacks: {{ label: (c) => c.dataset.label.split(' | ')[0] + ': ' + c.parsed.y.toFixed(3) }} }},
      annotation: {{ annotations: crashAnnotations }}
    }}
  }}
}});

const ctxDd = document.getElementById('ddChart').getContext('2d');
const ddChart = new Chart(ctxDd, {{
  type: 'line',
  data: {{ labels: dates, datasets: datasetsDd }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    scales: {{
      x: {{ type: 'time', time: {{ unit: 'year' }}, grid: {{ color: '#eee' }} }},
      y: {{ title: {{ display: true, text: 'Drawdown (%)' }}, max: 0, grid: {{ color: '#eee' }} }}
    }},
    plugins: {{
      legend: {{ position: 'top', labels: {{ font: {{ size: 11 }}, boxWidth: 20 }} }},
      tooltip: {{ callbacks: {{ label: (c) => c.dataset.label + ': ' + c.parsed.y.toFixed(2) + '%' }} }},
      annotation: {{ annotations: crashAnnotations }}
    }}
  }}
}});

function toggleYScale(which) {{
  const chart = which === 'nav' ? navChart : ddChart;
  const cur = chart.options.scales.y.type;
  chart.options.scales.y.type = cur === 'linear' ? 'logarithmic' : 'linear';
  chart.update();
}}

function toggleGroup(group, show) {{
  datasetsNav.forEach((ds, i) => {{
    const sid = ds.label.split(' | ')[0].trim();
    if (strategyGroups[sid] === group) navChart.setDatasetVisibility(i, show);
  }});
  datasetsDd.forEach((ds, i) => {{
    if (strategyGroups[ds.label] === group) ddChart.setDatasetVisibility(i, show);
  }});
  navChart.update();
  ddChart.update();
}}

// 填充汇总表
const summaryBody = document.getElementById('summary-body');
datasetsNav.forEach(ds => {{
  const parts = ds.label.split(' | ');
  const sid = parts[0].trim();
  const totalRet = parts[2].replace('总回报 ', '');
  const dd = parts[3].replace('DD ', '');
  const retDd = parts[4].replace('ret/dd ', '');
  const lastNav = ds.data[ds.data.length - 1];
  const annRet = ((Math.pow(lastNav, 1/(ds.data.length/244)) - 1) * 100).toFixed(2);
  const tr = document.createElement('tr');
  tr.innerHTML = '<td>' + sid + '</td>' +
                 '<td>' + totalRet + '</td>' +
                 '<td>' + annRet + '%</td>' +
                 '<td>' + dd + '</td>' +
                 '<td>' + retDd + '</td>';
  summaryBody.appendChild(tr);
}});
</script>
</body>
</html>
"""

    groups = {sid: meta[2] for sid, meta in STRATEGY_META.items()}

    return html.format(
        start=start,
        end=end,
        underlying_label=underlying_label,
        dates_json=json.dumps(dates),
        datasets_nav_json=json.dumps(datasets_nav, ensure_ascii=False),
        datasets_dd_json=json.dumps(datasets_dd, ensure_ascii=False),
        groups_json=json.dumps(groups, ensure_ascii=False),
        crash_json=json.dumps([list(c) for c in [
            ("2015 股灾", "2015-06-12", "2015-09-30"),
            ("2016 熔断", "2016-01-04", "2016-02-29"),
            ("2018 熊", "2018-01-25", "2018-12-28"),
            ("2020 COVID", "2020-02-01", "2020-04-30"),
            ("2022 三杀", "2021-12-01", "2022-10-31"),
            ("2024-Q1", "2024-01-15", "2024-02-29"),
        ]]),
    )


REFERENCES_DIR = "/home/rooot/.openclaw/workspace/skills/strategy/market-regime-classifier/references"


def run_and_write(start: str, end: str, underlying: str, output_path: str):
    results, underlying_label = run_all_strategies(start, end, underlying)
    html = build_html(results, start, end, underlying_label)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"  → {output_path}")
    logger.info(f"    策略数: {len(results)}, 数据点: {len(next(iter(results.values())))} 天")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument(
        "--underlying",
        default="hs300",
        choices=["hs300", "blended", "both"],
        help="底层资产: hs300 (默认), blended (50/50 HS300+CSI1000), both (两个都生成)",
    )
    parser.add_argument("--output", default=None, help="自定义输出路径 (仅当 underlying 非 both)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.underlying == "both":
        logger.info("=== 生成 HS300 版本 ===")
        run_and_write(
            args.start, args.end, "hs300",
            os.path.join(REFERENCES_DIR, "strategy_comparison.html"),
        )
        logger.info("=== 生成 50/50 HS300+CSI1000 版本 ===")
        run_and_write(
            args.start, args.end, "blended",
            os.path.join(REFERENCES_DIR, "strategy_comparison_blended.html"),
        )
    else:
        default_name = {
            "hs300": "strategy_comparison.html",
            "blended": "strategy_comparison_blended.html",
        }[args.underlying]
        output = args.output or os.path.join(REFERENCES_DIR, default_name)
        run_and_write(args.start, args.end, args.underlying, output)

    logger.info("完成. 用浏览器打开查看 (需要联网加载 Chart.js CDN).")


if __name__ == "__main__":
    main()
