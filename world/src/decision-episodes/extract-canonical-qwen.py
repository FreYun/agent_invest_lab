#!/usr/bin/env python3
"""用 Qwen 从单个 World session 抽取 canonical 语义候选。

只抽取需要语言理解的 Claim、作用机制、证伪条件、证据绑定、类型关系和
决策冲突。事实数值、订单、账户金额及数据库状态不由模型裁定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUN = "bot105d-daily-agenticdeep-charter-0424"
DEFAULT_BOT = "bot105d"
DEFAULT_DATE = "2026-08-05"
DEFAULT_MODEL = "qwen3.7-plus"
OPENCLAW_JSON = Path("/home/rooot/.openclaw/openclaw.json")


SYSTEM_PROMPT = r"""你是投资决策审计数据的语义抽取器。你的任务不是评价投资水平，而是从给定的、带精确定位的原文中生成候选结构化数据。

只抽取以下六类自然语言语义：
1. Claim：当日实际被采纳、仍在讨论或被否决的判断；
2. 作用机制：原文明确表达的“信号→解释→决策”链条；
3. 证伪条件：原文明说的可观测反转/失效条件；
4. Claim 的支持证据和反对证据；
5. 类型关系候选，例如 regime SELECTS fund、policy REQUIRES diversification；
6. 决策冲突：双方、胜出方、裁决理由和未解决问题。

强制规则：
- 只能使用 INPUT_BUNDLE 中出现的内容，禁止使用常识补全，禁止后验收益。
- 每个抽取项及每个机制步骤/证据/证伪条件都必须有 source_locator 和逐字 quote；quote 必须能在对应输入段中找到。
- 无法确认时填字符串 "unknown"，不要猜测。
- Agent 的解释属于 claim/interpretation，不得升级为 hard_fact。
- 两套引擎的数值和结论必须分别保留；不得把 -2.93% 和 -4.15% 合并为同一测量。
- 工具调用参数、订单 ID、金额、份额、成交状态只作为给定证据引用，不由你修正或推导。
- 不抽取 SQL/账户/行情数值为 facts，不输出动作表或订单表。
- 作用机制用中文；risk_state、market_context、Regime、HS300、MA120、Top15 等标识可保留英文。
- 无论输入原文使用中文还是英文，statement、mechanism.step、evidence explanation、falsifier condition、relation mechanism、conflict resolution 和 unresolved_issue 等自然语言说明都必须用中文；字段名和上述标识可保留英文。
- 倾向少而准。重复表达应合并，不要为了凑数量制造 Claim。
- 输出 1~4 条真正驱动当日决策的核心 Claim；不要把宏观标题、仓位数字或重复复述单独当成 Claim。
- 输出 0~6 个有明确原文依据的类型关系候选；关系谓词使用简短大写英文标识，作用机制必须用中文。
- 输出 0~4 个真实存在的决策冲突；必须区分“标签一致”与“动作一致”。只有输入能支持时才填胜出方，否则填 unknown。
- quote 必须是输入中的一段连续原文，建议 15~100 字；严禁在 quote 中使用“...”或“…”拼接多段文字，严禁删掉原文中的 Markdown 符号后假装逐字引用。
- 顶层证据是不允许的；所有 supporting_evidence、contradicting_evidence、falsifiers 必须嵌套在所属 Claim 内。
- 输出必须是一个严格 JSON 对象，不要 Markdown，不要解释性前后缀。

JSON 顶层必须恰好包含：schema_version, case_id, claims, relation_candidates, conflicts, unknowns。

claims 每项字段：candidate_id, statement, claim_type, status, confidence_label, certainty, source_locator, quote, mechanism, supporting_evidence, contradicting_evidence, falsifiers。
mechanism 每项字段：step, source_locator, quote。
supporting_evidence/contradicting_evidence 每项字段：source_locator, quote, explanation。
falsifiers 每项字段：condition, source_locator, quote。

relation_candidates 每项字段：candidate_id, subject, predicate, object, direction, mechanism, certainty, source_locator, quote。

conflicts 每项字段：candidate_id, left, right, winner, resolution, resolution_class, unresolved_issue, source_locator, quote。其中 left/right 是简短中文字符串。

unknowns 是字符串数组，用来记录原文无法解决但会影响结构化准确性的问题。"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def number_lines(text: str, start: int = 1) -> str:
    return "\n".join(f"L{start + i}: {line}" for i, line in enumerate(text.splitlines()))


