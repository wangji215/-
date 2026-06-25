"""交易规则 DSL（结构化持仓/退出策略）。

与策略 DSL（LLM 生成）解耦：本结构由 pages/5 表单填写，回测时按 trade_rule_id
加载并注入 DslSignalStrategy。语义：

- 入场信号：仍由 core.strategy_engine.evaluate_history 提供（每日 DSL 命中）
- 持仓/退出：由本规则的 stop_loss / take_profit / max_holding_days / time_stop 决定
- 卖出优先级：止损 > 止盈 > 时间到。**信号消失不再触发卖出**。
- buy_time/sell_time：日内 HH:MM 时点；回测精度日线，近似为「次日开盘买/当日收盘卖」。
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class TradeRuleSpec(BaseModel):
    """交易规则字段集。stop/take profit 用正数 magnitude（5.0 表示 5%）。"""

    max_positions: int = Field(5, ge=1, le=50, description="最多同时持有股票数")
    position_pct: float = Field(0.20, ge=0.01, le=1.0, description="单只仓位占比（等权时建议 = 1/max_positions）")
    max_holding_days: int = Field(10, ge=1, le=250, description="最大持股交易日数，到则强平（time_stop 开启时生效）")
    stop_loss_pct: Optional[float] = Field(5.0, ge=0, le=50, description="止损百分比 magnitude；None 表示不止损")
    take_profit_pct: Optional[float] = Field(10.0, ge=0, le=200, description="止盈百分比 magnitude；None 表示不止盈")
    time_stop: bool = Field(True, description="到达 max_holding_days 时强平")
    buy_time: str = Field("09:35", description="日内买入时点 HH:MM；回测忽略（日线数据无日内价），仅记录实盘意图")
    sell_time: str = Field("14:55", description="日内卖出时点 HH:MM；回测忽略（日线数据无日内价），仅记录实盘意图")

    @field_validator("buy_time", "sell_time")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        if not _HHMM_RE.fullmatch(v):
            raise ValueError("时间格式必须为 HH:MM（00:00 ~ 23:59）")
        return v


def rule_to_text(spec: TradeRuleSpec) -> str:
    """把规则渲染成可读中文，用于界面预览。"""
    parts = [
        f"最多持 {spec.max_positions} 只（单只 {spec.position_pct*100:.1f}%）",
        f"最大持股 {spec.max_holding_days} 交易日",
    ]
    if spec.stop_loss_pct is not None:
        parts.append(f"止损 {spec.stop_loss_pct:.1f}%")
    if spec.take_profit_pct is not None:
        parts.append(f"止盈 {spec.take_profit_pct:.1f}%")
    if spec.time_stop:
        parts.append(f"到 {spec.max_holding_days} 日强平")
    parts.append(f"买 {spec.buy_time} / 卖 {spec.sell_time}")
    return " · ".join(parts)
