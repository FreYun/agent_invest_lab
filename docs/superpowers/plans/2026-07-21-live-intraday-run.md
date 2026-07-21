# 盘中实时跑（Live Intraday Run）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把看板上 ~45 个「合格」历史回测 run 派生成每日盘中实盘 run，每交易日 14:00 让 bot 用实时信息决策、按当日收盘 NAV 成交，历史回测数据物理隔离、零污染。

**Architecture:** 新增 `world/live/` 编排层，复用现有 world 引擎与 fund-portfolio-mcp 交易系统，**不改交易逻辑、不改数据库 schema**。每个合格 run 派生一个新 `run_id`（继承持仓+记忆），历史 run 冻结只读。系统层拆两阶段：Phase 1（14:00 决策，bot 挂 pending 单、冻结现金、跳过收盘核算）+ Phase 2（傍晚 NAV 就绪后纯系统结算+落净值快照）。防泄漏靠 PIT 注入日期指向真实今天。

**Tech Stack:** TypeScript（world 引擎，Node ≥22.6，`node --experimental-strip-types`）、Python 3.12（seeding + 编排 + 测试，pytest）、SQLite（fund.db）、systemd user timers（定时触发）。

## Global Constraints

- **回复语言：全程中文**（代码/命令/标识符除外）——CLAUDE.md 硬性要求。
- **Python 解释器：`/usr/bin/python3.12`**（裸 `python3` 会解析到缺 mcp 包的 uv 3.11）。所有 Python 脚本 shebang 用 `#!/usr/bin/env python3.12`，测试用 `/usr/bin/python3.12 -m pytest`。
- **不改数据库 schema**：不动主键、不加表、不建 40+ 个 db 文件。per-run 现金隔离已由 `server.py:_replay_fund_account_state(run_id=...)` 现成保证。
- **不改交易逻辑**：`portfolio_place_buy_order` 已原生支持盘中挂单（今日 NAV 未披露→`pricing_status='awaiting_nav'`、现金即冻结、T+1 按当日 NAV 定价成交，见 `server.py:1600,1668`）。
- **历史回测 run（`dash-*`）只读冻结**，seeding 只读源 run、绝不写。
- **live run_id 命名**：`live-<bot>-<源run时间戳摘要>`，例：源 `dash-2026-07-20T15-04-39` → `live-bot18-20260720T150439`。
- **幂等**：seeding 已存在则跳过；Phase 1 今天已决策则跳过；Phase 2 今天已 close 则跳过。重跑安全。
- **15:00 硬红线**：Phase 1 硬截止 **14:55**，到点未完成的 run 记 `MISSED` 并停止提交（宁可今天不动，不越 15:00 拿 T+1 成交）。
- **fund.db 路径**：`/home/rooot/agent_invest_lab/data/fund.db`（184M 的那个，非 .openclaw 下 19M 的同名文件）。Python 侧通过 `FUND_DB_PATH` env 或 `db.DB_PATH` 定位。
- **world 工作目录**：`/home/rooot/agent_invest_lab/world`；所有 `oos-daily-driver.ts`、cli_tools 调用相对该目录。

---

## File Structure

**新建（`world/live/` 编排层）：**
- `world/live/live_common.py` — 共享工具：fund.db 连接、live run 发现、日历读取+兜底续期、NAV 就绪门闩、run_id 摘要生成。
- `world/live/seed_live_run.py` — 一次性派生：复制 per-run 交易历史到新 run_id、复制 memory、生成 live config、写基线快照。幂等。
- `world/live/live_decide.py` — Phase 1 驱动器：日历兜底、选 live run、并发 5、错峰、14:55 硬截止、`--phase decide`、summary.tsv。
- `world/live/live_settle.py` — Phase 2 驱动器：NAV 就绪门闩、`--phase settle`、幂等、summary.tsv。
- `world/live/tests/test_live_common.py` — live_common 纯函数单测（pytest）。
- `world/live/tests/test_seed_live_run.py` — seeding 在临时 sqlite 上的重键正确性单测（pytest）。

**修改（world 引擎，加两阶段开关）：**
- `world/src/config.ts` — WorldConfig 加 `skipClose?`/`skipChat?`，parse 映射 snake_case `skip_close`/`skip_chat`。
- `world/src/run.ts` — `close_my_day` 块受 `!config.skipClose` 守卫；`isChatDay` 受 `config.skipChat` 强制关闭。
- `world/src/oos-daily-driver.ts` — 加 `--phase decide|settle`，映射到 `skipClose`/`skipChat`。
- `world/test/oos-phase.test.ts` — 新建：验证 `--phase` → config 标志映射（node --test）。

**新建（定时器）：**
- `~/.config/systemd/user/live-decide.service` + `.timer` — 工作日 14:00 触发 Phase 1。
- `~/.config/systemd/user/live-settle.service` + `.timer` — 工作日傍晚触发 Phase 2（带 NAV 门闩）。

**生成物（不手写，由 seeding 产出）：**
- `world/config/world-live-<runId>.yaml` — 每个 live run 一份，解开实时工具白名单、注入日期指向今天。

---

## Task 1: 两阶段开关（config.ts + run.ts + oos-daily-driver）

**Files:**
- Modify: `world/src/config.ts`（WorldConfig 接口 + parse 函数）
- Modify: `world/src/run.ts:1070`（isChatDay 计算）、`world/src/run.ts:1243`（close_my_day 块）
- Modify: `world/src/oos-daily-driver.ts:42-77`（argv 解析 + config 构建）
- Test: `world/test/oos-phase.test.ts`

**Interfaces:**
- Produces: `WorldConfig.skipClose?: boolean`、`WorldConfig.skipChat?: boolean`；oos-daily-driver 新增 `--phase decide|settle` CLI 参数，`decide`→`{skipClose:true, skipChat:false}`，`settle`→`{skipClose:false, skipChat:true}`，缺省（无 `--phase`）→两者均 false（保持回测行为不变）。
- Consumes: run.ts 现有 `isChatDayAt(...)`（`run.ts:1045`）、close_my_day 调用块（`run.ts:1243-1255`）。

- [ ] **Step 1: 写 config.ts parse 的失败测试**

