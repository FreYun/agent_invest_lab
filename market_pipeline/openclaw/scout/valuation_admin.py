"""valuation_admin — 估值带标的自助管理(加股草案状态机 + 停用/恢复).

server.py /api/valuation/target/* 路由薄转发到这里. 数据流(三期设计):
add -> draft(drafting) -> spawn valuation_agent.py --onboard -> bot 经
writer --onboard 落草案(draft_ready/unsupported) -> enable 合并草案+研究部
现场修改, 原子写 valuation-targets.json(load_targets 每次调用重读,
原子写防读到半截文件) -> spawn 首次出带.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout_db  # noqa: E402
import valuation  # noqa: E402
import valuation_anchor_writer as anchor_writer  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
_BARE_RE = re.compile(r"^\d{6}$")

# drafting 孤儿超时阈值(分钟): 超过此值视为后台 spawn 进程失联, 允许覆盖重发。
# 必须 > valuation_agent.ONBOARD_HTTP_TIMEOUT_SEC(45min): 首次建模走深度研究耗时长,
# 阈值太短会在研判进行中放行重发, 造成重复派发。
DRAFTING_STALE_MIN = 60

# server 多线程下 targets.json 读改写互斥; 跨进程写者不存在——spawn 的子进程不写 targets
_TARGETS_LOCK = threading.Lock()


def _now() -> str:
    """当前时间戳(年-月-日 时:分:秒)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: str, data) -> None:
    """临时文件 + os.replace 原子落盘(load_targets mtime 热更依赖完整文件)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_targets_raw(path: str) -> dict:
    """读取原始 targets 配置字典."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def add_target(c, body, targets_path: str = valuation.TARGETS_PATH):
    """发起加股/重建模: 解析输入(代码或名称) -> 查重 -> 落 drafting 草案行. 不 spawn(handler 负责).

    返回 (http_code, body_dict)
    - 200: 成功,返回 ts_code/name/remodel/status
    - 400: 代码格式错误或不存在(名称多命中时带 candidates)
    - 409: 该股已有草案进行中(drafting)
    """
    raw = str((body or {}).get("code", "")).strip()
    bare = raw.upper().split(".")[0]
    if _BARE_RE.match(bare):
        row = c.execute("SELECT ts_code, name FROM stock_names WHERE code=?",
                        (bare,)).fetchone()
        if not row or not row["ts_code"]:
            return 400, {"error": f"{bare} 不在 stock_names(确认代码是否正确)"}
    else:
        # 名称路径: 精确命中优先, 否则模糊; 多命中返回候选由页面点选
        if not raw:
            return 400, {"error": "请输入 6 位代码或股票名称"}
        row = c.execute("SELECT ts_code, name FROM stock_names "
                        "WHERE name=? AND ts_code IS NOT NULL", (raw,)).fetchone()
        if not row:
            rows = c.execute("SELECT code, ts_code, name FROM stock_names "
                             "WHERE name LIKE ? AND ts_code IS NOT NULL "
                             "ORDER BY code LIMIT 10", (f"%{raw}%",)).fetchall()
            if not rows:
                return 400, {"error": f"未找到匹配「{raw}」的股票(也可输 6 位代码)"}
            if len(rows) > 1:
                return 400, {"error": "匹配到多只, 请点选",
                             "candidates": [{"code": r["code"], "ts_code": r["ts_code"],
                                             "name": r["name"]} for r in rows]}
            row = rows[0]
    ts_code, name = row["ts_code"], row["name"]
    # 检查是否已是启用的标的(重建模的标志)
    remodel = ts_code in _read_targets_raw(targets_path)
    # 检查是否有未完成的建模(drafting 状态)
    # 若 updated_at 超过 DRAFTING_STALE_MIN 分钟视为孤儿(spawn 进程失联), 放行重置
    d = c.execute("SELECT status, updated_at FROM valuation_target_draft "
                  "WHERE ts_code=?", (ts_code,)).fetchone()
    if d and d["status"] == "drafting":
        try:
            # updated_at 恒由 _now() 写入(本地时间, 无时区标记), 故用 datetime.now() 本地时比较;
            # 禁止用 SQLite strftime('%s','now') 计算 age: SQLite 把无时区字符串按 UTC 解析,
            # 而 _now() 写的是 Asia/Shanghai(UTC+8), 导致 age 被低估 8h(孤儿 2h → 算出 -6h),
            # 阈值 30 分钟形同虚设——真孤儿要 8.5h 后才放行.
            age_minutes = (datetime.now() - datetime.strptime(
                d["updated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
        except Exception:
            age_minutes = 0   # 保守: 格式异常视为新鲜, 仍 409
        if age_minutes <= DRAFTING_STALE_MIN:
            return 409, {"error": f"{ts_code} 建模研判进行中, 请等待完成或失败后重试"}
    now = _now()
    c.execute("INSERT INTO valuation_target_draft (ts_code, name, status, "
              "draft_json, error, created_at, updated_at) "
              "VALUES (?,?,'drafting',NULL,NULL,?,?) "
              "ON CONFLICT(ts_code) DO UPDATE SET status='drafting', "
              "draft_json=NULL, error=NULL, updated_at=excluded.updated_at",
              (ts_code, name, now, now))
    c.commit()
    return 200, {"ok": True, "ts_code": ts_code, "name": name,
                 "remodel": remodel, "status": "drafting"}


def list_drafts(c) -> dict:
    """草案列表(页面轮询). draft_json 解析成 draft 键."""
    rows = c.execute("SELECT * FROM valuation_target_draft "
                     "ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["draft"] = json.loads(d.pop("draft_json") or "null")
        except json.JSONDecodeError:
            d["draft"] = None
        out.append(d)
    return {"drafts": out}


def enable_target(c, body, targets_path: str = valuation.TARGETS_PATH):
    """确认启用: 草案 + 研究部现场修改(brief/research_industries) 合并写 targets.
    重建模(标的已在配置)只替换 model/driver/research_industries, 其余保留.

    返回 (http_code, body_dict)
    - 200: 成功
    - 400: 无草案记录/状态不对/brief太短
    """
    ts_code = str((body or {}).get("ts_code", "")).strip().upper()
    row = c.execute("SELECT * FROM valuation_target_draft WHERE ts_code=?",
                    (ts_code,)).fetchone()
    if not row:
        return 400, {"error": f"{ts_code} 无草案记录"}
    if row["status"] != "draft_ready":
        return 400, {"error": f"草案状态 {row['status']} 不可启用(仅 draft_ready)"}
    draft = json.loads(row["draft_json"] or "{}")
    # 优先取 body 中的 brief(研究部现场修改),否则用草案中的
    brief = str(body.get("brief") or (draft.get("model") or {}).get("brief")
                or "").strip()
    if len(brief) < anchor_writer.MIN_BRIEF_LEN:
        return 400, {"error": f"model.brief 不足 {anchor_writer.MIN_BRIEF_LEN} 字"}
    # 优先取 body 中的 research_industries,否则用草案中的
    inds = body.get("research_industries") or draft.get("research_industries") or []
    # model.kind 以草案为准(body.model_kind 可现场覆盖); 缺/非法硬拒, 不再默认
    kind = str(body.get("model_kind")
               or (draft.get("model") or {}).get("kind") or "").strip()
    if kind not in valuation.MODEL_RUNNERS:
        return 400, {"error": f"草案缺合法 model.kind"
                              f"(可选 {sorted(valuation.MODEL_RUNNERS)}), 请重新建模"}
    # driver 落盘前最后防线(仅商品模型): 坏 driver 落盘会让 load_targets 全体抛错
    if kind == "commodity_pe":
        try:
            valuation.validate_driver(ts_code, draft.get("driver") or {})
        except ValueError as e:
            return 400, {"error": f"草案 driver 不合法, 拒绝启用: {e}"}
    with _TARGETS_LOCK:
        # targets.json 读改写加锁, 防 ThreadingHTTPServer 并发丢更新
        data = _read_targets_raw(targets_path)
        entry = data.get(ts_code) or {}
        # 重建模时只替换这三个键,其他字段保留
        entry.update({
            "name": row["name"],
            "model": {"kind": kind, "brief": brief},
            "research_industries": inds,
            "enabled": True,
        })
        if kind == "commodity_pe":
            entry["driver"] = draft.get("driver") or {}
        else:
            entry.pop("driver", None)   # 重建模切 kind 时清掉旧商品 driver
        # 新标的才补充 .openclaw(重建模已有 .openclaw 则保留)
        if draft.get(".openclaw") and not entry.get(".openclaw"):
            entry[".openclaw"] = draft[".openclaw"]
        data[ts_code] = entry
        _atomic_write_json(targets_path, data)
    # draft 表 UPDATE 可留在锁外(无竞态: 同一 ts_code 同时启用的概率极低且幂等)
    c.execute("UPDATE valuation_target_draft SET status='enabled', updated_at=? "
              "WHERE ts_code=?", (_now(), ts_code))
    c.commit()
    return 200, {"ok": True, "ts_code": ts_code}


def dismiss_draft(c, body):
    """放弃草案(留档): 仅终局前状态(draft_ready/unsupported/failed)可放弃.

    返回 (http_code, body_dict)
    - 200: 成功
    - 400: 无记录或状态不对
    """
    ts_code = str((body or {}).get("ts_code", "")).strip().upper()
    row = c.execute("SELECT status FROM valuation_target_draft WHERE ts_code=?",
                    (ts_code,)).fetchone()
    if not row:
        return 400, {"error": f"{ts_code} 无草案记录"}
    if row["status"] not in ("draft_ready", "unsupported", "failed"):
        return 400, {"error": f"状态 {row['status']} 不可放弃"}
    c.execute("UPDATE valuation_target_draft SET status='dismissed', updated_at=? "
              "WHERE ts_code=?", (_now(), ts_code))
    c.commit()
    return 200, {"ok": True, "ts_code": ts_code}


def toggle_target(body, targets_path: str = valuation.TARGETS_PATH):
    """停用/恢复: 只改 enabled 字段, 配置与历史数据全保留.

    返回 (http_code, body_dict)
    - 200: 成功,返回 enabled 状态
    - 400: 标的不在配置
    """
    ts_code = str((body or {}).get("ts_code", "")).strip().upper()
    enabled = bool((body or {}).get("enabled"))
    with _TARGETS_LOCK:
        # targets.json 读改写加锁, 防 ThreadingHTTPServer 并发丢更新
        data = _read_targets_raw(targets_path)
        if ts_code not in data:
            return 400, {"error": f"{ts_code} 不在标的配置"}
        data[ts_code]["enabled"] = enabled
        _atomic_write_json(targets_path, data)
    return 200, {"ok": True, "ts_code": ts_code, "enabled": enabled}


def _spawn(cmd: list[str], log_path: str) -> None:
    """后台 detached 子进程(仿 server._spawn_whitelist_backfill).
    flock 阻塞排队串行; start_new_session 脱离请求进程; best-effort 失败静默."""
    try:
        logf = open(log_path, "a")
        subprocess.Popen(cmd, cwd=HERE, stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True)
        logf.close()
    except Exception:
        pass


def spawn_onboard(ts_code: str) -> None:
    """派生 valuation_agent.py --onboard 建模."""
    _spawn(["flock", "/tmp/scout-valuation-onboard.lock", "/usr/bin/python3.12",
            os.path.join(HERE, "valuation_agent.py"), "--onboard", ts_code],
           "/tmp/scout-valuation-onboard.log")


def spawn_first_band(ts_code: str) -> None:
    """启用后立即首次出带(不等盘后 cron). 失败无妨, 当晚 cron 兜底."""
    _spawn(["flock", "/tmp/scout-valuation-firstrun.lock", "/usr/bin/python3.12",
            os.path.join(HERE, "valuation.py"), "--code", ts_code],
           "/tmp/scout-valuation-firstrun.log")


def search_stocks(c, q: str, limit: int = 10, py_match=None) -> dict:
    """个股搜索(加股自动补全): stock_names 全A股, 代码前缀/名称模糊/拼音首字母三路合并.

    py_match: server.py 注入的拼音首字母匹配函数 (name, q)->bool;
    None 则跳过拼音路(valuation_admin 不 import server, 防循环依赖).
    """
    q = str(q or "").strip()
    if not q:
        return {"stocks": []}
    res: dict[str, dict] = {}

    def _take(rows):
        for r in rows:
            if len(res) >= limit:
                return
            res.setdefault(r["code"], {"code": r["code"], "ts_code": r["ts_code"],
                                       "name": r["name"]})

    if q[:1].isdigit():
        _take(c.execute("SELECT code, ts_code, name FROM stock_names "
                        "WHERE code LIKE ? ORDER BY code LIMIT ?",
                        (q + "%", limit)).fetchall())
    if len(res) < limit:
        _take(c.execute("SELECT code, ts_code, name FROM stock_names "
                        "WHERE name LIKE ? ORDER BY code LIMIT ?",
                        (f"%{q}%", limit)).fetchall())
    ql = q.lower()
    if py_match and ql.isascii() and ql.isalpha() and len(res) < limit:
        for r in c.execute("SELECT code, ts_code, name FROM stock_names ORDER BY code"):
            if r["code"] in res:
                continue
            if py_match(r["name"] or "", ql):
                res[r["code"]] = {"code": r["code"], "ts_code": r["ts_code"],
                                  "name": r["name"]}
                if len(res) >= limit:
                    break
    return {"stocks": list(res.values())[:limit]}
