"""分段策略草稿到可执行 DSL 的覆盖检查与默认编译。"""
from __future__ import annotations

from typing import Iterable

from core.strategy_draft import StrategyDraft
from core.strategy_dsl import Indicator, Rule, StrategyDSL

_WINDOW_INDS = {"HHV", "LLV", "HHVBARS", "LLVBARS", "RISEBARS", "COUNT", "EXIST", "EVERY", "BARSLAST"}


def _rules(dsl: StrategyDSL) -> list[Rule]:
    return [rule for group in dsl.conditions for rule in group.rules]


def _indicators(rule: Rule) -> Iterable[Indicator]:
    yield rule.left
    if rule.right is not None:
        yield rule.right


def _has_indicator(dsl: StrategyDSL, names: set[str]) -> bool:
    for rule in _rules(dsl):
        for indicator in _indicators(rule):
            if indicator.ind.upper() in names:
                return True
    return False


def _has_offset(dsl: StrategyDSL) -> bool:
    for rule in _rules(dsl):
        for indicator in _indicators(rule):
            if indicator.offset:
                return True
    return False


def _has_cross_trigger(dsl: StrategyDSL) -> bool:
    return any(rule.op in {"cross_up", "cross_down"} for rule in _rules(dsl))


def validate_draft_coverage(draft: StrategyDraft, dsl: StrategyDSL) -> list[str]:
    """检查可执行 DSL 是否覆盖分段草稿的关键时序语义。"""
    issues: list[str] = []
    has_staged_window = any(seg.window.start < 0 for seg in draft.segments)
    has_setup_like = any(seg.role in {"setup", "advance", "pullback", "consolidation"} for seg in draft.segments)
    has_trigger = draft.mode == "trigger" or any(seg.role == "trigger" for seg in draft.segments)

    if (has_staged_window or has_setup_like) and not _has_indicator(dsl, _WINDOW_INDS):
        issues.append("分段窗口语义缺少 HHV/LLV/HHVBARS 等窗口指标")
    if has_trigger and not _has_offset(dsl):
        issues.append("触发段缺少 offset 昨日/前一日条件")
    if has_trigger and not _has_cross_trigger(dsl):
        issues.append("触发段缺少 cross_up/cross_down 等明确触发条件")

    return issues


def compile_draft_defaults(draft: StrategyDraft) -> StrategyDSL:
    """用通用模板把分段草稿编译成现有 StrategyDSL。

    这是第一版的保守默认编译器：只生成通用时序骨架，不尝试拟合单只股票。
    """
    lookback = draft.lookback
    rules = [
        {
            "left": {"ind": "HHV", "period": lookback, "field": "HIGH"},
            "op": ">",
            "right": {"ind": "LLV", "period": lookback, "field": "LOW"},
            "multiplier": 1.25,
        },
        {
            "left": {"ind": "HHVBARS", "period": lookback, "field": "HIGH"},
            "op": ">=",
            "value": 3,
        },
        {
            "left": {"ind": "HHVBARS", "period": lookback, "field": "HIGH"},
            "op": "<=",
            "value": min(30, max(3, lookback // 2)),
        },
        {
            "left": {"ind": "CLOSE"},
            "op": "<=",
            "right": {"ind": "HHV", "period": lookback, "field": "HIGH"},
            "multiplier": 0.95,
        },
        {
            "left": {"ind": "CLOSE"},
            "op": ">=",
            "right": {"ind": "HHV", "period": lookback, "field": "HIGH"},
            "multiplier": 0.78,
        },
    ]

    if draft.mode == "trigger" or any(seg.role == "trigger" for seg in draft.segments):
        rules.extend([
            {
                "left": {"ind": "CLOSE", "offset": 1},
                "op": "<",
                "right": {"ind": "MA", "period": 10, "offset": 1},
            },
            {
                "left": {"ind": "CLOSE"},
                "op": "cross_up",
                "right": {"ind": "MA", "period": 5},
            },
        ])

    return StrategyDSL.model_validate({
        "name": draft.name,
        "description": draft.description,
        "timeframe": draft.timeframe,
        "lookback": lookback,
        "conditions": [{
            "logic": "and",
            "rules": rules,
        }],
    })