新建 `world/test/oos-phase.test.ts`：

```ts
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'

// oos-daily-driver 的 --phase 映射：用 --help 之外的干跑不现实（要起引擎），
// 所以直接测 config.ts 的 parse + 一个纯映射函数 phaseToFlags。
import { parseWorldConfig } from '../src/config.ts'
import { phaseToFlags } from '../src/oos-daily-driver.ts'

test('parseWorldConfig 映射 skip_close/skip_chat', () => {
  const cfg = parseWorldConfig({
    bots: ['bot1'], replay: { from: '2026-07-21', to: '2026-07-21' },
    calendar: '../runtime/calendar.json',
    skip_close: true, skip_chat: false,
  } as any)
  assert.equal(cfg.skipClose, true)
  assert.equal(cfg.skipChat, false)
})

test('phaseToFlags: decide/settle/缺省', () => {
  assert.deepEqual(phaseToFlags('decide'), { skipClose: true, skipChat: false })
  assert.deepEqual(phaseToFlags('settle'), { skipClose: false, skipChat: true })
  assert.deepEqual(phaseToFlags(undefined), { skipClose: false, skipChat: false })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/rooot/agent_invest_lab/world && node --test test/oos-phase.test.ts`
Expected: FAIL —— `parseWorldConfig` 未导出 skipClose/skipChat 映射，且 `phaseToFlags` 未定义。

> 注：若 `config.ts` 现有导出名不是 `parseWorldConfig`，先 `grep -n "export" src/config.ts` 对齐真实导出名（摘要记录为 `loadWorldConfig` + 内部 parse），把测试与实现的函数名统一。这里以 `parseWorldConfig` 为占位约定，实现时按真实结构对齐。

- [ ] **Step 3: config.ts 加字段与映射**

在 `WorldConfig` 接口加：

```ts
  /** Phase 1（decide）：跳过 close_my_day（当日收盘 NAV 尚未披露）。 */
  skipClose?: boolean
  /** Phase 2（settle）：不唤醒 bot，只跑系统侧 settle + close。 */
  skipChat?: boolean
```

在 parse（snake_case → camelCase 的那段）加：

```ts
  skipClose: raw.skip_close ?? undefined,
  skipChat: raw.skip_chat ?? undefined,
```

- [ ] **Step 4: run.ts 用两个标志守卫**

`run.ts:1045` 处 isChatDay 计算改为：

```ts
  const isChatDay = config.skipChat ? false : isChatDayAt(cursor, dates, config.chatStepMode, config.chatStepDays, chatDayOpts)
```

`run.ts:1243` 的 close_my_day 块外层加守卫：

```ts
  if (config.fundMcpCli && !config.skipClose) {
    for (const { botId } of setupRes.bots) {
      // ...原有 close_my_day 调用不变...
    }
  }
```

（settle_pending_orders 块 `run.ts:1056` **不加守卫**——两阶段都要跑：Phase 1 结算昨天的单、Phase 2 catch-up 幂等。）

- [ ] **Step 5: oos-daily-driver 加 --phase 与 phaseToFlags**

`oos-daily-driver.ts` 顶部加导出函数：

```ts
export function phaseToFlags(phase: string | undefined): { skipClose: boolean; skipChat: boolean } {
  if (phase === 'decide') return { skipClose: true, skipChat: false }
  if (phase === 'settle') return { skipClose: false, skipChat: true }
  return { skipClose: false, skipChat: false }
}
```

`main()` 里解析并合入 config（在 `const config: WorldConfig = { ...base, ... }` 内）：

```ts
  const phase = argVal(argv, '--phase')
  const { skipClose, skipChat } = phaseToFlags(phase)
  // ...在 config 对象字面量里加：
  skipClose,
  skipChat,
```

并在 `--help` 文本加一行：`  --phase decide|settle   live 两阶段：decide=盘中决策跳过收盘核算；settle=盘后纯系统结算`。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd /home/rooot/agent_invest_lab/world && node --test test/oos-phase.test.ts`
Expected: PASS（两个 test 全绿）。

- [ ] **Step 7: 回归——确认缺省行为不变**

Run: `cd /home/rooot/agent_invest_lab/world && node --test test/*.test.ts`
Expected: 现有测试全部通过（无 `--phase` 时 skipClose/skipChat 均 undefined/false，回测路径不受影响）。

- [ ] **Step 8: Commit**

```bash
cd /home/rooot/agent_invest_lab/world && git add src/config.ts src/run.ts src/oos-daily-driver.ts test/oos-phase.test.ts
git commit -m "feat(live): oos-daily-driver 加 --phase decide/settle 两阶段开关"
```

---

## Task 2: live_common.py — 共享工具（fund.db、run 发现、日历兜底、NAV 门闩）

**Files:**
- Create: `world/live/live_common.py`
- Create: `world/live/tests/test_live_common.py`

**Interfaces:**
- Produces:
  - `live_run_id(source_run_id: str, bot: str) -> str` — `dash-2026-07-20T15-04-39` + `bot18` → `live-bot18-20260720T150439`。
  - `discover_live_runs(runs_dir: str) -> list[tuple[str, str]]` — 扫 `runtime/runs/live-*/state.json`，返回 `[(run_id, bot), ...]`。
  - `today_str() -> str`（`YYYY-MM-DD`，可被 `TODAY` env 覆盖，供测试）。
  - `is_weekday(date: str) -> bool`（周一~周五 True）。
  - `ensure_calendar_has(cal_path: str, date: str) -> bool` — 若 date 不在 calendar 且是工作日则追加并写回、返回 True；已在则返回 False；非工作日抛 `ValueError`。
  - `nav_ready(db_path: str, fund_codes: list[str], date: str) -> tuple[bool, list[str]]` — 全部 fund_code 在 `fund_nav` 有 `nav_date == date` 记录才 True；否则返回 (False, 缺失码列表)。
  - `run_fund_codes(db_path: str, bot: str, run_id: str) -> list[str]` — 该 run 当前 active 持仓 + pending 单涉及的 fund_code 去重。
- Consumes: `runtime/calendar.json`（`{"trading_days": [...]}`）、`fund.db` 表 `fund_nav(fund_code,nav_date,nav)`、`fund_bot_holdings(bot_id,fund_code,run_id,status)`、`fund_bot_orders(bot_id,fund_code,order_run_id,status)`。

- [ ] **Step 1: 写 live_common 纯函数失败测试**

新建 `world/live/tests/test_live_common.py`：

```python
import json, sqlite3, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import live_common as lc


