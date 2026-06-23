"""tushare pro 接入与日 K 缓存。

设计要点：
- 股票列表缓存到 ``stocks`` 表。
- 日 K 按 ``trade_date`` 全市场抓取（``pro.daily(trade_date=...)`` 一次返回当日全部股票），
  缓存到 ``daily_bars``，命中则不重复抓，避免按票逐个调用。
- 令牌桶限流，超限自动等待。
- 交易日历缓存到 ``trade_cal``，用于回看窗口与 T+1..T+5 偏移计算。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import List

import pandas as pd
import tushare as ts
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.config import apply_proxies, get_setting
from core.db import get_session
from core.models import DailyBar, Stock, TradeCal

_pro = None
_pro_token = None
_last_call = 0.0


class TushareError(RuntimeError):
    pass


def _client():
    global _pro, _pro_token
    token = get_setting("tushare_token")
    if not token:
        raise TushareError("未配置 tushare token，请到「环境设置」页填写。")
    apply_proxies()
    if _pro is None or token != _pro_token:
        ts.set_token(token)
        _pro = ts.pro_api()
        _pro_token = token
    return _pro


def _rate_limit() -> None:
    """按每分钟最大调用数做最简限流（相邻调用最小间隔）。"""
    try:
        rpm = int(get_setting("tushare_rate_per_min", "200"))
    except (TypeError, ValueError):
        rpm = 200
    if rpm <= 0:
        return
    min_interval = 60.0 / rpm
    global _last_call
    wait = min_interval - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def test_connection() -> dict:
    """连通性测试：拉一次 stock_basic 计数。返回 {ok, count, error}。"""
    try:
        pro = _client()
        _rate_limit()
        df = pro.stock_basic(exchange="", list_status="L", fields="ts_code")
        return {"ok": True, "count": int(len(df)), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "error": str(e)}


def refresh_stocks() -> int:
    """拉取并缓存全市场在交易股票基础信息，返回条数。"""
    pro = _client()
    _rate_limit()
    df = pro.stock_basic(
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name,area,industry,market,list_date",
    )
    if df is None or df.empty:
        return 0
    with get_session() as s:
        for _, r in df.iterrows():
            s.merge(
                Stock(
                    ts_code=r["ts_code"],
                    name=r.get("name", ""),
                    industry=r.get("industry", ""),
                    market=r.get("market", ""),
                    list_date=str(r.get("list_date", "")),
                )
            )
        s.commit()
    return len(df)


def fetch_trade_cal(start: str, end: str) -> int:
    """抓取交易日历 [start, end]（YYYYMMDD），返回抓取天数。"""
    pro = _client()
    _rate_limit()
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    if df is None or df.empty:
        return 0
    with get_session() as s:
        for _, r in df.iterrows():
            s.merge(TradeCal(cal_date=str(r["cal_date"]), is_open=bool(r["is_open"])))
        s.commit()
    return len(df)


def trading_days_between(start: str, end: str, refresh: bool = True) -> List[str]:
    """返回 [start, end] 内的开放交易日，升序。需要时先抓交易日历。"""
    with get_session() as s:
        rows = (
            s.query(TradeCal)
            .filter(TradeCal.cal_date >= start, TradeCal.cal_date <= end, TradeCal.is_open.is_(True))
            .all()
        )
    if refresh and not rows:
        fetch_trade_cal(start, end)
        with get_session() as s:
            rows = (
                s.query(TradeCal)
                .filter(
                    TradeCal.cal_date >= start,
                    TradeCal.cal_date <= end,
                    TradeCal.is_open.is_(True),
                )
                .all()
            )
    return sorted(r.cal_date for r in rows)


def _persist_bars(df: pd.DataFrame) -> int:
    """upsert 日 K 到缓存，返回写入行数。"""
    if df is None or df.empty:
        return 0
    cols = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
    rows = [{c: r.get(c) for c in cols} for _, r in df.iterrows()]
    # SQLite 单条语句变量数默认上限 999；9 列 × 每批 100 行 = 900，留出余量，
    # 否则整市场单日约 5000+ 只股票会触发 too many SQL variables。
    BATCH = 100
    total = len(rows)
    with get_session() as s:
        for i in range(0, total, BATCH):
            batch = rows[i:i + BATCH]
            stmt = sqlite_insert(DailyBar.__table__).values(batch)
            update_cols = {c: stmt.excluded[c] for c in cols if c not in ("ts_code", "trade_date")}
            stmt = stmt.on_conflict_do_update(index_elements=["ts_code", "trade_date"], set_=update_cols)
            s.execute(stmt)
        s.commit()
    return total


def _cached_trade_dates() -> set:
    with get_session() as s:
        return {r[0] for r in s.query(DailyBar.trade_date).distinct().all()}


def ensure_bars_for_dates(trade_dates: List[str]) -> int:
    """确保给定交易日（YYYYMMDD）的日 K 已缓存，返回新抓取的天数。"""
    trade_dates = [d for d in trade_dates if d]
    if not trade_dates:
        return 0
    cached = _cached_trade_dates()
    missing = [d for d in trade_dates if d not in cached]
    if not missing:
        return 0
    pro = _client()
    fetched = 0
    for d in missing:
        _rate_limit()
        df = pro.daily(trade_date=d)
        fetched += _persist_bars(df)
    # 抓取后即使某日无数据（停市），也以调用次数为准返回
    return len(missing)


def get_lookback_dates(snapshot_date: str, lookback: int) -> List[str]:
    """返回截至 snapshot_date 的最近 lookback 个交易日（含），不足则尽量返回。"""
    snap = datetime.strptime(snapshot_date, "%Y%m%d")
    # 用更宽的自然日区间确保覆盖足够交易日
    start = (snap - timedelta(days=lookback * 2 + 40)).strftime("%Y%m%d")
    days = trading_days_between(start, snapshot_date)
    days = [d for d in days if d <= snapshot_date]
    return days[-lookback:]


def load_bars(trade_dates: List[str]) -> pd.DataFrame:
    """从缓存加载指定交易日的日 K，返回 DataFrame。"""
    if not trade_dates:
        return pd.DataFrame(
            columns=["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
        )
    with get_session() as s:
        rows = s.query(DailyBar).filter(DailyBar.trade_date.in_(trade_dates)).all()
    if not rows:
        return pd.DataFrame(
            columns=["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"]
        )
    return pd.DataFrame(
        [
            {
                "ts_code": r.ts_code,
                "trade_date": r.trade_date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "vol": r.vol,
                "amount": r.amount,
                "pct_chg": r.pct_chg,
            }
            for r in rows
        ]
    )


def latest_cached_trade_date(on_or_before: str) -> str | None:
    """返回缓存里 <= on_or_before 的最近一个有数据的交易日，无则 None。"""
    with get_session() as s:
        row = (
            s.query(DailyBar.trade_date)
            .filter(DailyBar.trade_date <= on_or_before)
            .order_by(DailyBar.trade_date.desc())
            .first()
        )
    return row[0] if row else None


def list_stocks() -> pd.DataFrame:
    with get_session() as s:
        rows = s.query(Stock).all()
    return pd.DataFrame(
        [{"ts_code": r.ts_code, "name": r.name, "industry": r.industry, "market": r.market} for r in rows]
    )
