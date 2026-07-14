"""Classifier 结果容器 + 可信度标签.

本模块在 Phase 2 精简后只保留 replay.py 真正需要的部分:
  - ClassifyResult        结果 dataclass
  - determine_confidence  缺失维度数 → high/medium/low

之前的 MD/JSON/regime-log.md 渲染/解析函数随 classify.py 一并退役 (2026-04-17).
历史版本如有需要可从 git 历史找回.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ClassifyResult:
    """一次分类的完整结果, 所有下游消费者从这里取数据."""

    date: str

    # 得分
    score_total: int
    score_breakdown: dict  # {ma_position, advance_decline, sentiment_delta,
    #                        sentiment_index, streak_height, volume_trend}

    # 原始数据快照
    raw_data: dict

    # Regime 决策
    regime_code: str
    last_regime_code: str
    switched: bool
    bootstrap: bool

    # 降级
    confidence: str  # "high" | "medium" | "low"
    missing_dims: list

    # 提示
    switch_warning: Optional[str] = None

    # 战法
    playbook: dict = field(default_factory=dict)

    # 逃生门
    emergency_switch: bool = False
    emergency_direction: Optional[str] = None  # "up" | "down" | None
    emergency_reason: list = field(default_factory=list)


def determine_confidence(missing_dim_count: int) -> str:
    """按缺失维度数返回可信度标签 (spec §8)."""
    if missing_dim_count <= 0:
        return "high"
    if missing_dim_count <= 2:
        return "medium"
    return "low"
