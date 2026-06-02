#!/usr/bin/env python3
"""
refresh-catalog.py — 从 simworld-data（ttjj/天天基金）上游刷新 tools-catalog.json

用法: cd skills/research-mcp && python refresh-catalog.py [--upstream URL]

功能:
1. 连上游 MCP（默认本地 simworld-mcp :18078），调 tools/list 获取最新工具
2. 读现有 catalog 保留 category 分组
3. 剥掉 proxy 自动注入的 simulated_datetime（对外 schema 里 bot 不传）
4. 新增工具标记 _uncategorized
5. 输出 diff 并写入 tools-catalog.json

注：bot 运行时实际经 simworld-proxy 调这些工具（mcp__simworld_data__<name>），
proxy 会自动注入 simulated_datetime；本 catalog 因此剥掉该参数。
"""

import argparse
import json
import os
from datetime import datetime

import httpx

DEFAULT_UPSTREAM = os.getenv("SIMWORLD_UPSTREAM_URL", "http://127.0.0.1:18078/mcp")
CATALOG_PATH = os.path.join(os.path.dirname(__file__), "tools-catalog.json")
EXCLUDED_TOOLS = {"system_info", "reload_tools"}
INJECTED_PARAM = "simulated_datetime"  # proxy 注入，对外剥掉

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream, application/json",
}


def fetch_upstream_tools(url: str) -> list[dict]:
    client = httpx.Client(timeout=30)
    r = client.post(url, headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "refresh-catalog", "version": "2.0"}},
    })
    sid = r.headers.get("Mcp-Session-Id")
    h = {**HEADERS}
    if sid:
        h["Mcp-Session-Id"] = sid
        client.post(url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    r = client.post(url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    for line in r.text.split("\n"):
        if line.startswith("data:"):
            return json.loads(line[5:].strip())["result"]["tools"]
    return json.loads(r.text)["result"]["tools"]


def strip_injected(schema: dict) -> dict:
    schema = dict(schema or {})
    props = dict(schema.get("properties") or {})
    props.pop(INJECTED_PARAM, None)
    schema["properties"] = props
    schema["required"] = [r for r in (schema.get("required") or []) if r != INJECTED_PARAM]
    return schema


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    args = ap.parse_args()

    print(f"Fetching tools from {args.upstream} ...")
    upstream = fetch_upstream_tools(args.upstream)
    print(f"  Got {len(upstream)} tools")

    existing = json.load(open(CATALOG_PATH)) if os.path.exists(CATALOG_PATH) else {}
    lookup = {n: m.get("category") for n, m in (existing.get("tools") or {}).items()}

    new_tools, cat_tools, uncategorized = {}, {}, []
    for t in upstream:
        name = t["name"]
        if name in EXCLUDED_TOOLS:
            continue
        cat = lookup.get(name)
        if not cat:
            uncategorized.append(name)
            cat = "_uncategorized"
        new_tools[name] = {
            "category": cat,
            "description": (t.get("description") or "").split("\n")[0].strip(),
            "inputSchema": strip_injected(t.get("inputSchema", {})),
        }
        cat_tools.setdefault(cat, []).append(name)

    added = set(new_tools) - set(lookup)
    removed = set(lookup) - set(new_tools)
    if added:
        print(f"  + Added: {sorted(added)}")
    if removed:
        print(f"  - Removed: {sorted(removed)}")
    if uncategorized:
        print(f"  ! Uncategorized (assign in tools-catalog.json): {uncategorized}")

    categories = {}
    for cn, cd in (existing.get("categories") or {}).items():
        if cn in cat_tools:
            categories[cn] = {"description": cd.get("description", ""), "tools": sorted(cat_tools[cn])}
    if "_uncategorized" in cat_tools:
        categories["_uncategorized"] = {"description": "未分类工具", "tools": cat_tools["_uncategorized"]}

    catalog = {
        "generated_at": datetime.now().isoformat(),
        "generated_from": f"simworld-data (ttjj / 天天基金, {args.upstream})",
        "note": "时点参数 simulated_datetime 由 world simworld-proxy 自动注入，bot 调用时不要传。",
        "categories": categories,
        "tools": dict(sorted(new_tools.items())),
    }
    json.dump(catalog, open(CATALOG_PATH, "w"), ensure_ascii=False, indent=2)
    print(f"Written {len(new_tools)} tools.")
    for cn, cd in categories.items():
        print(f"  {cn}: {len(cd['tools'])}")


if __name__ == "__main__":
    main()
