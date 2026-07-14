"""盘中实时选股看板 — 共享 DB 层 (market.db 盘中表 schema + 连接).

所有 scout 脚本 (collect.py / logic.py / server.py) 引用本模块.
盘中表与选股层/策略表同库 (/home/rooot/agent_invest_lab/data/market.db), WAL 并发安全.
"""
from __future__ import annotations

import sqlite3

DB_PATH = __import__("os").environ.get("MARKET_DB_PATH", __import__("os").environ.get("SCOUT_DB_PATH", "/home/rooot/agent_invest_lab/data/market.db"))

# 主动权益基金类型白名单(对标 ETF 之外的"主动重仓"测度用; 详见 board_active_fund 设计文档)
ACTIVE_EQUITY_TYPES = ("股票型", "混合型-偏股", "混合型-灵活", "混合型-平衡")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_snapshot (
    snapshot_time  TEXT PRIMARY KEY,   -- 'YYYY-MM-DD HH:MM:SS'
    trade_date     TEXT,               -- YYYYMMDD
    up_count       INTEGER,
    down_count     INTEGER,
    flat_count     INTEGER,
    limit_up       INTEGER,
    limit_down     INTEGER,
    blast_count    INTEGER,            -- 炸板数
    total_amount   REAL,               -- 两市成交额(亿元)
    sh_pct         REAL,               -- 上证指数涨幅
    gem_pct        REAL,               -- 创业板指涨幅
    szcz_pct       REAL,               -- 深证成指涨幅
    star50_pct     REAL,               -- 科创50涨幅
    bz50_pct       REAL,               -- 北证50涨幅
    csi300_pct     REAL,               -- 沪深300涨幅
    csi1000_pct    REAL,               -- 中证1000涨幅
    csi2000_pct    REAL,               -- 中证2000涨幅
    regime_code    TEXT,
    regime_name    TEXT,
    position_limit REAL,               -- 当前 regime 单票仓位上限
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS intraday_candidate_live (
    snapshot_time  TEXT NOT NULL,
    strategy       TEXT NOT NULL,      -- s1..s7
    code           TEXT NOT NULL,      -- 裸 6 位
    ts_code        TEXT,               -- 带后缀
    name           TEXT,
    industry       TEXT,
    cand_date      TEXT,               -- 候选所属交易日 YYYY-MM-DD
    price          REAL,
    pct            REAL,
    vol_ratio      REAL,
    turnover_rate  REAL,
    amount         REAL,               -- 成交额(亿)
    speed_5min     REAL,               -- 5分钟涨速
    net_main       REAL,               -- 实时主力净流入(万元)
    entry_low      REAL,
    entry_high     REAL,
    stop_loss      REAL,
    target_1       REAL,
    target_2       REAL,
    position_pct   REAL,
    dist_to_entry  REAL,               -- 现价相对买点区间的距离(%), 0=在区间内
    signal         TEXT,               -- 待入场/进区间/已突破/跌破止损/摸止盈
    top_theme      TEXT,               -- 最强归属题材
    top_theme_pct  REAL,               -- 该题材今日涨幅
    PRIMARY KEY (snapshot_time, strategy, code)
);
CREATE INDEX IF NOT EXISTS idx_icl_time ON intraday_candidate_live(snapshot_time);

-- 触发记录 + 跟踪收益 (全策略通用, 按交易日保留): 候选首现即落一条, 信号首次触发买点
-- 时定格 entry_price, 此后每帧滚动浮盈. S8 候选随盘变化, 掉出池标 still_candidate=0
-- 但记录保留全天; S1~S7 候选锚定昨日固定, still_candidate 恒 1.
CREATE TABLE IF NOT EXISTS intraday_trigger_log (
    trade_date      TEXT NOT NULL,      -- YYYY-MM-DD (选股日=当日)
    strategy        TEXT NOT NULL,      -- s1..s8
    code            TEXT NOT NULL,      -- 裸 6 位
    ts_code         TEXT,
    name            TEXT,
    industry        TEXT,
    first_seen_time TEXT,               -- 首次进入候选的快照时间
    entry_low       REAL,               -- 买点计划(首现时定格)
    entry_high      REAL,
    stop_loss       REAL,
    target_1        REAL,
    target_2        REAL,
    position_pct    REAL,
    triggered       INTEGER NOT NULL DEFAULT 0,
    trigger_time    TEXT,               -- 首次买点触发(进区间/已突破)时间
    trigger_signal  TEXT,
    entry_price     REAL,               -- 触发时现价(假设买入价)
    last_time       TEXT,
    last_price      REAL,
    ret_pct         REAL,               -- (现价-entry)/entry*100, 触发后才有
    max_ret         REAL,               -- 触发以来最高浮盈
    min_ret         REAL,               -- 触发以来最低浮盈
    still_candidate INTEGER NOT NULL DEFAULT 1,
    tone            TEXT,               -- S8 当帧定调
    path            TEXT,               -- S8 路径
    mainline        TEXT,               -- S8 所属主线题材
    PRIMARY KEY (trade_date, strategy, code)
);
CREATE INDEX IF NOT EXISTS idx_itl_date ON intraday_trigger_log(trade_date);

CREATE TABLE IF NOT EXISTS intraday_board (
    snapshot_time  TEXT NOT NULL,
    board_code     TEXT NOT NULL,
    board_name     TEXT,
    live_pct       REAL,               -- 成分股实时均涨幅
    member_count   INTEGER,
    up_count       INTEGER,            -- 成分中上涨家数
    rank           INTEGER,
    limit_up_count INTEGER,            -- 成分中涨停家数
    lead_code      TEXT,               -- 领涨股(成分中实时涨幅最高) 裸6位
    lead_name      TEXT,
    lead_pct       REAL,               -- 领涨股实时涨幅
    lead_price     REAL,               -- 领涨股现价
    limit_down_count INTEGER,          -- 成分中跌停家数
    amount_yi      REAL,               -- 成分股实时成交额合计(亿元)
    PRIMARY KEY (snapshot_time, board_code)
);
CREATE INDEX IF NOT EXISTS idx_ib_time ON intraday_board(snapshot_time);

-- 板块个股下钻: 只保留最新一帧(每次采集整表覆盖), 给前端点开板块时懒加载.
-- 历史成分清单无展示需求, 故不按 snapshot_time 累积, 避免 986 板块 × 帧数 撑爆库.
CREATE TABLE IF NOT EXISTS intraday_board_members (
    board_code      TEXT PRIMARY KEY,
    snapshot_time   TEXT,
    limit_up_json   TEXT,   -- 涨停成分 [{c,n,p,pct,vr,tr,mf}] c=裸6位 n=名 p=现价 pct=涨幅 vr=量比 tr=换手 mf=主力净流入(万)
    limit_down_json TEXT,   -- 跌停成分
    lead_json       TEXT,   -- 领涨 top N (pct 降序)
    lag_json        TEXT,   -- 领跌 bottom N (pct 升序)
    all_json        TEXT    -- 当前帧全成分 [{c,n,p,pct,vr,tr,mf}]
);

CREATE TABLE IF NOT EXISTS candidate_logic (
    strategy     TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    refresh_time TEXT,
    themes_json  TEXT,                 -- [{"theme":..,"pct":..}]  题材归属+今日涨幅
    zsxq_json    TEXT,                 -- [{"date":..,"author":..,"title":..}]
    summary      TEXT,                 -- 可选文字总结(留接口位)
    PRIMARY KEY (strategy, code)
);

CREATE TABLE IF NOT EXISTS board_trend_daily (
    trade_date      TEXT NOT NULL,
    board_code      TEXT NOT NULL,
    board_name      TEXT,
    trend_score     REAL,      -- 0~100 趋势分
    ret20           REAL,      -- 20日累计涨幅 %
    ret60           REAL,      -- 60日累计涨幅 %
    vol_ratio       REAL,      -- 近5日/近20日 板块成交额比
    amt_share       REAL,      -- 近5日均 板块成交额/大盘成交额
    amt_share_trend REAL,      -- 占比提升幅度 (近5/近20 - 1)
    ret5            REAL,      -- 近5日累计涨幅 % (高位滞涨: 价滞判定)
    amt_peak_ratio  REAL,      -- 近5日均额/近20日峰值额 (高位滞涨: 天量度)
    dist_ma20       REAL,      -- 当前指数相对 MA20 距离 % (上涨中继: 回踩到位判定)
    ma_aligned      INTEGER,   -- MA5>MA10>MA20>MA60 多头排列 0/1
    above_ma20      INTEGER,
    above_ma60      INTEGER,
    lead_code       TEXT,
    lead_name       TEXT,
    rank            INTEGER,
    computed_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_btd_date ON board_trend_daily(trade_date);

-- 主线 Top10 次日前瞻累计 (选后 T+1 组合净值/超额). 算法见 scout/mainline_forward_daily.py.
CREATE TABLE IF NOT EXISTS board_trend_forward_daily (
    trade_date  TEXT NOT NULL,     -- 选中日 T
    fwd_date    TEXT NOT NULL,     -- 实现日 T+1
    top         INTEGER NOT NULL,  -- 口径 (=10)
    port_ret    REAL,              -- 组合次日等权收益 %
    mkt_ret     REAL,              -- 全A等权次日收益 %
    excess      REAL,              -- port_ret - mkt_ret
    nav         REAL,              -- 组合累计净值 (从 1.0)
    mkt_nav     REAL,              -- 大盘累计净值
    excess_nav  REAL,              -- nav / mkt_nav
    n_boards    INTEGER,           -- 当日实际纳入板块数
    n_stocks    INTEGER,           -- 保留列; 板块指数口径下不适用, 恒为 NULL
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, top)
);
CREATE INDEX IF NOT EXISTS idx_btfd_top_date ON board_trend_forward_daily(top, trade_date);

-- 盘中个股点评 + 双星评级 (按交易日+code+reviewer, 多 agent 共用, collect/logic 永不写)
CREATE TABLE IF NOT EXISTS intraday_review (
    trade_date   TEXT NOT NULL,   -- YYYY-MM-DD
    code         TEXT NOT NULL,   -- 裸 6 位
    name         TEXT,
    sources      TEXT,            -- 's3,trend' 逗号串
    logic_stars  INTEGER,         -- 1~5 逻辑硬度(基本面/题材)
    action_stars INTEGER,         -- 1~5 今日可操作性(技术/资金)
    summary      TEXT,            -- agent 点评(一段话)
    reviewed_at  TEXT,            -- 'YYYY-MM-DD HH:MM:SS'
    reviewer     TEXT NOT NULL DEFAULT 'bot11',  -- 点评 agent, e.g. bot11/bot7
    PRIMARY KEY (trade_date, code, reviewer)
);
CREATE INDEX IF NOT EXISTS idx_irv_date ON intraday_review(trade_date);

-- 趋势主线板块级深度点评 + 持续性星级 (按交易日+board_code+reviewer, 多 agent 共用, collect/logic 永不写)
CREATE TABLE IF NOT EXISTS intraday_board_review (
    trade_date         TEXT NOT NULL,   -- YYYY-MM-DD
    board_code         TEXT NOT NULL,
    board_name         TEXT,
    summary            TEXT,            -- 板块深度分析正文(成段, 多角度推演)
    logic_stars        INTEGER,         -- 1~5 板块逻辑硬度(产业/政策/景气)
    continuation_stars INTEGER,         -- 1~5 持续性(主线还能不能跟)
    reviewed_at        TEXT,            -- 'YYYY-MM-DD HH:MM:SS'
    reviewer           TEXT NOT NULL DEFAULT 'bot11',  -- 点评 agent, e.g. bot11/bot7
    PRIMARY KEY (trade_date, board_code, reviewer)
);
CREATE INDEX IF NOT EXISTS idx_ibr_date ON intraday_board_review(trade_date);

-- 每日推荐池: bot7/bot11 经 Stage 1 (intraday_review) + Stage 2 (SCOUT/DIG/CHALLENGE 深研) 后各推 0-3 只
-- 主键 (trade_date, slot, reviewer, rank): 同 slot 内最多 3 行, 跨 slot 自然留快照
CREATE TABLE IF NOT EXISTS daily_pick (
    trade_date          TEXT NOT NULL,           -- YYYY-MM-DD
    slot                TEXT NOT NULL,           -- 'premarket' | 'intraday_am' | 'intraday_pm'
    reviewer            TEXT NOT NULL,           -- 'bot7' | 'bot11'
    rank                INTEGER NOT NULL,        -- 1..3
    code                TEXT NOT NULL,           -- 裸 6 位
    name                TEXT,
    sources             TEXT,                    -- 's3,trend' 自 intraday_review 复制
    one_liner           TEXT NOT NULL,           -- 10-60 字抢眼逻辑
    logic_stars         INTEGER NOT NULL,        -- 1-5
    action_stars        INTEGER NOT NULL,        -- 1-5
    entry_low           REAL,
    entry_high          REAL,
    stop_loss           REAL,
    position_tier       TEXT,                    -- '试仓' | '观望' | '重仓'
    deep_research_json  TEXT NOT NULL,           -- {scout,dig,challenge,verdict}
    research_session_id TEXT,                    -- 允许 NULL, wrapper 在 chat 结束后回填
    picked_at           TEXT NOT NULL,           -- 'YYYY-MM-DD HH:MM:SS'
    PRIMARY KEY (trade_date, slot, reviewer, rank)
);
CREATE INDEX IF NOT EXISTS idx_dp_date      ON daily_pick(trade_date);
CREATE INDEX IF NOT EXISTS idx_dp_date_code ON daily_pick(trade_date, code);

-- 板块"真"龙头 EOD 评分 (替代东财 leading_code 一日游, 强调持续性+抗跌+量能)
-- 算法见 scout/board_leader.py, 与 board_trend 同窗口 (近 20 交易日).
CREATE TABLE IF NOT EXISTS board_leader_daily (
    trade_date    TEXT NOT NULL,        -- YYYYMMDD
    board_code    TEXT NOT NULL,
    rank          INTEGER NOT NULL,     -- 1=龙头, 2/3=接力位
    ts_code       TEXT NOT NULL,        -- 带后缀
    name          TEXT,
    leader_score  REAL,                 -- 0~100 综合分
    str_factor    REAL,                 -- 相对强度因子 (双窗口加权 0.4*s20+0.6*s60)
    per_factor    REAL,                 -- 持续性因子 (60日跑赢板块天数占比)
    vol_factor    REAL,                 -- 量能放大因子 (个股 5/20 日额比)
    def_factor    REAL,                 -- 抗跌因子 (60日板块下跌日的超额)
    stock_ret20   REAL,                 -- 个股 20 日累计涨幅 (诊断用)
    stock_ret60   REAL,                 -- 个股 60 日累计涨幅 (诊断用)
    board_ret20   REAL,                 -- 板块 60 日累计涨幅 (诊断用, 字段名沿用兼容)
    PRIMARY KEY (trade_date, board_code, rank)
);
CREATE INDEX IF NOT EXISTS idx_bl_date ON board_leader_daily(trade_date);

CREATE TABLE IF NOT EXISTS board_moneyflow_daily (
    trade_date  TEXT NOT NULL,
    sw_l1_code  TEXT NOT NULL,
    sw_l1_name  TEXT,
    net_1d      REAL,    -- 当日主力净流入(万元)
    net_5d      REAL,    -- 近5日累计(万元, 含当日)
    member_n    INTEGER, -- 当日参与汇总成分股数
    top_in      TEXT,    -- 当日净流入Top3个股 JSON [{code,name,net}]
    top_out     TEXT,    -- 当日净流出Top3个股 JSON
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, sw_l1_code)
);
CREATE INDEX IF NOT EXISTS idx_bmf_date ON board_moneyflow_daily(trade_date);

