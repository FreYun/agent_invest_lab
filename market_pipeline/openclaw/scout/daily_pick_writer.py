"""daily_pick_writer — 把 bot 推荐池的 deep_research JSON 落库.

设计:
- CLI 接 --slot / --reviewer / [--date]
- stdin 接 JSON: {"items": [{rank, code, name, sources, deep_research_json}]}
- 单事务: DELETE 同 (date,slot,reviewer) 旧行 + INSERT 新行 (覆盖式)
- 任一校验失败整批拒绝 (exit !=0), 不部分写入

环境变量:
- SCOUT_DB_PATH: 覆盖默认 DB 路径 (单测用)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402

VALID_SLOTS = {"premarket", "intraday_am", "intraday_pm"}
VALID_REVIEWERS = {"bot7", "bot11"}
VALID_TIERS = {"试仓", "观望", "重仓"}
CODE_RE = re.compile(r"^\d{6}$")
# 交易状态前缀(*ST/ST/N新股/C次新/XD除息/XR除权/DR除权除息), 比对 code↔name 时归一去掉
_NAME_PREFIX_RE = re.compile(r"^(?:\*?ST|XD|XR|DR|N|C)+")


def _err(msg: str) -> None:
    print(f"[daily-pick-writer] ERROR: {msg}", file=sys.stderr)


def _norm_name(s: str) -> str:
    """归一股票简称: 去空白 + 去交易状态前缀, 便于和 stock_names 权威名比对."""
    s = (s or "").strip().replace(" ", "").replace("　", "")
    return _NAME_PREFIX_RE.sub("", s)


def validate_codes_against_names(items: list, dbp: str) -> str | None:
    """用 stock_names 权威表校验 code↔name 一致性.

    - code 在库且归一后名称不符 → 返回错误(整批拒收, 疑似把名字配错了代码).
    - stock_names 表缺失 / code 查不到(如刚上市未同步) / name 缺省 → 放行(仅 stderr 警告),
      避免误挡合法新票. 主要拦截的是"代码指向另一只已存在个股"这类 bot11 6-22 翻车.
    """
    if not items:
        return None
    try:
        c = sqlite3.connect(dbp, timeout=30)
    except Exception as e:  # DB 打不开不阻断落库
        _err(f"code↔name 校验跳过(DB 连接失败: {str(e)[:80]})")
        return None
    try:
        if not c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_names'"
        ).fetchone():
            _err("stock_names 表不存在, 跳过 code↔name 校验(WARN)")
            return None
        for i, it in enumerate(items):
            code = scout_db.bare(it.get("code", ""))
            name = it.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            row = c.execute(
                "SELECT name FROM stock_names WHERE code=?", (code,)).fetchone()
            if row is None:
                _err(f"items[{i}] code {code} 不在 stock_names(疑似新股未同步), 跳过校验(WARN)")
                continue
            db_name = row[0] or ""
            if _norm_name(db_name) != _norm_name(name):
                return (f"items[{i}] code {code} 名称不符: 库内={db_name!r} "
                        f"提交={name!r} (疑似配错代码, 整批拒收)")
        return None
    finally:
        c.close()


# 买点中值偏离参考价超过此比例 → 视为配错代码/取错行情
PRICE_DEVIATION_LIMIT = 0.5


def _ref_prices(c: sqlite3.Connection, code: str) -> list[float]:
    """该 code 的所有可用参考价: 实时现价(intraday_quote_latest) + 日线最新收盘(daily).
    多源并取, 让买点贴近其中任一源即视为合理, 避免单一源滞后/脏值误拒. 表不存在则跳过该源."""
    out = []
    for sql, param in (
        ("SELECT price FROM intraday_quote_latest WHERE code=? AND price>0", (code,)),
        ("SELECT close FROM daily WHERE ts_code LIKE ? AND close>0 "
         "ORDER BY trade_date DESC LIMIT 1", (code + ".%",)),
    ):
        try:
            row = c.execute(sql, param).fetchone()
        except sqlite3.OperationalError:
            continue  # 表不存在
        if row and row[0]:
            out.append(float(row[0]))
    return out


def validate_prices_sanity(items: list, dbp: str) -> str | None:
    """买点合理性自检: 买点中值偏离所有可用行情源均 > PRICE_DEVIATION_LIMIT → 拒收.

    抓的是 bot 把对的逻辑/代码配上另一只票买点的"缝合"场景
    (bot11 6-22: 描述铜箔龙头却报价 7 元, 真实 160 元). 只要贴近任一行情源即放行
    (单源抽风不误拒); 无任何行情可比 → 仅警告放行.
    """
    if not items:
        return None
    try:
        c = sqlite3.connect(dbp, timeout=30)
    except Exception as e:
        _err(f"价格自检跳过(DB 连接失败: {str(e)[:80]})")
        return None
    try:
        for i, it in enumerate(items):
            v = (it.get("deep_research_json") or {}).get("verdict") or {}
            lo, hi = v.get("entry_low"), v.get("entry_high")
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)) \
                    or lo <= 0 or hi <= 0:
                continue  # validate_payload 已保证, 这里只防御
            mid = (lo + hi) / 2
            code = scout_db.bare(it.get("code", ""))
            refs = _ref_prices(c, code)
            if not refs:
                _err(f"items[{i}] code {code} 无行情(intraday_quote_latest/daily 均无), "
                     f"跳过价格自检(WARN)")
                continue
            best = min(refs, key=lambda r: abs(mid - r) / r)  # 最贴近的源
            dev = abs(mid - best) / best
            if dev > PRICE_DEVIATION_LIMIT:
                return (f"items[{i}] code {code} 买点 {lo}-{hi}(中值 {mid:.2f}) "
                        f"偏离最新行情 {'/'.join(f'{r:.2f}' for r in refs)} 达 {dev * 100:.0f}% "
                        f"(>{int(PRICE_DEVIATION_LIMIT * 100)}%, 疑似配错代码/取错行情, 整批拒收)")
        return None
    finally:
        c.close()


def _validate_deep_research(dr: dict, item_id: str) -> str | None:
    """返回错误描述,None 表示通过."""
    if not isinstance(dr, dict):
        return f"{item_id} deep_research_json 不是 dict"
    scout = dr.get("scout") or {}
    if not isinstance(scout.get("sources"), list) or not scout["sources"]:
        return f"{item_id} scout.sources 必须非空数组"
    dig = dr.get("dig") or {}
    dps = dig.get("data_points")
    if not isinstance(dps, list) or len(dps) < 3:
        return f"{item_id} dig.data_points 必须 ≥ 3 条"
    for j, dp in enumerate(dps):
        if not isinstance(dp, dict):
            return f"{item_id} dig.data_points[{j}] 必须是 dict 含 label/value/src (实际 {type(dp).__name__})"
        for f in ("label", "value", "src"):
            v = dp.get(f)
            if not isinstance(v, str) or not v.strip():
                return f"{item_id} dig.data_points[{j}].{f} 必须是非空字符串 (实际 {v!r})"
    challenge = dr.get("challenge") or {}
    for f in ("bear_case", "killer_signal"):
        v = challenge.get(f)
        if not isinstance(v, str) or not v.strip():
            return f"{item_id} challenge.{f} 必须是非空字符串 (实际 {v!r})"
    verdict = dr.get("verdict") or {}
    for k in ("logic_stars", "action_stars"):
        v = verdict.get(k)
        if not isinstance(v, int) or not (1 <= v <= 5):
            return f"{item_id} verdict.{k} 必须 ∈ [1,5] 整数 (实际 {v!r})"
    for f in ("entry_low", "entry_high", "stop_loss", "target_price"):
        v = verdict.get(f)
        if not isinstance(v, (int, float)) or v <= 0:
            return f"{item_id} verdict.{f} 必须是正数 (实际 {v!r})"
    if verdict["entry_low"] > verdict["entry_high"]:
        return f"{item_id} verdict.entry_low ({verdict['entry_low']}) 不能 > entry_high ({verdict['entry_high']})"
    ol = verdict.get("one_liner")
    if not isinstance(ol, str) or not (10 <= len(ol) <= 60):
        return f"{item_id} verdict.one_liner 长度必须 ∈ [10,60] 字符 (实际 {len(ol) if isinstance(ol, str) else 'N/A'})"
    tier = verdict.get("position_tier")
    if tier not in VALID_TIERS:
        return f"{item_id} verdict.position_tier 必须 ∈ {VALID_TIERS} (实际 {tier!r})"
    return None


def validate_payload(items: list, slot: str, reviewer: str) -> str | None:
    if slot not in VALID_SLOTS:
        return f"slot 必须 ∈ {VALID_SLOTS} (实际 {slot!r})"
    if reviewer not in VALID_REVIEWERS:
        return f"reviewer 必须 ∈ {VALID_REVIEWERS} (实际 {reviewer!r})"
    if not isinstance(items, list):
        return "items 必须是数组"
    if len(items) > 3:
        return f"items 长度必须 0-3 (实际 {len(items)})"
    seen_ranks = set()
    for i, it in enumerate(items):
        item_id = f"items[{i}]"
        if not isinstance(it, dict):
            return f"{item_id} 不是 dict"
        rank = it.get("rank")
        if not isinstance(rank, int) or not (1 <= rank <= 3):
            return f"{item_id} rank 必须 ∈ [1,3] (实际 {rank!r})"
        if rank in seen_ranks:
            return f"{item_id} rank={rank} 重复"
        seen_ranks.add(rank)
        code = it.get("code")
        if not isinstance(code, str) or not CODE_RE.match(code):
            return f"{item_id} code 必须 6 位裸数字 (实际 {code!r})"
        err = _validate_deep_research(it.get("deep_research_json"), item_id)
        if err:
            return err
    return None


def db_path() -> str:
    return os.environ.get("SCOUT_DB_PATH", scout_db.DB_PATH)


def attach_industry_view(items: list) -> None:
    """落库前给每条 pick 的 deep_research_json 补算权威行业研究立场(幂等, fail-soft).

    已有 industry_view 则不覆盖(尊重 bot 自填); compute 任何异常都跳过, 绝不阻断落库.
    """
    try:
        import research_overlay  # 与本文件同目录, sys.path 已在模块顶部插入
    except Exception:
        return
    for it in items:
        dr = it.get("deep_research_json")
        if not isinstance(dr, dict) or dr.get("industry_view"):
            continue
        try:
            entries = research_overlay.compute(codes=[it.get("code", "")])
        except Exception:
            continue
        if entries:
            e = dict(entries[0])
            e.pop("name", None)  # name 与 daily_pick.name 重复, 不入 json
            dr["industry_view"] = e


def write_picks(items: list, slot: str, reviewer: str, trade_date: str) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = scout_db.conn(db_path())
    try:
        c.execute("BEGIN")
        c.execute(
            "DELETE FROM daily_pick WHERE trade_date=? AND slot=? AND reviewer=?",
            (trade_date, slot, reviewer))
        rows = []
        for it in items:
            dr = it["deep_research_json"]
            v = dr["verdict"]
            sources = it.get("sources")
            if isinstance(sources, list):
                sources = ",".join(sources)
            rows.append((
                trade_date, slot, reviewer, it["rank"],
                scout_db.bare(it["code"]), it.get("name"), sources,
                v["one_liner"], v["logic_stars"], v["action_stars"],
                v.get("entry_low"), v.get("entry_high"), v.get("stop_loss"),
                v["position_tier"],
                json.dumps(dr, ensure_ascii=False),
                None,  # research_session_id, wrapper 后填
                now,
            ))
        if rows:
            c.executemany(
                "INSERT INTO daily_pick "
                "(trade_date,slot,reviewer,rank,code,name,sources,one_liner,"
                "logic_stars,action_stars,entry_low,entry_high,stop_loss,"
                "position_tier,deep_research_json,research_session_id,picked_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.commit()
        return len(rows)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", required=True)
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--date", help="交易日 YYYY-MM-DD (默认今日)")
    a = ap.parse_args()
    today = a.date or datetime.now().strftime("%Y-%m-%d")
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {"items": []}
    except json.JSONDecodeError as e:
        _err(f"stdin JSON 解析失败: {e}")
        return 2
    if isinstance(payload, list):
        items = payload  # 容错: 直接传数组也接
    elif isinstance(payload, dict):
        items = payload.get("items", [])
    else:
        _err("stdin 顶层必须是数组或带 items 字段的对象")
        return 2

    err = validate_payload(items, a.slot, a.reviewer)
    if err:
        _err(err)
        return 2

    scout_db.init_schema(db_path())

    err = validate_codes_against_names(items, db_path())
    if err:
        _err(err)
        return 2

    err = validate_prices_sanity(items, db_path())
    if err:
        _err(err)
        return 2

    attach_industry_view(items)   # ← 新增: 兜底补算行业立场

    n = write_picks(items, a.slot, a.reviewer, today)
    print(f"[daily-pick-writer] {today} slot={a.slot} reviewer={a.reviewer} 写入 {n} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
