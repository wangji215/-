"""策略引擎自测（不依赖 tushare/网络）。直接运行：python tests/test_engine.py

用足够长的合成序列，并对部分用例做**独立验算**（不经过引擎），确保引擎接线正确。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from core.strategy_dsl import StrategyDSL  # noqa: E402
from core.strategy_engine import evaluate, evaluate_history  # noqa: E402


def _make_bars(closes_by_code: dict, dates: list[str]) -> pd.DataFrame:
    rows = []
    for code, closes in closes_by_code.items():
        prev = closes[0]
        for d, c in zip(dates, closes):
            o = prev
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": o, "high": max(o, c) * 1.01, "low": min(o, c) * 0.99,
                "close": c, "vol": 1000.0 + abs(c - o) * 1000, "amount": c * 1000, "pct_chg": 0.0,
            })
            prev = c
    return pd.DataFrame(rows)


def _dates(n: int, start: str = "20240101") -> list[str]:
    from datetime import datetime, timedelta

    d0 = datetime.strptime(start, "%Y%m%d")
    return [(d0 + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def test_golden_cross():
    """MA5 上穿 MA10：独立算出交叉日，断言引擎在交叉日命中、非交叉日不命中。"""
    n = 40
    dates = _dates(n)
    # 先下跌后明显上涨，制造一次金叉
    closes = [12 - i * 0.2 for i in range(20)] + [8 + (i - 20) ** 1.4 * 0.6 for i in range(20, n)]
    bars = _make_bars({"000001.SZ": closes}, dates)

    dsl = StrategyDSL.model_validate({
        "name": "MA5金叉MA10",
        "lookback": n,
        "conditions": [{
            "logic": "and",
            "rules": [{
                "left": {"ind": "MA", "period": 5}, "op": "cross_up",
                "right": {"ind": "MA", "period": 10},
            }],
        }],
    })

    # 独立验算交叉日
    s = pd.Series(closes)
    ma5, ma10 = s.rolling(5).mean(), s.rolling(10).mean()
    expected_cross = set()
    for i in range(1, n):
        if pd.notna(ma5.iloc[i]) and pd.notna(ma10.iloc[i]) and pd.notna(ma5.iloc[i - 1]) and pd.notna(ma10.iloc[i - 1]):
            if ma5.iloc[i] > ma10.iloc[i] and ma5.iloc[i - 1] <= ma10.iloc[i - 1]:
                expected_cross.add(dates[i])
    assert expected_cross, "测试数据未产生金叉，需调整序列"

    hist = evaluate_history(dsl, bars)
    matched_days = set(hist[hist["matched"]]["trade_date"])
    assert matched_days == expected_cross, f"金叉日不一致：引擎={matched_days} 预期={expected_cross}"

    # 抽一个交叉日做单点评估
    day = next(iter(expected_cross))
    assert "000001.SZ" in evaluate(dsl, bars, day)
    # 非交叉日不应命中
    safe_day = dates[0]
    assert "000001.SZ" not in evaluate(dsl, bars, safe_day)
    print(f"[ok] test_golden_cross（交叉日 {sorted(expected_cross)}）")


def test_multi_head_alignment():
    """MA5>MA10>MA20 多头排列（单调上涨，足够长度）。"""
    n = 30
    dates = _dates(n)
    closes = [10 + i * 0.5 for i in range(n)]
    bars = _make_bars({"000002.SZ": closes}, dates)
    dsl = StrategyDSL.model_validate({
        "name": "均线多头排列",
        "lookback": n,
        "conditions": [{
            "logic": "and",
            "rules": [
                {"left": {"ind": "MA", "period": 5}, "op": ">", "right": {"ind": "MA", "period": 10}},
                {"left": {"ind": "MA", "period": 10}, "op": ">", "right": {"ind": "MA", "period": 20}},
            ],
        }],
    })
    assert "000002.SZ" in evaluate(dsl, bars, dates[-1])
    # 早期（MA20 未定义）不应命中
    assert "000002.SZ" not in evaluate(dsl, bars, dates[5])
    print("[ok] test_multi_head_alignment")


def test_volume_multiplier():
    """成交量 > 1.5 × 5日均量。"""
    n = 10
    dates = _dates(n)
    closes = [10.0] * n
    bars = _make_bars({"000003.SZ": closes}, dates)
    bars.loc[bars.index[-1], "vol"] = 100000.0  # 最后一日放量
    dsl = StrategyDSL.model_validate({
        "name": "放量",
        "lookback": n,
        "conditions": [{
            "logic": "and",
            "rules": [{
                "left": {"ind": "VOL"}, "op": ">",
                "right": {"ind": "MA_VOL", "period": 5}, "multiplier": 1.5,
            }],
        }],
    })
    assert "000003.SZ" in evaluate(dsl, bars, dates[-1])
    print("[ok] test_volume_multiplier")


def test_pattern_consecutive_up():
    """N 连阳形态（PATTERN consecutive_up）。"""
    n = 12
    dates = _dates(n)
    # 让 open<close 连续 3 天（构造上涨 K），最后一组为 3 连阳
    closes = [10.0] * n
    bars = _make_bars({"000004.SZ": closes}, dates)
    # 手动设最后 3 天为阳线（open 低于 close）
    for i in [-3, -2, -1]:
        bars.loc[bars.index[i], "open"] = bars.loc[bars.index[i], "close"] - 0.5
    dsl = StrategyDSL.model_validate({
        "name": "3连阳",
        "lookback": n,
        "conditions": [{
            "logic": "and",
            "rules": [{
                "left": {"ind": "PATTERN", "field": "consecutive_up", "period": 3},
                "op": "is_true",
            }],
        }],
    })
    assert "000004.SZ" in evaluate(dsl, bars, dates[-1])
    print("[ok] test_pattern_consecutive_up")


def test_not_matched_when_flat():
    """横盘（足够长度）无金叉不应命中。"""
    n = 30
    dates = _dates(n)
    closes = [10.0] * n
    bars = _make_bars({"000005.SZ": closes}, dates)
    dsl = StrategyDSL.model_validate({
        "name": "MA5金叉MA10",
        "lookback": n,
        "conditions": [{
            "logic": "and",
            "rules": [{
                "left": {"ind": "MA", "period": 5}, "op": "cross_up",
                "right": {"ind": "MA", "period": 10},
            }],
        }],
    })
    assert "000005.SZ" not in evaluate(dsl, bars, dates[-1])
    print("[ok] test_not_matched_when_flat")


if __name__ == "__main__":
    test_golden_cross()
    test_multi_head_alignment()
    test_volume_multiplier()
    test_pattern_consecutive_up()
    test_not_matched_when_flat()
    print("\n全部引擎自测通过 ✅")
