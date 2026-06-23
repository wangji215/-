"""大模型策略输出解析测试（不访问网络）。"""
from __future__ import annotations

from core import llm_client
from core.llm_client import _parse_strategy_object, _validate_text_dsl_coverage


def _dsl_payload() -> dict:
    return {
        "name": "简单策略",
        "lookback": 30,
        "conditions": [{
            "logic": "and",
            "rules": [{
                "left": {"ind": "CLOSE"},
                "op": ">",
                "right": {"ind": "MA", "period": 5},
            }],
        }],
    }


def _draft_payload() -> dict:
    return {
        "name": "鱼跃龙门",
        "description": "前期拉升后回踩，触发启动",
        "timeframe": "daily",
        "lookback": 60,
        "mode": "trigger",
        "segments": [
            {
                "name": "前期拉升",
                "role": "setup",
                "window": {"start": -60, "end": -20},
                "intent": "出现明显阶段涨幅",
                "technical_description": "HHV/LLV 有足够涨幅",
            },
            {
                "name": "回踩整理",
                "role": "pullback",
                "window": {"start": -30, "end": -2},
                "intent": "回踩但趋势未破",
                "technical_description": "价格不追前高",
            },
            {
                "name": "启动确认",
                "role": "trigger",
                "window": {"start": -1, "end": 0},
                "intent": "今日触发",
                "technical_description": "昨日弱，今日上穿",
            },
        ],
    }


def _compiled_temporal_dsl() -> dict:
    return {
        "name": "鱼跃龙门",
        "lookback": 60,
        "conditions": [{
            "logic": "and",
            "rules": [
                {"left": {"ind": "HHV", "period": 60, "field": "HIGH"}, "op": ">", "right": {"ind": "LLV", "period": 60, "field": "LOW"}, "multiplier": 1.25},
                {"left": {"ind": "HHVBARS", "period": 60, "field": "HIGH"}, "op": ">=", "value": 3},
                {"left": {"ind": "CLOSE", "offset": 1}, "op": "<", "right": {"ind": "MA", "period": 10, "offset": 1}},
                {"left": {"ind": "CLOSE"}, "op": "cross_up", "right": {"ind": "MA", "period": 5}},
            ],
        }],
    }


def test_parse_strategy_object_accepts_legacy_direct_dsl():
    dsl, generation, err = _parse_strategy_object(_dsl_payload())

    assert err is None
    assert dsl is not None
    assert generation is None
    assert dsl.name == "简单策略"


def test_parse_strategy_object_accepts_draft_generation_result():
    dsl, generation, err = _parse_strategy_object({
        "draft": _draft_payload(),
        "compiled_dsl": _compiled_temporal_dsl(),
    })

    assert err is None
    assert dsl is not None
    assert generation is not None
    assert dsl.name == "鱼跃龙门"
    assert generation.draft.segments[0].name == "前期拉升"


def test_parse_strategy_object_keeps_dsl_when_generation_result_has_coverage_warnings():
    dsl, generation, err = _parse_strategy_object({
        "draft": _draft_payload(),
        "compiled_dsl": _dsl_payload(),
    })

    assert dsl is not None
    assert generation is not None
    assert err is not None
    assert "覆盖检查提示" in err


def test_parse_strategy_object_keeps_compiled_dsl_when_draft_is_invalid():
    payload = {
        "draft": _draft_payload(),
        "compiled_dsl": _compiled_temporal_dsl(),
    }
    payload["draft"]["segments"][0]["window"] = {"start": -10, "end": -20}

    dsl, generation, err = _parse_strategy_object(payload)

    assert dsl is not None
    assert generation is None
    assert dsl.name == "鱼跃龙门"
    assert err is not None
    assert "分段策略校验失败" in err


def test_generate_strategy_uses_zero_temperature_for_repeatable_structure(monkeypatch):
    captured = {}

    def fake_chat(messages, **overrides):
        captured.update(overrides)
        return "```json\n" + str(_dsl_payload()).replace("'", '"') + "\n```"

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    raw, dsl, generation, err = llm_client.generate_strategy([
        {"role": "user", "content": "生成一个收盘价站上MA5的策略"}
    ])

    assert err is None
    assert dsl is not None
    assert generation is None
    assert captured["temperature"] == 0


def test_generate_strategy_retries_when_temporal_semantics_are_missing(monkeypatch):
    replies = [
        "```json\n" + str(_dsl_payload()).replace("'", '"') + "\n```",
        "```json\n" + str(_compiled_temporal_dsl()).replace("'", '"') + "\n```",
    ]

    def fake_chat(messages, **overrides):
        return replies.pop(0)

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    raw, dsl, generation, err = llm_client.generate_strategy([
        {"role": "user", "content": "生成一个前期拉升后回踩，买点再次站上的策略"}
    ])

    assert err is None
    assert dsl is not None
    assert generation is None
    assert dsl.name == "鱼跃龙门"
    assert replies == []


def test_text_coverage_flags_flat_rules_when_prompt_requires_stage_shape():
    dsl, _, err = _parse_strategy_object({
        "name": "拉升回踩再上买点",
        "description": "20日左右拉升后回踩，MA5贴近MA13再上，K线站上MA5触发买点",
        "lookback": 60,
        "conditions": [{
            "logic": "and",
            "rules": [
                {"left": {"ind": "MA", "period": 13}, "op": ">=", "right": {"ind": "MA", "period": 13, "offset": 3}},
                {"left": {"ind": "MA", "period": 21}, "op": ">=", "right": {"ind": "MA", "period": 21, "offset": 3}},
                {"left": {"ind": "CLOSE"}, "op": ">", "right": {"ind": "MA", "period": 21}},
                {"left": {"ind": "CLOSE"}, "op": ">", "right": {"ind": "MA", "period": 60}},
                {"left": {"ind": "MA", "period": 5}, "op": "<", "right": {"ind": "MA", "period": 13}, "multiplier": 1.03},
                {"left": {"ind": "MA", "period": 5}, "op": ">", "right": {"ind": "MA", "period": 13}, "multiplier": 0.98},
                {"left": {"ind": "MA", "period": 5}, "op": ">=", "right": {"ind": "MA", "period": 13}},
                {"left": {"ind": "CLOSE"}, "op": "cross_up", "right": {"ind": "MA", "period": 5}},
            ],
        }],
    })

    assert err is None
    assert dsl is not None

    issues = _validate_text_dsl_coverage(
        "阶段一：20日左右拉升，ma13、ma21向上；阶段二：回踩，ma5回踩贴近ma13，再次向上，未形成死叉；阶段三：买点，K线再次站上ma5",
        dsl,
    )

    assert any("拉升" in issue for issue in issues)
    assert any("回踩" in issue for issue in issues)
    assert any("未形成死叉" in issue for issue in issues)
