#!/usr/bin/env python3.12
"""用 qwen3.5-plus 给单指数 d-bot 每日 reply.json 的「最终结论」段做 5 档方向定性分类.

用途: 弥补 belief.p_up 只有数值、agent 自然语言里的「谨慎/中性偏多/看多」等定性表态无结构化字段的问题.
输出: research/belief_qualitative_labels.jsonl (每行一条, 支持断点续跑).

用法:
    # 小样本预跑 (每 bot 6 条, 检查质量)
    /usr/bin/python3.12 scripts/classify_belief_qualitative.py --sample 6

    # 全库跑
    /usr/bin/python3.12 scripts/classify_belief_qualitative.py

    # 强制重跑某些 bot
    /usr/bin/python3.12 scripts/classify_belief_qualitative.py --bots bot18d --force
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

ROOT = "/home/rooot/agent_invest_lab"
OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
OUT_PATH = f"{ROOT}/research/belief_qualitative_labels.jsonl"

RUNS = {
    "bot5d":  "dash-2026-07-30T09-56-04",
    "bot10d": "dash-2026-07-27T08-03-45",
    "bot16d": "dash-2026-07-28T14-53-39",
    "bot18d": "dash-2026-07-26T15-08-54",
    "bot20d": "dash-2026-07-27T02-10-21",
}

# ── 抽结论段 (与之前 classifier 一致) ─────────────────────────────────────────
SECTION_HEAD = re.compile(
    r"(?im)^\s*(?:#{2,6}|\*\*)\s*"
    r"(仓位决策|最终决策|决策|最终结论|结论|执行|今日决策|今日行动|今日操作|操作|判断|结构判断|结构定|定性|风险状态|市场环境|action)\b[^\n]{0,120}"
)

def extract_conclusion_span(reply: str) -> tuple[str, str]:
    m_belief = re.search(r"```yaml\s*belief[\s\S]*?```", reply)
    body = reply if not m_belief else reply.replace(m_belief.group(0), "")
    matches = list(SECTION_HEAD.finditer(body))
    if matches:
        m = matches[-1]
        seg = body[m.start(): m.start() + 1500]
        next_head = re.search(r"(?im)^\s*#{2,6}\s*\S", seg[100:])
        if next_head:
            seg = seg[:100 + next_head.start()]
        return seg.strip(), "A_段头"
    if m_belief:
        return reply[:m_belief.start()][-600:].strip(), "B_belief前"
    return body[-600:].strip(), "C_末尾"


# ── qwen 调用 (复用 res-reports-backfill.py 的模式) ───────────────────────────
def load_qwen_creds(model: str) -> tuple[str, str, str]:
    cfg = json.load(open(OPENCLAW_JSON, encoding="utf-8"))
    for prov in cfg.get("models", {}).get("providers", {}).values():
        ids = {m.get("id") for m in prov.get("models", [])}
        if model in ids and prov.get("baseUrl") and prov.get("apiKey"):
            return prov["baseUrl"].rstrip("/"), prov["apiKey"], model
    raise SystemExit(f"openclaw.json 里找不到含 {model} 的 provider")


def call_qwen(base_url: str, api_key: str, model: str, system: str, user: str, retries: int = 3) -> str:
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    last = ""
    for i in range(retries):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=120)
            if r.status_code != 200:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(3 * (i + 1))
                continue
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"qwen 调用失败(重试 {retries} 次): {last}")


SYSTEM_PROMPT = """你是量化投研文档的分类助手。任务:给一段 A 股 index-投资 bot 当天决策文本(约 500~1500 字),判定 bot 对**未来市场方向**的**最终定性表态**属于以下 6 档中的哪一档。

**6 档标签定义**:
1. **明确看多** - bot 明确判断未来市场会涨,例:"结构性看多"/"坚定看多"/"确认右侧"/"确认反转"
2. **偏多** - bot 判断偏向上涨但保留余地,例:"中性偏多"/"温和看多"/"谨慎乐观"/"震荡偏多"
3. **中性** - bot 判断震荡/观望/不表态方向,例:"震荡市"/"纠缠"/"观望"/"维持基线"
4. **偏空** - bot 判断偏向下跌但保留余地,例:"中性偏空"/"温和看空"/"谨慎"/"警惕"/"震荡偏空"
5. **明确看空** - bot 明确判断未来市场会跌,例:"结构性看空"/"坚定看空"/"risk_off 主导"/"破位深化"
6. **无表态** - 文本里没有对未来方向的定性判断(纯操作记录/纯数据罗列/自我风格标签)

**关键规则(必须严格执行)**:
- **仓位动作 ≠ 方向表态**:"维持 40% 仓位"/"降至 0%"/"加仓至 60%" 是**操作**,不是方向判断。除非文中明确说"因为看空所以降仓",否则不能凭仓位动作反推方向。
- **单一维度描述 ≠ 最终结论**:"估值偏空,趋势偏多" 是各维度分述,取 bot 综合后的最终定调。若无综合定调 → 无表态。
- **描述性词汇 ≠ 方向表态**:"估值修复"/"MA60 破位"/"温度回落" 描述指标本身,不算方向判断。
- **自我风格标签 ≠ 方向**:"保守账户下"/"保守风格"/"激进风格" 是 bot 人设,不是市场看法。
- **历史 / 假设 / 反驳性 ≠ 当日**:"之前看空"/"过度看多"/"若企稳则加仓" 都不算当日表态。
- **优先在文本收尾部分找结论**,若通篇只有维度分述、无综合结论 → 无表态。

