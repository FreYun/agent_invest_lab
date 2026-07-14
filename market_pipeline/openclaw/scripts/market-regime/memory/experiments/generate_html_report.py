"""把 replay CSV + SQLite HS300 生成单文件 HTML 可视化报告。

布局 (自上而下, 共享 X 轴 + 底部 dataZoom):
    1. HS300 收盘价曲线 + regime 彩色背景带 + 逃生门事件标记
    2. 总分柱状图 (正红负绿)
    3. 六维打分线图 (6 条线可切换)

使用 ECharts CDN, 单文件 HTML 离线可看 (数据内嵌), 支持鼠标悬停/zoom/拖拽。

用法:
    python3 generate_html_report.py                             # 默认路径
    python3 generate_html_report.py --csv /tmp/x.csv --out /tmp/y.html
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect  # noqa: E402

logger = logging.getLogger("report")


# --------------------------------------------------------------------------- #
# 颜色方案 (A 股红涨绿跌)
# --------------------------------------------------------------------------- #


REGIME_COLORS = {
    "强牛":   "#C0392B",  # 深红 — 最乐观
    "强势震荡": "#E67E22",  # 橙
    "中性震荡": "#95A5A6",  # 灰
    "弱势震荡": "#3498DB",  # 蓝
    "熊":     "#27AE60",  # 深绿 — 最悲观
}

# 透明度 (hex) 用于背景带
REGIME_BG_ALPHA = "30"


# --------------------------------------------------------------------------- #
# 数据准备
# --------------------------------------------------------------------------- #


def load_replay_rows(csv_path: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_hs300(conn) -> dict:
    """返回 {iso_date: close}"""
    result = {}
    for d, c in conn.execute(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date"
    ):
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        result[iso] = float(c)
    return result


def compact_regime_segments(rows: list[dict]) -> list[dict]:
    """把逐日 regime 压缩成连续段 [{start, end, regime}, ...]"""
    segments: list[dict] = []
    if not rows:
        return segments
    cur = {"start": rows[0]["date"], "end": rows[0]["date"], "regime": rows[0]["regime_name"]}
    for r in rows[1:]:
        if r["regime_name"] == cur["regime"]:
            cur["end"] = r["date"]
        else:
            segments.append(cur)
            cur = {"start": r["date"], "end": r["date"], "regime": r["regime_name"]}
    segments.append(cur)
    return segments


def collect_emergencies(rows: list[dict]) -> list[dict]:
    """逃生门事件点 [{date, direction, reason}, ...]"""
    return [
        {"date": r["date"], "direction": r["emergency_direction"], "reason": r["emergency_reason"]}
        for r in rows
        if r["emergency_switch"] == "1"
    ]


def build_stats(rows: list[dict]) -> dict:
    """统计摘要, 供 HTML header 显示"""
    from collections import Counter
    regime_counts = Counter(r["regime_name"] for r in rows)
    n_total = len(rows)
    n_switched = sum(1 for r in rows if r["switched"] == "1")
    n_emergency_down = sum(1 for r in rows if r["emergency_switch"] == "1" and r["emergency_direction"] == "down")
    n_emergency_up = sum(1 for r in rows if r["emergency_switch"] == "1" and r["emergency_direction"] == "up")
    return {
        "n_total": n_total,
        "date_start": rows[0]["date"],
        "date_end": rows[-1]["date"],
        "regime_counts": dict(regime_counts),
        "n_switched": n_switched,
        "n_emergency_down": n_emergency_down,
        "n_emergency_up": n_emergency_up,
    }


# --------------------------------------------------------------------------- #
# HTML 生成
# --------------------------------------------------------------------------- #


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Market Regime Classifier — 3 年回放</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f5f7;
    color: #1d1d1f;
  }}
  .header {{
    background: white;
    padding: 24px 32px;
    border-bottom: 1px solid #e5e5e7;
  }}
  .header h1 {{
    margin: 0 0 8px 0;
    font-size: 22px;
    font-weight: 600;
  }}
  .header .subtitle {{
    color: #86868b;
    font-size: 13px;
  }}
  .stats {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin-top: 16px;
  }}
  .stat-card {{
    background: #f5f5f7;
    border-radius: 8px;
    padding: 10px 16px;
    min-width: 110px;
  }}
  .stat-card .label {{
    font-size: 11px;
    color: #86868b;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .stat-card .value {{
    font-size: 20px;
    font-weight: 600;
    margin-top: 2px;
  }}
  .stat-card .detail {{
    font-size: 11px;
    color: #86868b;
    margin-top: 2px;
  }}
  .regime-legend {{
    display: flex;
    gap: 12px;
    margin-top: 12px;
    flex-wrap: wrap;
  }}
  .regime-legend .item {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
  }}
  .regime-legend .dot {{
    width: 12px;
    height: 12px;
    border-radius: 2px;
  }}
  #chart {{
    width: 100%;
    height: calc(100vh - 260px);
    min-height: 720px;
    background: white;
  }}
  .footer {{
    padding: 12px 32px;
    color: #86868b;
    font-size: 11px;
    text-align: center;
  }}
</style>
</head>
<body>

<div class="header">
  <h1>Market Regime Classifier · 3 年历史回放</h1>
  <div class="subtitle">基于 Tushare 全市场数据 + market-regime-classifier 六维打分规则 · {date_start} ~ {date_end}</div>

  <div class="stats">
    <div class="stat-card">
      <div class="label">交易日数</div>
      <div class="value">{n_total}</div>
    </div>
    <div class="stat-card">
      <div class="label">Regime 切换</div>
      <div class="value">{n_switched}</div>
      <div class="detail">平均每 {avg_days_per_switch:.1f} 天</div>
    </div>
    <div class="stat-card">
      <div class="label">下行逃生门</div>
      <div class="value" style="color:#27AE60">{n_emergency_down}</div>
    </div>
    <div class="stat-card">
      <div class="label">上行逃生门</div>
      <div class="value" style="color:#C0392B">{n_emergency_up}</div>
    </div>
    {regime_stat_cards}
  </div>

  <div class="regime-legend">
    {legend_items}
  </div>
</div>

<div id="chart"></div>

<div class="footer">
  数据源: /home/rooot/agent_invest_lab/data/market.db · 算法: market-regime-classifier/scripts (spec §4-§6)
</div>

<script>
const DATA = {data_json};

const REGIME_COLORS = {regime_colors_js};

const chart = echarts.init(document.getElementById('chart'));

// --- Regime 背景带 (转成 markArea) ---
const regimeAreas = DATA.regime_segments.map(s => [
  {{
    xAxis: s.start,
    itemStyle: {{ color: REGIME_COLORS[s.regime] + '{regime_bg_alpha}' }},
    name: s.regime
  }},
  {{ xAxis: s.end }}
]);

// --- 逃生门标记点 ---
const emergencyMarkers = DATA.emergencies.map(e => ({{
  name: e.direction === 'down' ? '🚨' : '🚀',
  coord: [e.date, DATA.hs300[DATA.dates.indexOf(e.date)]],
  value: e.reason,
  itemStyle: {{
    color: e.direction === 'down' ? '#27AE60' : '#C0392B'
  }},
  symbolSize: 14,
}}));

const option = {{
  animation: false,
  grid: [
    {{ left: '3%', right: '3%', top: '4%', height: '44%' }},   // grid 0: HS300
    {{ left: '3%', right: '3%', top: '52%', height: '16%' }},  // grid 1: 总分柱
    {{ left: '3%', right: '3%', top: '72%', height: '20%' }},  // grid 2: 六维线
  ],
  xAxis: [
    {{ type: 'category', data: DATA.dates, gridIndex: 0,
       axisLine: {{ onZero: false }}, axisLabel: {{ show: false }} }},
    {{ type: 'category', data: DATA.dates, gridIndex: 1,
       axisLine: {{ onZero: false }}, axisLabel: {{ show: false }} }},
    {{ type: 'category', data: DATA.dates, gridIndex: 2,
       axisLine: {{ onZero: false }}, axisLabel: {{ fontSize: 10 }} }},
  ],
  yAxis: [
    {{ gridIndex: 0, name: 'HS300', scale: true, splitNumber: 5 }},
    {{ gridIndex: 1, name: '总分', min: -12, max: 12, interval: 6 }},
    {{ gridIndex: 2, name: '六维', min: -2, max: 2, interval: 1 }},
  ],
  tooltip: {{
    trigger: 'axis',
    axisPointer: {{ type: 'cross', link: [{{ xAxisIndex: 'all' }}] }},
    formatter: function(params) {{
      const date = params[0].axisValue;
      const idx = DATA.dates.indexOf(date);
      if (idx < 0) return '';
      const r = DATA.rows[idx];
      let html = `<b>${{date}}</b> · <span style="color:${{REGIME_COLORS[r.regime_name]}}">${{r.regime_name}}</span>`;
      html += `<br/>HS300 收盘: <b>${{DATA.hs300[idx].toFixed(0)}}</b>`;
      html += `<br/>总分: <b>${{r.total_score > 0 ? '+' + r.total_score : r.total_score}}</b>`;
      html += `<br/>六维: MA ${{r.ma_position}}, 涨跌 ${{r.advance_decline}}, 情绪Δ ${{r.sentiment_delta}}, 评分 ${{r.sentiment_index}}, 连板 ${{r.streak_height}}, 量 ${{r.volume_trend}}`;
      if (r.switched == '1') {{
        html += `<br/><span style="color:#C0392B">切换 ${{r.last_regime_name}}→${{r.regime_name}}</span>`;
      }}
      if (r.emergency_switch == '1') {{
        html += `<br/><span style="color:${{r.emergency_direction === 'down' ? '#27AE60' : '#C0392B'}}">🚨 ${{r.emergency_direction}} · ${{r.emergency_reason}}</span>`;
      }}
      return html;
    }}
  }},
  axisPointer: {{
    link: [{{ xAxisIndex: 'all' }}],
    label: {{ backgroundColor: '#777' }}
  }},
  legend: {{
    data: ['HS300', '总分', 'MA', '涨跌家数', '情绪Δ', '情绪评分', '连板', '成交量'],
    top: 8,
    right: 16,
    textStyle: {{ fontSize: 11 }}
  }},
  dataZoom: [
    {{
      type: 'inside',
      xAxisIndex: [0, 1, 2],
      start: 0,
      end: 100
    }},
    {{
      type: 'slider',
      xAxisIndex: [0, 1, 2],
      bottom: 10,
      height: 20,
      start: 0,
      end: 100
    }}
  ],
  series: [
    {{
      name: 'HS300',
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: DATA.hs300,
      symbol: 'none',
      lineStyle: {{ width: 1.5, color: '#1d1d1f' }},
      markArea: {{ silent: true, data: regimeAreas }},
      markPoint: {{
        symbol: 'pin',
        symbolSize: 20,
        data: emergencyMarkers,
        label: {{ show: false }}
      }},
    }},
    {{
      name: '总分',
      type: 'bar',
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: DATA.total_scores.map(s => ({{
        value: s,
        itemStyle: {{
          color: s > 0 ? '#C0392B' : (s < 0 ? '#27AE60' : '#95A5A6')
        }}
      }})),
    }},
    {{
      name: 'MA',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: DATA.ma_position,
      symbol: 'none',
      smooth: true,
      lineStyle: {{ width: 1, color: '#8E44AD' }},
    }},
    {{
      name: '涨跌家数',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: DATA.advance_decline,
      symbol: 'none',
      smooth: true,
      lineStyle: {{ width: 1, color: '#2980B9' }},
    }},
    {{
      name: '情绪Δ',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: DATA.sentiment_delta,
      symbol: 'none',
      smooth: true,
      lineStyle: {{ width: 1, color: '#D35400' }},
    }},
    {{
      name: '情绪评分',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: DATA.sentiment_index,
      symbol: 'none',
      smooth: true,
      lineStyle: {{ width: 1, color: '#16A085' }},
    }},
    {{
      name: '连板',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: DATA.streak_height,
      symbol: 'none',
      smooth: true,
      lineStyle: {{ width: 1, color: '#C0392B' }},
    }},
    {{
      name: '成交量',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: DATA.volume_trend,
      symbol: 'none',
      smooth: true,
      lineStyle: {{ width: 1, color: '#F39C12' }},
    }},
  ]
}};

chart.setOption(option);

window.addEventListener('resize', () => chart.resize());
</script>
</body>
</html>
"""


