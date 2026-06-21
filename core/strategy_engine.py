"""策略引擎：把 DSL 在日 K 数据上确定性求值，输出 snapshot 当日命中的 ts_code 列表。

采用「按 ts_code 分组、向量化求值」的方式，每只股票在回看窗口内的指标一次性算出，
最后取 snapshot_date 当日布尔结果。约 6000 只股票可在秒级完成。
"""
from __future__ import annotations

from typing import List

import pandas as pd

from core import indicators as ind
from core.strategy_dsl import Indicator, Rule, StrategyDSL

_BAR_COLS = ["open", "high", "low", "close", "vol", "amount", "pct_chg"]


def _resolve_indicator(df: pd.DataFrame, spec: Indicator) -> pd.Series:
    """返回一只股票某指标的 Series（布尔形态也返回布尔 Series）。"""
    name = spec.ind.upper()
    if name == "CLOSE":
        return df["close"]
    if name == "OPEN":
        return df["open"]
    if name == "HIGH":
        return df["high"]
    if name == "LOW":
        return df["low"]
    if name == "VOL":
        return df["vol"]
    if name == "AMOUNT":
        return df["amount"]
    if name == "PCT_CHG":
        return df["pct_chg"]
    if name == "MA":
        return ind.MA(df, spec.period or 5)
    if name == "EMA":
        return ind.EMA(df, spec.period or 5)
    if name == "MA_VOL":
        return ind.MA_VOL(df, spec.period or 5)
    if name == "RSI":
        return ind.RSI(df, spec.period or 14)
    if name == "MACD":
        dif, dea, hist = ind.MACD(df)
        return {"dif": dif, "dea": dea, "hist": hist}[(spec.field or "dif").lower()]
    if name == "KDJ":
        k, d, j = ind.KDJ(df)
        return {"k": k, "d": d, "j": j}[(spec.field or "k").lower()]
    if name == "BOLL":
        up, mid, low = ind.BOLL(df, spec.period or 20)
        return {"upper": up, "mid": mid, "lower": low}[(spec.field or "mid").lower()]
    if name == "PATTERN":
        return _resolve_pattern(df, spec)
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
    """返回每个 (ts_code, trade_date) 是否命中（用于回测/可视化），列：ts_code,trade_date,matched。"""
    out_rows = []
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
        for idx, row in g.iterrows():
            try:
                hit = bool(combined.loc[idx])
            except KeyError:
                hit = False
            out_rows.append({"ts_code": ts_code, "trade_date": row["trade_date"], "matched": hit})
    return pd.DataFrame(out_rows)
