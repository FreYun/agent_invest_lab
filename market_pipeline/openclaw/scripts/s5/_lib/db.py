"""共享 DB 访问层 — 所有 strategy skill 用这个模块开 SQLite 连接。

单库架构 (2026-04-17 合库):
  market.db   所有 strategy 数据都住这里
    - 共享市场数据: regime_classify_daily / 涨停池 / K 线 / 行业 / 交易日历
    - S5 专属: s5_select_runs / s5_candidates / s5_candidate_rejects / s5_verifications
    - 未来新策略加 sX_* 前缀表, 避免跨策略命名冲突

设计原则:
  - 零依赖, 只用 sqlite3 (Python stdlib)
  - 所有连接默认 PRAGMA journal_mode=WAL, foreign_keys=ON
  - 行工厂 sqlite3.Row 让结果可以 row['col_name'] 访问
  - DDL 用 CREATE TABLE IF NOT EXISTS, init_*_db() 完全 idempotent
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #

# 本文件位于 scripts/s5/_lib/db.py
_LIB_DIR = os.path.dirname(os.path.abspath(__file__))

# 2026-04-15 迁移: market.db 从 strategy/.cache/ 搬到 /home/rooot/database/
# - 跟 backfill 流水线 (daily/stk_limit/index_daily 等大表) 共用同一个文件
# - 路径在 git 仓库外, 数据不被跟踪 (严禁在 .openclaw 目录下创建 market.db 的 symlink/副本)
MARKET_DB_PATH = os.environ.get("MARKET_DB_PATH") or os.environ.get("SCOUT_DB_PATH") or "/home/rooot/agent_invest_lab/data/market.db"

# --------------------------------------------------------------------------- #
# 共用的 schema_version 表 + PRAGMA
# --------------------------------------------------------------------------- #

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

MARKET_DB_VERSION = 2  # v2: s5_* 表并入 market.db (原 s5.db 下线)


def _open_conn(path: str) -> sqlite3.Connection:
    """开连接, 设 PRAGMA, 设 row factory。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_market_db() -> sqlite3.Connection:
    return _open_conn(MARKET_DB_PATH)


def _set_version(conn: sqlite3.Connection, name: str, version: int):
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (name, version, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (name, version),
    )


# --------------------------------------------------------------------------- #
# market.db DDL
# --------------------------------------------------------------------------- #