**输出格式**(严格 JSON,不要 markdown 代码块):
{"label": "<6档之一>", "confidence": "高"|"中"|"低", "evidence": "<原文里 20 字以内的关键句>", "reason": "<1 句 30 字以内解释>"}"""


def build_user_prompt(bot: str, date: str, span: str) -> str:
    return f"""bot={bot} date={date}

<决策文本>
{span}
</决策文本>

请判定该 bot 当日对**未来市场方向**的最终定性表态,严格按 JSON 输出。"""


def parse_qwen_out(txt: str) -> dict:
    # 优先 ```json ... ``` 块
    m = re.search(r"```json\s*(\{.*?\})\s*```", txt, re.S)
    if not m:
        m = re.search(r"(\{[^{}]*\"label\"[^{}]*\})", txt, re.S)
    if not m:
        raise ValueError(f"无 JSON: {txt[:200]}")
    obj = json.loads(m.group(1))
    if obj.get("label") not in {"明确看多", "偏多", "中性", "偏空", "明确看空", "无表态"}:
        raise ValueError(f"label 非法: {obj.get('label')}")
    return obj


# ── 收集所有待跑样本 ─────────────────────────────────────────────────────────
def collect_targets(bots_filter: set[str] | None = None) -> list[dict]:
    out = []
    for bot, run in RUNS.items():
        if bots_filter and bot not in bots_filter:
            continue
        for fp in sorted(glob.glob(f"{ROOT}/world/runtime/runs/{run}/2*/{bot}/reply.json")):
            date = fp.split("/")[-3]  # YYYY-MM-DD
            out.append({"bot": bot, "run": run, "date": date, "reply_path": fp})
    return out


def load_done() -> set[tuple[str, str]]:
    if not os.path.exists(OUT_PATH):
        return set()
    done = set()
    with open(OUT_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
                done.add((r["bot"], r["date"]))
            except Exception:
                pass
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.5-plus")
    ap.add_argument("--sample", type=int, default=0, help="每 bot 抽 N 条 dry-run")
    ap.add_argument("--limit", type=int, default=0, help="总条数上限 (0=无)")
    ap.add_argument("--bots", default="", help="逗号分隔 bot 白名单")
    ap.add_argument("--force", action="store_true", help="忽略已跑缓存")
    ap.add_argument("--sleep", type=float, default=0.3, help="每次调用间隔秒")
    args = ap.parse_args()

    bots_filter = set(args.bots.split(",")) if args.bots else None
    targets = collect_targets(bots_filter)

    if args.sample:
        random.seed(42)
        by_bot: dict = {}
        for t in targets:
            by_bot.setdefault(t["bot"], []).append(t)
        picked = []
        for bot, lst in by_bot.items():
            random.shuffle(lst)
            picked.extend(lst[:args.sample])
        targets = picked

    if args.limit:
        targets = targets[:args.limit]

    done = set() if args.force else load_done()
    todo = [t for t in targets if (t["bot"], t["date"]) not in done]

    print(f"target={len(targets)} done={len(targets) - len(todo)} todo={len(todo)} model={args.model}", flush=True)
    if not todo:
        print("nothing to do")
        return

    base_url, api_key, model = load_qwen_creds(args.model)
    print(f"qwen: {base_url}  model={model}", flush=True)

    Path(OUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    fout = open(OUT_PATH, "a", encoding="utf-8")

    ok = 0
    fail = 0
    label_counts: dict[str, int] = {}
    t0 = time.time()

    for i, t in enumerate(todo, 1):
        try:
            reply = json.load(open(t["reply_path"])).get("reply", "")
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {t['bot']} {t['date']} reply.json 读失败: {e}", flush=True)
            fail += 1
            continue
        span, how = extract_conclusion_span(reply)
        if not span:
            print(f"  [{i}/{len(todo)}] {t['bot']} {t['date']} 空段, skip", flush=True)
            fail += 1
            continue
        try:
            raw = call_qwen(base_url, api_key, model, SYSTEM_PROMPT, build_user_prompt(t["bot"], t["date"], span))
            parsed = parse_qwen_out(raw)
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {t['bot']} {t['date']} qwen 失败: {e}", flush=True)
            fail += 1
            continue

        rec = {
            "bot": t["bot"],
            "date": t["date"],
            "run": t["run"],
            "extract_how": how,
            "span_len": len(span),
            "label": parsed["label"],
            "confidence": parsed.get("confidence", ""),
            "evidence": parsed.get("evidence", ""),
            "reason": parsed.get("reason", ""),
        }
        fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fout.flush()
        label_counts[parsed["label"]] = label_counts.get(parsed["label"], 0) + 1
        ok += 1
        if i % 20 == 0 or i == len(todo):
            eta = (time.time() - t0) / i * (len(todo) - i)
            print(f"  [{i}/{len(todo)}] ok={ok} fail={fail} 用时={time.time() - t0:.0f}s eta={eta:.0f}s dist={label_counts}", flush=True)
        time.sleep(args.sleep)

    fout.close()
    print(f"\n完成 ok={ok} fail={fail} 总耗时={time.time() - t0:.0f}s")
    print(f"分布: {label_counts}")
    print(f"写入: {OUT_PATH}")


if __name__ == "__main__":
    main()
