"""策略引擎：把 DSL 在日 K 数据上确定性求值，输出 snapshot 当日命中的 ts_code 列表。

采用「按 ts_code 分组、向量化求值」的方式，每只股票在回看窗口内的指标一次性算出，
最后取 snapshot_date 当日布尔结果。约 6000 只股票可在秒级完成。
"""
from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from core import indicators as ind
from core.strategy_dsl import Indicator, Rule, StrategyDSL

_BAR_COLS = ["open", "high", "low", "close", "vol", "amount", "pct_chg"]


def _apply_offset(series: pd.Series, spec: Indicator) -> pd.Series:
    offset = spec.offset or 0
    return series.shift(offset) if offset else series


def _price_field(df: pd.DataFrame, spec: Indicator, default: str) -> pd.Series:
    field = (spec.field or default).lower()
    if field not in _BAR_COLS:
        raise ValueError(f"{spec.ind} 不支持字段: {spec.field}")
    return df[field]


def _bars_since_extreme(series: pd.Series, period: int, kind: str) -> pd.Series:
    def bars_since(values) -> float:
        target = values.min() if kind == "min" else values.max()
        positions = np.flatnonzero(values == target)
        return float(len(values) - 1 - positions[-1])

    return series.rolling(period, min_periods=period).apply(bars_since, raw=True)


