#!/usr/bin/env python3
"""批量运行 Qwen 语义抽取，并只晋升通过逐字引文校验的候选。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUN = "bot105d-daily-agenticdeep-charter-0424"
DEFAULT_BOT = "bot105d"
DEFAULT_MODEL = "qwen3.7-plus"


def natural_language_is_chinese(candidate: dict[str, Any]) -> bool:
    values: list[Any] = []
    for claim in candidate.get("claims") or []:
        values.append(claim.get("statement"))
        values.extend(item.get("step") for item in claim.get("mechanism") or [])
        for field in ("supporting_evidence", "contradicting_evidence"):
            values.extend(item.get("explanation") for item in claim.get(field) or [])
        values.extend(item.get("condition") for item in claim.get("falsifiers") or [])
    values.extend(item.get("mechanism") for item in candidate.get("relation_candidates") or [])
    for conflict in candidate.get("conflicts") or []:
        values.extend((conflict.get("resolution"), conflict.get("unresolved_issue")))
    return all(
        not isinstance(value, str) or value in ("", "unknown") or re.search(r"[\u3400-\u9fff]", value)
        for value in values
    )


def promote(candidate: Path, validation: Path, validated_candidate: Path, validated_validation: Path) -> bool:
    if not candidate.is_file() or not validation.is_file():
        return False
    report = json.loads(validation.read_text(encoding="utf-8"))
    if report.get("valid") is not True:
        return False
    envelope = json.loads(candidate.read_text(encoding="utf-8"))
    if not natural_language_is_chinese(envelope.get("candidate") or {}):
        return False
    candidate_tmp = validated_candidate.with_suffix(validated_candidate.suffix + ".tmp")
    validation_tmp = validated_validation.with_suffix(validated_validation.suffix + ".tmp")
    shutil.copyfile(candidate, candidate_tmp)
    shutil.copyfile(validation, validation_tmp)
    os.replace(candidate_tmp, validated_candidate)
    os.replace(validation_tmp, validated_validation)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default=DEFAULT_RUN)
    parser.add_argument("--bot", default=DEFAULT_BOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-dir", help="输出目录；多 run 应使用独立目录，避免同 bot/日期文件互相覆盖")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    run_root = ROOT / "world/runtime/runs" / args.run
    output_dir = Path(args.output_dir).resolve() if args.output_dir else ROOT / "world/runtime/decision-episodes" / args.model
    output_dir.mkdir(parents=True, exist_ok=True)
    extractor = Path(__file__).with_name("extract-canonical-qwen.py")
    dates = sorted(
        day.name for day in run_root.iterdir()
        if day.is_dir() and len(day.name) == 10
        and (not args.start or day.name >= args.start)
        and (not args.end or day.name <= args.end)
    )

    incomplete: list[dict[str, Any]] = []
    eligible: list[str] = []
    for date in dates:
        day_root = run_root / date / args.bot
        missing = [name for name in ("sent.md", "reply.json", "close_my_day.json") if not (day_root / name).is_file()]
        if missing:
            incomplete.append({"date": date, "missing": missing})
        else:
            eligible.append(date)

    def worker(date: str) -> dict[str, Any]:
        case_id = f"{args.bot}_{date}"
        validated_candidate = output_dir / f"{case_id}.validated.candidate.json"
        validated_validation = output_dir / f"{case_id}.validated.validation.json"
        if validated_candidate.is_file() and validated_validation.is_file() and natural_language_is_chinese(
            (json.loads(validated_candidate.read_text(encoding="utf-8")).get("candidate") or {})
        ):
            return {"date": date, "status": "already_validated"}
        base_candidate = output_dir / f"{case_id}.candidate.json"
        base_validation = output_dir / f"{case_id}.validation.json"
        if promote(base_candidate, base_validation, validated_candidate, validated_validation):
            return {"date": date, "status": "promoted_existing"}
        last_error = "unknown"
        for attempt in range(1, args.attempts + 1):
            suffix = "" if attempt == 1 and not base_candidate.exists() else f"batch-attempt-{attempt}"
            candidate = output_dir / f"{case_id}{'.' + suffix if suffix else ''}.candidate.json"
            validation = output_dir / f"{case_id}{'.' + suffix if suffix else ''}.validation.json"
            if candidate.exists() or validation.exists():
                continue
            command = [sys.executable, str(extractor), "--run", args.run, "--bot", args.bot, "--date", date, "--model", args.model]
            command += ["--output-dir", str(output_dir)]
            if suffix:
                command += ["--attempt", suffix]
            completed = subprocess.run(command, text=True, capture_output=True)
            if promote(candidate, validation, validated_candidate, validated_validation):
                report = json.loads(validation.read_text(encoding="utf-8"))
                return {"date": date, "status": "validated", "attempt": attempt, "counts": report.get("counts", {})}
            last_error = (completed.stderr or completed.stdout or f"exit={completed.returncode}")[-2000:]
        return {"date": date, "status": "failed", "error": last_error}

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(worker, date): date for date in eligible}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    results.sort(key=lambda item: item["date"])
    summary = {
        "run": args.run,
        "bot": args.bot,
        "date_count": len(dates),
        "eligible_count": len(eligible),
        "incomplete": incomplete,
        "status_counts": {status: sum(1 for item in results if item["status"] == status) for status in sorted({item["status"] for item in results})},
        "failed": [item for item in results if item["status"] == "failed"],
    }
    report_path = output_dir / f"batch-{args.run}-{args.bot}.json"
    report_path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), **summary}, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
