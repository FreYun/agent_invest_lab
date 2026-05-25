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
