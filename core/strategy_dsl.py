"""策略 DSL（结构化图形策略）。

LLM 输出该结构（JSON），由 :mod:`core.strategy_engine` 确定性执行。
设计：顶层 ``conditions`` 是多个组的列表，组与组之间 **AND**；
每组内 ``rules`` 按 ``logic``（and/or）组合。
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# 原生字段指标（无需 period）
_FIELD_INDS = {"CLOSE", "OPEN", "HIGH", "LOW", "VOL", "AMOUNT", "PCT_CHG"}
# 需要 period 的指标
_PERIOD_INDS = {
    "MA", "EMA", "MA_VOL", "RSI", "BOLL",
    "HHV", "LLV", "HHVBARS", "LLVBARS", "RISEBARS",
    "COUNT", "EXIST", "EVERY", "BARSLAST",
}
# 子字段指标（用 field 指定分量）
_SUBFIELD_INDS = {"MACD", "KDJ", "BOLL"}
# 形态（返回布尔）
_PATTERNS = {"DOJI", "HAMMER", "ENGULFING_BULL", "ENGULFING_BEAR", "CONSECUTIVE_UP", "CONSECUTIVE_DOWN"}

OPS = Literal[">", "<", ">=", "<=", "cross_up", "cross_down", "between", "is_true"]


class Indicator(BaseModel):
    ind: str = Field(..., description="指标名，如 MA / EMA / MACD / KDJ / RSI / BOLL / CLOSE / PATTERN")
    period: Optional[int] = Field(None, description="MA/EMA/RSI/BOLL/MA_VOL 的周期；连阳/连阴的 N")
    field: Optional[str] = Field(None, description="MACD:dif/dea/hist；KDJ:k/d/j；BOLL:upper/mid/lower")
    offset: Optional[int] = Field(None, ge=0, description="向前偏移的交易日数，1 表示上一交易日")
    expr: Optional[dict[str, Any]] = Field(None, description="COUNT/EXIST/EVERY/BARSLAST 的嵌套条件 Rule")


class Rule(BaseModel):
    left: Indicator
    op: OPS = ">"
    right: Optional[Indicator] = None
    value: Optional[float] = None
    multiplier: Optional[float] = Field(None, description="对 right 再乘的倍数，如放量 1.5 倍")
    between_low: Optional[float] = None
    between_high: Optional[float] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_value_right(cls, data):
        if not isinstance(data, dict):
            return data
        right = data.get("right")
        if isinstance(right, dict) and set(right.keys()) <= {"value"} and "value" in right:
            data = dict(data)
            data["value"] = right["value"] if data.get("value") is None else data.get("value")
            data["right"] = None
        return data


class ConditionGroup(BaseModel):
    logic: Literal["and", "or"] = "and"
    rules: List[Rule] = Field(default_factory=list)


class StrategyDSL(BaseModel):
    name: str
    description: str = ""
    timeframe: Literal["daily"] = "daily"
    lookback: int = Field(60, ge=5, le=500)
    conditions: List[ConditionGroup] = Field(default_factory=list)

    @field_validator("conditions")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("conditions 不能为空")
        return v


def dsl_to_text(dsl: StrategyDSL) -> str:
    """把 DSL 渲染成可读中文，用于界面预览。"""
    op_cn = {
        ">": ">", "<": "<", ">=": "≥", "<=": "≤",
        "cross_up": "上穿", "cross_down": "下穿",
        "between": "介于", "is_true": "成立",
    }

    def ind_text(ind: Indicator) -> str:
        n = ind.ind.upper()

        def with_offset(text: str) -> str:
            return f"{ind.offset}日前{text}" if ind.offset else text

        if n in _PATTERNS:
            n2 = {"DOJI": "十字星", "HAMMER": "锤子线", "ENGULFING_BULL": "阳包阴",
                  "ENGULFING_BEAR": "阴包阳", "CONSECUTIVE_UP": "N连阳", "CONSECUTIVE_DOWN": "N连阴"}[n]
            text = f"{n2}" + (f"({ind.period})" if ind.period else "")
            return with_offset(text)
        if n in _FIELD_INDS:
            text = {"CLOSE": "收盘价", "OPEN": "开盘价", "HIGH": "最高价", "LOW": "最低价",
                    "VOL": "成交量", "AMOUNT": "成交额", "PCT_CHG": "涨跌幅"}[n]
            return with_offset(text)
        if n in {"HHV", "LLV", "HHVBARS", "LLVBARS"}:
            default_field = "LOW" if n == "LLV" else "HIGH"
            field = {"HIGH": "最高价", "LOW": "最低价", "CLOSE": "收盘价", "OPEN": "开盘价",
                     "VOL": "成交量", "AMOUNT": "成交额", "PCT_CHG": "涨跌幅"}.get((ind.field or default_field).upper(), ind.field or default_field)
            label = {"HHV": "最高值", "LLV": "最低值", "HHVBARS": "距最高值天数", "LLVBARS": "距最低值天数"}[n]
            text = f"{ind.period or 20}日{field}{label}"
            return with_offset(text)
        if n == "RISEBARS":
            return with_offset(f"{ind.period or 20}日低点到高点天数")
        if n in {"COUNT", "EXIST", "EVERY", "BARSLAST"}:
            label = {"COUNT": "计数", "EXIST": "存在", "EVERY": "持续", "BARSLAST": "距上次成立天数"}[n]
            span = f"{ind.period}日" if ind.period else ""
            return with_offset(f"{span}{label}条件")
        base = {"MA": "MA均线", "EMA": "EMA均线", "MA_VOL": "成交量均线", "RSI": "RSI",
                "MACD": "MACD", "KDJ": "KDJ", "BOLL": "布林带"}.get(n, n)
        parts = [base]
        if ind.period:
            parts.append(str(ind.period))
        if ind.field:
            parts.append(ind.field)
        text = "".join(parts) if n in ("MA", "EMA", "MA_VOL", "RSI") else f"{base}({ind.field or ''}{ind.period or ''})"
        return with_offset(text)

    def rule_text(r: Rule) -> str:
        if r.left.ind.upper() in _PATTERNS or r.op == "is_true":
            return f"{ind_text(r.left)}成立"
        left = ind_text(r.left)
        op = op_cn.get(r.op, r.op)
        if r.op == "between":
            return f"{left}介于[{r.between_low},{r.between_high}]"
        if r.right is not None:
            right = ind_text(r.right)
            if r.multiplier:
                right = f"{r.multiplier}×{right}"
            return f"{left} {op} {right}"
        return f"{left} {op} {r.value}"

    lines = []
    for i, g in enumerate(dsl.conditions, 1):
        join = "且" if g.logic == "and" else "或"
        inner = f" {join} ".join(rule_text(r) for r in g.rules)
        lines.append(f"条件组{i}：({inner})")
    head = f"【{dsl.name}】回看{dsl.lookback}日"
    if dsl.description:
        head += f" · {dsl.description}"
    return head + "\n" + "\n".join(lines)