def test_live_run_id():
    assert lc.live_run_id("dash-2026-07-20T15-04-39", "bot18") == "live-bot18-20260720T150439"


def test_is_weekday():
    assert lc.is_weekday("2026-07-21") is True   # 周二
    assert lc.is_weekday("2026-07-18") is False  # 周六


def test_ensure_calendar_appends_weekday(tmp_path):
    cal = tmp_path / "calendar.json"
    cal.write_text(json.dumps({"trading_days": ["2026-07-20"]}))
    changed = lc.ensure_calendar_has(str(cal), "2026-07-21")
    assert changed is True
    days = json.loads(cal.read_text())["trading_days"]
    assert days[-1] == "2026-07-21"
    # 幂等：再调不重复追加
    assert lc.ensure_calendar_has(str(cal), "2026-07-21") is False


def test_ensure_calendar_rejects_weekend(tmp_path):
    cal = tmp_path / "calendar.json"
    cal.write_text(json.dumps({"trading_days": ["2026-07-17"]}))
    with pytest.raises(ValueError):
        lc.ensure_calendar_has(str(cal), "2026-07-18")  # 周六


def _mk_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fund_nav (fund_code TEXT, nav_date TEXT, nav REAL)")
    conn.execute("INSERT INTO fund_nav VALUES ('000216','2026-07-21',3.5)")
    conn.commit(); conn.close()


def test_nav_ready(tmp_path):
    db = str(tmp_path / "fund.db"); _mk_db(db)
    ok, missing = lc.nav_ready(db, ["000216"], "2026-07-21")
    assert ok is True and missing == []
    ok2, missing2 = lc.nav_ready(db, ["000216", "999999"], "2026-07-21")
    assert ok2 is False and missing2 == ["999999"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 -m pytest live/tests/test_live_common.py -v`
Expected: FAIL —— `No module named live_common`。

- [ ] **Step 3: 实现 live_common.py**

新建 `world/live/live_common.py`：

```python
#!/usr/bin/env python3.12
"""live 盘中跑共享工具：run 发现 / 日历兜底 / NAV 门闩 / run_id 摘要。"""
import json, os, sqlite3, glob, datetime, re

WORLD = os.environ.get("LIVE_WORLD", "/home/rooot/agent_invest_lab/world")
DB_PATH = os.environ.get("FUND_DB_PATH", "/home/rooot/agent_invest_lab/data/fund.db")


def today_str() -> str:
    return os.environ.get("TODAY") or datetime.date.today().isoformat()


def is_weekday(date: str) -> bool:
    return datetime.date.fromisoformat(date).weekday() < 5


def live_run_id(source_run_id: str, bot: str) -> str:
    # dash-2026-07-20T15-04-39 -> 20260720T150439
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})", source_run_id)
    stamp = "".join(m.groups()[:3]) + "T" + "".join(m.groups()[3:]) if m else re.sub(r"[^0-9A-Za-z]", "", source_run_id)
    return f"live-{bot}-{stamp}"


def discover_live_runs(runs_dir: str) -> list:
    out = []
    for f in glob.glob(os.path.join(runs_dir, "live-*", "state.json")):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        rid = os.path.basename(os.path.dirname(f))
        bots = s.get("bots", [])
        if len(bots) == 1:
            out.append((rid, bots[0]))
    out.sort()
    return out


def ensure_calendar_has(cal_path: str, date: str) -> bool:
    cal = json.load(open(cal_path))
    days = cal["trading_days"]
    if date in days:
        return False
    if not is_weekday(date):
        raise ValueError(f"{date} 非工作日，拒绝自动追加日历（节假日需人工维护）")
    days.append(date)
    days.sort()
    json.dump(cal, open(cal_path, "w"), ensure_ascii=False, indent=2)
    return True


def nav_ready(db_path: str, fund_codes: list, date: str):
    if not fund_codes:
        return True, []
    conn = sqlite3.connect(db_path)
    try:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT fund_code FROM fund_nav WHERE nav_date = ? AND fund_code IN (%s)"
            % ",".join("?" * len(fund_codes)),
            [date, *fund_codes],
        )}
    finally:
        conn.close()
    missing = [c for c in fund_codes if c not in have]
    return (len(missing) == 0), missing


def run_fund_codes(db_path: str, bot: str, run_id: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        codes = {r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_bot_holdings WHERE bot_id=? AND run_id=? AND status='active'",
            (bot, run_id))}
        codes |= {r[0] for r in conn.execute(
            "SELECT fund_code FROM fund_bot_orders WHERE bot_id=? AND order_run_id=? AND status='pending'",
            (bot, run_id))}
    finally:
        conn.close()
    return sorted(c for c in codes if c)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 -m pytest live/tests/test_live_common.py -v`
Expected: PASS（6 个测试全绿）。

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab/world && git add live/live_common.py live/tests/test_live_common.py
git commit -m "feat(live): live_common 共享工具（run 发现/日历兜底/NAV 门闩）"
```

---

## Task 3: seed_live_run.py — 派生 live run（复制 per-run 历史，让系统重算）

**Files:**
- Create: `world/live/seed_live_run.py`
- Create: `world/live/tests/test_seed_live_run.py`