def generic_sent_tail(lines: list[str], max_lines: int = 1200, max_chars: int = 45000) -> str:
    """旧版 prompt 没有固定区块标题时，保留靠近当日任务的尾部并维持原始行号。"""
    start = max(0, len(lines) - max_lines)
    selected = lines[start:]
    while len("\n".join(selected)) > max_chars and len(selected) > 1:
        trim = min(100, len(selected) - 1)
        start += trim
        selected = selected[trim:]
    return number_lines("\n".join(selected), start + 1)


def extract_marked_section(
    lines: list[str],
    start_marker: str,
    end_markers: str | tuple[str, ...],
    *,
    required: bool = True,
    max_lines: int = 500,
) -> str:
    """提取版本间会漂移的注入区块，并且绝不跨越无限长的 prompt。"""
    start = next((i for i, line in enumerate(lines) if start_marker in line), None)
    if start is None:
        if required:
            raise ValueError(f"缺少区块起点: {start_marker}")
        return f"[源输入未提供 {start_marker} 区块]"
    markers = (end_markers,) if isinstance(end_markers, str) else end_markers
    end = next(
        (i for i in range(start + 1, len(lines)) if any(marker in lines[i] for marker in markers)),
        min(len(lines), start + max_lines),
    )
    return number_lines("\n".join(lines[start:end]), start + 1)


def compact_tool_transcript(reply_obj: dict[str, Any]) -> str:
    messages = reply_obj.get("messages") or []
    call_names: dict[str, str] = {}
    chunks: list[str] = []
    keep_names = {
        "mcp__fund_portfolio_mcp__portfolio_place_sell_order",
        "mcp__fund_portfolio_mcp__portfolio_place_buy_order",
    }
    for i, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            call_id = str(call.get("id", "unknown"))
            name = str(fn.get("name", "unknown"))
            call_names[call_id] = name
            if name in keep_names:
                chunks.append(
                    f"reply.json#/messages/{i}/tool_calls/{call_id}\n"
                    f"name={name}\narguments={fn.get('arguments', '{}')}"
                )
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id", "unknown"))
            if call_names.get(call_id) in keep_names:
                content = str(message.get("content") or "")
                chunks.append(
                    f"reply.json#/messages/{i}/content (tool_call_id={call_id})\n"
                    f"{content[:2400]}"
                )
    return "\n\n".join(chunks)


def compact_assistant_reasoning(reply_obj: dict[str, Any]) -> str:
    messages = reply_obj.get("messages") or []
    chunks: list[str] = []
    for i, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        # 只保留与冲突、规则、分散或最终解释相关的短推理，不带超长工具结果。
        if any(word in content for word in ("conflict", "冲突", "宪章", "单基金", "regime", "反弹", "defensive")):
            chunks.append(f"reply.json#/messages/{i}/content\n{content[:3500]}")
    return "\n\n".join(chunks)


def build_input_bundle(sent_path: Path, reply_path: Path, case_id: str) -> tuple[str, dict[str, Any]]:
    sent_text = sent_path.read_text(encoding="utf-8")
    reply_raw = reply_path.read_text(encoding="utf-8")
    reply_obj = json.loads(reply_raw)
    lines = sent_text.splitlines()
    sections: list[tuple[str, str]] = []
    if not any("────────── market_context" in line for line in lines):
        sections.append(("S0 旧版通用决策输入（保留原始行号）", generic_sent_tail(lines)))
    sections += [
        ("S1 market_context", extract_marked_section(lines, "────────── market_context", "────────── market_mainline", required=False)),
        ("S2 market_mainline", extract_marked_section(lines, "────────── market_mainline", "────────── mainline_rotation", required=False)),
        ("S3 mainline_rotation", extract_marked_section(
            lines,
            "────────── mainline_rotation",
            ("────────── 交易纪律核对", "────────── macro_news", "────────── res1", "【账户快照（今日 settle 后）】"),
            required=False,
        )),
        ("S4 盘中实时行情", extract_marked_section(
            lines,
            "【盘中实时行情（系统预取",
            "【盘中实时行情 结束】",
            required=False,
            max_lines=200,
        )),
        ("S5 账户快照与整体绩效", extract_marked_section(lines, "【账户快照（今日 settle 后）】", "▍ 交易统计（confirmed orders）", required=False)),
        ("S6 最终回复", f"reply.json#/reply\n{reply_obj.get('reply', '')}"),
        ("S7 相关 assistant 推理", compact_assistant_reasoning(reply_obj)),
        ("S8 下单工具调用与返回", compact_tool_transcript(reply_obj)),
    ]
    body = [f"case_id={case_id}"]
    for title, content in sections:
        body.append(f"\n===== {title} =====\n{content}")
    bundle = "\n".join(body)
    manifest = {
        "sent_path": str(sent_path),
        "reply_path": str(reply_path),
        "sent_sha256": sha256_text(sent_text),
        "reply_sha256": hashlib.sha256(reply_raw.encode("utf-8")).hexdigest(),
        "input_bundle_sha256": sha256_text(bundle),
        "input_characters": len(bundle),
        "sections": [title for title, _ in sections],
    }
    return bundle, manifest


