"""ttjj-data-mcp HTTP/SSE 客户端 helper.

只暴露一个函数: call(tool, **args) -> dict
- 内部维护全局 requests.Session + Mcp-Session-Id
- 60s 硬超时, 不重试
- 失败抛 RuntimeError, 调用方负责 try/except 跳过
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

_ENDPOINT = "http://localhost:18077/mcp"
_TIMEOUT = 60
_SESSION: requests.Session | None = None
_SESSION_ID: str | None = None
_REQ_ID = 0


def _next_id() -> int:
    global _REQ_ID
    _REQ_ID += 1
    return _REQ_ID


def _parse_sse(raw: bytes | str) -> dict:
    """解析 SSE 格式 'event: message\\ndata: {...}\\n\\n', 取最后一个可解析为 JSON 的 data 行.

    接受 bytes 或 str, 内部统一按 UTF-8 处理, 避免 requests 用 ISO-8859-1
    错误解码中文内容的问题 (text/event-stream 无 charset 时 requests 默认 latin-1).
    跳过 `data: [DONE]` 等流终止标记或心跳行.
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    last_parsed: dict | None = None
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        candidate = line[6:].strip()
        try:
            last_parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
    if last_parsed is None:
        raise RuntimeError(f"SSE 响应里没有可解析的 data: 行: {text[:200]}")
    return last_parsed


def _init_session() -> None:
    global _SESSION, _SESSION_ID
    _SESSION = requests.Session()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    try:
        resp = _SESSION.post(_ENDPOINT, headers=headers, timeout=_TIMEOUT, json={
            "jsonrpc": "2.0", "id": _next_id(), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "scout-board-etf", "version": "1.0"}},
        })
    except requests.RequestException as e:
        raise RuntimeError(f"ttjj initialize 网络错误: {e}") from e
    sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
    if not sid:
        raise RuntimeError(f"initialize 没返回 Mcp-Session-Id: {resp.headers}")
    _SESSION_ID = sid
    # notifications/initialized
    try:
        _SESSION.post(_ENDPOINT, headers={**headers, "Mcp-Session-Id": _SESSION_ID},
                      timeout=_TIMEOUT, json={"jsonrpc": "2.0",
                                              "method": "notifications/initialized",
                                              "params": {}})
    except requests.RequestException as e:
        log.warning("notifications/initialized 失败 (会话已可用, 继续): %s", e)


def call(tool: str, **args: Any) -> dict:
    """调用 ttjj-data-mcp 工具. 失败抛 RuntimeError."""
    if _SESSION is None or _SESSION_ID is None:
        _init_session()
    assert _SESSION is not None and _SESSION_ID is not None
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Mcp-Session-Id": _SESSION_ID}
    try:
        resp = _SESSION.post(_ENDPOINT, headers=headers, timeout=_TIMEOUT, json={
            "jsonrpc": "2.0", "id": _next_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
    except requests.RequestException as e:
        raise RuntimeError(f"ttjj.{tool} 网络错误: {e}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"ttjj.{tool} HTTP {resp.status_code}: {resp.text[:300]}")
    payload = _parse_sse(resp.content)
    if "error" in payload:
        raise RuntimeError(f"ttjj.{tool} JSON-RPC error: {payload['error']}")
    content = payload.get("result", {}).get("content", [])
    if not content or content[0].get("type") != "text":
        raise RuntimeError(f"ttjj.{tool} 响应缺 text content: {payload}")
    return json.loads(content[0]["text"])
