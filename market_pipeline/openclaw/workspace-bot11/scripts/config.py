"""Shared configuration for the project-local market pipeline."""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

# Use the brze proxy by default. Tokens must come from environment or local files.
TUSHARE_HTTP_URL = os.getenv("TUSHARE_HTTP_URL", "https://tu.brze.top")
PREFERRED_DATA_SOURCE = os.getenv("PREFERRED_DATA_SOURCE", "akshare")


def _read_first_existing(paths: list[str]) -> str | None:
    for path in paths:
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            continue
        with open(expanded, encoding="utf-8") as f:
            value = f.read().strip()
            if value:
                return value
    return None


def get_tushare_token() -> str:
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("BRZE_TUSHARE_TOKEN")
    if token:
        return token.strip()
    token = _read_first_existing([
        "~/.openclaw/.tushare-token",
        "~/.tushare-token",
    ])
    if token:
        return token
    raise RuntimeError(
        "未找到 tushare token: 设置 TUSHARE_TOKEN/BRZE_TUSHARE_TOKEN "
        "或写入 ~/.openclaw/.tushare-token"
    )


def get_tushare_pro():
    """Return a Tushare Pro client routed through the configured brze proxy."""
    import tushare as ts
    from tushare.pro import client as _ts_client

    _ts_client.DataApi._DataApi__http_url = TUSHARE_HTTP_URL
    return ts.pro_api(get_tushare_token())
