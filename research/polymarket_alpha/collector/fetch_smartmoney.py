#!/usr/bin/env /usr/bin/python3.12
"""
聪明钱采集管道：调 polymarket CLI 把 leaderboard + top market holders 灌进
/home/rooot/database/polymarket.db 的空表 leaderboard / holders。

用法：
    /usr/bin/python3.12 fetch_smartmoney.py

依赖：仅标准库（subprocess/sqlite3/json/time/os）。
CLI 路径可用环境变量 POLY_CLI 覆盖，默认指向 release 二进制。

CLI flag 来源（源码核对，非猜测）：
    /home/rooot/polymarket-cli-main/src/commands/data.rs
        Leaderboard { period: Option<TimePeriod>, order_by: Option<OrderBy>,
                       limit: i32, offset: Option<i32> }
            TimePeriod = day|week|month|all   (不是 1d/7d/30d)
            OrderBy    = pnl|vol               (不是 pnl/volume)
        Holders { market: B256 (位置参数，condition_id，不是 --market flag！),
                  limit: i32 }
    JSON 字段名来源：/home/rooot/polymarket-cli-main/src/output/data.rs
        leaderboard: 扁平数组 [{rank, proxy_wallet, user_name, pnl, volume, ...}]
        holders:     数组 [{token, holders:[{proxy_wallet, name, pseudonym,
                                              amount, outcome_index, ...}]}]
                     注意 market 参数传的是 condition_id，返回按 token 分组。
"""
import json
import os
import sqlite3
import subprocess
import time

DB = "/home/rooot/database/polymarket.db"
CLI = os.environ.get(
    "POLY_CLI", "/home/rooot/polymarket-cli-main/target/release/polymarket"
)
# 本机访问 Polymarket API 需走本地代理（同 /home/rooot/pm_crawler/pmclient.py 的约定），
# 否则 CLI 会报 "Connection reset by peer"。可用 PM_PROXY 覆盖。
PROXY = os.environ.get("PM_PROXY", "http://127.0.0.1:7897")
_ENV = {**os.environ, "HTTP_PROXY": PROXY, "HTTPS_PROXY": PROXY,
         "http_proxy": PROXY, "https_proxy": PROXY}

LEADERBOARD_PERIODS = ["day", "week", "month", "all"]
LEADERBOARD_ORDERS = ["pnl", "vol"]
LEADERBOARD_LIMIT = "50"  # data-api 限制: limit must be between 1 and 50
TOP_MARKETS_N = 20
HOLDERS_LIMIT = "20"
SLEEP_BETWEEN_CALLS = 0.5  # 别对上游并发/连发狂刷


def _run(args):
    try:
        out = subprocess.run(
            [CLI, "-o", "json", "data", *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=_ENV,
        )
    except Exception as e:
        print("CLI 调用异常:", args, e)
        return None
    if out.returncode != 0:
        print("CLI 返回非0:", args, out.returncode, out.stderr.strip()[:300])
        return None
    try:
        return json.loads(out.stdout)
    except Exception as e:
        print("JSON 解析失败:", args, e, out.stdout[:300])
        return None


def collect_leaderboard(con, ts):
    inserted = 0
    for period in LEADERBOARD_PERIODS:
        for order in LEADERBOARD_ORDERS:
            data = _run(
                [
                    "leaderboard",
                    "--period",
                    period,
                    "--order-by",
                    order,
                    "--limit",
                    LEADERBOARD_LIMIT,
                ]
            )
            time.sleep(SLEEP_BETWEEN_CALLS)
            if not data:
                continue
            for e in data:
                con.execute(
                    "INSERT OR IGNORE INTO leaderboard VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ts,
                        period,
                        order,
                        e.get("rank"),
                        e.get("proxy_wallet"),
                        e.get("user_name"),
                        e.get("pnl"),
                        e.get("volume"),
                    ),
                )
                inserted += 1
            con.commit()  # 流式提交
    return inserted


def top_markets(con, n=TOP_MARKETS_N):
    cur = con.execute(
        "SELECT DISTINCT condition_id FROM market "
        "WHERE closed=0 AND condition_id IS NOT NULL "
        "ORDER BY last_seen DESC LIMIT ?",
        (n,),
    )
    return [row[0] for row in cur.fetchall()]


def collect_holders(con, ts):
    inserted = 0
    for condition_id in top_markets(con):
        data = _run(["holders", condition_id, "--limit", HOLDERS_LIMIT])
        time.sleep(SLEEP_BETWEEN_CALLS)
        if not data:
            continue
        for mh in data:
            token = mh.get("token")
            for h in mh.get("holders", []):
                con.execute(
                    "INSERT OR IGNORE INTO holders VALUES (?,?,?,?,?,?,?)",
                    (
                        condition_id,
                        token,
                        ts,
                        h.get("proxy_wallet"),
                        h.get("name") or h.get("pseudonym"),
                        h.get("amount"),
                        h.get("outcome_index"),
                    ),
                )
                inserted += 1
        con.commit()
    return inserted


if __name__ == "__main__":
    ts = int(time.time())
    con = sqlite3.connect(DB, timeout=30)
    try:
        n_lb = collect_leaderboard(con, ts)
        n_hd = collect_holders(con, ts)
    finally:
        con.close()
    print(f"collected at {ts}: leaderboard +{n_lb}, holders +{n_hd}")
