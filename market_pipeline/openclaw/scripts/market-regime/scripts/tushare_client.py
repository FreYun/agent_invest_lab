"""Tushare Pro HTTP 客户端 — 薄封装。

Token 读取优先级:
1. 环境变量 TUSHARE_TOKEN
2. ~/.openclaw/.tushare-token 文件

调用失败 (权限不足 / 超时 / 网络) 抛 TushareError, 由调用方决定降级或重试。
返回 pandas DataFrame, fields 展开为列名, items 展开为行。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

import pandas as pd


TUSHARE_URL = "https://tu.brze.top"  # brze 代理, 绕过官方限频
DEFAULT_TIMEOUT = 20


class TushareError(Exception):
    """Tushare API 调用失败 (code != 0 / 网络错误 / 解析失败)。"""


_cached_token: Optional[str] = None


def load_token() -> str:
    """读取 Tushare token, 结果缓存到进程内避免重复 IO。

    优先级:
      1. 环境变量 TUSHARE_TOKEN
      2. ~/.openclaw/.tushare-token 文件
    """
    global _cached_token
    if _cached_token:
        return _cached_token

    env = os.environ.get("TUSHARE_TOKEN")
    if env:
        _cached_token = env.strip()
        return _cached_token

    token_file = os.path.expanduser("~/.openclaw/.tushare-token")
    if os.path.exists(token_file):
        try:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as e:
            raise TushareError(f"读取 {token_file} 失败: {e}") from e
        if token:
            _cached_token = token
            return _cached_token

    raise TushareError(
        "未找到 Tushare token: 请设置环境变量 TUSHARE_TOKEN, 或写入 "
        "~/.openclaw/.tushare-token 文件"
    )


def call(api_name: str, params: Optional[dict] = None, fields: str = "") -> pd.DataFrame:
    """POST api.tushare.pro, 返回 DataFrame。

    Args:
        api_name: Tushare 接口名, 如 "index_daily" / "daily_info"
        params:   接口参数 dict, 如 {"ts_code": "000300.SH", ...}
        fields:   要返回的字段列表 (逗号分隔), 为空时返回全部字段

    Raises:
        TushareError: code != 0 (权限不足/参数错误等), 网络异常, 解析失败
    """
    body = {
        "api_name": api_name,
        "token": load_token(),
        "params": params or {},
        "fields": fields,
    }
    data = json.dumps(body).encode("utf-8")
    # brze 代理按 path 路由 api_name (SDK 内部约定); 官方 api.tushare.pro 走 body 路由
    # 拼 /{api_name} 同时兼容两端
    url = f"{TUSHARE_URL.rstrip('/')}/{api_name}"
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise TushareError(f"Tushare 网络请求失败 ({api_name}): {e}") from e
    except json.JSONDecodeError as e:
        raise TushareError(f"Tushare 返回非 JSON ({api_name}): {e}") from e

    if payload.get("code") != 0:
        raise TushareError(
            f"Tushare {api_name} 失败: code={payload.get('code')} msg={payload.get('msg')!r}"
        )

    data_block = payload.get("data") or {}
    columns = data_block.get("fields") or []
    items = data_block.get("items") or []
    return pd.DataFrame(items, columns=columns)