**Interfaces:**
- Consumes: `live_common.live_run_id`、`live_common.today_str`；源 config `world/config/world-tmp-<源runId>.yaml`；源 run 目录 `runtime/runs/<源runId>/`。
- Produces:
  - `clone_run_rows(conn, bot, src_run_id, dst_run_id, seed_date) -> dict` — 在**同一** sqlite 连接内，把源 run 的以下行按新 run_id 复制插入（读源→改 run_id 列→INSERT，主键 AUTOINCREMENT 自然新分配）：
    - `fund_bot_actions`（`run_id`）
    - `fund_bot_holdings`（`run_id`，仅 `status='active'`）
    - `fund_bot_holding_lots`（`run_id`，仅 `status='open'`；`holding_id`/`source_order_id` 软外键置 NULL 避免跨 run 悬挂）
    - `fund_bot_orders` 中 `status='pending'` 的（`order_run_id`→dst，`settle_run_id`→NULL）
    - 返回各表复制行数 dict。
  - `seed_live_config(src_cfg_path, dst_cfg_path, dst_run_id)` — 读源 tmp config，追加实时工具白名单、写 `world-live-<runId>.yaml`。
  - `main()` — 幂等编排：dst 已存在（state.json 或 config 已在）则跳过。
- **注意**：不复制 `fund_bot_accounts` 行（PK=bot_id 的缓存，不可信；现金由 replay 重算）。不改任何表结构。

- [ ] **Step 1: 写 clone_run_rows 失败测试**

新建 `world/live/tests/test_seed_live_run.py`：

```python
import sqlite3, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import seed_live_run as seed


def _schema(conn):
    conn.executescript("""
    CREATE TABLE fund_bot_actions (action_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, action_type TEXT, amount REAL, action_date TEXT, run_id TEXT);
    CREATE TABLE fund_bot_holdings (holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, shares REAL, status TEXT, run_id TEXT);
    CREATE TABLE fund_bot_holding_lots (lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, run_id TEXT, holding_id INTEGER,
        shares_remaining REAL, source_order_id INTEGER, status TEXT);
    CREATE TABLE fund_bot_orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT, fund_code TEXT, order_type TEXT, order_date TEXT,
        order_amount REAL, status TEXT, order_run_id TEXT, settle_run_id TEXT);
    """)


def test_clone_run_rows_rekeys(tmp_path):
    conn = sqlite3.connect(":memory:"); _schema(conn)
    src = "dash-2026-07-20T15-04-39"
    conn.execute("INSERT INTO fund_bot_actions (bot_id,fund_code,action_type,amount,action_date,run_id) "
                 "VALUES ('bot18','000216','BUY',1000,'2026-07-20',?)", (src,))
    conn.execute("INSERT INTO fund_bot_holdings (bot_id,fund_code,shares,status,run_id) "
                 "VALUES ('bot18','000216',100,'active',?)", (src,))
    conn.execute("INSERT INTO fund_bot_holdings (bot_id,fund_code,shares,status,run_id) "
                 "VALUES ('bot18','000216',0,'closed',?)", (src,))
    conn.execute("INSERT INTO fund_bot_holding_lots (bot_id,fund_code,run_id,holding_id,shares_remaining,source_order_id,status) "
                 "VALUES ('bot18','000216',?,7,100,9,'open')", (src,))
    conn.execute("INSERT INTO fund_bot_orders (bot_id,fund_code,order_type,order_date,order_amount,status,order_run_id,settle_run_id) "
                 "VALUES ('bot18','000216','buy','2026-07-20',500,'pending',?,NULL)", (src,))
    conn.execute("INSERT INTO fund_bot_orders (bot_id,fund_code,order_type,order_date,order_amount,status,order_run_id,settle_run_id) "
                 "VALUES ('bot18','000216','buy','2026-07-10',500,'confirmed',?,?)", (src, src))
    conn.commit()

    dst = "live-bot18-20260720T150439"
    counts = seed.clone_run_rows(conn, "bot18", src, dst, "2026-07-21")
    conn.commit()

    assert counts == {"actions": 1, "holdings": 1, "lots": 1, "orders": 1}
    # 新 run_id 下能读到复制行
    assert conn.execute("SELECT COUNT(*) FROM fund_bot_actions WHERE run_id=?", (dst,)).fetchone()[0] == 1
    assert conn.execute("SELECT shares FROM fund_bot_holdings WHERE run_id=? AND status='active'", (dst,)).fetchone()[0] == 100
    # closed 持仓不复制
    assert conn.execute("SELECT COUNT(*) FROM fund_bot_holdings WHERE run_id=?", (dst,)).fetchone()[0] == 1
    # lot 软外键置 NULL
    lot = conn.execute("SELECT holding_id, source_order_id FROM fund_bot_holding_lots WHERE run_id=?", (dst,)).fetchone()
    assert lot == (None, None)
    # 只复制 pending 单；order_run_id 重键、settle_run_id NULL
    o = conn.execute("SELECT order_run_id, settle_run_id, status FROM fund_bot_orders WHERE order_run_id=?", (dst,)).fetchone()
    assert o == (dst, None, "pending")
    # 源行不动
    assert conn.execute("SELECT COUNT(*) FROM fund_bot_actions WHERE run_id=?", (src,)).fetchone()[0] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 -m pytest live/tests/test_seed_live_run.py -v`
Expected: FAIL —— `No module named seed_live_run`。

- [ ] **Step 3: 实现 seed_live_run.py**

新建 `world/live/seed_live_run.py`：

```python
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
    import yaml  # world 已依赖 yaml（node），Python 侧用 pyyaml；若无则退化为文本追加
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
        os.makedirs(os.path.dirname(dst_mem), exist_ok=True)
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
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 -m pytest live/tests/test_seed_live_run.py -v`
Expected: PASS。

- [ ] **Step 5: 单 run 真实派生 dry-run（1 个种子）**

先挑一个种子 run（例如 bot16 的某个合格 dash run），真实派生一次并核对：

Run:
```bash
cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 live/seed_live_run.py \
  --source-run-id dash-2026-05-18T12-07-02 --bot-id bot16 --seed-date 2026-07-21
```
Expected: 打印 `[seed] live-bot16-20260518T120702 完成：{...}  config=...`；`config/world-live-live-bot16-....yaml` 存在且 simworld_tools 含 `market_realtime_quote`。

核对 DB 复制正确（现金隔离——新 run 与源 run 持仓一致、互不影响）：
```bash
/usr/bin/python3.12 - <<'PY'
import sqlite3
c = sqlite3.connect("/home/rooot/agent_invest_lab/data/fund.db")
for rid in ("dash-2026-05-18T12-07-02", "live-bot16-20260518T120702"):
    h = c.execute("SELECT COUNT(*),COALESCE(SUM(shares),0) FROM fund_bot_holdings WHERE run_id=? AND status='active'", (rid,)).fetchone()
    print(rid, "active holdings:", h)
PY
```
Expected: 两个 run 的 active 持仓数量/份额一致。

