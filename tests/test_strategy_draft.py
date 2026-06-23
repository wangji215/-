"""分段策略草稿层测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.strategy_draft import (
    SegmentWindow,
    StrategyDraft,
    StrategyGenerationResult,
    StrategySegment,
)
from core.strategy_compiler import compile_draft_defaults, validate_draft_coverage
from core.strategy_dsl import StrategyDSL


def _segment(name: str, role: str, start: int, end: int) -> dict:
    return {
        "name": name,
        "role": role,
        "window": {"start": start, "end": end},
        "intent": f"{name}意图",
        "technical_description": f"{name}技术描述",
        "rules": [],
    }


def _draft_payload() -> dict:
    return {
        "name": "鱼跃龙门",
        "description": "前期拉升后回踩，触发启动",
        "timeframe": "daily",
        "lookback": 60,
        "mode": "trigger",
        "segments": [
            _segment("前期拉升", "setup", -60, -20),
            _segment("回踩整理", "pullback", -30, -2),
            _segment("启动确认", "trigger", -1, 0),
        ],
    }


def test_strategy_draft_accepts_three_segment_trigger_shape():
    draft = StrategyDraft.model_validate(_draft_payload())

    assert draft.lookback == 60
    assert [seg.role for seg in draft.segments] == ["setup", "pullback", "trigger"]
    assert draft.segments[0].window.start == -60
    assert draft.segments[-1].window.end == 0


def test_segment_window_requires_order_and_non_future_end():
    with pytest.raises(ValidationError):
        SegmentWindow.model_validate({"start": -5, "end": -10})

    with pytest.raises(ValidationError):
        SegmentWindow.model_validate({"start": -3, "end": 1})


def test_strategy_draft_requires_lookback_to_cover_earliest_segment():
    payload = _draft_payload()
    payload["lookback"] = 30

    with pytest.raises(ValidationError):
        StrategyDraft.model_validate(payload)


def test_trigger_mode_requires_trigger_segment():
    payload = _draft_payload()
    payload["segments"] = [
        _segment("前期拉升", "setup", -60, -20),
        _segment("回踩整理", "pullback", -30, -2),
    ]

    with pytest.raises(ValidationError):
        StrategyDraft.model_validate(payload)


def test_generation_result_holds_draft_and_compiled_dsl_dict():
    result = StrategyGenerationResult.model_validate({
        "draft": _draft_payload(),
        "compiled_dsl": {
            "name": "鱼跃龙门",
            "lookback": 60,
            "conditions": [{
                "logic": "and",
                "rules": [{
                    "left": {"ind": "CLOSE"},
                    "op": ">",
                    "right": {"ind": "MA", "period": 5},
                }],
            }],
        },
    })

    assert result.draft.name == "鱼跃龙门"
    assert result.compiled_dsl.name == "鱼跃龙门"


def test_validate_draft_coverage_reports_missing_temporal_semantics():
    draft = StrategyDraft.model_validate(_draft_payload())
    flat_dsl = StrategyDSL.model_validate({
        "name": "扁平条件",
        "lookback": 60,
        "conditions": [{
            "logic": "and",
            "rules": [{
                "left": {"ind": "CLOSE"},
                "op": ">",
                "right": {"ind": "MA", "period": 5},
            }],
        }],
    })

    issues = validate_draft_coverage(draft, flat_dsl)

    assert any("窗口" in issue for issue in issues)
    assert any("offset" in issue or "昨日" in issue for issue in issues)
    assert any("触发" in issue or "cross_up" in issue for issue in issues)


def test_compile_draft_defaults_generates_executable_temporal_dsl():
    draft = StrategyDraft.model_validate(_draft_payload())

    dsl = compile_draft_defaults(draft)
    rules = [rule for group in dsl.conditions for rule in group.rules]
    inds = {(rule.left.ind.upper(), rule.left.period, rule.left.field) for rule in rules}
    ops = {rule.op for rule in rules}

    assert dsl.name == draft.name
    assert dsl.lookback == draft.lookback
    assert ("HHV", 60, "HIGH") in inds
    assert ("LLV", 60, "LOW") in {
        ((rule.right.ind.upper(), rule.right.period, rule.right.field) if rule.right else (None, None, None))
        for rule in rules
    }
    assert any(rule.left.ind.upper() == "HHVBARS" for rule in rules)
    assert any(rule.left.offset == 1 or (rule.right and rule.right.offset == 1) for rule in rules)
    assert "cross_up" in ops
    assert validate_draft_coverage(draft, dsl) == []