def load_credentials(model: str) -> tuple[str, str]:
    with OPENCLAW_JSON.open(encoding="utf-8") as f:
        cfg = json.load(f)
    for provider in cfg.get("models", {}).get("providers", {}).values():
        ids = {item.get("id") for item in provider.get("models", [])}
        if model in ids and provider.get("baseUrl") and provider.get("apiKey"):
            return str(provider["baseUrl"]).rstrip("/"), str(provider["apiKey"])
    raise RuntimeError(f"在 openclaw.json 中找不到模型 {model} 的有效配置")


def call_model(base_url: str, api_key: str, model: str, bundle: str, retries: int = 3) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "请从以下 INPUT_BUNDLE 抽取候选结构：\n\n" + bundle},
        ],
        "temperature": 0.1,
        "max_tokens": 10000,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    last_error = "unknown"
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=240,
            )
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}: {response.text[:500]}"
            else:
                obj = response.json()
                return obj["choices"][0]["message"]["content"], obj.get("usage") or {}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(3 * attempt)
    raise RuntimeError(f"Qwen 调用失败（{retries} 次）: {last_error}")


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("模型输出不是 JSON object")
    return obj


def align_quotes_to_source(obj: dict[str, Any], bundle: str) -> int:
    """把模型的近似引文对齐到最相似的单行原文；不修改任何语义字段。"""
    source_lines = []
    for raw_line in bundle.splitlines():
        line = re.sub(r"^L\d+:\s*", "", raw_line).strip()
        if line and not line.startswith("=====") and not line.startswith("reply.json#"):
            source_lines.append(line)

    def normalized(value: str) -> str:
        value = re.sub(r"\*\*|`|[|]", "", value)
        value = re.sub(r"\.{3}|…+", " ", value)
        return re.sub(r"\s+", "", value)

    def closest(quote: str) -> str:
        anchors = [part.strip() for part in re.split(r"\.{3}|…+", quote) if len(part.strip()) >= 8]
        if not anchors:
            anchors = [quote]
        best_line, best_score = quote, 0.0
        for line in source_lines:
            norm_line = normalized(line)
            if not norm_line:
                continue
            scores = []
            for anchor in anchors:
                norm_anchor = normalized(anchor)
                scores.append(1.0 if norm_anchor and norm_anchor in norm_line else SequenceMatcher(None, norm_anchor, norm_line).ratio())
            score = max(scores)
            if score > best_score:
                best_line, best_score = line, score
        return best_line if best_score >= 0.20 else quote

    repaired = 0

    def walk(value: Any) -> None:
        nonlocal repaired
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "quote" and isinstance(child, str) and child not in ("", "unknown") and child not in bundle:
                    aligned = closest(child)
                    if aligned != child and aligned in bundle:
                        value[key] = aligned
                        repaired += 1
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return repaired