- [ ] **Step 6: Commit**

```bash
cd /home/rooot/agent_invest_lab/world && git add live/seed_live_run.py live/tests/test_seed_live_run.py
git commit -m "feat(live): seed_live_run 派生 live run（复制 per-run 历史+memory+config）"
```

---

## Task 4: live_decide.py — Phase 1 驱动器（14:00 决策）

**Files:**
- Create: `world/live/live_decide.py`

**Interfaces:**
- Consumes: `live_common`（discover_live_runs / ensure_calendar_has / today_str / is_weekday）；`oos-daily-driver.ts --phase decide`。
- Produces: 对每个 live run 跑一次 `node --experimental-strip-types src/oos-daily-driver.ts --date <today> --bot-id <bot> --run-id <rid> --config config/world-live-<rid>.yaml --phase decide`；并发 5、错峰 3s、14:55 硬截止（`HARD_DEADLINE = "14:55"`）、幂等（今天已有决策则跳过）、写 `/tmp/live-decide-<today>/summary.tsv`。
- 幂等判据：该 run 今天是否已产出决策 —— 检查 `runtime/runs/<rid>/<today>/` 下是否已有 bot 决策目录（`P.botDayDir`），或 state.json 的 trading_dates 是否含 today。

- [ ] **Step 1: 实现 live_decide.py**

新建 `world/live/live_decide.py`：

```python
#!/usr/bin/env python3.12
"""Phase 1：live 盘中决策（默认 14:00 触发，14:55 硬截止）。

对每个 live run 跑 oos-daily-driver --phase decide：结算昨天的单 + bot 决策挂今天的
pending 单（awaiting_nav 冻现金）+ 跳过 close_my_day。到 14:55 仍未完成的 run 记 MISSED。
"""
import json, os, subprocess, sys, time, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(__file__))
import live_common as lc

WORLD = lc.WORLD
CONC = 5
HARD_DEADLINE = os.environ.get("LIVE_DECIDE_DEADLINE", "14:55")  # HH:MM 本地时


def _past_deadline() -> bool:
    now = datetime.datetime.now().strftime("%H:%M")
    return now >= HARD_DEADLINE


def _already_decided(rid, today) -> bool:
    try:
        s = json.load(open(os.path.join(WORLD, "runtime", "runs", rid, "state.json")))
        if today in (s.get("trading_dates") or []):
            return True
    except Exception:
        pass
    return False


def run_one(rid, bot, today, logdir):
    log = os.path.join(logdir, f"{rid}.log")
    with open(log, "a") as lf:
        if _already_decided(rid, today):
            lf.write(f"--- {today} 已决策，跳过 ---\n")
            return (rid, bot, "SKIP", today)
        if _past_deadline():
            lf.write(f"--- {today} 已过硬截止 {HARD_DEADLINE}，MISSED，不提交 ---\n")
            return (rid, bot, "MISSED", today)
        cfg = f"config/world-live-{rid}.yaml"
        lf.write(f"\n===== {rid}/{bot} decide {today} {time.strftime('%F %T')} =====\n"); lf.flush()
        rc = subprocess.call(
            ["node", "--experimental-strip-types", "src/oos-daily-driver.ts",
             "--date", today, "--bot-id", bot, "--run-id", rid,
             "--config", cfg, "--phase", "decide"],
            cwd=WORLD, stdout=lf, stderr=subprocess.STDOUT)
        status = "OK" if rc == 0 else "FAILED"
        lf.write(f"--- {today} {status} rc={rc} ---\n")
        return (rid, bot, status, today)


def main():
    today = lc.today_str()
    if not lc.is_weekday(today):
        print(f"[live-decide] {today} 非工作日，退出"); return
    changed = lc.ensure_calendar_has(os.path.join(WORLD, "runtime", "calendar.json"), today)
    if changed:
        print(f"[live-decide] 日历已兜底追加 {today}")

    runs = lc.discover_live_runs(os.path.join(WORLD, "runtime", "runs"))
    if not runs:
        print("[live-decide] 无 live run，退出"); return
    logdir = f"/tmp/live-decide-{today}"; os.makedirs(logdir, exist_ok=True)
    print(f"[live-decide] {today} 待决策 {len(runs)} run，并发 {CONC}，硬截止 {HARD_DEADLINE}")

    results = []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {}
        for rid, bot in runs:
            futs[ex.submit(run_one, rid, bot, today, logdir)] = rid
            time.sleep(3)  # 错峰启动
        for fut in as_completed(futs):
            r = fut.result(); results.append(r)
            print(f"[{len(results)}/{len(runs)}] {r[2]:7} {r[1]:7} {r[0]}")

    with open(os.path.join(logdir, "summary.tsv"), "w") as sf:
        sf.write("status\tbot\trun_id\tdate\n")
        for r in sorted(results, key=lambda x: x[2] != "OK"):
            sf.write(f"{r[2]}\t{r[1]}\t{r[0]}\t{r[3]}\n")
    missed = [r for r in results if r[2] in ("MISSED", "FAILED")]
    print(f"[live-decide] 完成 OK={sum(1 for r in results if r[2]=='OK')} 异常={len(missed)}")
    for r in missed:
        print(f"  ✗ {r[2]:7} {r[1]:7} {r[0]}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 语法+发现自检（不真跑引擎）**

Run:
```bash
cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 -c "import sys; sys.path.insert(0,'live'); import live_decide, live_common; print('import ok'); print('runs:', live_common.discover_live_runs('runtime/runs'))"
```
Expected: 打印 `import ok` 和已派生的 live run 列表（至少含 Task 3 Step 5 派生的那个）。

- [ ] **Step 3: 单 run 真实决策端到端（用 Task 3 派生的种子）**

Run（限定只跑 1 个 live run —— 临时把其它 live run 目录移开或用 `LIVE_ONLY` 过滤；最简单：只保留一个 live-* 目录时直接跑）：
```bash
cd /home/rooot/agent_invest_lab/world && TODAY=2026-07-21 /usr/bin/python3.12 live/live_decide.py
```
Expected: summary.tsv 里该 run 状态 `OK`；`runtime/runs/<rid>/2026-07-21/<bot>/` 下有决策产物；DB 里该 run 今天有新 pending 单（`awaiting_nav`）、现金已冻结、**无** close_my_day 快照。

核对：
```bash
/usr/bin/python3.12 - <<'PY'
import sqlite3
c=sqlite3.connect("/home/rooot/agent_invest_lab/data/fund.db")
rid="live-bot16-20260518T120702"
print("today pending:", c.execute("SELECT COUNT(*),GROUP_CONCAT(pricing_status) FROM fund_bot_orders WHERE order_run_id=? AND order_date='2026-07-21' AND status='pending'",(rid,)).fetchone())
print("today snapshot:", c.execute("SELECT COUNT(*) FROM fund_bot_daily_snapshots WHERE run_id=? AND trade_date='2026-07-21'",(rid,)).fetchone())
PY
```
Expected: 今天 pending 单存在且 `pricing_status` 含 `awaiting_nav`；今天 daily_snapshot 数量为 **0**（Phase 1 跳过 close）。

- [ ] **Step 4: Commit**

```bash
cd /home/rooot/agent_invest_lab/world && git add live/live_decide.py
git commit -m "feat(live): live_decide Phase 1 驱动器（14:00 决策、14:55 硬截止、并发5）"
```

---

## Task 5: live_settle.py — Phase 2 驱动器（盘后结算+落净值）

**Files:**
- Create: `world/live/live_settle.py`

**Interfaces:**
- Consumes: `live_common`（discover_live_runs / nav_ready / run_fund_codes / today_str）；`oos-daily-driver.ts --phase settle`。
- Produces: 对每个 live run，先过 NAV 就绪门闩（该 run 涉及基金今天的 `fund_nav` 全就绪才放行，否则记 `NAV_PENDING` 顺延）；就绪则跑 `--phase settle`（不唤醒 bot，只系统侧 settle catch-up + close_my_day 落今天净值快照）；幂等（今天已 close 则跳过）；写 `/tmp/live-settle-<today>/summary.tsv`。
- 幂等判据：`fund_bot_daily_snapshots` 该 run 今天已有快照则 SKIP。

- [ ] **Step 1: 实现 live_settle.py**

新建 `world/live/live_settle.py`：

```python
#!/usr/bin/env python3.12
"""Phase 2：盘后纯系统结算 + 落净值（傍晚触发，NAV 就绪门闩）。

不唤醒 bot（--phase settle → skipChat）。settle catch-up 幂等 + close_my_day 按今天真实
收盘 NAV 落净值快照。今天挂的单 order_date=today，settle as-of=today 不结算（T+1），
留到明天 Phase 1；本阶段只保证净值快照落库。
"""
import json, os, sqlite3, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(__file__))
import live_common as lc

