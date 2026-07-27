#!/usr/bin/env python3.12
"""派生 live run：复制种子 run 的 per-run 交易历史到新 run_id，让系统按 run_id 重算现金。

不复制 fund_bot_accounts（PK=bot_id 的缓存，不可信）；不改任何表结构。
"""
import json, os, sqlite3, shutil, sys, argparse, datetime
sys.path.insert(0, os.path.dirname(__file__))
import live_common as lc

WORLD = lc.WORLD
DB_PATH = lc.DB_PATH


def _copy_rows(conn, table, where_sql, params, rekey: dict):
    """读 table 满足 where 的行，按 rekey 改列值后 INSERT 回同表（PK 自增新分配）。"""
    rows = conn.execute(f"SELECT * FROM {table} WHERE {where_sql}", params).fetchall()
    cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
    pk = cols[0]  # AUTOINCREMENT 主键在首列（本项目约定），复制时丢弃让其重分配
    ins_cols = [c for c in cols if c != pk]
    n = 0
    for r in rows:
        d = dict(zip(cols, r))
        for k, v in rekey.items():
            d[k] = v
        vals = [d[c] for c in ins_cols]
        conn.execute(
            f"INSERT INTO {table} ({','.join(ins_cols)}) VALUES ({','.join('?' * len(ins_cols))})",
            vals,
        )
        n += 1
    return n


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _share_epsilon(shares: float) -> float:
    return max(0.05, abs(float(shares or 0.0)) * 1e-7)


def _repair_cloned_lots(conn, bot, dst_run_id) -> dict:
    """Make cloned live holdings sellable even when the source run's lots are stale.

    Historical dash runs predate the lot ledger and some have active holdings with
    no matching open lots. Live runs need a coherent carry lot; otherwise the
    first sell order will remain pending forever.
    """
    h_cols = _cols(conn, "fund_bot_holdings")
    l_cols = _cols(conn, "fund_bot_holding_lots")
    required_h = {"holding_id", "bot_id", "fund_code", "run_id", "status", "shares"}
    required_l = {"bot_id", "fund_code", "run_id", "holding_id", "entry_date", "entry_nav",
                  "shares_initial", "shares_remaining", "cost_initial", "cost_remaining", "status"}
    if not required_h.issubset(h_cols) or not required_l.issubset(l_cols):
        return {"checked": 0, "rebuilt": 0, "skipped": "schema_without_lot_cost_columns"}

    rows = conn.execute(
        "SELECT * FROM fund_bot_holdings WHERE bot_id=? AND run_id=? AND status='active'",
        (bot, dst_run_id),
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM fund_bot_holdings LIMIT 0").description]
    checked = 0
    rebuilt = 0
    for row in rows:
        h = dict(zip(cols, row))
        checked += 1
        shares = float(h.get("shares") or 0.0)
        if shares <= 0:
            continue
        cur = conn.execute(
            "SELECT COALESCE(SUM(shares_remaining), 0) FROM fund_bot_holding_lots "
            "WHERE bot_id=? AND fund_code=? AND run_id=? AND status='open'",
            (bot, h["fund_code"], dst_run_id),
        ).fetchone()
        open_shares = float(cur[0] or 0.0)
        if abs(shares - open_shares) <= _share_epsilon(shares):
            continue

        entry_date = h.get("entry_date") or h.get("exit_date") or "1970-01-01"
        entry_nav = float(h.get("entry_nav") or h.get("latest_nav") or 0.0)
        cost = float(h.get("amount_invested") or (shares * entry_nav if entry_nav else 0.0))
        conn.execute(
            "DELETE FROM fund_bot_holding_lots "
            "WHERE bot_id=? AND fund_code=? AND run_id=? AND status='open'",
            (bot, h["fund_code"], dst_run_id),
        )
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
            " shares_initial, shares_remaining, cost_initial, cost_remaining, source_order_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open')",
            (bot, h["fund_code"], dst_run_id, h["holding_id"], entry_date, entry_nav,
             shares, shares, cost, cost),
        )
        rebuilt += 1
    return {"checked": checked, "rebuilt": rebuilt}


def clone_run_rows(conn, bot, src_run_id, dst_run_id, seed_date) -> dict:
    conn.row_factory = None
    actions = _copy_rows(conn, "fund_bot_actions",
                         "bot_id=? AND run_id=?", (bot, src_run_id),
                         {"run_id": dst_run_id})
    holdings = _copy_rows(conn, "fund_bot_holdings",
                          "bot_id=? AND run_id=? AND status='active'", (bot, src_run_id),
                          {"run_id": dst_run_id})
    lots = _copy_rows(conn, "fund_bot_holding_lots",
                      "bot_id=? AND run_id=? AND status='open'", (bot, src_run_id),
                      {"run_id": dst_run_id, "holding_id": None, "source_order_id": None})
    orders = _copy_rows(conn, "fund_bot_orders",
                        "bot_id=? AND order_run_id=? AND status='pending'", (bot, src_run_id),
                        {"order_run_id": dst_run_id, "settle_run_id": None})
    lot_repair = _repair_cloned_lots(conn, bot, dst_run_id)
    return {"actions": actions, "holdings": holdings, "lots": lots, "orders": orders,
            "lot_repair": lot_repair}


