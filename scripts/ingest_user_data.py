#!/usr/bin/env python3
"""Ingest real-user fund cycles (Result_7.csv) into fund.db.

Spec: docs/superpowers/specs/2026-05-25-real-user-overlay-design.md

Rebuilds 3 tables (idempotent DROP + CREATE; other tables untouched):
  real_user_cycles  — one row per (user, fund) holding cycle
  real_user_txns    — transaction detail
  real_user_curves  — pre-computed money-weighted net-value path,
                      endpoint pinned to the authoritative ClearReturn2

Usage:
    python3 scripts/ingest_user_data.py             # write to lab DB
    python3 scripts/ingest_user_data.py --dry-run   # parse + report, no writes
    python3 scripts/ingest_user_data.py --db PATH --csv PATH
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from dataclasses import dataclass, field

# Make `import db` resolve whether run from repo root or scripts/
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "fund-portfolio-mcp"))

EPS = 1e-4


def classify(busin_name: str) -> str:
    """Map a fund transaction's Chinese busin_name to its effect on the holding.

    buy/sell move both shares and external cash; xfer_in/xfer_out move shares
    only (no cost basis change); skip = dividend-method / conversion / custody
    rows we deliberately ignore in v1 (endpoint pinning absorbs the drift)."""
    name = (busin_name or "").strip()
    if name in ("申购确认", "定时定额投资确认"):
        return "buy"
    if name in ("赎回确认", "强行赎回"):
        return "sell"
    if name in ("转入投资账户", "转托管入确认", "份额转卡转入"):
        return "xfer_in"
    if name in ("转出投资账户", "份额转卡转出"):
        return "xfer_out"
    return "skip"


def _raw_roi_path(txns, dates, nav_by_date):
    """Money-weighted roi(t) for each NAV date in `dates` (ascending).

    roi(t) = (mv(t) + proceeds(t) - invested(t)) / invested(t)
    Transactions are folded in cumulatively as their txn_date is reached;
    invested(t) <= 0 ⇒ roi defined as 0 (no cost basis yet)."""
    txns_sorted = sorted(txns, key=lambda t: t.get("txn_date") or "")
    shares = invested = proceeds = 0.0
    ti = 0
    out = []
    for d in dates:
        while ti < len(txns_sorted) and (txns_sorted[ti].get("txn_date") or "") <= d:
            t = txns_sorted[ti]
            ti += 1
            kind = classify(t.get("busin_name", ""))
            vol = float(t.get("vol") or 0.0)
            amt = float(t.get("amount") or 0.0)
            if kind == "buy":
                shares += vol
                invested += amt
            elif kind == "sell":
                shares -= vol
                proceeds += amt
            elif kind == "xfer_in":
                shares += vol
            elif kind == "xfer_out":
                shares -= vol
            # skip: no effect
        nav = nav_by_date.get(d)
        if nav is None:
            continue
        mv = shares * float(nav)
        roi = (mv + proceeds - invested) / invested if invested > 1e-9 else 0.0
        out.append((d, roi))
    return out


def reconstruct_curve(txns, nav_by_date, start_date, end_date, clear_return2):
    """Return (series, method).

    series = [{'trade_date', 'net_value'}] with net_value = 1 + roi_adj(t).
    Endpoint is pinned to the authoritative clear_return2:
      - same-sign & |roi_end|>=EPS  → multiplicative scale f = c / roi_end  (method 'scale')
      - otherwise (sign flip / ~0)  → additive shift = c - roi_end          (method 'shift')
    method 'empty' when no NAV date falls inside [start_date, end_date]."""
    dates = sorted(d for d in nav_by_date if start_date <= d <= end_date)
    raw = _raw_roi_path(txns, dates, nav_by_date) if dates else []
    if not raw:
        return [], "empty"
    roi_end = raw[-1][1]
    c = float(clear_return2)
    same_sign = (roi_end > 0 and c > 0) or (roi_end < 0 and c < 0)
    if abs(roi_end) >= EPS and same_sign:
        f = c / roi_end
        series = [{"trade_date": d, "net_value": 1.0 + roi * f} for d, roi in raw]
        return series, "scale"
    shift = c - roi_end
    series = [{"trade_date": d, "net_value": 1.0 + roi + shift} for d, roi in raw]
    return series, "shift"
