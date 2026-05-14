#!/usr/bin/env python3
"""
refresh-catalog.py — 从上游 research-mcp 刷新 tools-catalog.json

用法: cd workspace/skills/research-mcp && python refresh-catalog.py

功能:
1. 连上游 MCP，调 tools/list 获取最新工具列表
2. 读现有 catalog 保留 category 分组
3. 新增工具标记 _uncategorized
4. 输出 diff（新增/删除/描述变更）
5. 写入 tools-catalog.json
"""

import json
import os
import sys
from datetime import datetime

import httpx

UPSTREAM_URL = "http://research-mcp.jijinmima.cn/mcp"
CATALOG_PATH = os.path.join(os.path.dirname(__file__), "tools-catalog.json")
EXCLUDED_TOOLS = {"system_info", "health", "reload_tools"}

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream, application/json",
}


def fetch_upstream_tools() -> list[dict]:
    """连上游 MCP，获取全部工具列表"""
    client = httpx.Client(timeout=30)

    # Initialize session
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "refresh-catalog", "version": "1.0"},
        },
    }
    r = client.post(UPSTREAM_URL, headers=HEADERS, json=init_body)
    session_id = r.headers.get("Mcp-Session-Id")

    # List tools
    list_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    h = {**HEADERS}
    if session_id:
        h["Mcp-Session-Id"] = session_id
    r = client.post(UPSTREAM_URL, headers=h, json=list_body)

    # Parse SSE response
    for line in r.text.split("\n"):
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            return data["result"]["tools"]
    return json.loads(r.text)["result"]["tools"]


def load_existing_catalog() -> dict | None:
    """读现有 catalog"""
    if not os.path.exists(CATALOG_PATH):
        return None
    with open(CATALOG_PATH) as f:
        return json.load(f)


def build_category_lookup(catalog: dict) -> dict[str, str]:
    """从现有 catalog 构建 tool_name → category 映射"""
    lookup = {}
    if catalog and "tools" in catalog:
        for name, meta in catalog["tools"].items():
            if "category" in meta:
                lookup[name] = meta["category"]
    return lookup


def main():
    print("Fetching tools from upstream MCP...")
    upstream_tools = fetch_upstream_tools()
    print(f"  Got {len(upstream_tools)} tools from upstream")

    existing = load_existing_catalog()
    existing_lookup = build_category_lookup(existing) if existing else {}
    existing_names = set(existing_lookup.keys()) if existing else set()

    # Build new catalog
    new_tools = {}
    category_tools: dict[str, list[str]] = {}
    uncategorized = []

    for tool in upstream_tools:
        name = tool["name"]
        if name in EXCLUDED_TOOLS:
            continue

        desc_first = tool.get("description", "").split("\n")[0].strip()
        cat = existing_lookup.get(name)

        if not cat:
            uncategorized.append(name)
            cat = "_uncategorized"

        new_tools[name] = {
            "category": cat,
            "description": desc_first,
            "inputSchema": tool.get("inputSchema", {}),
        }

        category_tools.setdefault(cat, []).append(name)

    new_names = set(new_tools.keys())

    # Diff
    added = new_names - existing_names
    removed = existing_names - new_names
    desc_changed = []
    if existing and "tools" in existing:
        for name in new_names & existing_names:
            old_desc = existing["tools"].get(name, {}).get("description", "")
            new_desc = new_tools[name]["description"]
            if old_desc != new_desc:
                desc_changed.append((name, old_desc, new_desc))

    # Report diff
    if added:
        print(f"\n  + Added ({len(added)}):")
        for n in sorted(added):
            print(f"    + {n}")
    if removed:
        print(f"\n  - Removed ({len(removed)}):")
        for n in sorted(removed):
            print(f"    - {n}")
    if desc_changed:
        print(f"\n  ~ Description changed ({len(desc_changed)}):")
        for name, old, new in desc_changed:
            print(f"    ~ {name}")
            print(f"      old: {old[:80]}")
            print(f"      new: {new[:80]}")
    if not added and not removed and not desc_changed:
        print("\n  No changes detected.")

    if uncategorized:
        print(f"\n  ! Uncategorized ({len(uncategorized)}):")
        for n in uncategorized:
            print(f"    ? {n}")
        print("  → Edit tools-catalog.json to assign categories")

    # Rebuild categories from existing catalog + new data
    categories = {}
    if existing and "categories" in existing:
        for cat_name, cat_data in existing["categories"].items():
            if cat_name in category_tools:
                categories[cat_name] = {
                    "description": cat_data.get("description", ""),
                    "tools": sorted(category_tools[cat_name]),
                }
    # Add _uncategorized if any
    if "_uncategorized" in category_tools:
        categories["_uncategorized"] = {
            "description": "未分类工具",
            "tools": category_tools["_uncategorized"],
        }

    # Write
    catalog = {
        "generated_at": datetime.now().isoformat(),
        "upstream_url": UPSTREAM_URL,
        "categories": categories,
        "tools": dict(sorted(new_tools.items())),
    }

    with open(CATALOG_PATH, "w") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    print(f"\nWritten {len(new_tools)} tools to {CATALOG_PATH}")
    for cat_name, cat_data in categories.items():
        print(f"  {cat_name}: {len(cat_data['tools'])} tools")


if __name__ == "__main__":
    main()
