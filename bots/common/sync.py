#!/usr/bin/env python3
"""把 common/agents_common.md, common/soul_common.md 同步注入到所有 bot 的对应文件顶部。

用 <!-- COMMON:START --> ... <!-- COMMON:END --> 包裹注入区，幂等可重跑。
bot 专属内容写在标记之后；标记内的内容下次同步会被覆盖。

用法：编辑 bots/common/*.md 之后跑 `python3 bots/common/sync.py`。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
BOTS_DIR = ROOT.parent

MAPPINGS = [
    ("agents_common.md", "AGENTS.md"),
    ("soul_common.md", "SOUL.md"),
]

START = "<!-- COMMON:START — 自动同步自 bots/common/，请勿手动修改此区块 -->"
END = "<!-- COMMON:END -->"
BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)


def inject(common_text: str, target_text: str) -> str:
    block = f"{START}\n{common_text.strip()}\n{END}"
    if BLOCK_RE.search(target_text):
        return BLOCK_RE.sub(block, target_text, count=1)
    tail = target_text.lstrip()
    return f"{block}\n\n{tail}" if tail else f"{block}\n"


def main() -> int:
    updated = 0
    for src_name, dst_name in MAPPINGS:
        src = ROOT / src_name
        if not src.exists():
            print(f"skip {src_name}: missing in common/")
            continue
        common = src.read_text(encoding="utf-8")
        for bot in sorted(BOTS_DIR.glob("bot*")):
            if not bot.is_dir():
                continue
            dst = bot / dst_name
            existing = dst.read_text(encoding="utf-8") if dst.exists() else ""
            new = inject(common, existing)
            if new != existing:
                dst.write_text(new, encoding="utf-8")
                print(f"updated {dst.relative_to(BOTS_DIR.parent)}")
                updated += 1
    print(f"done. {updated} file(s) changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
