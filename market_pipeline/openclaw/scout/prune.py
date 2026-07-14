"""盘中看板数据保留 — 只留最近 N 个交易日 (按实际有数据的日期算), 更早的逐帧删除.

用法:
  python3 prune.py            # 默认保留最近 30 个交易日
  python3 prune.py --keep 20

- "交易日" = intraday_snapshot 里实际出现过的日期, 自动跳过周末/节假日.
- 不足 N 个交易日时直接 no-op, 不删任何东西.
- 不做 VACUUM: market.db 为多策略共享库, 全库锁代价高; WAL 下删除的空间会被后续 INSERT 复用.
"""
from __future__ import annotations

import argparse

import scout_db

TABLES = ["intraday_snapshot", "intraday_board", "intraday_candidate_live"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=30, help="保留最近多少个交易日(有数据的日期)")
    a = ap.parse_args()

    c = scout_db.conn()
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT substr(snapshot_time,1,10) AS d FROM intraday_snapshot "
        "ORDER BY d DESC LIMIT ?", (a.keep,))]
    if len(dates) < a.keep:
        print(f"[scout-prune] 当前仅 {len(dates)} 个交易日 (<{a.keep}), 无需清理")
        c.close()
        return

    cutoff = dates[-1]  # 第 N 新的日期; 删除日期 < cutoff 的所有帧
    total = 0
    for t in TABLES:
        # snapshot_time = 'YYYY-MM-DD HH:MM:SS', 与 'YYYY-MM-DD' 比较:
        # 同日的 '... HH:MM:SS' 前缀相同但更长 => 大于 cutoff, 保留; 更早日期整体小于 cutoff, 删除.
        # 直接用 snapshot_time 比较可命中索引, 无需 substr.
        cur = c.execute(f"DELETE FROM {t} WHERE snapshot_time < ?", (cutoff,))
        print(f"  {t}: 删除 {cur.rowcount} 行")
        total += cur.rowcount
    c.commit()
    c.close()
    print(f"[scout-prune] 保留 >= {cutoff} 共 {a.keep} 个交易日, 清理 {total} 行")


if __name__ == "__main__":
    main()
