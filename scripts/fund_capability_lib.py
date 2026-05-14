"""能力圈解析 + 范式选择 — 纯函数库,无 IO 副作用。

由 fund-phase-b1.py 调用。yaml frontmatter 解析依赖 PyYAML(若不存在,fallback 到简单解析)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError as _:
    yaml = None  # noqa: N816  fallback below


GATE_SKIP = "SKIP"
SWITCH_COOLDOWN_DAYS = 30


@dataclass
class CapabilityCircle:
    macro: bool
    industry_rotation: bool
    industry_focus: Optional[str]
    fund_alpha: bool
    default_paradigm: Optional[str]
    secondary_paradigm: Optional[str]
    switch_rules: list[dict] = field(default_factory=list)
    last_assessment_date: Optional[str] = None
    next_assessment_due: Optional[str] = None

    def is_empty(self) -> bool:
        return not (self.macro or self.industry_rotation or
                    bool(self.industry_focus) or self.fund_alpha)

    def active_paradigms(self) -> list[str]:
        out = []
        if self.macro:
            out.append("A")
        if self.industry_rotation:
            out.append("B1")
        if self.industry_focus:
            out.append("B2")
        if self.fund_alpha:
            out.append("C")
        return out


@dataclass
class ParadigmDecision:
    paradigm_active: str             # A | B1 | B2 | C | SKIP
    capability_field: Optional[str]
    capability_value: Optional[str]
    switched_from: Optional[str]
    reason: str


def parse_capability_md(md_text: str) -> CapabilityCircle:
    """从 markdown 文件内容里抽取 yaml frontmatter,返回 CapabilityCircle。

    Raises ValueError 若无 frontmatter 或互斥规则违反。
    """
    if not md_text.startswith("---"):
        raise ValueError("能力圈宣告 markdown 必须以 yaml frontmatter '---' 开头")
    try:
        end = md_text.index("---", 3)
    except ValueError:
        raise ValueError("找不到 yaml frontmatter 结束 '---'")
    fm = md_text[3:end].strip()

    if yaml is None:
        raise RuntimeError("PyYAML 未安装,请 pip install pyyaml")
    data = yaml.safe_load(fm) or {}

    levels = data.get("capability_levels", {})
    macro = bool(levels.get("macro", False))
    rotation = bool(levels.get("industry_rotation", False))
    focus_raw = levels.get("industry_focus")
    focus = focus_raw.strip() if isinstance(focus_raw, str) and focus_raw.strip() else None
    alpha = bool(levels.get("fund_alpha", False))

    if rotation and focus:
        raise ValueError(
            f"B1(industry_rotation=true) 与 B2(industry_focus={focus!r}) 互斥,不能同时声明"
        )

    return CapabilityCircle(
        macro=macro,
        industry_rotation=rotation,
        industry_focus=focus,
        fund_alpha=alpha,
        default_paradigm=data.get("default_paradigm"),
        secondary_paradigm=data.get("secondary_paradigm"),
        switch_rules=data.get("switch_rules", []) or [],
        last_assessment_date=data.get("last_assessment_date"),
        next_assessment_due=data.get("next_assessment_due"),
    )


def _capability_field_value(cc: CapabilityCircle, paradigm: str) -> tuple[str, str]:
    if paradigm == "A":
        return "macro", "true"
    if paradigm == "B1":
        return "industry_rotation", "true"
    if paradigm == "B2":
        return "industry_focus", cc.industry_focus or ""
    if paradigm == "C":
        return "fund_alpha", "true"
    return "", ""


def _days_between(start_iso: str, end_iso: str) -> int:
    s = datetime.fromisoformat(start_iso).date()
    e = datetime.fromisoformat(end_iso).date()
    return (e - s).days


def select_paradigm(
    cc: CapabilityCircle,
    current_paradigm: Optional[str],
    last_switch_date: Optional[str],
    today: str,
) -> ParadigmDecision:
    """决定本日的 paradigm。

    规则:
    1. 能力圈全空 → SKIP。
    2. 单一能力圈 → 自动选定该范式。
    3. 复合能力圈:
       a. 首次运行(current_paradigm 为 None)→ default_paradigm。
       b. 切换间隔 < 30 天 → 保持 current_paradigm。
       c. 否则保持 current_paradigm(切换由 bot 在策略摘要里写规则,本框架不预设)。
    """
    if cc.is_empty():
        return ParadigmDecision(
            paradigm_active=GATE_SKIP,
            capability_field=None, capability_value=None,
            switched_from=current_paradigm,
            reason="no_capability_skip",
        )

    actives = cc.active_paradigms()

    if len(actives) == 1:
        p = actives[0]
        f, v = _capability_field_value(cc, p)
        return ParadigmDecision(
            paradigm_active=p,
            capability_field=f, capability_value=v,
            switched_from=None,
            reason="single_capability_auto",
        )

    if current_paradigm is None:
        target = cc.default_paradigm or actives[0]
        f, v = _capability_field_value(cc, target)
        return ParadigmDecision(
            paradigm_active=target,
            capability_field=f, capability_value=v,
            switched_from=None,
            reason="first_run_default",
        )

    if last_switch_date:
        try:
            days = _days_between(last_switch_date, today)
        except Exception:
            days = SWITCH_COOLDOWN_DAYS
        if days < SWITCH_COOLDOWN_DAYS:
            f, v = _capability_field_value(cc, current_paradigm)
            return ParadigmDecision(
                paradigm_active=current_paradigm,
                capability_field=f, capability_value=v,
                switched_from=None,
                reason=f"cooldown_keep_{days}/{SWITCH_COOLDOWN_DAYS}d",
            )

    f, v = _capability_field_value(cc, current_paradigm)
    return ParadigmDecision(
        paradigm_active=current_paradigm,
        capability_field=f, capability_value=v,
        switched_from=None,
        reason="composite_keep_current",
    )
