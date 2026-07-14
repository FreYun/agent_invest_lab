"""valuation_anchor_writer — agent(小奶龙) 估值锚点建议落库通道.

bot 在 valuation-anchor skill 中调用本脚本, 而非直接写库:
校验 payload -> sanity 钳制(valuation.clamp_agent) -> 算 valid_until -> 落
valuation_agent_view. "无需修正"(全 null)也必须落一行, 记录已审.

用法:
  python3 valuation_anchor_writer.py --reviewer bot11 --json '<payload>'
  python3 valuation_anchor_writer.py --reviewer bot11 --stdin  < payload.json
  python3 valuation_anchor_writer.py --reviewer bot11 --onboard --json '<draft>'
payload schema 见 workspace/skills/research/valuation-anchor/SKILL.md.
exit: 0 成功 / 3 校验失败.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import valuation  # noqa: E402

VALID_TRIGGERS = ("weekly", "earnings", "driver_shift", "band_breach", "manual",
                  "daily_st", "consensus_shift")
NUMERIC_KEYS = ("driver_center", "driver_low", "driver_high",
                "pe_lo_adj", "pe_hi_adj", "st_center_adj", "st_width_pct")
DAILY_ST_TTL_DAYS = 2    # 每日情绪研判保鲜 2 交易日(日历近似 3 天), 过期回落机械

MIN_BRIEF_LEN = 50               # 建模草案 brief 最短长度(字)
# 单 driver 或 basket 成分可用的 kind(basket 校验在 valuation.validate_driver 内);
# 仅 commodity_pe 生效
ONBOARD_DRIVER_KINDS = ("sge", "fut")


def _valid_until(today: datetime, ttl_days: int) -> str:
    """交易日 TTL 的日历日近似(trading_calendar 表为空): n 交易日 ≈ round(n×1.45) 自然日."""
    return (today + timedelta(days=round(ttl_days * 1.45))).strftime("%Y%m%d")


def _validate(p: dict, targets: dict) -> None:
    for k in ("ts_code", "rationale", "trigger"):
        if not p.get(k):
            raise ValueError(f"payload 缺必填字段: {k}")
    if p["ts_code"] not in targets:
        raise ValueError(f"未配置的标的: {p['ts_code']} (见 valuation-targets.json)")
    if p["trigger"] not in VALID_TRIGGERS:
        raise ValueError(f"非法 trigger: {p['trigger']}")
    for k in NUMERIC_KEYS + ("confidence",):
        v = p.get(k)
        if v is not None and not isinstance(v, (int, float)):
            raise ValueError(f"{k} 必须是数值或 null: {v!r}")
    for q, pair in (p.get("profit_adj") or {}).items():
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not all(isinstance(x, (int, float)) for x in pair)
                or pair[0] > pair[1]):
            raise ValueError(f"profit_adj.{q} 必须是 [lo, hi] 且 lo<=hi: {pair!r}")


def write(payload: dict, reviewer: str, db_path=None) -> dict:
    """校验 -> 钳制 -> 落库. 返回 {'run_at','valid_until','clamped'}."""
    targets = valuation.load_targets()
    _validate(payload, targets)
    ts_code = payload["ts_code"]
    cfg = targets[ts_code]
    c = scout_db.conn(db_path or scout_db.DB_PATH)
    try:
        band = c.execute(
            "SELECT meta_json FROM valuation_band_daily WHERE ts_code=? "
            "ORDER BY trade_date DESC LIMIT 1", (ts_code,)).fetchone()
        ctx = (json.loads(band["meta_json"] or "{}") if band else {}).get("clamp_ctx")
        has_numeric = (any(payload.get(k) is not None for k in NUMERIC_KEYS)
                       or payload.get("profit_adj"))
        if has_numeric and not ctx:
            raise ValueError("最新带子缺 clamp_ctx, 拒绝数值修正(机械带兜底); "
                             "先跑 valuation.py 再提交")
        p, clamped = (valuation.clamp_agent(payload, ctx) if (has_numeric and ctx)
                      else (dict(payload), {}))
        now = datetime.now()
        run_at = now.strftime("%Y-%m-%d %H:%M:%S")
        ttl = (DAILY_ST_TTL_DAYS if p["trigger"] == "daily_st"
               else cfg["agent_ttl_days"])
        vu = _valid_until(now, ttl)
        c.execute(
            "INSERT INTO valuation_agent_view (run_at, ts_code, reviewer, "
            "driver_center, driver_low, driver_high, profit_adj_json, pe_lo_adj, "
            "pe_hi_adj, st_center_adj, st_width_pct, confidence, rationale, "
            "sources_json, clamped_json, valid_until, trigger) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_at, ts_code, reviewer,
             p.get("driver_center"), p.get("driver_low"), p.get("driver_high"),
             json.dumps(p["profit_adj"], ensure_ascii=False)
             if p.get("profit_adj") else None,
             p.get("pe_lo_adj"), p.get("pe_hi_adj"),
             p.get("st_center_adj"), p.get("st_width_pct"), p.get("confidence"),
             p["rationale"],
             json.dumps(p.get("sources") or [], ensure_ascii=False),
             json.dumps(clamped, ensure_ascii=False) if clamped else None,
             vu, p["trigger"]))
        c.commit()
        return {"run_at": run_at, "valid_until": vu, "clamped": clamped}
    finally:
        c.close()


def _validate_onboard(p: dict) -> None:
    """建模草案 payload 校验: 必填字段、类型、枚举、长度."""
    for k in ("ts_code", "rationale"):
        if not p.get(k):
            raise ValueError(f"onboard payload 缺必填字段: {k}")
    if not isinstance(p.get("applicable"), bool):
        raise ValueError("applicable 必须是 true/false")
    if not p["applicable"]:
        return                       # 不适用: 只需 rationale 说明
    model = p.get("model") or {}
    kind = model.get("kind")
    if kind not in valuation.MODEL_RUNNERS:
        raise ValueError(f"model.kind 必须是已实现模型 "
                         f"{sorted(valuation.MODEL_RUNNERS)}: {kind!r}")
    if len((model.get("brief") or "").strip()) < MIN_BRIEF_LEN:
        raise ValueError(f"model.brief 必须≥{MIN_BRIEF_LEN}字(该股估值方法论)")
    # driver 是 commodity_pe 专属: 校验走 valuation.validate_driver(单一事实源);
    # 其余 kind 传了 driver 报错(防语义混淆)。取数可得性仍由首跑失败信号兜底。
    if kind == "commodity_pe":
        valuation.validate_driver(p["ts_code"], p.get("driver") or {})
    elif p.get("driver"):
        raise ValueError(f"{kind} 不接受 driver 字段(商品驱动专属), 请去掉")
    inds = p.get("research_industries")
    if not isinstance(inds, list) or not inds:
        raise ValueError("research_industries 必须是非空列表")


def write_onboard(payload: dict, reviewer: str, db_path=None) -> dict:
    """建模研判草案落 valuation_target_draft. 前置: add_target 已落 drafting 行."""
    _validate_onboard(payload)
    ts_code = payload["ts_code"]
    c = scout_db.conn(db_path or scout_db.DB_PATH)
    try:
        row = c.execute("SELECT status FROM valuation_target_draft WHERE ts_code=?",
                        (ts_code,)).fetchone()
        if not row:
            raise ValueError(f"{ts_code} 无草案记录(研究部未从页面发起加股?)")
        status = "draft_ready" if payload["applicable"] else "unsupported"
        payload = dict(payload)
        payload["reviewer"] = reviewer
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 状态守卫: 只允许覆盖 drafting 行, 防 bot 迟到/重复调用翻覆已确认状态
        before = c.total_changes
        c.execute("UPDATE valuation_target_draft SET status=?, draft_json=?, "
                  "error=NULL, updated_at=? WHERE ts_code=? AND status='drafting'",
                  (status, json.dumps(payload, ensure_ascii=False),
                   now_str, ts_code))
        if c.total_changes == before:
            # rowcount 为 0: 行存在但状态已不是 drafting(迟到/重复)
            cur_status = c.execute(
                "SELECT status FROM valuation_target_draft WHERE ts_code=?",
                (ts_code,)).fetchone()["status"]
            raise ValueError(
                f"{ts_code} 草案状态已不是 drafting(当前: {cur_status}), "
                "拒绝覆盖(迟到/重复的建模结论?)")
        c.commit()
        return {"status": status}
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="agent 估值锚点建议落库")
    ap.add_argument("--reviewer", default="bot11")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", help="payload JSON 字符串")
    g.add_argument("--stdin", action="store_true", help="从 stdin 读 payload")
    ap.add_argument("--onboard", action="store_true",
                    help="建模研判草案落 valuation_target_draft(而非锚点表)")
    ap.add_argument("--db", help="覆盖 DB 路径(测试用)")
    a = ap.parse_args()
    raw = sys.stdin.read() if a.stdin else a.json
    try:
        payload = json.loads(raw)
        if a.onboard:
            out = write_onboard(payload, a.reviewer, db_path=a.db)
            print(f"[anchor-writer] ONBOARD OK status={out['status']}")
            return 0
        out = write(payload, a.reviewer, db_path=a.db)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[anchor-writer] 校验失败: {e}", file=sys.stderr)
        return 3
    msg = f"[anchor-writer] OK run_at={out['run_at']} 生效至={out['valid_until']}"
    if out["clamped"]:
        msg += f" 钳制字段={list(out['clamped'])}"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
