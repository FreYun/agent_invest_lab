#!/usr/bin/env python3
"""清理各 bot 基金目录中的历史扰动文档。

保留：
- 投资框架.md
- 市场环境判断.md
- 个性化基金选择.md
- 当前基金持仓.md
- 基金巡检记录.md
- 能力圈宣告.md

删除：
- .prev / .bak / .tmp / 编辑器残留
- 历史策略摘要 / 调仓日志 / 全流程报告
- 误放到 fund 目录的投顾文件
- 命名带日期后缀的旧市场判断等历史版本
"""

from __future__ import annotations

from pathlib import Path

OPENCLAW_ROOT = Path("/home/rooot/agent_invest_lab")
KEEP_NAMES = {
    "投资框架.md",
    "市场环境判断.md",
    "个性化基金选择.md",
    "当前基金持仓.md",
    "基金巡检记录.md",
    "能力圈宣告.md",
}
DROP_NAMES = {
    "基金投资策略摘要.md",
    "基金调仓日志.md",
    "个性化投顾产品选择.md",
    "当前投顾持仓.md",
    "投顾巡检记录.md",
}
DROP_PREFIXES = (
    "基金投资全流程报告",
    "市场环境判断_",
)
DROP_SUFFIXES = (
    ".prev",
    ".bak",
    ".tmp",
    ".swp",
    ".swo",
    ".orig",
    "~",
)


def _should_delete(path: Path) -> bool:
    name = path.name
    if name in KEEP_NAMES:
        return False
    if name in DROP_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in DROP_PREFIXES):
        return True
    if any(name.endswith(suffix) for suffix in DROP_SUFFIXES):
        return True
    if name.startswith("._"):
        return True
    return False


def main() -> int:
    deleted: list[str] = []
    for md_dir in sorted(OPENCLAW_ROOT.glob("workspace-bot*/memory/portfolio/fund")):
        if not md_dir.is_dir():
            continue
        for path in sorted(md_dir.iterdir()):
            if not path.is_file():
                continue
            if _should_delete(path):
                path.unlink()
                deleted.append(str(path))

    for path in deleted:
        print(path)
    print(f"deleted={len(deleted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