# 实时工具白名单：解开已有 ttjj 工具（注入日期指向今天 → 返回截至今天的真实数据）。
_REALTIME_TOOLS = [
    {"name": "market_realtime_quote", "description": "盘中实时行情：当前时点现价截面（quote/order_book/capital_flow/valuation 多维）。"},
    {"name": "stock_capital_flow", "description": "股票资金流向：资金流 + 北向持股 + 超大单。"},
    {"name": "macro_data", "description": "宏观数据。"},
    {"name": "stock_events", "description": "股票事件：停复牌 + SUE（催化/事件）。"},
    {"name": "research_view", "description": "研究观点。"},
    {"name": "ttjj_research_search", "description": "research 检索：已入库研究成果。"},
]


def seed_live_config(src_cfg_path, dst_cfg_path, dst_run_id):
    import yaml  # Python 侧依赖 pyyaml（已确认可用 6.0.1）
    with open(src_cfg_path) as f:
        cfg = yaml.safe_load(f)
    tools = cfg.get("simworld_tools", [])
    have = {t["name"] for t in tools}
    for t in _REALTIME_TOOLS:
        if t["name"] not in have:
            tools.append(t)
    cfg["simworld_tools"] = tools

    # 纯数字字符串（基金代码，如 '006328'）必须强制加引号落盘：PyYAML 默认对带前导 0
    # 的数字样字符串不加引号，而 node 的 js-yaml(YAML 1.2) 会把 006328 读成整数 6328，
    # 导致 world config 校验 "buyable_fund_codes must be an array of 6-digit fund code strings" 失败。
    class _QuoteDigitDumper(yaml.SafeDumper):
        pass

    def _repr_str(dumper, data):
        style = "'" if data.isdigit() else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    _QuoteDigitDumper.add_representer(str, _repr_str)
    with open(dst_cfg_path, "w") as f:
        yaml.dump(cfg, f, Dumper=_QuoteDigitDumper, allow_unicode=True, sort_keys=False)


def write_state_stub(dst_state_path, dst_run_id, bot, src_run_id):
    """写一个最小 state.json 存根，让 discover_live_runs 在首次 decide 之前就能发现该 live run。

    引擎首次跑 oos-daily-driver --phase decide 时，runWorld.setup 会用真实字段整份覆盖它
    （见 world/src/run.ts 的 writeState(initial)），所以这里只需 bots 单元素即可被发现。
    """
    os.makedirs(os.path.dirname(dst_state_path), exist_ok=True)
    with open(dst_state_path, "w") as f:
        json.dump({
            "run_id": dst_run_id,
            "status": "seeded",
            "bots": [bot],
            "source_run_id": src_run_id,
            "seeded_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-run-id", required=True)
    ap.add_argument("--bot-id", required=True)
    ap.add_argument("--seed-date", default=lc.today_str())
    args = ap.parse_args()

    dst = lc.live_run_id(args.source_run_id, args.bot_id)
    dst_state = os.path.join(WORLD, "runtime", "runs", dst, "state.json")
    dst_cfg = os.path.join(WORLD, "config", f"world-live-{dst}.yaml")
    if os.path.exists(dst_state) or os.path.exists(dst_cfg):
        print(f"[seed] {dst} 已存在，跳过（幂等）")
        return

    # 1. 复制 memory
    src_mem = os.path.join(WORLD, "runtime", "runs", args.source_run_id, "memory")
    dst_mem = os.path.join(WORLD, "runtime", "runs", dst, "memory")
    if os.path.isdir(src_mem):
        # 先建 live run 目录（dst_mem 的父目录），copytree 再在其中创建 memory/
        os.makedirs(os.path.join(WORLD, "runtime", "runs", dst), exist_ok=True)
        shutil.copytree(src_mem, dst_mem, dirs_exist_ok=True)

    # 2. 生成 live config
    src_cfg = os.path.join(WORLD, "config", f"world-tmp-{args.source_run_id}.yaml")
    seed_live_config(src_cfg, dst_cfg, dst)

    # 3. 复制 per-run 交易历史（单事务）
    conn = sqlite3.connect(DB_PATH)
    try:
        counts = clone_run_rows(conn, args.bot_id, args.source_run_id, dst, args.seed_date)
        conn.commit()
    finally:
        conn.close()

    # 4. 写 state.json 存根（发现标记；首次 decide 由引擎整份覆盖）
    write_state_stub(dst_state, dst, args.bot_id, args.source_run_id)

    print(f"[seed] {dst} 完成：{counts}  config={dst_cfg}")


if __name__ == "__main__":
    main()