def _risebars(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    out = np.full(len(df), np.nan)
    for end in range(period - 1, len(df)):
        start = end - period + 1
        lows = low[start:end + 1]
        low_positions = np.flatnonzero(lows == lows.min())
        low_pos = int(low_positions[0])
        highs_after_low = high[start + low_pos:end + 1]
        high_positions = np.flatnonzero(highs_after_low == highs_after_low.max())
        high_pos = low_pos + int(high_positions[-1])
        out[end] = float(high_pos - low_pos)
    return pd.Series(out, index=df.index)


def _barslast(flag: pd.Series) -> pd.Series:
    values = flag.fillna(False).astype(bool).to_numpy()
    out = np.full(len(values), np.nan)
    last_true = None
    for i, value in enumerate(values):
        if value:
            last_true = i
        if last_true is not None:
            out[i] = float(i - last_true)
    return pd.Series(out, index=flag.index)


def _resolve_condition_expr(df: pd.DataFrame, spec: Indicator) -> pd.Series:
    if not spec.expr:
        raise ValueError(f"{spec.ind} 需要 expr 条件")
    return _eval_rule(df, Rule.model_validate(spec.expr)).fillna(False).astype(bool)


def _resolve_indicator(df: pd.DataFrame, spec: Indicator) -> pd.Series:
    """返回一只股票某指标的 Series（布尔形态也返回布尔 Series）。"""
    name = spec.ind.upper()
    if name == "CLOSE":
        return _apply_offset(df["close"], spec)
    if name == "OPEN":
        return _apply_offset(df["open"], spec)
    if name == "HIGH":
        return _apply_offset(df["high"], spec)
    if name == "LOW":
        return _apply_offset(df["low"], spec)
    if name == "VOL":
        return _apply_offset(df["vol"], spec)
    if name == "AMOUNT":
        return _apply_offset(df["amount"], spec)
    if name == "PCT_CHG":
        return _apply_offset(df["pct_chg"], spec)
    if name == "MA":
        return _apply_offset(ind.MA(df, spec.period or 5), spec)
    if name == "EMA":
        return _apply_offset(ind.EMA(df, spec.period or 5), spec)
    if name == "MA_VOL":
        return _apply_offset(ind.MA_VOL(df, spec.period or 5), spec)
    if name == "RSI":
        return _apply_offset(ind.RSI(df, spec.period or 14), spec)
    if name == "MACD":
        dif, dea, hist = ind.MACD(df)
        return _apply_offset({"dif": dif, "dea": dea, "hist": hist}[(spec.field or "dif").lower()], spec)
    if name == "KDJ":
        k, d, j = ind.KDJ(df)
        return _apply_offset({"k": k, "d": d, "j": j}[(spec.field or "k").lower()], spec)
    if name == "BOLL":
        up, mid, low = ind.BOLL(df, spec.period or 20)
        return _apply_offset({"upper": up, "mid": mid, "lower": low}[(spec.field or "mid").lower()], spec)
    if name == "HHV":
        return _apply_offset(_price_field(df, spec, "high").rolling(spec.period or 20).max(), spec)
    if name == "LLV":
        return _apply_offset(_price_field(df, spec, "low").rolling(spec.period or 20).min(), spec)
    if name == "HHVBARS":
        return _apply_offset(_bars_since_extreme(_price_field(df, spec, "high"), spec.period or 20, "max"), spec)
    if name == "LLVBARS":
        return _apply_offset(_bars_since_extreme(_price_field(df, spec, "low"), spec.period or 20, "min"), spec)
    if name == "RISEBARS":
        return _apply_offset(_risebars(df, spec.period or 20), spec)
    if name == "COUNT":
        flag = _resolve_condition_expr(df, spec)
        return _apply_offset(flag.astype(float).rolling(spec.period or 20, min_periods=spec.period or 20).sum(), spec)
    if name == "EXIST":
        flag = _resolve_condition_expr(df, spec)
        return _apply_offset(flag.astype(float).rolling(spec.period or 20, min_periods=spec.period or 20).sum() > 0, spec)
    if name == "EVERY":
        period = spec.period or 20
        flag = _resolve_condition_expr(df, spec)
        return _apply_offset(flag.astype(float).rolling(period, min_periods=period).sum() == period, spec)
    if name == "BARSLAST":
        return _apply_offset(_barslast(_resolve_condition_expr(df, spec)), spec)
    if name == "PATTERN":
        return _apply_offset(_resolve_pattern(df, spec), spec)
    raise ValueError(f"未知指标: {spec.ind}")


def _resolve_pattern(df: pd.DataFrame, spec: Indicator) -> pd.Series:
    field = (spec.field or "").lower()
    if field == "doji":
        return ind.doji(df)
    if field == "hammer":
        return ind.hammer(df)
    if field == "engulfing_bull":
        return ind.engulfing_bull(df)
    if field == "engulfing_bear":
        return ind.engulfing_bear(df)
    if field == "consecutive_up":
        return ind.consecutive_up(df, spec.period or 3)
    if field == "consecutive_down":
        return ind.consecutive_down(df, spec.period or 3)
    raise ValueError(f"未知形态: {spec.field}")


def _eval_rule(df: pd.DataFrame, rule: Rule) -> pd.Series:
    left_name = rule.left.ind.upper()
    is_pattern = left_name == "PATTERN" or left_name in {
        "DOJI", "HAMMER", "ENGULFING_BULL", "ENGULFING_BEAR", "CONSECUTIVE_UP", "CONSECUTIVE_DOWN",
    }
    if is_pattern:
        # 形态规则：left 即为模式名，op 应为 is_true（兼容历史写法）
        spec = rule.left
        if spec.ind.upper() != "PATTERN":
            spec = Indicator(ind="PATTERN", field=spec.ind.lower(), period=spec.period)
        return _resolve_indicator(df, spec).fillna(False).astype(bool)

    L = _resolve_indicator(df, rule.left)
    if rule.right is not None:
        R = _resolve_indicator(df, rule.right)
        if rule.multiplier:
            R = R * rule.multiplier
    elif rule.op == "between":
        R = None
    else:
        R = pd.Series(rule.value if rule.value is not None else 0.0, index=df.index)

    op = rule.op
    if op == ">":
        return L > R
    if op == "<":
        return L < R
    if op == ">=":
        return L >= R
    if op == "<=":
        return L <= R
    if op == "cross_up":
        return (L > R) & (L.shift(1) <= R.shift(1))
    if op == "cross_down":
        return (L < R) & (L.shift(1) >= R.shift(1))
    if op == "between":
        lo = rule.between_low if rule.between_low is not None else float("-inf")
        hi = rule.between_high if rule.between_high is not None else float("inf")
        return (L >= lo) & (L <= hi)
    if op == "is_true":
        return L.fillna(False).astype(bool)
    raise ValueError(f"未知运算符: {op}")


def _eval_group(df: pd.DataFrame, group) -> pd.Series:
    if not group.rules:
        return pd.Series(True, index=df.index)
    flags = [_eval_rule(df, r).fillna(False).astype(bool) for r in group.rules]
    if group.logic == "and":
        out = flags[0]
        for f in flags[1:]:
            out = out & f
    else:
        out = flags[0]
        for f in flags[1:]:
            out = out | f
    return out


def evaluate(dsl: StrategyDSL, bars: pd.DataFrame, snapshot_date: str) -> List[str]:
    """对 bars（多只股票 × 回看窗口）求值，返回 snapshot_date 当日命中的 ts_code。

    bars 需含列：ts_code, trade_date 以及 OHLCV。
    """
    required = {"ts_code", "trade_date"} | set(_BAR_COLS)
    if not required.issubset(set(bars.columns)):
        raise ValueError(f"bars 缺少列: {required - set(bars.columns)}")
    if snapshot_date not in set(bars["trade_date"].astype(str)):
        return []

    matched: List[str] = []
    for ts_code, g in bars.groupby("ts_code", sort=False):
        g = g.sort_values("trade_date")
        if g.empty:
            continue
        # 顶层 conditions 之间 AND
        combined = None
        for grp in dsl.conditions:
            flag = _eval_group(g, grp)
            combined = flag if combined is None else (combined & flag)
        if combined is None:
            continue
        snap_row = g[g["trade_date"].astype(str) == snapshot_date]
        if snap_row.empty:
            continue
        idx = snap_row.index[-1]
        try:
            hit = bool(combined.loc[idx])
        except KeyError:
            hit = False
        if hit:
            matched.append(ts_code)
    return matched


def evaluate_history(dsl: StrategyDSL, bars: pd.DataFrame) -> pd.DataFrame:
    """返回每个 (ts_code, trade_date) 是否命中（用于回测/可视化），列：ts_code,trade_date,matched。

    向量化：每只股票的布尔结果一次性组装成 DataFrame，避免逐行 iterrows+append
    （全市场 ~5600 只 × 全周期会让纯 Python 行循环达到数分钟量级）。
    """
    frames = []
    for ts_code, g in bars.groupby("ts_code", sort=False):
        g = g.sort_values("trade_date")
        if g.empty:
            continue
        combined = None
        for grp in dsl.conditions:
            flag = _eval_group(g, grp)
            combined = flag if combined is None else (combined & flag)
        if combined is None:
            combined = pd.Series(False, index=g.index)
        matched = combined.fillna(False).astype(bool)
        frames.append(pd.DataFrame({
            "ts_code": ts_code,
            "trade_date": g["trade_date"].to_numpy(),
            "matched": matched.to_numpy(),
        }))
    if not frames:
        return pd.DataFrame(columns=["ts_code", "trade_date", "matched"])
    return pd.concat(frames, ignore_index=True)
