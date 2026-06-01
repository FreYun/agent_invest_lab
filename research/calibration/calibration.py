"""Weekly calibration runner — Brier + 活性 + 校准曲线 for catalyst-aware bots.

依赖 Belief Schema v1 (写在 `memory/portfolio/fund/市场环境判断.md` frontmatter):
  belief:
    schema: v1
    target_index: hs300|zz1000|csi500|chinext|...
    horizons:
      t+1: { p_up, prior_p_up, delta }
      t+5: { ... }
      t+20: { ... }
    evidence: [...]
    activity_self_check: { abs_delta_t1, evidence_count, ok }

跑法:
    python calibration.py                # 扫真实数据
    python calibration.py --mock         # 落 5 份合成 MD 到临时目录跑通 pipeline
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

# ───────────────────────── paths ─────────────────────────
BASE = Path("/home/rooot/agent_invest_lab/research/calibration")
RESULTS = BASE / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

LAB_ROOT = Path("/home/rooot/agent_invest_lab")
BOTS_DIR = LAB_ROOT / "bots"
RUNS_DIR = LAB_ROOT / "world/runtime/runs"
SR_DATA = LAB_ROOT / "research/sr_factor/data"

# target_index → CSV (复用 sr_factor 数据; 缺的就跳过)
INDEX_CSV = {
    "hs300":   SR_DATA / "510300.SH.csv",
    "csi500":  SR_DATA / "512100.SH.csv",   # 实际上是中证1000的 ETF, 占位
    "zz1000":  SR_DATA / "512100.SH.csv",
    "chinext": SR_DATA / "588800.SH.csv",   # 占位; 真正用时换
}

HORIZONS = {"t+1": 1, "t+5": 5, "t+20": 20}
BIN_EDGES = np.linspace(0, 1, 11)  # 10 bin


# ───────────────────────── data loading ─────────────────────────
def load_index_close(target: str) -> pd.Series | None:
    csv = INDEX_CSV.get(target)
    if csv is None or not csv.exists():
        return None
    df = pd.read_csv(csv)
    date_col = "trade_date" if "trade_date" in df.columns else ("date" if "date" in df.columns else None)
    if date_col is None or "close" not in df.columns:
        return None
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).set_index(date_col)
    return df["close"].astype(float)


def parse_belief_md(path: Path) -> dict | None:
    """读 frontmatter, 提取 belief block. 没 belief 返回 None."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    belief = fm.get("belief")
    if not isinstance(belief, dict):
        return None
    trade_date = fm.get("trade_date")
    if trade_date is None:
        return None
    belief["_trade_date"] = pd.to_datetime(trade_date)
    belief["_source"] = str(path)
    return belief


def scan_beliefs(roots: list[Path]) -> pd.DataFrame:
    """扫所有 bot 的 市场环境判断.md, 抽 belief 字段成长表.

    返回列: bot_id, trade_date, target_index, horizon, p_up, prior_p_up, delta,
             evidence_count, abs_delta_t1, self_ok, src
    """
    rows = []
    for root in roots:
        for md in root.rglob("市场环境判断.md"):
            belief = parse_belief_md(md)
            if belief is None:
                continue
            # bot_id 从路径里抽: .../bots/bot101/memory/... → bot101
            parts = md.parts
            bot_id = None
            for i, p in enumerate(parts):
                if p == "bots" and i + 1 < len(parts):
                    bot_id = parts[i + 1]
                    break
            if bot_id is None:
                # 也支持 runs/dash-*/<date>/<botId>/memory/...
                for i, p in enumerate(parts):
                    if p.startswith("dash-") and i + 2 < len(parts):
                        bot_id = parts[i + 2]
                        break
            if bot_id is None:
                bot_id = "unknown"

            target = belief.get("target_index", "hs300")
            horizons = belief.get("horizons", {}) or {}
            ev = belief.get("evidence", []) or []
            asc = belief.get("activity_self_check", {}) or {}
            base = {
                "bot_id": bot_id,
                "trade_date": belief["_trade_date"],
                "target_index": target,
                "evidence_count": len(ev),
                "abs_delta_t1": asc.get("abs_delta_t1"),
                "self_ok": asc.get("ok"),
                "src": belief["_source"],
            }
            for hname in HORIZONS:
                h = horizons.get(hname, {}) or {}
                if "p_up" not in h:
                    continue
                rows.append({
                    **base,
                    "horizon": hname,
                    "p_up": float(h["p_up"]),
                    "prior_p_up": (None if h.get("prior_p_up") is None else float(h["prior_p_up"])),
                    "delta": (None if h.get("delta") is None else float(h["delta"])),
                })
    return pd.DataFrame(rows)


