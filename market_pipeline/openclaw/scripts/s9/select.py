"""S9 — 大市值 regime 自适应选股 (编排 + CLI).

收盘后跑: python3 scripts/s9/select.py --date 2026-05-21
流程: 算 regime (沪深300 vs MA150) → 建池 (≥100亿) → 因子打分 → 写库.

研究底稿: research/stock_select_factor/summary.md (已升级"建议 shadow")
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_SCRIPTS = os.path.dirname(SCRIPT_DIR)
for path in (SCRIPT_DIR, ROOT_SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

from analysis import IndexDataMissing, compute_regime
from config import full_config
from db_writer import write_select_run
from filter import filter_candidates


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _annotate(c: dict, regime: dict) -> None:
    bits = [c.get("sleeve_label") or c.get("sleeve")]
    if c.get("factor") is not None:
        if c.get("factor_name") == "high_prox":
            bits.append(f"距高点 {c['factor']*100:.1f}%")
        elif c.get("factor_name") == "idiovol_low":
            bits.append(f"特质波动 {-c['factor']*100:.2f}%")
    if c.get("total_mv_yi") is not None:
        bits.append(f"总市值 {c['total_mv_yi']:.0f}亿")
    if c.get("pct_chg") is not None:
        bits.append(f"今日{c['pct_chg']:+.1f}%")
    c["reason"] = " · ".join(b for b in bits if b)


def run_select(t_date_iso: str, config=None, persist=True, verbose=False) -> dict:
    setup_logging(verbose)
    cfg = full_config(config)
    logging.info("===== S9 select t=%s =====", t_date_iso)

    regime = compute_regime(t_date_iso, cfg)
    logging.info("regime: %s (沪深300/MA%d 偏离 %+.2f%%)",
                 regime["regime"], regime["ma_window"], regime["signal"] * 100)

    result = filter_candidates(t_date_iso, regime, cfg)
    kept = result["candidates"]

    max_board = int(cfg.get("max_board_candidates") or 0)
    board = kept[:max_board] if max_board > 0 else kept
    for c in board:
        _annotate(c, regime)

    payload = {
        "date": t_date_iso,
        "strategy": "S9",
        "regime": regime,
        "candidates": board,
        "stats": result["stats"],
        "config": cfg,
    }
    if persist:
        write_select_run(payload)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="T date YYYY-MM-DD")
    ap.add_argument("--no-write-db", action="store_true")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--ma-window", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    override = {}
    if args.top_k is not None:
        override["top_k"] = args.top_k
        override["max_board_candidates"] = args.top_k
    if args.ma_window is not None:
        override["regime"] = {"ma_window": args.ma_window}

    try:
        payload = run_select(args.date, config=override or None,
                             persist=not args.no_write_db, verbose=args.verbose)
    except IndexDataMissing as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)

    reg, st = payload["regime"], payload["stats"]
    print(f"\nS9 {args.date}  regime={reg['regime']} (沪深300/MA{reg['ma_window']} 偏离 {reg['signal']*100:+.2f}%)")
    print(f"  universe={st['universe_size']} valid_factor={st['valid_factor_count']} 进看板={st['kept_count']}")
    print(f"  腿: {payload['candidates'][0]['sleeve_label'] if payload['candidates'] else '?'}\n")
    print(f"  {'#':<3} {'code':<10} {'name':<8} {'industry':<10} {'factor':>8} {'mv亿':>7} {'今日%':>6} reason")
    for c in payload["candidates"]:
        fv = c.get("factor")
        fv_str = f"{fv*100:.2f}" if fv is not None else "—"
        print(f"  #{c['rank']:<2} {c['code']:<10} {(c.get('name') or '')[:6]:<8} "
              f"{(c.get('industry') or '')[:8]:<10} {fv_str:>8} "
              f"{c.get('total_mv_yi') or 0:>7.0f} {c.get('pct_chg') or 0:>+6.1f} {c.get('reason') or ''}")


if __name__ == "__main__":
    main()