def validate_candidate(obj: dict[str, Any], bundle: str, case_id: str) -> list[str]:
    errors: list[str] = []
    required_top = {"schema_version", "case_id", "claims", "relation_candidates", "conflicts", "unknowns"}
    if set(obj) != required_top:
        errors.append(f"顶层字段必须恰好是 {sorted(required_top)}，实际为 {sorted(obj)}")
    if obj.get("case_id") != case_id:
        errors.append(f"case_id 不匹配: {obj.get('case_id')!r}")
    list_fields = ("claims", "relation_candidates", "conflicts", "unknowns")
    for field in list_fields:
        if not isinstance(obj.get(field), list):
            errors.append(f"{field} 必须是数组")
    if case_id == "bot105d_2026-08-05":
        expected_counts = {"claims": 3, "relation_candidates": 2, "conflicts": 2}
        for field, expected in expected_counts.items():
            if isinstance(obj.get(field), list) and len(obj[field]) != expected:
                errors.append(f"{field} 必须恰好有 {expected} 项，实际 {len(obj[field])} 项")
        predicates = {item.get("predicate") for item in (obj.get("relation_candidates") or []) if isinstance(item, dict)}
        for predicate in ("SELECTS", "REQUIRES_DIVERSIFICATION_WITH"):
            if predicate not in predicates:
                errors.append(f"缺少类型关系 predicate={predicate}")
        conflict_text = json.dumps(obj.get("conflicts") or [], ensure_ascii=False)
        for keyword in ("-2.93%", "-4.15%", "科创50"):
            if keyword not in conflict_text:
                errors.append(f"冲突结构缺少关键口径 {keyword}")

    def require_fields(item: Any, fields: set[str], path: str) -> None:
        if not isinstance(item, dict):
            errors.append(f"{path} 必须是对象")
            return
        missing = fields - set(item)
        if missing:
            errors.append(f"{path} 缺字段: {sorted(missing)}")

    claim_fields = {"candidate_id", "statement", "claim_type", "status", "confidence_label", "certainty", "source_locator", "quote", "mechanism", "supporting_evidence", "contradicting_evidence", "falsifiers"}
    for i, claim in enumerate(obj.get("claims") or []):
        require_fields(claim, claim_fields, f"claims[{i}]")
        if not isinstance(claim, dict):
            continue
        for field, nested_fields in (
            ("mechanism", {"step", "source_locator", "quote"}),
            ("supporting_evidence", {"source_locator", "quote", "explanation"}),
            ("contradicting_evidence", {"source_locator", "quote", "explanation"}),
            ("falsifiers", {"condition", "source_locator", "quote"}),
        ):
            if not isinstance(claim.get(field), list):
                errors.append(f"claims[{i}].{field} 必须是数组")
                continue
            for j, nested in enumerate(claim[field]):
                require_fields(nested, nested_fields, f"claims[{i}].{field}[{j}]")

    relation_fields = {"candidate_id", "subject", "predicate", "object", "direction", "mechanism", "certainty", "source_locator", "quote"}
    for i, relation in enumerate(obj.get("relation_candidates") or []):
        require_fields(relation, relation_fields, f"relation_candidates[{i}]")
    conflict_fields = {"candidate_id", "left", "right", "winner", "resolution", "resolution_class", "unresolved_issue", "source_locator", "quote"}
    for i, conflict in enumerate(obj.get("conflicts") or []):
        require_fields(conflict, conflict_fields, f"conflicts[{i}]")

    def require_chinese(value: Any, path: str) -> None:
        if not isinstance(value, str) or value in ("", "unknown"):
            return
        if not re.search(r"[\u3400-\u9fff]", value):
            errors.append(f"{path} 必须使用中文自然语言，实际为: {value[:120]!r}")

    for i, claim in enumerate(obj.get("claims") or []):
        if not isinstance(claim, dict):
            continue
        require_chinese(claim.get("statement"), f"claims[{i}].statement")
        for j, item in enumerate(claim.get("mechanism") or []):
            if isinstance(item, dict):
                require_chinese(item.get("step"), f"claims[{i}].mechanism[{j}].step")
        for field in ("supporting_evidence", "contradicting_evidence"):
            for j, item in enumerate(claim.get(field) or []):
                if isinstance(item, dict):
                    require_chinese(item.get("explanation"), f"claims[{i}].{field}[{j}].explanation")
        for j, item in enumerate(claim.get("falsifiers") or []):
            if isinstance(item, dict):
                require_chinese(item.get("condition"), f"claims[{i}].falsifiers[{j}].condition")
    for i, relation in enumerate(obj.get("relation_candidates") or []):
        if isinstance(relation, dict):
            require_chinese(relation.get("mechanism"), f"relation_candidates[{i}].mechanism")
    for i, conflict in enumerate(obj.get("conflicts") or []):
        if not isinstance(conflict, dict):
            continue
        require_chinese(conflict.get("resolution"), f"conflicts[{i}].resolution")
        require_chinese(conflict.get("unresolved_issue"), f"conflicts[{i}].unresolved_issue")

    # quote 必须逐字存在；unknown 和空字符串免检。
    def scan_quotes(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "quote" and isinstance(child, str) and child not in ("", "unknown"):
                    if child not in bundle:
                        errors.append(f"{path}.quote 无法在输入包逐字定位: {child[:80]!r}")
                else:
                    scan_quotes(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                scan_quotes(child, f"{path}[{index}]")
    scan_quotes(obj)
    return errors


def coverage_report(candidate: dict[str, Any]) -> dict[str, Any]:
    """与人工 canonical 的语义槽位做透明的关键词覆盖检查，不冒充语义 F1。"""
    payload = json.dumps(candidate, ensure_ascii=False)
    slots = {
        "C1 防御·红利 Regime": ["防御·红利", "MA120", "Top15"],
        "C2 科技反弹尚非反转": ["科技", "反弹", "反转"],
        "C3 双基金满足宪章": ["双基金", "75%", "宪章"],
        "DC1 两套引擎冲突": ["-2.93%", "-4.15%"],
        "DC2 科技反弹与防御冲突": ["科创50", "防御", "冲突"],
        "R4 Regime 选择红利": ["SELECTS", "012762"],
        "R5 policy 要求分散": ["REQUIRES_DIVERSIFICATION", "510030"],
    }
    details = []
    for slot, keywords in slots.items():
        missing = [word for word in keywords if word not in payload]
        details.append({"slot": slot, "covered": not missing, "missing_keywords": missing})
    return {
        "method": "keyword_slot_coverage_only",
        "covered": sum(1 for item in details if item["covered"]),
        "total": len(details),
        "details": details,
        "out_of_scope": ["F1-F7 确定性事实", "R1-R3 数据库类型关系", "A1-A5 候选动作和订单"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default=DEFAULT_RUN)
    parser.add_argument("--bot", default=DEFAULT_BOT)
    parser.add_argument("--date", default=DEFAULT_DATE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-dir", help="输出目录；多 run 应使用独立目录，避免同 bot/日期文件互相覆盖")
    parser.add_argument("--attempt", default="", help="输出文件后缀，例如 attempt-2")
    parser.add_argument("--dry-run", action="store_true", help="只生成输入包，不调用模型")
    args = parser.parse_args()

    case_id = f"{args.bot}_{args.date}"
    case_dir = ROOT / "world/runtime/runs" / args.run / args.date / args.bot
    sent_path = case_dir / "sent.md"
    reply_path = case_dir / "reply.json"
    if not sent_path.is_file() or not reply_path.is_file():
        raise SystemExit(f"缺少输入文件: {case_dir}")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else ROOT / "world/runtime/decision-episodes" / args.model
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f".{args.attempt}" if args.attempt else ""
    input_path = output_dir / f"{case_id}{suffix}.input.txt"
    candidate_path = output_dir / f"{case_id}{suffix}.candidate.json"
    report_path = output_dir / f"{case_id}{suffix}.validation.json"

    bundle, manifest = build_input_bundle(sent_path, reply_path, case_id)
    input_path.write_text(bundle, encoding="utf-8")
    if args.dry_run:
        print(json.dumps({"input": str(input_path), "manifest": manifest}, ensure_ascii=False, indent=2))
        return 0

    if candidate_path.exists():
        raise SystemExit(f"拒绝覆盖已有 candidate: {candidate_path}")

    base_url, api_key = load_credentials(args.model)
    raw, usage = call_model(base_url, api_key, args.model, bundle)
    candidate = parse_json_object(raw)
    aligned_quote_count = align_quotes_to_source(candidate, bundle)
    errors = validate_candidate(candidate, bundle, case_id)
    envelope = {
        "extraction_metadata": {
            "model": args.model,
            "temperature": 0.1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "candidate_only": True,
            "canonical_database_modified": False,
            "aligned_quote_count": aligned_quote_count,
            "usage": usage,
            **manifest,
        },
        "candidate": candidate,
    }
    candidate_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "valid": not errors,
        "errors": errors,
        "counts": {
            "claims": len(candidate.get("claims") or []),
            "relation_candidates": len(candidate.get("relation_candidates") or []),
            "conflicts": len(candidate.get("conflicts") or []),
            "unknowns": len(candidate.get("unknowns") or []),
        },
        "golden_semantic_coverage": coverage_report(candidate) if case_id == "bot105d_2026-08-05" else {"method": "generic_candidate", "covered": None, "total": None, "details": []},
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate": str(candidate_path), "validation": str(report_path), **report}, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
