"""技术指标实现（pandas 向量化）。

所有函数接受一只股票按交易日升序排列的 DataFrame（含 open/high/low/close/vol/amount），
返回与索引对齐的 Series 或 (Series, ...) 元组。
"""
from __future__ import annotations

import pandas as pd


def MA(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].rolling(period).mean()


def EMA(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].ewm(span=period, adjust=False).mean()


def MA_VOL(df: pd.DataFrame, period: int) -> pd.Series:
    return df["vol"].rolling(period).mean()


def MACD(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def KDJ(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    denom = high_n - low_n
    denom = denom.replace(0, pd.NA)
    rsv = (df["close"] - low_n) / denom * 100
    rsv = rsv.fillna(50.0)
    k = rsv.ewm(alpha=1.0 / m1, adjust=False).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def RSI(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return (100 - (100 / (1 + rs))).fillna(50.0)


def BOLL(df: pd.DataFrame, period: int = 20, std: float = 2.0):
    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std()
    upper = mid + std * sd
    lower = mid - std * sd
    return upper, mid, lower


# ---- K 线形态（返回布尔 Series）----

def _body(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def _range(df: pd.DataFrame) -> pd.Series:
    return (df["high"] - df["low"]).replace(0, pd.NA)


def doji(df: pd.DataFrame) -> pd.Series:
    """十字星：实体 <= 振幅的 10%。"""
    return _body(df) <= 0.1 * _range(df)


def hammer(df: pd.DataFrame) -> pd.Series:
    """锤子线：下影线 >= 2 倍实体，上影线 <= 实体。"""
    body = _body(df)
    lower = df[["open", "close"]].min(axis=1) - df["low"]
    upper = df["high"] - df[["open", "close"]].max(axis=1)
    return (lower >= 2 * body) & (upper <= body)


def engulfing_bull(df: pd.DataFrame) -> pd.Series:
    """阳包阴：前阴今阳且实体吞没。"""
    prev_down = df["close"].shift(1) < df["open"].shift(1)
    cur_up = df["close"] > df["open"]
    engulf = (df["close"] > df["open"].shift(1)) & (df["open"] < df["close"].shift(1))
    return prev_down & cur_up & engulf


def engulfing_bear(df: pd.DataFrame) -> pd.Series:
    """阴包阳：前阳今阴且实体吞没。"""
    prev_up = df["close"].shift(1) > df["open"].shift(1)
    cur_down = df["close"] < df["open"]
    engulf = (df["close"] < df["open"].shift(1)) & (df["open"] > df["close"].shift(1))
    return prev_up & cur_down & engulf


def consecutive_up(df: pd.DataFrame, n: int) -> pd.Series:
    """N 连阳（含平：close>open）。"""
    up = (df["close"] > df["open"]).astype(float)
    return up.rolling(n).sum() == n


def consecutive_down(df: pd.DataFrame, n: int) -> pd.Series:
    """N 连阴。"""
    down = (df["close"] < df["open"]).astype(float)
    return down.rolling(n).sum() == n
