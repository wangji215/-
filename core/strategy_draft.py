"""分段策略草稿 schema。

草稿用于表达模型对图形策略阶段结构的理解；真正执行仍使用
``StrategyDSL``。
"""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field, model_validator

from core.strategy_dsl import Rule, StrategyDSL


SegmentRole = Literal["setup", "advance", "pullback", "consolidation", "trigger", "filter"]
StrategyMode = Literal["stage", "trigger"]


class SegmentWindow(BaseModel):
    """相对当前交易日的交易日窗口，0 表示当前日。"""

    start: int
    end: int

    @model_validator(mode="after")
    def _validate_order(self):
        if self.start > self.end:
            raise ValueError("window.start 必须小于等于 window.end")
        if self.end > 0:
            raise ValueError("window.end 不能指向未来")
        return self


class StrategySegment(BaseModel):
    name: str
    role: SegmentRole
    window: SegmentWindow
    intent: str = ""
    technical_description: str = ""
    rules: List[Rule] = Field(default_factory=list)


class StrategyDraft(BaseModel):
    name: str
    description: str = ""
    timeframe: Literal["daily"] = "daily"
    lookback: int = Field(60, ge=5, le=500)
    mode: StrategyMode = "stage"
    segments: List[StrategySegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_segments(self):
        if not self.segments:
            raise ValueError("segments 不能为空")
        earliest = min(seg.window.start for seg in self.segments)
        if self.lookback < abs(earliest):
            raise ValueError("lookback 必须覆盖最早分段窗口")
        if self.mode == "trigger" and not any(seg.role == "trigger" for seg in self.segments):
            raise ValueError("trigger 模式必须包含 trigger 分段")
        return self


class StrategyGenerationResult(BaseModel):
    """LLM 输出顶层结构：分段草稿 + 可执行 DSL。"""

    draft: StrategyDraft
    compiled_dsl: StrategyDSL
