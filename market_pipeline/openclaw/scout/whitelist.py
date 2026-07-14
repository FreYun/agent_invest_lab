"""粗粒度题材白名单加载器 (mtime 热更).

文件: scout/coarse_themes.json
格式见 docs/superpowers/specs/2026-05-26-scout-coarse-theme-pool-design.md §3.1
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class Whitelist:
    def __init__(self, path: str):
        self.path = path
        self._mtime: Optional[float] = None
        self._codes: set[str] = set()
        self._groups: list[str] = []
        self._code_to_group: dict[str, str] = {}
        self._code_to_name: dict[str, str] = {}

    def load_if_changed(self) -> bool:
        """若文件 mtime 变化则重新加载.

        返回值:
            True  — 本次实际重新加载成功 (mtime 变化 + JSON 有效).
            False — 文件无变化 / 文件不存在 / JSON 损坏 (加载失败时保留上次状态, 看日志).
        """
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if self._mtime is None:
                log.warning("whitelist 文件不存在: %s", self.path)
                self._mtime = 0  # 标记已尝试过, 避免重复 warn
            return False
        if self._mtime is not None and mtime <= self._mtime:
            return False
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error("whitelist 加载失败, 保留上次状态: %s", e)
            return False

        codes: set[str] = set()
        groups: list[str] = []
        code_to_group: dict[str, str] = {}
        code_to_name: dict[str, str] = {}
        for g in data.get("groups", []):
            name = g.get("name")
            if not name:
                continue
            groups.append(name)
            for b in g.get("boards", []):
                code = b.get("code")
                if not code:
                    continue
                codes.add(code)
                code_to_group[code] = name
                code_to_name[code] = b.get("name") or code

        self._codes = codes
        self._groups = groups
        self._code_to_group = code_to_group
        self._code_to_name = code_to_name
        self._mtime = mtime
        log.info("whitelist reloaded: %d boards in %d groups", len(codes), len(groups))
        return True

    def codes(self) -> set[str]:
        return self._codes

    def group_order(self) -> list[str]:
        return list(self._groups)

    def code_to_group(self, code: str) -> Optional[str]:
        return self._code_to_group.get(code)

    def code_to_name(self, code: str) -> Optional[str]:
        return self._code_to_name.get(code)

    def filter_boards(self, rows: list[dict]) -> list[dict]:
        """过滤 + 注入 group / 显示名. 白名单空时原样返回 (软回滚)."""
        if not self._codes:
            return rows
        out: list[dict] = []
        for r in rows:
            code = r.get("board_code")
            if code not in self._codes:
                continue
            r = dict(r)  # 不污染入参
            r["group"] = self._code_to_group.get(code)
            disp = self._code_to_name.get(code)
            if disp:
                r["board_name"] = disp
            out.append(r)
        return out