# ───────────────────────── metrics ─────────────────────────
def compute_actuals(beliefs: pd.DataFrame, closes: dict[str, pd.Series]) -> pd.DataFrame:
    """对每条 belief 算 actual_up = 1 if close[t+h] > close[t]."""
    out = beliefs.copy()
    actual = []
    for _, r in out.iterrows():
        c = closes.get(r["target_index"])
        if c is None:
            actual.append(np.nan)
            continue
        h = HORIZONS[r["horizon"]]
        t0 = r["trade_date"]
        # 找 t0 当日或之前最近的交易日
        idx_t0 = c.index.searchsorted(t0, side="right") - 1
        idx_th = idx_t0 + h
        if idx_t0 < 0 or idx_th >= len(c):
            actual.append(np.nan)
            continue
        actual.append(1.0 if c.iloc[idx_th] > c.iloc[idx_t0] else 0.0)
    out["actual_up"] = actual
    return out


def brier_summary(beliefs_with_actual: pd.DataFrame, closes: dict[str, pd.Series]) -> pd.DataFrame:
    """对 (bot,target,horizon) 算 Brier + baseline Brier (用过去5y该指数 base rate)."""
    rows = []
    for (bot, tgt, h), grp in beliefs_with_actual.groupby(["bot_id", "target_index", "horizon"]):
        valid = grp.dropna(subset=["actual_up"])
        if valid.empty:
            continue
        brier = float(((valid["p_up"] - valid["actual_up"]) ** 2).mean())
        # baseline: 该指数过去 5y rolling base rate
        c = closes.get(tgt)
        if c is not None and len(c) > HORIZONS[h] + 10:
            ret_pos = (c.shift(-HORIZONS[h]) > c).astype(float)
            base_rate = float(ret_pos.iloc[-min(len(ret_pos) - HORIZONS[h], 252 * 5):].mean())
        else:
            base_rate = 0.5
        baseline_brier = float(((base_rate - valid["actual_up"]) ** 2).mean())
        rows.append({
            "bot_id": bot,
            "target_index": tgt,
            "horizon": h,
            "n_obs": int(len(valid)),
            "brier": brier,
            "baseline_brier": baseline_brier,
            "baseline_p_up": base_rate,
            "improvement": baseline_brier - brier,  # >0 好
        })
    return pd.DataFrame(rows).sort_values(["bot_id", "target_index", "horizon"])