def _safe_int(v, default=0):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def generate_html(replay_csv: str, output_path: str) -> None:
    """生成单文件 HTML 报告。"""
    conn = connect()
    rows = load_replay_rows(replay_csv)
    hs300 = load_hs300(conn)
    conn.close()

    # 按 dates 顺序组装
    dates = [r["date"] for r in rows]
    hs300_series = [hs300.get(d, 0) for d in dates]

    total_scores = [_safe_int(r["total_score"]) for r in rows]
    ma_pos = [_safe_int(r["ma_position"]) for r in rows]
    adv = [_safe_int(r["advance_decline"]) for r in rows]
    sent_d = [_safe_int(r["sentiment_delta"]) for r in rows]
    sent_i = [_safe_int(r["sentiment_index"]) for r in rows]
    streak = [_safe_int(r["streak_height"]) for r in rows]
    vol = [_safe_int(r["volume_trend"]) for r in rows]

    # 精简 rows 给前端 tooltip 用 (只保留需要的字段)
    REGIME_NAMES = {
        "STRONG_BULL": "强牛", "STRONG_RANGE": "强势震荡", "NEUTRAL_RANGE": "中性震荡",
        "WEAK_RANGE": "弱势震荡", "BEAR": "熊",
    }
    slim_rows = [
        {
            "total_score": int(r["total_score"]),
            "ma_position": int(r["ma_position"]),
            "advance_decline": int(r["advance_decline"]),
            "sentiment_delta": int(r["sentiment_delta"]),
            "sentiment_index": int(r["sentiment_index"]),
            "streak_height": int(r["streak_height"]),
            "volume_trend": int(r["volume_trend"]),
            "regime_name": r["regime_name"],
            "last_regime_name": REGIME_NAMES.get(r["last_regime_code"], r["last_regime_code"]),
            "switched": r["switched"],
            "emergency_switch": r["emergency_switch"],
            "emergency_direction": r["emergency_direction"],
            "emergency_reason": r["emergency_reason"],
        }
        for r in rows
    ]

    data = {
        "dates": dates,
        "hs300": hs300_series,
        "total_scores": total_scores,
        "ma_position": ma_pos,
        "advance_decline": adv,
        "sentiment_delta": sent_d,
        "sentiment_index": sent_i,
        "streak_height": streak,
        "volume_trend": vol,
        "rows": slim_rows,
        "regime_segments": compact_regime_segments(rows),
        "emergencies": collect_emergencies(rows),
    }

    stats = build_stats(rows)

    # HTML header 的 regime 分布卡片
    regime_order_display = ["强牛", "强势震荡", "中性震荡", "弱势震荡", "熊"]
    regime_cards_parts = []
    for name in regime_order_display:
        n = stats["regime_counts"].get(name, 0)
        pct = n / stats["n_total"] * 100
        color = REGIME_COLORS[name]
        regime_cards_parts.append(
            f'<div class="stat-card">'
            f'<div class="label" style="color:{color}">{name}</div>'
            f'<div class="value">{n}</div>'
            f'<div class="detail">{pct:.1f}%</div>'
            f'</div>'
        )
    regime_stat_cards = "\n    ".join(regime_cards_parts)

    legend_parts = [
        f'<div class="item"><span class="dot" style="background:{REGIME_COLORS[n]}"></span>{n}</div>'
        for n in regime_order_display
    ]
    legend_items = "\n    ".join(legend_parts)

    avg_days_per_switch = stats["n_total"] / stats["n_switched"] if stats["n_switched"] else 0

    html = HTML_TEMPLATE.format(
        data_json=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        regime_colors_js=json.dumps(REGIME_COLORS, ensure_ascii=False),
        regime_bg_alpha=REGIME_BG_ALPHA,
        date_start=stats["date_start"],
        date_end=stats["date_end"],
        n_total=stats["n_total"],
        n_switched=stats["n_switched"],
        n_emergency_down=stats["n_emergency_down"],
        n_emergency_up=stats["n_emergency_up"],
        avg_days_per_switch=avg_days_per_switch,
        regime_stat_cards=regime_stat_cards,
        legend_items=legend_items,
    )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(output_path) / 1024
    logger.info(f"HTML 已生成: {output_path} ({size_kb:.0f} KB)")


def main():
    parser = argparse.ArgumentParser(description="把 replay CSV 生成可视化 HTML")
    parser.add_argument("--csv", default="/tmp/regime_replay_3y.csv")
    parser.add_argument("--out", default="/tmp/regime_report_3y.html")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not os.path.exists(args.csv):
        logger.error(f"CSV 不存在: {args.csv}, 请先跑 replay.py")
        return 1

    generate_html(args.csv, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