WORLD = lc.WORLD
DB_PATH = lc.DB_PATH
CONC = 5


def _already_closed(rid, today) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM fund_bot_daily_snapshots WHERE run_id=? AND trade_date=?",
            (rid, today)).fetchone()[0]
    finally:
        conn.close()
    return n > 0


def run_one(rid, bot, today, logdir):
    log = os.path.join(logdir, f"{rid}.log")
    with open(log, "a") as lf:
        if _already_closed(rid, today):
            lf.write(f"--- {today} 已 close，跳过 ---\n")
            return (rid, bot, "SKIP", today)
        codes = lc.run_fund_codes(DB_PATH, bot, rid)
        ok, missing = lc.nav_ready(DB_PATH, codes, today)
        if not ok:
            lf.write(f"--- {today} NAV 未就绪，缺 {missing}，顺延 ---\n")
            return (rid, bot, "NAV_PENDING", today)
        cfg = f"config/world-live-{rid}.yaml"
        lf.write(f"\n===== {rid}/{bot} settle {today} {time.strftime('%F %T')} =====\n"); lf.flush()
        rc = subprocess.call(
            ["node", "--experimental-strip-types", "src/oos-daily-driver.ts",
             "--date", today, "--bot-id", bot, "--run-id", rid,
             "--config", cfg, "--phase", "settle"],
            cwd=WORLD, stdout=lf, stderr=subprocess.STDOUT)
        status = "OK" if rc == 0 else "FAILED"
        lf.write(f"--- {today} {status} rc={rc} ---\n")
        return (rid, bot, status, today)


def main():
    today = lc.today_str()
    runs = lc.discover_live_runs(os.path.join(WORLD, "runtime", "runs"))
    if not runs:
        print("[live-settle] 无 live run，退出"); return
    logdir = f"/tmp/live-settle-{today}"; os.makedirs(logdir, exist_ok=True)
    print(f"[live-settle] {today} 待结算 {len(runs)} run，并发 {CONC}")

    results = []
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {}
        for rid, bot in runs:
            futs[ex.submit(run_one, rid, bot, today, logdir)] = rid
            time.sleep(3)
        for fut in as_completed(futs):
            r = fut.result(); results.append(r)
            print(f"[{len(results)}/{len(runs)}] {r[2]:12} {r[1]:7} {r[0]}")

    with open(os.path.join(logdir, "summary.tsv"), "w") as sf:
        sf.write("status\tbot\trun_id\tdate\n")
        for r in sorted(results, key=lambda x: x[2] != "OK"):
            sf.write(f"{r[2]}\t{r[1]}\t{r[0]}\t{r[3]}\n")
    pend = [r for r in results if r[2] == "NAV_PENDING"]
    bad = [r for r in results if r[2] == "FAILED"]
    print(f"[live-settle] OK={sum(1 for r in results if r[2]=='OK')} 顺延={len(pend)} 失败={len(bad)}")
    if pend:
        print("  顺延（等 NAV）：", [r[0] for r in pend])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 导入+门闩自检**