def activity_summary(beliefs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for bot, grp in beliefs.groupby("bot_id"):
        t1 = grp[grp["horizon"] == "t+1"].sort_values("trade_date")
        if t1.empty:
            continue
        abs_d = t1["delta"].abs().dropna()
        if abs_d.empty:
            status = "no_delta"
            med = p10 = np.nan
        else:
            # 滚动 21 日 median
            med = float(abs_d.rolling(21, min_periods=5).median().iloc[-1])
            p10 = float(np.percentile(abs_d, 10))
            if med >= 0.05:
                status = "alive"
            elif med >= 0.03:
                status = "warn"
            else:
                status = "dead"
        rows.append({
            "bot_id": bot,
            "n_days": int(len(t1)),
            "abs_delta_t1_median_21d": med,
            "abs_delta_t1_p10": p10,
            "status": status,
        })
    return pd.DataFrame(rows).sort_values("bot_id")


def calibration_bins(beliefs_with_actual: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (bot, h), grp in beliefs_with_actual.groupby(["bot_id", "horizon"]):
        valid = grp.dropna(subset=["actual_up"])
        if valid.empty:
            continue
        bins = pd.cut(valid["p_up"], BIN_EDGES, include_lowest=True)
        agg = valid.groupby(bins, observed=True).agg(
            n=("p_up", "size"),
            mean_p=("p_up", "mean"),
            mean_actual=("actual_up", "mean"),
        ).reset_index()
        for _, r in agg.iterrows():
            iv = r["p_up"]
            rows.append({
                "bot_id": bot,
                "horizon": h,
                "bin_lo": float(iv.left),
                "bin_hi": float(iv.right),
                "n": int(r["n"]),
                "mean_p": float(r["mean_p"]),
                "mean_actual": float(r["mean_actual"]),
            })
    return pd.DataFrame(rows)


# ───────────────────────── mock data ─────────────────────────
def write_mock_beliefs(tmpdir: Path, closes: dict[str, pd.Series]) -> Path:
    """造 5 份 (bot101, 5 个不同日期) 的合成 belief MD."""
    c = closes.get("hs300")
    if c is None:
        raise RuntimeError("hs300 close 数据缺失, mock 跑不了")
    # 选 5 个近 100 日内的交易日, 保证 t+20 也有数据
    avail = c.index[-200:-30]
    sample_dates = avail[::40][:5]
    rng = np.random.default_rng(42)
    bot_dir = tmpdir / "bots" / "bot101" / "memory" / "portfolio" / "fund"
    bot_dir.mkdir(parents=True, exist_ok=True)
    prior = None
    for d in sample_dates:
        p_up = float(np.clip(0.5 + rng.normal(0, 0.15), 0.05, 0.95))
        delta = None if prior is None else round(p_up - prior, 4)
        fm = {
            "step": "market_context",
            "trade_date": d.strftime("%Y-%m-%d"),
            "belief": {
                "schema": "v1",
                "target_index": "hs300",
                "horizons": {
                    "t+1":  {"p_up": p_up,                "prior_p_up": prior, "delta": delta},
                    "t+5":  {"p_up": float(np.clip(p_up + rng.normal(0, 0.05), 0.05, 0.95)),
                             "prior_p_up": None, "delta": None},
                    "t+20": {"p_up": float(np.clip(p_up + rng.normal(0, 0.08), 0.05, 0.95)),
                             "prior_p_up": None, "delta": None},
                },
                "evidence": [
                    {"type": "research", "ref": f"{d.strftime('%Y%m%d')}_mock1",
                     "summary": "mock 政策", "polarity": "+"},
                    {"type": "technical", "ref": "MA60_cross_up",
                     "summary": "mock 技术", "polarity": "+"},
                ],
                "activity_self_check": {
                    "abs_delta_t1": abs(delta) if delta is not None else 0.0,
                    "evidence_count": 2,
                    "ok": (delta is not None and abs(delta) >= 0.05),
                },
            },
        }
        md = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n# mock\n"
        (bot_dir / "市场环境判断.md").write_text(md, encoding="utf-8")
        # 每天都覆盖上一份 — 但因为我们只有一个文件位置, 用 trade_date 命名分文件
        # 简化: 改成 mock 写到 bot{N} 目录里 (每天一个 bot id)
        prior = p_up

    # 上面只会留最后一天; 换策略: 5 个 bot 各一天 → bot101 a~e
    bot_dirs = []
    prior = None
    for i, d in enumerate(sample_dates):
        bid = f"bot10{1 + i}"
        bd = tmpdir / "bots" / bid / "memory" / "portfolio" / "fund"
        bd.mkdir(parents=True, exist_ok=True)
        p_up = float(np.clip(0.5 + rng.normal(0, 0.15), 0.05, 0.95))
        delta = None if prior is None else round(p_up - prior, 4)
        fm = {
            "step": "market_context",
            "trade_date": d.strftime("%Y-%m-%d"),
            "belief": {
                "schema": "v1",
                "target_index": "hs300",
                "horizons": {
                    "t+1":  {"p_up": p_up,                "prior_p_up": prior, "delta": delta},
                    "t+5":  {"p_up": float(np.clip(p_up + rng.normal(0, 0.05), 0.05, 0.95)),
                             "prior_p_up": None, "delta": None},
                    "t+20": {"p_up": float(np.clip(p_up + rng.normal(0, 0.08), 0.05, 0.95)),
                             "prior_p_up": None, "delta": None},
                },
                "evidence": [
                    {"type": "research", "ref": f"{d.strftime('%Y%m%d')}_mock1",
                     "summary": "mock 政策", "polarity": "+"},
                    {"type": "technical", "ref": "MA60_cross_up",
                     "summary": "mock 技术", "polarity": "+"},
                ],
                "activity_self_check": {
                    "abs_delta_t1": abs(delta) if delta is not None else 0.0,
                    "evidence_count": 2,
                    "ok": (delta is not None and abs(delta) >= 0.05),
                },
            },
        }
        md = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n# mock\n"
        (bd / "市场环境判断.md").write_text(md, encoding="utf-8")
        bot_dirs.append(bd)
        prior = p_up
    return tmpdir / "bots"


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="用合成 belief 跑一次 pipeline")
    args = ap.parse_args()

    # 加载所有 target_index 收盘
    closes = {}
    for k, p in INDEX_CSV.items():
        c = load_index_close(k)
        if c is not None:
            closes[k] = c
    print(f"[load] {len(closes)} indices loaded: {list(closes.keys())}")

    if args.mock:
        tmp = Path(tempfile.mkdtemp(prefix="cal_mock_"))
        roots = [write_mock_beliefs(tmp, closes)]
        print(f"[mock] wrote synthetic beliefs to {tmp}")
    else:
        roots = [BOTS_DIR, RUNS_DIR]

    beliefs = scan_beliefs(roots)
    print(f"[scan] {len(beliefs)} belief rows across {beliefs['bot_id'].nunique() if not beliefs.empty else 0} bots")
    if beliefs.empty:
        print("[warn] 没有 belief 数据 (frontmatter 里没有 belief: 字段). 用 --mock 跑测试.")
        return

    beliefs = compute_actuals(beliefs, closes)
    bs = brier_summary(beliefs, closes)
    act = activity_summary(beliefs)
    bins = calibration_bins(beliefs)

    bs_path = RESULTS / "brier_summary.csv"
    act_path = RESULTS / "activity_summary.csv"
    bin_path = RESULTS / "calibration_bins.csv"
    bs.to_csv(bs_path, index=False)
    act.to_csv(act_path, index=False)
    bins.to_csv(bin_path, index=False)

    print("\n════════════════ Brier Summary ════════════════")
    if bs.empty:
        print("  (no valid (bot,index,horizon) groups — actuals 全 NaN, 可能日期超出收盘数据范围)")
    else:
        for _, r in bs.iterrows():
            mark = "✓" if r["improvement"] > 0 else "✗"
            print(f"  {r['bot_id']:<10} {r['target_index']:<8} {r['horizon']:<4} "
                  f"n={r['n_obs']:<3} brier={r['brier']:.4f}  base={r['baseline_brier']:.4f}  "
                  f"imp={r['improvement']:+.4f} {mark}")

    print("\n════════════════ Activity ════════════════")
    for _, r in act.iterrows():
        print(f"  {r['bot_id']:<10} n_days={r['n_days']:<3} med21|Δ|={r['abs_delta_t1_median_21d']!r}  "
              f"p10={r['abs_delta_t1_p10']!r}  status={r['status']}")

    print("\n════════════════ Calibration Bins (head) ════════════════")
    print(bins.head(20).to_string(index=False) if not bins.empty else "  (空)")

    print(f"\n[out] {bs_path}\n[out] {act_path}\n[out] {bin_path}")


if __name__ == "__main__":
    main()