MARKET_DDL = [
    SCHEMA_VERSION_DDL,

    # ========================================================================
    # trading_calendar — 交易日历
    # 用途: 判断某日是否为交易日, 被 shift_trading_days() 等日历运算调用
    # 状态: v1 暂不主动填充, 日历运算 fallback 到 akshare tool_trade_date_hist_sina
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS trading_calendar (
        date      TEXT PRIMARY KEY,    -- YYYY-MM-DD
        is_open   INTEGER NOT NULL,    -- 1=交易日, 0=休市 (周末/节假日)
        weekday   INTEGER NOT NULL     -- 0=周一 .. 6=周日
    );
    """,

    # ========================================================================
    # regime_daily — 每日市场 regime 判断 (classifier 的 source of truth)
    # 一行 = 一个交易日的完整 classifier 输出, 所有下游策略从这里读
    # 由 scripts/market-regime (cron 管道) 写入, S5/S4/S7 等策略读取
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS regime_daily (
        date                      TEXT PRIMARY KEY,  -- 交易日 YYYY-MM-DD
        regime_code               TEXT NOT NULL,     -- 最终档位 code: STRONG_BULL/STRONG_RANGE/NEUTRAL_RANGE/WEAK_RANGE/BEAR
        regime_name               TEXT NOT NULL,     -- 中文名: 强牛/强势震荡/中性震荡/弱势震荡/熊
        score_total               INTEGER NOT NULL,  -- 六维总分 [-12, +12]

        -- 六维 breakdown (各维度 -2..+2, 独立列存便于 GROUP BY 和校准)
        score_ma_position         INTEGER,  -- 维度 1: 指数 vs 均线 (HS300/CSI1000)
        score_advance_decline     INTEGER,  -- 维度 2: 涨跌家数比
        score_sentiment_delta     INTEGER,  -- 维度 3: 涨停-跌停差
        score_sentiment_index     INTEGER,  -- 维度 4: 情绪评分 (0-100)
        score_streak_height       INTEGER,  -- 维度 5: 最高连板高度
        score_volume_trend        INTEGER,  -- 维度 6: 5日/20日均量比

        -- 原始数据快照 (JSON: MA 值/涨跌比/情绪评分/连板数/量比 等)
        -- 用 JSON 存的原因: 嵌套结构 (hs300/csi1000 子 dict), 查询频率低
        raw_data_json             TEXT,

        -- 数据可信度
        confidence                TEXT NOT NULL,     -- high (0 缺) / medium (1-2 缺) / low (≥3 缺)
        missing_dims_json         TEXT,              -- JSON 数组, 缺失的维度名

        -- 切档/逃生门状态
        last_regime               TEXT,              -- 上一次 log 记录的 regime (3 日确认对比用)
        switched                  INTEGER NOT NULL,  -- 1=今日发生常规切换, 0=维持
        emergency_switch          INTEGER NOT NULL,  -- 1=今日逃生门触发, 0=无
        emergency_reason_json     TEXT,              -- 逃生门触发原因, JSON 数组: [total_drop_5, index_crash, sentiment_collapse, total_surge_5, index_rally, breadth_confirm]
        switch_warning            TEXT,              -- 未达 3 日确认但已进入新档位的警戒文本

        -- 战法推荐与仓位上限 (playbook)
        playbook_recommended_json TEXT,  -- JSON: [{id:"S5", name:"龙回头", priority:1, mode:"试错"}, ...]
        playbook_forbidden_json   TEXT,  -- JSON 数组: 禁止的战法 ID
        position_limit_total      REAL NOT NULL,  -- 总仓位上限 (0-1)
        position_limit_single     REAL NOT NULL,  -- 单票上限 (0-1)

        created_at                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP  -- 写入时间 (非交易日)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_regime_code ON regime_daily(regime_code);  -- 按档位分组统计",

    # ========================================================================
    # limit_up_pool — 每日涨停股池快照
    # 用途: 永久存档, 绕开 akshare stock_zt_pool_em 2-3 周历史窗口限制
    # 由 data_fetcher.fetch_zt_pool 按需写入 (cache-aside)
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS limit_up_pool (
        date             TEXT NOT NULL,  -- 交易日 YYYY-MM-DD
        code             TEXT NOT NULL,  -- 股票 6 位代码
        name             TEXT,           -- 股票简称
        industry         TEXT,           -- 东财所属行业 (~80 个大类)
        pct_chg          REAL,           -- 当日涨跌幅 (%)
        streak           INTEGER,        -- 连板数 (1=首板, 2=2板...)
        close            REAL,           -- 收盘价 (元)
        amount           REAL,           -- 成交额 (元)
        market_cap       REAL,           -- 流通市值 (元)
        turnover_rate    REAL,           -- 换手率 (%)
        seal_amount      REAL,           -- 封板资金 (元)
        first_seal_time  TEXT,           -- 首次封板时间 HHMMSS
        last_seal_time   TEXT,           -- 最后封板时间 HHMMSS
        blast_count      INTEGER,        -- 炸板次数
        cached_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 入库时间
        PRIMARY KEY (date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_lup_industry ON limit_up_pool(date, industry);  -- 按行业聚合算热门行业 top-N",
    "CREATE INDEX IF NOT EXISTS idx_lup_streak ON limit_up_pool(date, streak DESC); -- 按连板数倒排取高度板",

    # ========================================================================
    # klines_cache — 个股日 K 线缓存
    # 用途: research-mcp 批量拉过的 K 线永久存, 避免重复拉
    # 由 data_fetcher.fetch_klines_batch 按需写入 (cache-aside)
    # 命中策略简化版: 只要 (code, start..end) 范围内有任意行即视为该 code 已缓存
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS klines_cache (
        code               TEXT NOT NULL,  -- 股票 6 位代码
        date               TEXT NOT NULL,  -- 交易日 YYYY-MM-DD
        open               REAL,           -- 开盘价
        high               REAL,           -- 最高价
        low                REAL,           -- 最低价
        close              REAL,           -- 收盘价
        pct_chg            REAL,           -- 涨跌幅 (%)
        volume             REAL,           -- 成交量 (手)
        amount             REAL,           -- 成交额 (元)
        is_limit_up        INTEGER,        -- 1=涨停, 0=非
        is_limit_down      INTEGER,        -- 1=跌停, 0=非
        limit_up_streak    INTEGER,        -- 连续涨停天数 (含当日)
        limit_down_streak  INTEGER,        -- 连续跌停天数
        cached_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 入库时间, 未来按日过期时参考
        PRIMARY KEY (code, date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_klines_date ON klines_cache(date);  -- 按日期查 (例如 '某日所有股的 K 线')",

    # ========================================================================
    # hot_industries_daily — 每日热门行业排名
    # 用途: select.py 预筛股池用 (前 N 热门行业的成分股 = universe)
    # 数据来源: 从 limit_up_pool 聚合, 或独立写入
    # 状态: v1 暂由 select.py 运行时计算 + 写入 s5.db.select_runs.hot_industries_json
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS hot_industries_daily (
        date          TEXT NOT NULL,    -- 交易日 YYYY-MM-DD
        rank          INTEGER NOT NULL, -- 排名 (1=最热)
        industry      TEXT NOT NULL,    -- 行业名
        limit_count   INTEGER NOT NULL, -- 当日该行业涨停家数
        PRIMARY KEY (date, industry)
    );
    """,

    # ========================================================================
    # regime_raw_daily — classifier 派生层每日六维原始数据 (2026-04-15 新增)
    # 由 backfill/derive_raw_data.py 从 daily + stk_limit + index_daily 聚合
    # 跟 daily 大表的关系: 这是按日聚合后的 792 行 vs 全量 4.2M 行
    # 跟 regime_daily 的关系: 这是 raw 数据 (维度 2-6 的源), regime_daily 是打分后结果
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS regime_raw_daily (
        trade_date              TEXT PRIMARY KEY,    -- YYYYMMDD (与 daily 表口径一致)

        -- 维度 2: 涨跌家数比
        total                   INTEGER,             -- 全市场样本数 (扣停牌)
        up                      INTEGER,             -- 上涨股数
        down                    INTEGER,             -- 下跌股数
        flat                    INTEGER,             -- 平盘股数
        advance_decline_ratio   REAL,                -- up / total

        -- 维度 3: 精确涨跌停 (close >= up_limit / close <= down_limit)
        limit_up_count          INTEGER,             -- 真实涨停股数
        limit_down_count        INTEGER,             -- 真实跌停股数
        sentiment_delta         INTEGER,             -- lu - ld

        -- 维度 4: 情绪评分 (复刻 mod_sentiment._assess_sentiment 的 0-100)
        sentiment_index         INTEGER,

        -- 维度 5: 最高连板 (从涨停集合本地递归算)
        max_streak              INTEGER,

        -- 维度 6: 全市场成交额
        total_amount_yi         REAL,                -- daily.amount 合计 (亿元)
        index_amount_yi         REAL,                -- 上证综指+深证综指 amount (亿元)

        -- 指数层 (维度 1 + 6 参考数据)
        hs300_close             REAL,
        hs300_pct_chg           REAL,
        csi1000_close           REAL,
        csi1000_pct_chg         REAL,
        shanghai_close          REAL,
        shenzhen_close          REAL,

        computed_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,

    # ========================================================================
    # regime_classify_daily — 多版本 regime 回放结果 (2026-04-15 新增)
    # 跟 regime_daily 的区别:
    #   regime_daily          单一记录, 每日生产 classifier 写一次, 无版本
    #   regime_classify_daily 多版本, 同一天可存 v1/v2/v3/.../production, 用于规则对比
    # 主键 (trade_date, rules_version) 让"换规则跑回放"INSERT 不冲突
    # 由 backfill/replay.py 写入
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS regime_classify_daily (
        trade_date              TEXT NOT NULL,       -- ISO YYYY-MM-DD (跟 regime_daily 一致)
        rules_version           TEXT NOT NULL,       -- 'v1' | 'v2' | 'v3' | 'production'

        -- 总分 + 六维 breakdown
        total_score             INTEGER,
        score_ma_position       INTEGER,
        score_advance_decline   INTEGER,
        score_sentiment_delta   INTEGER,
        score_sentiment_index   INTEGER,
        score_streak_height     INTEGER,
        score_volume_trend      INTEGER,

        -- Regime 决策结果
        regime_code             TEXT,                -- STRONG_BULL / STRONG_RANGE / NEUTRAL_RANGE / WEAK_RANGE / BEAR
        regime_name             TEXT,                -- 强牛 / 强势震荡 / 中性震荡 / 弱势震荡 / 熊
        last_regime_code        TEXT,                -- 上一天的 regime
        switched                INTEGER,             -- 1=今日发生切换
        bootstrap               INTEGER,             -- 1=前 3 日 bootstrap 模式

        -- 逃生门
        emergency_switch        INTEGER,             -- 1=逃生门触发
        emergency_direction     TEXT,                -- 'down' | 'up' | NULL
        emergency_reason        TEXT,                -- 触发原因 (分号分隔: 'total_drop_5;sentiment_collapse')

        -- 数据可信度
        confidence              TEXT,                -- 'high' | 'medium' | 'low'
        missing_dims            TEXT,                -- 缺失维度 (分号分隔)

        computed_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (trade_date, rules_version)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_classify_version ON regime_classify_daily(rules_version);",
    "CREATE INDEX IF NOT EXISTS idx_classify_regime ON regime_classify_daily(regime_code);",

    # ========================================================================
    # regime_strategy_nav — 策略净值时序 (2026-04-15 新增)
    # 用于沉淀按 regime 调仓的策略回放净值, 支持多策略对比
    # 一行 = 一个策略一个交易日的净值快照
    # 由 backfill/simulate_strategies.py 写入
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS regime_strategy_nav (
        trade_date              TEXT NOT NULL,       -- ISO YYYY-MM-DD
        strategy_id             TEXT NOT NULL,       -- 'fullhold' | 'v2_dynamic' | 'v2_entry_only' | ...

        position                REAL,                -- 当日仓位 0-1
        daily_pnl_pct           REAL,                -- 当日收益率 % (扣仓位)
        cumulative_nav          REAL,                -- 累计净值, 起点 1.0
        max_drawdown_to_date    REAL,                -- 截至该日的历史最大回撤 (负数)

        computed_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (trade_date, strategy_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_strategy_nav_id ON regime_strategy_nav(strategy_id);",

    # ========================================================================
    # market_amount_daily — 全市场成交额时序 (2026-04-16 新增)
    # Tushare daily_info (SH_MARKET + SZ_MARKET) 的全 A 成交额
    # 口径: 沪市全部 + 深市全部 (含主板+创业板+科创板), 比上证综指+深证成指完整 ~6%
    # 由 classify.py fetch_full_market_volume 写入, 替代原 CSV 缓存
    # ========================================================================
    """
    CREATE TABLE IF NOT EXISTS market_amount_daily (
        trade_date   TEXT PRIMARY KEY,   -- YYYYMMDD
        amount_yi    REAL NOT NULL,      -- 全市场成交额 (亿元)
        computed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,

    # ========================================================================
    # S5 龙回头战法专属表 (2026-04-17 从 s5.db 迁入, 加 s5_ 前缀避免跨策略冲突)
    # ========================================================================

    # s5_select_runs — 每次 select.py 运行的 metadata
    # 一行 = 一天一次 select (无论是否出 candidate), 作为 s5_candidates/rejects 的父表
    """
    CREATE TABLE IF NOT EXISTS s5_select_runs (
        date                        TEXT PRIMARY KEY,
        strategy                    TEXT NOT NULL,

        regime_code                 TEXT NOT NULL,
        regime_name                 TEXT NOT NULL,
        regime_score                INTEGER NOT NULL,
        confidence                  TEXT,
        switched                    INTEGER,
        emergency_switch            INTEGER,
        position_limit_single_base  REAL,

        skipped_reason              TEXT,
        regime_gate_allowed         INTEGER,
        regime_gate_reason          TEXT,

        universe_size               INTEGER,
        dragon_pool_size            INTEGER,
        passed_count                INTEGER,
        hot_industries_json         TEXT,

        config_json                 TEXT,

        created_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,

    # s5_candidates — 通过 S5 信号的标的 (一行 = 一只)
    """
    CREATE TABLE IF NOT EXISTS s5_candidates (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        date                      TEXT NOT NULL,
        code                      TEXT NOT NULL,
        name                      TEXT,
        industry                  TEXT,

        dragon_peak_date          TEXT NOT NULL,
        dragon_peak_close         REAL NOT NULL,
        dragon_peak_max_streak    INTEGER NOT NULL,

        cooldown_days             INTEGER NOT NULL,
        cooldown_drop_pct         REAL NOT NULL,
        cooldown_t1_close         REAL,

        rebound_t_pct             REAL NOT NULL,
        rebound_t_close           REAL NOT NULL,
        rebound_t_low             REAL,
        rebound_t_high            REAL,
        rebound_t1_high           REAL,

        entry_zone_low            REAL NOT NULL,
        entry_zone_high           REAL NOT NULL,
        entry_rule                TEXT,

        stop_loss_price           REAL NOT NULL,
        stop_loss_rule            TEXT,

        target_1_price            REAL,
        target_2_price            REAL,

        position_pct              REAL NOT NULL,
        position_calc             TEXT,

        UNIQUE(date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s5_cand_date ON s5_candidates(date);",
    "CREATE INDEX IF NOT EXISTS idx_s5_cand_code ON s5_candidates(code);",

    # s5_daily_universe — 每日 S5 universe 预算结果 (prewarm 写入, select 读)
    # 一行 = 某日某只股属于 universe, 按 (date, code) 唯一
    """
    CREATE TABLE IF NOT EXISTS s5_daily_universe (
        date       TEXT NOT NULL,
        code       TEXT NOT NULL,
        industry   TEXT,
        PRIMARY KEY (date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s5_univ_date ON s5_daily_universe(date);",

    # s5_candidate_rejects — 差一点的标的抽样
    """
    CREATE TABLE IF NOT EXISTS s5_candidate_rejects (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        date           TEXT NOT NULL,
        code           TEXT NOT NULL,
        name           TEXT,
        stage_failed   TEXT NOT NULL,
        reject_reason  TEXT NOT NULL,
        UNIQUE(date, code)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s5_rej_date ON s5_candidate_rejects(date);",

    # s5_verifications — T+1 盘中/回测验证结果
    """
    CREATE TABLE IF NOT EXISTS s5_verifications (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        t_date            TEXT NOT NULL,
        t1_date           TEXT NOT NULL,
        candidate_id      INTEGER NOT NULL,
        code              TEXT NOT NULL,
        mode              TEXT NOT NULL,

        status            TEXT NOT NULL,

        entry_status      TEXT,
        entry_price       REAL,
        exit_price        REAL,
        exit_reason       TEXT,
        pnl_pct           REAL,

        t1_open           REAL,
        t1_high           REAL,
        t1_low            REAL,
        t1_close          REAL,

        live_open_price   REAL,
        live_current      REAL,

        note              TEXT,

        created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(candidate_id) REFERENCES s5_candidates(id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_s5_veri_t1 ON s5_verifications(t1_date);",
    "CREATE INDEX IF NOT EXISTS idx_s5_veri_code ON s5_verifications(code);",
    "CREATE INDEX IF NOT EXISTS idx_s5_veri_mode ON s5_verifications(mode);",
]


def init_market_db():
    """跑 market.db DDL, idempotent。"""
    conn = get_market_db()
    try:
        for stmt in MARKET_DDL:
            conn.execute(stmt)
        _set_version(conn, "market", MARKET_DB_VERSION)
        conn.commit()
    finally:
        conn.close()


def init_all():
    """兼容老代码, 现在只是 init_market_db 的别名 (单库架构)."""
    init_market_db()


# --------------------------------------------------------------------------- #
# 一些共享 helper (查询)
# --------------------------------------------------------------------------- #


def query_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return row


def query_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


if __name__ == "__main__":
    print(f"Initializing market.db at {MARKET_DB_PATH}")
    init_market_db()
    print("✅ DB init done")
    # 验证两次跑都不报错
    init_market_db()
    print("✅ Idempotent re-run OK")