Run:
```bash
cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 -c "import sys; sys.path.insert(0,'live'); import live_settle, live_common as lc; codes=lc.run_fund_codes(lc.DB_PATH,'bot16','live-bot16-20260518T120702'); print('codes',codes); print('nav_ready',lc.nav_ready(lc.DB_PATH,codes,'2026-07-21'))"
```
Expected: 打印该 run 涉及基金码 + `nav_ready` 结果（今天 NAV 若已入库则 True）。

- [ ] **Step 3: 单 run 结算端到端 + 账户守恒核对**

Run:
```bash
cd /home/rooot/agent_invest_lab/world && TODAY=2026-07-21 /usr/bin/python3.12 live/live_settle.py
```
Expected: 该 run 状态 `OK`（若今天 NAV 已入库）或 `NAV_PENDING`（未入库则合理顺延）；OK 后今天 daily_snapshot 落库。

守恒核对（现金 + 持仓市值 + 在途 = 总值）：
```bash
/usr/bin/python3.12 - <<'PY'
import sqlite3
c=sqlite3.connect("/home/rooot/agent_invest_lab/data/fund.db"); c.row_factory=sqlite3.Row
rid="live-bot16-20260518T120702"
r=c.execute("SELECT cash,cash_receivable,invested_value,total_value FROM fund_bot_daily_snapshots WHERE run_id=? AND trade_date='2026-07-21'",(rid,)).fetchone()
if r:
    lhs=(r["cash"] or 0)+(r["invested_value"] or 0)+(r["cash_receivable"] or 0)
    print("cash+invested+receivable=%.2f  total_value=%.2f  diff=%.4f"%(lhs,r["total_value"],lhs-r["total_value"]))
else:
    print("今天快照未落（NAV 可能未就绪）")
PY
```
Expected: `diff` 接近 0（守恒成立），或明确提示 NAV 未就绪。

- [ ] **Step 4: Commit**

```bash
cd /home/rooot/agent_invest_lab/world && git add live/live_settle.py
git commit -m "feat(live): live_settle Phase 2 驱动器（NAV 门闩+结算+落净值）"
```

---

## Task 6: systemd user timers（定时触发）

**Files:**
- Create: `~/.config/systemd/user/live-decide.service`
- Create: `~/.config/systemd/user/live-decide.timer`
- Create: `~/.config/systemd/user/live-settle.service`
- Create: `~/.config/systemd/user/live-settle.timer`

**Interfaces:**
- Consumes: `live_decide.py`、`live_settle.py`。
- Produces: 两个工作日定时器；`live-decide` 每工作日 14:00、`live-settle` 每工作日 18:30（傍晚，A 股基金 NAV 通常当晚披露；未就绪由门闩顺延）。用 `flock` 防重叠。

- [ ] **Step 1: 写 live-decide.service**

```ini
[Unit]
Description=Live intraday Phase 1 (decide) for qualified runs

[Service]
Type=oneshot
WorkingDirectory=/home/rooot/agent_invest_lab/world
ExecStart=/usr/bin/flock -n /tmp/live-decide.lock /usr/bin/python3.12 /home/rooot/agent_invest_lab/world/live/live_decide.py
```

- [ ] **Step 2: 写 live-decide.timer**

```ini
[Unit]
Description=Trigger live decide at 14:00 on weekdays

[Timer]
OnCalendar=Mon..Fri 14:00
Persistent=false

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: 写 live-settle.service**

```ini
[Unit]
Description=Live intraday Phase 2 (settle+close) for qualified runs

[Service]
Type=oneshot
WorkingDirectory=/home/rooot/agent_invest_lab/world
ExecStart=/usr/bin/flock -n /tmp/live-settle.lock /usr/bin/python3.12 /home/rooot/agent_invest_lab/world/live/live_settle.py
```

- [ ] **Step 4: 写 live-settle.timer**

```ini
[Unit]
Description=Trigger live settle at 18:30 on weekdays

[Timer]
OnCalendar=Mon..Fri 18:30
Persistent=false

[Install]
WantedBy=timers.target
```

- [ ] **Step 5: 加载并校验（不 enable，先 dry validate）**

Run:
```bash
systemctl --user daemon-reload
systemd-analyze --user verify ~/.config/systemd/user/live-decide.timer ~/.config/systemd/user/live-settle.timer && echo "timers valid"
```
Expected: 打印 `timers valid`，无 verify 报错。

> 启用留给用户显式决定（涉及真实每日自动交易）：`systemctl --user enable --now live-decide.timer live-settle.timer`。计划不自动 enable。

- [ ] **Step 6: Commit（把 unit 文件纳入仓库副本，便于版本管理）**

在 `world/live/systemd/` 存一份副本便于追踪：
```bash
mkdir -p /home/rooot/agent_invest_lab/world/live/systemd
cp ~/.config/systemd/user/live-decide.service ~/.config/systemd/user/live-decide.timer \
   ~/.config/systemd/user/live-settle.service ~/.config/systemd/user/live-settle.timer \
   /home/rooot/agent_invest_lab/world/live/systemd/
cd /home/rooot/agent_invest_lab/world && git add live/systemd/
git commit -m "feat(live): systemd user timers（14:00 decide / 18:30 settle，flock 防重叠）"
```

---

## Task 7: 全量派生 + 端到端联跑验证

**Files:**
- Create: `world/live/seed_all_qualified.py`（批量派生，复用 `advance_qualified.py:build_pairs` 的合格口径）

**Interfaces:**
- Consumes: `seed_live_run.main` 的编排逻辑（改为可批量调用 `clone_run_rows` + `seed_live_config`）；`advance_qualified.py` 的合格 run 发现口径（单指数 bot1~20、非多指数、未手动标 fail、config 存在）。
- Produces: 对全部 ~45 个合格 run 幂等派生 live run。

- [ ] **Step 1: 写 seed_all_qualified.py**

```python
#!/usr/bin/env python3.12
"""批量派生：把全部合格历史 run 一次性 seed 成 live run（幂等，已存在跳过）。"""
import json, os, glob, re, sys, subprocess
sys.path.insert(0, os.path.dirname(__file__))
import live_common as lc

WORLD = lc.WORLD


