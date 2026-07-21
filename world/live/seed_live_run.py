#!/usr/bin/env python3.12
"""派生 live run：复制种子 run 的 per-run 交易历史到新 run_id，让系统按 run_id 重算现金。

不复制 fund_bot_accounts（PK=bot_id 的缓存，不可信）；不改任何表结构。
"""
import json, os, sqlite3, shutil, sys, argparse
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
    return {"actions": actions, "holdings": holdings, "lots": lots, "orders": orders}


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
    with open(dst_cfg_path, "w") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


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

    print(f"[seed] {dst} 完成：{counts}  config={dst_cfg}")


if __name__ == "__main__":
    main()