CREATE TABLE IF NOT EXISTS market_moneyflow_daily (
    trade_date  TEXT PRIMARY KEY,
    net_1d      REAL,    -- 全市场当日主力净流入(万元)
    net_5d      REAL,    -- 近5日累计(万元, 含当日)
    in_n        INTEGER, -- 净流入行业数
    out_n       INTEGER, -- 净流出行业数
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 板块对标 ETF (EOD, 仅 Top10 趋势主线板块, 详见 scout/board_etf.py)
CREATE TABLE IF NOT EXISTS board_etf_daily (
    trade_date    TEXT NOT NULL,
    board_code    TEXT NOT NULL,
    rank          INTEGER NOT NULL,        -- 1/2/3, 按 hit_weight 降序
    fund_code     TEXT NOT NULL,
    fund_name     TEXT,
    hit_weight    REAL NOT NULL,           -- 命中 Top10 的权重总和 (% NAV)
    hit_count     INTEGER,                 -- 命中个股数 (1-10)
    report_date   TEXT,                    -- 持仓报告日期 YYYY-MM-DD
    fund_scale    REAL,                    -- 基金规模 (亿元)
    PRIMARY KEY (trade_date, board_code, rank)
);
CREATE INDEX IF NOT EXISTS idx_be_date ON board_etf_daily(trade_date);

-- 板块对标主动基金 (EOD, 仅 Top10 趋势主线; 季报前十重仓 ∩ 板块龙头; 详见 scout/board_etf.py)
CREATE TABLE IF NOT EXISTS board_active_fund_daily (
    trade_date    TEXT NOT NULL,
    board_code    TEXT NOT NULL,
    rank          INTEGER NOT NULL,        -- 1..N, 按 hit_weight 降序
    fund_code     TEXT NOT NULL,
    fund_name     TEXT,
    hit_weight    REAL NOT NULL,           -- 命中龙头权重总和 (% NAV)
    hit_count     INTEGER,                 -- 命中龙头数 (>=2)
    report_date   TEXT,                    -- 持仓报告日期 YYYY-MM-DD
    fund_scale    REAL,                    -- 基金规模 (亿元)
    PRIMARY KEY (trade_date, board_code, rank)
);
CREATE INDEX IF NOT EXISTS idx_baf_date ON board_active_fund_daily(trade_date);

-- (已退役 2026-06) 个股→基金 持仓反查表 stock_etf_holdings 已弃用,
-- 改由正向表 fund_top_holdings 统一供数; 旧表由 _migrate() 的 DROP 清理.

-- 基金→前十大重仓股 正向持仓表 (来自 ttjj fund_top_holdings; fund_holdings_sync.py 季度全量).
-- 与反查表 stock_etf_holdings 互补: 正向按基金抓全前十大, 覆盖白酒/消费等不上雷达的板块.
-- 仅存 6 位裸 A 股(港股已过滤). 主动基金匹配 (board_active_fund_daily) 读这张表.
CREATE TABLE IF NOT EXISTS fund_top_holdings (
    fund_code    TEXT NOT NULL,
    stock_code   TEXT NOT NULL,           -- 6 位裸 A 股 (已过滤港股)
    stock_name   TEXT,
    weight       REAL NOT NULL,           -- 占基金净值比例 (%)
    holding_rank INTEGER,                 -- 1..10, 由重仓股列表顺序赋
    report_date  TEXT,                    -- 持仓报告日期 YYYY-MM-DD
    cached_at    TEXT NOT NULL,           -- ISO8601 拉取时间
    PRIMARY KEY (fund_code, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_fth_stock ON fund_top_holdings(stock_code);

-- 基金元数据本地缓存 (来自 ttjj fund_basic_info)
CREATE TABLE IF NOT EXISTS etf_meta (
    fund_code     TEXT PRIMARY KEY,
    fund_name     TEXT,
    fund_type     TEXT,
    is_strict_etf INTEGER NOT NULL,        -- 0/1: 申购=场内 AND 是否指数=1
    fund_scale    REAL,
    cached_at     TEXT NOT NULL
);

-- 用户跟买持仓 (dashboard「我的持仓」面板): 只存成本价, 盈亏按百分比算.
-- code 为主键 → 同股重复添加=upsert(改成本价). 现价由 collect.py 每帧写
-- strategy='hold' 的 intraday_candidate_live 行提供, server 读不到时回落 daily 收盘价.
CREATE TABLE IF NOT EXISTS follow_holding (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,      -- 裸 6 位代码(允许同 code 多笔, 每次跟买独立成笔)
    name        TEXT,               -- 股票名(加入时快照)
    cost_price  REAL NOT NULL,      -- 成本价
    added_at    TEXT NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
    owner_id    INTEGER             -- 归属用户 scout_user.id (老库迁移补列, 全新库直接带)
);
CREATE INDEX IF NOT EXISTS idx_fh_code ON follow_holding(code);

-- 已清仓交易流水 (dashboard「已清仓」历史区 + 跟随胜率统计).
-- 与 follow_holding(开仓持仓) 分离: 卖出=从 follow_holding 删行 + 这里插一笔.
-- 自增 id 主键, 允许同一 code 多笔(买回再卖). 胜=sell_price>cost_price.
CREATE TABLE IF NOT EXISTS follow_trade_closed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,        -- 裸 6 位代码
    name        TEXT,                 -- 股票名(从持仓带过来)
    cost_price  REAL NOT NULL,        -- 买入成本价
    sell_price  REAL NOT NULL,        -- 卖出价
    added_at    TEXT,                 -- 原买入加入时间(从 follow_holding 带过来)
    sold_at     TEXT NOT NULL,        -- 卖出时间 'YYYY-MM-DD HH:MM:SS'
    owner_id    INTEGER               -- 归属用户 scout_user.id
);
CREATE INDEX IF NOT EXISTS idx_ftc_sold ON follow_trade_closed(sold_at);

-- 新兴主线信号每日汇总 (由 emerging_mainline_detector 写入, server/html 读取展示)
CREATE TABLE IF NOT EXISTS emerging_signal_daily (
    trade_date   TEXT,
    board_code   TEXT,
    board_name   TEXT,
    n_signals    INTEGER,
    fired        TEXT,
    persist_days INTEGER,
    is_whitelist INTEGER,
    regime_code  TEXT,
    actionable   INTEGER,
    reason       TEXT,
    PRIMARY KEY (trade_date, board_code)
);

-- 账号系统: 用户表. password_hash 格式 pbkdf2_sha256$<迭代>$<salt_b64>$<hash_b64>.
CREATE TABLE IF NOT EXISTS scout_user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 账号系统: 会话表. token 由 secrets.token_urlsafe(32) 生成, 7 天过期, 支持主动失效.
CREATE TABLE IF NOT EXISTS scout_session (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scout_session_user ON scout_session(user_id);

CREATE TABLE IF NOT EXISTS lof_universe (
    lof_code     TEXT PRIMARY KEY,            -- ts_code 如 501225.SH / 161116.SZ
    code6        TEXT,                         -- 裸6位(供 ttjj 实时/费率用)
    name         TEXT,
    category     TEXT,                         -- 'QDII' | '商品' | '其他'
    sub_theme    TEXT,                         -- '纳指'/'海外科技'/'原油'/'黄金'...
    in_universe  INTEGER NOT NULL DEFAULT 1,   -- 人工白名单开关(0=排除)
    source       TEXT,                         -- 'auto' | 'manual'
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lof_nav (
    lof_code   TEXT NOT NULL,
    nav_date   TEXT NOT NULL,                  -- 净值日期 YYYY-MM-DD
    unit_nav   REAL NOT NULL,                  -- 单位净值(申赎口径)
    ann_date   TEXT,                           -- 公告日期 YYYY-MM-DD
    cached_at  TEXT NOT NULL,
    PRIMARY KEY (lof_code, nav_date)
);

CREATE TABLE IF NOT EXISTS lof_rate (
    lof_code              TEXT PRIMARY KEY,
    purchase_fee_pct      REAL,                -- 申购费率(优惠后首档) %
    redeem_fee_pct        REAL,                -- 赎回费率 %(无数据→默认1.5)
    redeem_fee_source     TEXT,                -- 'ttjj' | 'default'
    max_purchase_amt      REAL,                -- 最大申购限额 元
    purchase_confirm_days INTEGER,             -- 申购确认 T+N
    redeem_confirm_days   INTEGER,             -- 赎回确认 T+N
    redeem_arrival_days   INTEGER,             -- 赎回到账 T+N
    purchasable           INTEGER,             -- 是否可申购 1/0(由申购状态判定)
    redeemable            INTEGER,             -- 是否可赎回 1/0(由赎回状态判定)
    purchase_status       TEXT,                -- 申购状态原文(开放申购/暂停申购/限大额...)
    redeem_status         TEXT,                -- 赎回状态原文(开放赎回/暂停赎回...)
    cached_at             TEXT NOT NULL
);

-- 盘中折溢价快照: 每日每只 LOF 仅一行, 每帧 INSERT OR REPLACE 滚动刷新到最新
-- (同 intraday_trigger_log 的"每日一行滚动"模式, 非逐帧留存); snapshot_time=最后刷新时间.
CREATE TABLE IF NOT EXISTS lof_arbitrage_live (
    trade_date        TEXT NOT NULL,
    lof_code          TEXT NOT NULL,
    snapshot_time     TEXT NOT NULL,           -- 最后刷新时间 'YYYY-MM-DD HH:MM:SS'
    price             REAL,                    -- 场内实时价
    nav               REAL,                    -- 基准净值(最新已知)
    nav_date          TEXT,                    -- 基准净值日期
    premium_pct       REAL,                    -- 折溢价率 %(正=溢价,负=折价)
    net_premium_arb   REAL,                    -- 溢价方向净套利 %
    net_discount_arb  REAL,                    -- 折价方向净套利 %
    actionable        TEXT,                    -- 'opp' | 'watch' | 'none'
    PRIMARY KEY (trade_date, lof_code)
);
CREATE INDEX IF NOT EXISTS idx_lof_arb_date ON lof_arbitrage_live(trade_date);

CREATE TABLE IF NOT EXISTS margin_security_daily (
    trade_date TEXT NOT NULL,
    ts_code    TEXT NOT NULL,
    rzye   REAL,   -- 融资余额
    rqye   REAL,   -- 融券余额
    rzrqye REAL,   -- 融资融券余额
    rzmre  REAL,   -- 融资买入额
    rzche  REAL,   -- 融资偿还额
    rqyl   REAL,   -- 融券余量
    rqmcl  REAL,   -- 融券卖出量
    rqchl  REAL,   -- 融券偿还量
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_msd_date ON margin_security_daily(trade_date);
-- 按 ts_code 驱动的板块成分聚合 (kline 融资曲线): 否则按日期全扫大表
CREATE INDEX IF NOT EXISTS idx_msd_ts ON margin_security_daily(ts_code, trade_date);

CREATE TABLE IF NOT EXISTS margin_index_daily (
    trade_date TEXT NOT NULL,
    index_name TEXT NOT NULL,
    etf_count  INTEGER,
    rzye_sum   REAL,
    rqye_sum   REAL,
    rz_net_buy REAL,
    rzye_chg1  REAL,
    rzye_chg5  REAL,
    top_etf    TEXT,
    PRIMARY KEY (trade_date, index_name)
);
CREATE INDEX IF NOT EXISTS idx_mid_date ON margin_index_daily(trade_date);

CREATE TABLE IF NOT EXISTS margin_board_daily (
    trade_date  TEXT NOT NULL,
    board_code  TEXT NOT NULL,
    board_name  TEXT,
    rank        INTEGER,
    member_n    INTEGER,
    margin_n    INTEGER,
    rzye_sum    REAL,
    rqye_sum    REAL,
    rz_net_buy  REAL,
    rzye_chg1   REAL,
    rzye_chg5   REAL,
    total_mv    REAL,   -- 板块全部成分股总市值合计 (元)
    circ_mv     REAL,   -- 板块全部成分股流通市值合计 (元)
    top_stocks  TEXT,
    PRIMARY KEY (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_mbd_date ON margin_board_daily(trade_date);

CREATE TABLE IF NOT EXISTS earnings_resonance_daily (
    trade_date                 TEXT NOT NULL,
    ts_code                    TEXT NOT NULL,
    name                       TEXT,
    gap_pct                    REAL,
    day_ret                    REAL,
    vol_ratio                  REAL,
    open_px                    REAL,
    high_px                    REAL,
    low_px                     REAL,
    close_px                   REAL,
    pre_close                  REAL,
    shape_tag                  TEXT,
    primary_board_code         TEXT,
    primary_board_name         TEXT,
    primary_board_rank         INTEGER,
    primary_board_trend_score  REAL,
    other_boards               TEXT,
    computed_at                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_erd_date_board
    ON earnings_resonance_daily(trade_date, primary_board_rank);

CREATE TABLE IF NOT EXISTS earnings_events (
    ts_code         TEXT NOT NULL,
    ann_date        TEXT NOT NULL,       -- YYYYMMDD 公告日
    source          TEXT NOT NULL,       -- 'forecast' | 'express'
    end_date        TEXT,                -- 报告期期末 YYYYMMDD
    event_type      TEXT,                -- forecast: 预增/预减/扭亏/首亏/略增/略减/续亏/续盈/不确定; express: NULL
    p_change_min    REAL,                -- forecast 净利润变动% 下限; express 用 yoy_net_profit 填
    p_change_max    REAL,                -- forecast 上限; express NULL
    net_profit_min  REAL,                -- 万元
    net_profit_max  REAL,                -- 万元
    yoy_sales       REAL,                -- express only, 营收同比%
    summary         TEXT,                -- forecast.summary 或 express.perf_summary
    change_reason   TEXT,                -- forecast only
    is_hard_hit     INTEGER NOT NULL,    -- 0/1 硬标命中(落库时预算)
    fetched_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, ann_date, source)
);
CREATE INDEX IF NOT EXISTS idx_ee_ann  ON earnings_events(ann_date);
CREATE INDEX IF NOT EXISTS idx_ee_hard ON earnings_events(is_hard_hit, ann_date);

-- 周期股估值带 · 机械层每日带子 (scout/valuation.py 写, server/页面读)
-- 设计: docs/superpowers/specs/2026-07-10-cyclical-valuation-band-design.md
CREATE TABLE IF NOT EXISTS valuation_band_daily (
    trade_date      TEXT NOT NULL,      -- YYYYMMDD
    ts_code         TEXT NOT NULL,      -- 带后缀 601899.SH
    driver_center   REAL,               -- 商品价中枢(生效值)
    driver_low      REAL,
    driver_high     REAL,
    driver_source   TEXT,               -- 'auto' | 'agent' | 'manual'
    profit_year_lo  REAL,               -- 当年年化归母净利区间(亿)
    profit_year_hi  REAL,
    eps_lo          REAL,               -- 年化 EPS 区间(元/股)
    eps_hi          REAL,
    earnings_source TEXT,               -- 未披露季主导来源 'rule'|'agent'|'manual'
    pe_lo           REAL,               -- 生效 PE 带
    pe_hi           REAL,
    pe_source       TEXT,               -- 'quantile' | 'agent'
    price_lo        REAL,               -- 价格带 = pe_lo*eps_lo / pe_hi*eps_hi
    price_hi        REAL,
    close           REAL,
    band_pos        REAL,               -- (close-lo)/(hi-lo), 可 <0 或 >1
    st_center       REAL,               -- 短期做T带中枢(生效值; 二期)
    st_lo           REAL,               -- 短期带下沿 = st_center*(1-半宽)
    st_hi           REAL,               -- 短期带上沿
    st_pos          REAL,               -- (close-st_lo)/(st_hi-st_lo)
    st_source       TEXT,               -- 'auto' | 'agent'
    meta_json       TEXT,               -- 输入快照+clamp_ctx+回归旁证+stale标记
    computed_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_vbd_code ON valuation_band_daily(ts_code, trade_date);

-- 周期股估值带 · 季度盈利估计明细 (动态修正载体; 历史季 actual 也存, 供追溯)
CREATE TABLE IF NOT EXISTS valuation_quarter_est (
    ts_code    TEXT NOT NULL,
    quarter    TEXT NOT NULL,           -- '2026Q3'
    status     TEXT NOT NULL,           -- 'estimated' | 'forecast' | 'actual'
    profit_lo  REAL,                    -- 单季归母净利(亿); actual 时 lo=hi
    profit_hi  REAL,
    basis_json TEXT,                    -- 依据: 参照季/金价因子g/ann_date/method 等
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, quarter)
);

-- 周期股估值带 · agent(小奶龙) 锚点建议 (valuation_anchor_writer.py 写;
-- 钳制后在保鲜期内自动生效, 过期回落机械值; 优先级 手动 > agent > 机械)
CREATE TABLE IF NOT EXISTS valuation_agent_view (
    run_at          TEXT NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
    ts_code         TEXT NOT NULL,
    reviewer        TEXT NOT NULL DEFAULT 'bot11',
    driver_center   REAL,               -- 建议商品价锚 (NULL=不修正)
    driver_low      REAL,
    driver_high     REAL,
    profit_adj_json TEXT,               -- {"2026Q3":[lo,hi]} 建议单季区间(亿), NULL=不修正
    pe_lo_adj       REAL,
    pe_hi_adj       REAL,
    st_center_adj   REAL,               -- 短期带中枢修正(钳 机械±8%; 二期)
    st_width_pct    REAL,               -- 短期带半宽(钳 [0.03,0.15]; 二期)
    confidence      REAL,               -- 0~1
    rationale       TEXT,               -- 理由全文
    sources_json    TEXT,               -- 引用来源列表
    clamped_json    TEXT,               -- 被 sanity 钳制的字段记录
    valid_until     TEXT,               -- YYYYMMDD 含当日(生效截止)
    trigger         TEXT,               -- weekly|earnings|driver_shift|band_breach|manual|daily_st
    PRIMARY KEY (run_at, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_vav_code ON valuation_agent_view(ts_code, run_at);

-- 周期股估值带 · 加股建模草案状态机 (server /api/valuation/target/* 写;
-- bot 经 valuation_anchor_writer.py --onboard 写草案; 三期设计:
-- docs/superpowers/specs/2026-07-11-valuation-phase3-design.md)
CREATE TABLE IF NOT EXISTS valuation_target_draft (
    ts_code    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,      -- 真名(stock_names)
    status     TEXT NOT NULL DEFAULT 'drafting',
                                   -- drafting|draft_ready|unsupported|failed|enabled|dismissed
    draft_json TEXT,               -- 小奶龙草案 {applicable,model,driver,research_industries,.openclaw,rationale}
    error      TEXT,               -- failed 时的原因
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 估值带 · 一致预期净利本地缓存 (research-mcp get_stock_consensus_growth TTM日度序列;
-- valuation.py consensus_pe 模型每日 upsert; 远程超时用缓存出带, 多模型扩展设计:
-- docs/superpowers/specs/2026-07-11-multi-model-valuation-band-design.md)
CREATE TABLE IF NOT EXISTS consensus_profit_daily (
    ts_code    TEXT NOT NULL,           -- 带后缀 603986.SH
    trade_date TEXT NOT NULL,           -- YYYYMMDD
    profit_yi  REAL,                    -- 当日TTM一致预期归母净利(亿)
    PRIMARY KEY (ts_code, trade_date)
);
"""


def conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.row_factory = sqlite3.Row
    return c


def init_schema(db_path: str = DB_PATH) -> None:
    c = conn(db_path)
    c.executescript(SCHEMA)
    _migrate(c)
    c.commit()
    c.close()


def backfill_active_equity(db_path: str = DB_PATH) -> None:
    """按已存的 fund_type 回填 etf_meta.is_active_equity(零 PIT 调用, 幂等)."""
    c = conn(db_path)
    ph = ",".join("?" * len(ACTIVE_EQUITY_TYPES))
    c.execute(f"UPDATE etf_meta SET is_active_equity = "
              f"CASE WHEN is_strict_etf = 0 AND fund_type IN ({ph}) THEN 1 ELSE 0 END",
              ACTIVE_EQUITY_TYPES)
    c.commit()
    c.close()


def _migrate(c) -> None:
    """幂等补列: 老表用 CREATE IF NOT EXISTS 不会自动加新列, 这里 ALTER 补齐."""
    # 反向持仓表已退役 (2026-06): 改由正向表 fund_top_holdings 统一供数. 幂等清理旧表.
    c.execute("DROP TABLE IF EXISTS stock_etf_holdings")
    # LOF 套利费率表补申赎状态列(2026-06): 由 fund_basic_info 申购/赎回状态判定门禁
    lr_cols = {r[1] for r in c.execute("PRAGMA table_info(lof_rate)")}
    for col, typ in (("redeemable", "INTEGER"), ("purchase_status", "TEXT"),
                     ("redeem_status", "TEXT")):
        if col not in lr_cols:
            c.execute(f"ALTER TABLE lof_rate ADD COLUMN {col} {typ}")
    bl_cols = {r[1] for r in c.execute("PRAGMA table_info(board_leader_daily)")}
    if "stock_ret60" not in bl_cols:
        c.execute("ALTER TABLE board_leader_daily ADD COLUMN stock_ret60 REAL")
    btd_cols = {r[1] for r in c.execute("PRAGMA table_info(board_trend_daily)")}
    for col in ("ret5", "amt_peak_ratio", "dist_ma20"):
        if col not in btd_cols:
            c.execute(f"ALTER TABLE board_trend_daily ADD COLUMN {col} REAL")
    # 融资融券板块表补市值列(2026-06): 融资余额/总市值、/流通市值 渗透率指标
    mbd_cols = {r[1] for r in c.execute("PRAGMA table_info(margin_board_daily)")}
    for col in ("total_mv", "circ_mv"):
        if col not in mbd_cols:
            c.execute(f"ALTER TABLE margin_board_daily ADD COLUMN {col} REAL")
    cols = {r[1] for r in c.execute("PRAGMA table_info(intraday_board)")}
    for col, typ in (("limit_up_count", "INTEGER"), ("lead_code", "TEXT"),
                     ("lead_name", "TEXT"), ("lead_pct", "REAL"), ("lead_price", "REAL"),
                     ("limit_down_count", "INTEGER"), ("amount_yi", "REAL")):
        if col not in cols:
            c.execute(f"ALTER TABLE intraday_board ADD COLUMN {col} {typ}")

    snap_cols = {r[1] for r in c.execute("PRAGMA table_info(intraday_snapshot)")}
    for col, typ in (("szcz_pct", "REAL"), ("star50_pct", "REAL"),
                     ("bz50_pct", "REAL"), ("csi300_pct", "REAL")):
        if col not in snap_cols:
            c.execute(f"ALTER TABLE intraday_snapshot ADD COLUMN {col} {typ}")

    its_cols = {r[1] for r in c.execute("PRAGMA table_info(intraday_snapshot)")}
    for col in ("csi1000_pct", "csi2000_pct"):
        if col not in its_cols:
            c.execute(f"ALTER TABLE intraday_snapshot ADD COLUMN {col} REAL")

    member_cols = {r[1] for r in c.execute("PRAGMA table_info(intraday_board_members)")}
    if "all_json" not in member_cols:
        c.execute("ALTER TABLE intraday_board_members ADD COLUMN all_json TEXT")

    _migrate_reviewer(c)

    brv_cols = {r[1] for r in c.execute("PRAGMA table_info(intraday_board_review)")}
    if "logic_stars" not in brv_cols:
        c.execute("ALTER TABLE intraday_board_review ADD COLUMN logic_stars INTEGER")

    em_cols = {r[1] for r in c.execute("PRAGMA table_info(etf_meta)")}
    if "company" not in em_cols:
        c.execute("ALTER TABLE etf_meta ADD COLUMN company TEXT")
    if "is_active_equity" not in em_cols:
        c.execute("ALTER TABLE etf_meta ADD COLUMN is_active_equity INTEGER NOT NULL DEFAULT 0")
        # 加列当次一次性回填既有行(按 fund_type, 零 PIT)
        ph = ",".join("?" * len(ACTIVE_EQUITY_TYPES))
        c.execute(f"UPDATE etf_meta SET is_active_equity = 1 "
                  f"WHERE is_strict_etf = 0 AND fund_type IN ({ph})", ACTIVE_EQUITY_TYPES)

    _migrate_holding_id(c)

    # 账号系统: 老的 follow_holding / follow_trade_closed 无 owner_id, 幂等补列.
    # 索引在补列后建(放 SCHEMA 里会因 executescript 早于 ALTER 撞"无 owner_id 列").
    fh_cols = {r[1] for r in c.execute("PRAGMA table_info(follow_holding)")}
    if "owner_id" not in fh_cols:
        c.execute("ALTER TABLE follow_holding ADD COLUMN owner_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fh_owner ON follow_holding(owner_id)")
    ftc_cols = {r[1] for r in c.execute("PRAGMA table_info(follow_trade_closed)")}
    if "owner_id" not in ftc_cols:
        c.execute("ALTER TABLE follow_trade_closed ADD COLUMN owner_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ftc_owner ON follow_trade_closed(owner_id)")

    # v2 业绩共振事件驱动: 给 earnings_resonance_daily 补 10 个新字段
    erd_cols = {r[1] for r in c.execute("PRAGMA table_info(earnings_resonance_daily)")}
    for col, typ in (
        ("latest_event_ann_date", "TEXT"),
        ("latest_event_source",   "TEXT"),
        ("latest_event_type",     "TEXT"),
        ("latest_p_change_min",   "REAL"),
        ("latest_p_change_max",   "REAL"),
        ("latest_summary",        "TEXT"),
        ("days_since_event",      "INTEGER"),
        ("t1_open_ret",           "REAL"),
        ("t1_close_ret",          "REAL"),
        ("cum_ret_since_event",   "REAL"),
    ):
        if col not in erd_cols:
            c.execute(f"ALTER TABLE earnings_resonance_daily ADD COLUMN {col} {typ}")

    # 2026-07-08 业绩共振盘中 rt 快照: intraday_candidate_live 加 low/high/pre_close 三列
    icl_cols = {r[1] for r in c.execute("PRAGMA table_info(intraday_candidate_live)")}
    for col, typ in [("low_px", "REAL"), ("high_px", "REAL"), ("pre_close_px", "REAL")]:
        if col not in icl_cols:
            c.execute(f"ALTER TABLE intraday_candidate_live ADD COLUMN {col} {typ}")

    # 2026-07-10 估值带二期: 短期做T带(st) 列
    vbd_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_band_daily)")}
    for col, typ in (("st_center", "REAL"), ("st_lo", "REAL"), ("st_hi", "REAL"),
                     ("st_pos", "REAL"), ("st_source", "TEXT")):
        if col not in vbd_cols:
            c.execute(f"ALTER TABLE valuation_band_daily ADD COLUMN {col} {typ}")
    vav_cols = {r[1] for r in c.execute("PRAGMA table_info(valuation_agent_view)")}
    for col in ("st_center_adj", "st_width_pct"):
        if col not in vav_cols:
            c.execute(f"ALTER TABLE valuation_agent_view ADD COLUMN {col} REAL")


def _migrate_holding_id(c) -> None:
    """follow_holding 主键 code → 自增 id (每次跟买独立成笔, 同 code 允许多行).
    老表 PK 是 code, SQLite 改 PK 必须 rebuild; 现有持仓行各分得一个新 id, 数据不丢.
    幂等: 已有 id 列则跳过; 表不存在(全新库已由 SCHEMA 建好新表)则跳过."""
    info = list(c.execute("PRAGMA table_info(follow_holding)"))
    if not info:
        return  # 全新库: SCHEMA 已建含 id 的新表
    if any(r[1] == "id" for r in info):
        return  # 已迁移
    # DROP IF EXISTS: 清掉上次 rebuild 崩溃残留的孤儿表再重来
    c.execute("DROP TABLE IF EXISTS follow_holding_new")
    c.execute(
        "CREATE TABLE follow_holding_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT, "
        "cost_price REAL NOT NULL, added_at TEXT NOT NULL)")
    c.execute("INSERT INTO follow_holding_new(code,name,cost_price,added_at) "
              "SELECT code,name,cost_price,added_at FROM follow_holding")
    c.execute("DROP TABLE follow_holding")
    c.execute("ALTER TABLE follow_holding_new RENAME TO follow_holding")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fh_code ON follow_holding(code)")


def _migrate_reviewer(c) -> None:
    """给两张点评表补 reviewer 维度. 老表 PK 不含 reviewer, SQLite 改 PK 必须 rebuild;
    旧数据 reviewer 补 'bot11'. 幂等: 已有 reviewer 列则跳过."""
    specs = [
        ("intraday_review",
         "trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, sources TEXT, "
         "logic_stars INTEGER, action_stars INTEGER, summary TEXT, reviewed_at TEXT, "
         "reviewer TEXT NOT NULL DEFAULT 'bot11', PRIMARY KEY (trade_date, code, reviewer)",
         "trade_date,code,name,sources,logic_stars,action_stars,summary,reviewed_at",
         "idx_irv_date"),
        ("intraday_board_review",
         "trade_date TEXT NOT NULL, board_code TEXT NOT NULL, board_name TEXT, summary TEXT, "
         "continuation_stars INTEGER, reviewed_at TEXT, "
         "reviewer TEXT NOT NULL DEFAULT 'bot11', PRIMARY KEY (trade_date, board_code, reviewer)",
         "trade_date,board_code,board_name,summary,continuation_stars,reviewed_at",
         "idx_ibr_date"),
    ]
    for tbl, new_def, old_cols, idx in specs:
        info = list(c.execute(f"PRAGMA table_info({tbl})"))
        if not info:
            continue  # 表不存在: 全新库已由 SCHEMA(含 reviewer)建好, 无需迁移
        if any(r[1] == "reviewer" for r in info):
            continue
        # DROP IF EXISTS: 若上次 rebuild 在 CREATE _new 后崩溃, 清掉残留孤儿表再重来
        c.execute(f"DROP TABLE IF EXISTS {tbl}_new")
        c.execute(f"CREATE TABLE {tbl}_new ({new_def})")
        c.execute(f"INSERT INTO {tbl}_new ({old_cols},reviewer) "
                  f"SELECT {old_cols},'bot11' FROM {tbl}")
        c.execute(f"DROP TABLE {tbl}")
        c.execute(f"ALTER TABLE {tbl}_new RENAME TO {tbl}")
        c.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {tbl}(trade_date)")


# 2026-06-02 按结构择时: S4 回踩已下线(不再生成候选), 从 review/anchor 列表移除。
# (历史战绩 perf.py 用自己的列表, 不受影响; S1/S5/S6 仍在=observe)
STRATEGIES = ["s1", "s2", "s3", "s5", "s6", "s7", "s8", "s9"]


def anchor_date(c, today: str, strategies=STRATEGIES):
    """昨日候选锚点 = 全策略候选表中 date < today 的最大日期 (YYYY-MM-DD).

    盘中盯盘的是"上一个收盘后选出的"候选(昨日), 不是各表各自的 MAX
    (各策略并非每天都出票, 直接 per-table MAX 会把 s1/s2/s5 的前天候选混进来).
    """
    dates = []
    for s in strategies:
        try:
            d = c.execute(
                f"SELECT MAX(date) FROM {s}_candidates WHERE date < ?", (today,)
            ).fetchone()[0]
        except Exception:
            d = None
        if d:
            dates.append(d)
    return max(dates) if dates else None


def to_suffix(code: str) -> str:
    """6 位代码 -> 带后缀. 已带后缀原样返回."""
    code = str(code).strip()
    if "." in code:
        return code
    code = code.zfill(6)
    if code.startswith(("6", "9")):
        return code + ".SH"
    if code.startswith(("0", "3", "2")):
        return code + ".SZ"
    if code.startswith(("4", "8")):
        return code + ".BJ"
    return code + ".SZ"


def bare(code: str) -> str:
    """带后缀代码 -> 裸 6 位(补零)。'600519.SH'->'600519', '1'->'000001'."""
    return str(code).strip().split(".")[0].zfill(6)


if __name__ == "__main__":
    init_schema()
    print(f"盘中表已初始化 @ {DB_PATH}")