def build_pairs():
    """合格口径：单指数 dash-* run，单 bot∈bot1..bot20（排除 bot101/102/103），未手动标 fail，config 存在。"""
    verdicts = json.load(open(os.path.join(WORLD, "runtime", "run-verdicts.json")))
    pairs = []
    for f in glob.glob(os.path.join(WORLD, "runtime", "runs", "dash-*", "state.json")):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        rid = os.path.basename(os.path.dirname(f))
        bots = s.get("bots", [])
        if len(bots) != 1:
            continue
        bot = bots[0]
        if bot in ("bot101", "bot102", "bot103"):
            continue
        if verdicts.get(f"{rid}|{bot}") == "fail":
            continue
        if not os.path.exists(os.path.join(WORLD, "config", f"world-tmp-{rid}.yaml")):
            continue
        pairs.append((rid, bot))
    pairs.sort(key=lambda p: (int(re.search(r"\d+", p[1]).group()), p[0]))
    return pairs


def main():
    pairs = build_pairs()
    print(f"合格 run {len(pairs)} 个，逐个幂等派生 live run")
    for rid, bot in pairs:
        subprocess.call(["/usr/bin/python3.12", os.path.join(WORLD, "live", "seed_live_run.py"),
                         "--source-run-id", rid, "--bot-id", bot, "--seed-date", lc.today_str()])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 全量派生**

Run: `cd /home/rooot/agent_invest_lab/world && /usr/bin/python3.12 live/seed_all_qualified.py`
Expected: ~45 行 `[seed] live-... 完成` 或 `已存在，跳过`；`ls config/world-live-*.yaml | wc -l` ≈ 派生数。

- [ ] **Step 3: 全量 Phase 1 决策（真实 14:00 前手动演练一次）**

Run: `cd /home/rooot/agent_invest_lab/world && TODAY=2026-07-21 /usr/bin/python3.12 live/live_decide.py`
Expected: `/tmp/live-decide-2026-07-21/summary.tsv` 大部分 `OK`；失败/`MISSED` 单独列出隔离，不影响其它 run。

- [ ] **Step 4: 全量 Phase 2 结算（傍晚 NAV 就绪后）**

Run: `cd /home/rooot/agent_invest_lab/world && TODAY=2026-07-21 /usr/bin/python3.12 live/live_settle.py`
Expected: `NAV_PENDING` 的 run 合理顺延（可重跑补齐），其余 `OK` 且今天净值快照落库。

- [ ] **Step 5: 全量守恒抽检 + 隔离核对**

Run:
```bash
/usr/bin/python3.12 - <<'PY'
import sqlite3, glob, os
c=sqlite3.connect("/home/rooot/agent_invest_lab/data/fund.db"); c.row_factory=sqlite3.Row
bad=[]
for r in c.execute("SELECT run_id,bot_id,cash,cash_receivable,invested_value,total_value FROM fund_bot_daily_snapshots WHERE trade_date='2026-07-21' AND run_id LIKE 'live-%'"):
    lhs=(r["cash"] or 0)+(r["invested_value"] or 0)+(r["cash_receivable"] or 0)
    if abs(lhs-(r["total_value"] or 0))>1.0:
        bad.append((r["run_id"], round(lhs-(r["total_value"] or 0),2)))
print("守恒破坏 run 数:", len(bad))
for b in bad: print("  ", b)
PY
```
Expected: `守恒破坏 run 数: 0`。

- [ ] **Step 6: Commit**

```bash
cd /home/rooot/agent_invest_lab/world && git add live/seed_all_qualified.py
git commit -m "feat(live): seed_all_qualified 批量派生 + 全量端到端验证通过"
```

---

## Task 8（第二期，可选）：dashboard live 面板

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts` + 对应前端页面（加 live run 当日决策/结算状态与净值曲线视图）。

**Interfaces:**
- Consumes: `fund_bot_daily_snapshots`（run_id LIKE 'live-%'）、`/tmp/live-decide-*/summary.tsv`、`/tmp/live-settle-*/summary.tsv`。
- Produces: 一个 live 面板，看当日 decide/settle 状态与 live run 净值曲线。

> 本任务列为第二期，不阻塞上线。前 7 个任务完成即可每日自动盘中跑。细化步骤在进入本任务前，用 brainstorming 明确面板需求后再展开。

---

## 计划自检（Self-Review）

**Spec 覆盖：** 设计文档 §4.1 派生（Task 3/7）、§4.2 Phase 1（Task 1/4）、§4.3 Phase 2（Task 1/5）、§4.4 实时信息暴露（Task 3 `_REALTIME_TOOLS` 白名单）、§4.5 cron/幂等/并发/兜底（Task 4/5/6，14:55 硬截止在 live_decide）、§5 失败隔离+观测（summary.tsv + 单 run 隔离）、§6 测试策略（Task 3/5/7 单 run E2E + 守恒核对）、§7 dashboard 第二期（Task 8）。日历续期（用户新确认「驱动器自动兜底」）→ Task 2 `ensure_calendar_has` + Task 4 调用。全部有对应任务。

**占位符扫描：** 无 TBD/TODO；每个代码步骤给出完整可执行代码；每个验证步骤给出精确命令+期望输出。唯一柔性点：Task 1 Step 2 备注 config.ts 真实导出名需对齐（`parseWorldConfig` vs `loadWorldConfig`），已显式标注在实现时校验。

**类型一致：** `skipClose`/`skipChat`（camelCase）↔ `skip_close`/`skip_chat`（yaml snake_case）全程一致；`phaseToFlags` 返回 `{skipClose,skipChat}` 与 config 字段名一致；`live_run_id`/`discover_live_runs`/`nav_ready`/`run_fund_codes`/`ensure_calendar_has` 在 Task 2 定义，Task 3/4/5 消费签名一致；`clone_run_rows` 返回 `{actions,holdings,lots,orders}` 与测试断言一致。

---

## 执行交接

计划已保存到 `docs/superpowers/plans/2026-07-21-live-intraday-run.md`。两种执行方式：

1. **Subagent-Driven（推荐）** — 每个任务派一个全新 subagent，任务间我来评审，迭代快。
2. **Inline Execution** — 在当前会话按 executing-plans 批量执行、带检查点评审。

选哪种？
