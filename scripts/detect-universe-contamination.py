"""一次性扫描 world/runtime/runs，标记可买池被并发 run 污染过的历史 run。

背景：早期 fund-portfolio-mcp 用单一全局文件 FUND_BUYABLE_CODES_FILE 承载可买基金白名单。
两个 world run 并发跑时，后启动 run 的 setup 会覆盖该文件，把先开 run 的可买池替换成
后开 run 的——bot11(半导体, 014854) 被 bot16(黄金, 000216) 污染就是这个机制。
新代码改成 <FUND_BUYABLE_CODES_DIR>/<run_id>.json 物理隔离不再有此问题；本脚本扫历史
runtime/runs，把受影响的 run 标记出来，dashboard 端贴 ⚠ 标。

检测逻辑（保守）：
  1. 每个 run/run.log 提取"fund buyable codes pinned (N): code1,code2,…"行 + 时间戳 +
     源 run 自己的 codes。
  2. 每个 run 的活动窗口 [started_at, end_at)：
       end_at = state.json.updated_at if status != 'running' else now
  3. 对每个 candidate run R，扫所有 OTHER run 的 pinned 行；若另一 run X 的 pinned 时间
     落在 R 的活动窗口内 **且 codes 与 R 自己不同** → R 受 X 污染。
  4. 写 <runDir>/universe-contamination.json marker（{source_run_id, pinned_at,
     polluted_codes, original_codes, detected_at}）。已存在 → skip（幂等）。

只读原 run.log / state.json；只写 marker 文件。安全可重跑。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


# run.log 行示例：
# 2026-05-18T12:02:30.096Z fund buyable codes pinned (1): 014854
# 2026-05-18T12:07:02.702Z fund buyable codes pinned (1): 000216
# 2026-05-18T12:02:30.096Z fund buyable codes pinned (5): 014854,015453,…
PINNED_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\s+"
    r"fund buyable codes pinned \(\d+\):\s*(?P<codes>.+)$"
)


def parse_iso(ts: str) -> datetime:
    """Z 形式 ISO8601 → aware datetime。"""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def parse_pinned_codes(s: str) -> list[str]:
    """log 行里 codes 段可能因 .slice(0,8) 截断 + 加 ',…'。截断的列表不是完整 codes，
    但首 8 个对比足以判定"两个 run 的 codes 不同"——它们的差异往往在前几个就显现。"""
    s = s.strip()
    out: list[str] = []
    truncated = False
    for raw in s.split(","):
        raw = raw.strip()
        if raw in ("…", "..."):
            truncated = True
            continue
        if raw:
            out.append(raw)
    # 标记附在末元素后缀也算
    if out and out[-1].endswith("…"):
        out[-1] = out[-1].rstrip("…").strip()
        truncated = True
    return out


def extract_pinned(run_log: Path) -> tuple[datetime, list[str]] | None:
    """读 run.log 第一行 pinned，返回 (ts, codes)。没有 → None（这个 run 不用 fund 服务）。"""
    try:
        with run_log.open(encoding="utf-8") as f:
            for line in f:
                m = PINNED_RE.match(line.rstrip("\n"))
                if m:
                    try:
                        return parse_iso(m.group("ts")), parse_pinned_codes(m.group("codes"))
                    except ValueError:
                        continue
    except OSError:
        return None
    return None


def extract_window(state_json: Path) -> tuple[datetime, datetime] | None:
    """从 state.json 取 [started_at, end_at)；run 仍在 running 时 end_at = now。"""
    try:
        s = json.loads(state_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        started = parse_iso(s["started_at"])
    except (KeyError, ValueError, TypeError):
        return None
    if s.get("status") and s["status"] != "running":
        end_str = s.get("updated_at") or s.get("started_at")
        try:
            ended = parse_iso(end_str)
        except (ValueError, TypeError):
            ended = datetime.now(timezone.utc)
    else:
        ended = datetime.now(timezone.utc)
    if ended < started:
        ended = started
    return started, ended


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-dir",
        default="/home/rooot/agent_invest_lab/world/runtime/runs",
        help="world runs 根目录（每个子目录是一个 run）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印检测结果，不写 marker 文件",
    )
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print(f"runs dir not found: {runs_dir}")
        return 2

    # 第一遍：扫所有 run 提取 (pinned_ts, codes) 和活动窗口
    runs: list[dict] = []
    for sub in sorted(runs_dir.iterdir()):
        if not sub.is_dir():
            continue
        pinned = extract_pinned(sub / "run.log")
        window = extract_window(sub / "state.json")
        if pinned is None or window is None:
            continue
        runs.append({
            "run_id": sub.name,
            "pinned_ts": pinned[0],
            "pinned_codes": pinned[1],
            "started_at": window[0],
            "ended_at": window[1],
            "run_dir": sub,
        })

    if not runs:
        print(f"no eligible runs found under {runs_dir}")
        return 0

    print(f"scanning {len(runs)} runs with fund-buyable activity")

    written = 0
    skipped = 0
    clean = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for r in runs:
        marker = r["run_dir"] / "universe-contamination.json"
        if marker.exists():
            skipped += 1
            continue
        # 找所有"在 r 活动窗内 pin 了不同 codes 的其它 run"
        # 取最早的一个作为 polluter 主因（后续的覆盖都基于已被污染的状态，元凶就是首个）。
        contaminators: list[dict] = []
        for other in runs:
            if other["run_id"] == r["run_id"]:
                continue
            if not (r["started_at"] <= other["pinned_ts"] <= r["ended_at"]):
                continue
            # codes 完全相同 → 无害（同一可买池被重写一遍，bot 看到的池没变）
            if set(other["pinned_codes"]) == set(r["pinned_codes"]):
                continue
            contaminators.append(other)
        if not contaminators:
            clean += 1
            continue
        first = min(contaminators, key=lambda x: x["pinned_ts"])
        payload = {
            "source_run_id": first["run_id"],
            "pinned_at": first["pinned_ts"].isoformat(),
            "polluted_codes": first["pinned_codes"],
            "original_codes": r["pinned_codes"],
            "all_polluters": [c["run_id"] for c in contaminators],
            "detected_at": now_iso,
            "note": "auto-detected by scripts/detect-universe-contamination.py — bot may have placed orders against polluted_codes mid-run",
        }
        if args.dry_run:
            print(f"  WOULD MARK {r['run_id']} (polluted by {first['run_id']} at {payload['pinned_at']}; "
                  f"orig={r['pinned_codes']} → polluted={first['pinned_codes']})")
        else:
            marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"  MARKED {r['run_id']} (polluted by {first['run_id']}; "
                  f"orig={r['pinned_codes']} → polluted={first['pinned_codes']})")
        written += 1

    print(f"\nsummary: marked={written} skipped(already_marked)={skipped} clean={clean} "
          f"total_scanned={len(runs)}{' [DRY RUN]' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
