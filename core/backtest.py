"""每日分析与 5 交易日跟踪服务。

- :func:`run_analysis` ：对一条策略在某 snapshot_date 跑引擎，落 AnalysisRun + Matches + Tracking(T+0..T+5)。
- :func:`fill_and_recompute` ：补齐已到交易日的收盘价并重算涨跌（懒计算）。
- :func:`update_buy_price` ：录入买入价后重算。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from core import strategy_engine, strategy_repo, tushare_api
from core.db import get_session
from core.models import AnalysisRun, Match, Tracking

TRACK_DAYS = 5  # 跟踪后 5 个交易日


def _snapshot_close(bars: pd.DataFrame, ts_code: str, trade_date: str) -> Optional[float]:
    sub = bars[(bars["ts_code"] == ts_code) & (bars["trade_date"].astype(str) == trade_date)]
    if sub.empty:
        return None
    return float(sub["close"].iloc[0])


def future_trade_days(snapshot_date: str, n: int = TRACK_DAYS) -> List[str]:
    """snapshot 当日 + 后 n 个交易日，共 n+1 个。未来未到的日期也会列出（懒计算时补）。"""
    snap = datetime.strptime(snapshot_date, "%Y%m%d")
    end = (snap + timedelta(days=n * 2 + 15)).strftime("%Y%m%d")
    days = tushare_api.trading_days_between(snapshot_date, end)
    days = [d for d in days if d >= snapshot_date]
    return days[: n + 1]


def run_analysis(strategy_id: int, snapshot_date: str, persist: bool = True) -> AnalysisRun:
    """对一条策略在 snapshot_date 运行分析，返回 AnalysisRun（已持久化）。"""
    dsl = strategy_repo.get_current_dsl(strategy_id)
    if dsl is None:
        raise ValueError("策略无可用 DSL 版本")

    today = datetime.now().strftime("%Y%m%d")
    run = AnalysisRun(
        strategy_id=strategy_id,
        run_date=today,
        snapshot_date=snapshot_date,
        matched_count=0,
        status="success",
    )

    try:
        lookback_dates = tushare_api.get_lookback_dates(snapshot_date, dsl.lookback)
        tushare_api.ensure_bars_for_dates(lookback_dates)
        bars = tushare_api.load_bars(lookback_dates)
        matched_codes = strategy_engine.evaluate(dsl, bars, snapshot_date)
        run.matched_count = len(matched_codes)

        if persist:
            with get_session() as s:
                s.add(run)
                s.flush()
                # 预取后续交易日，保证 T+0..T+5 收盘可填
                track_days = future_trade_days(snapshot_date)
                tushare_api.ensure_bars_for_dates(track_days)
                track_bars = tushare_api.load_bars(track_days)
                for code in matched_codes:
                    m = Match(
                        run_id=run.id,
                        ts_code=code,
                        strategy_id=strategy_id,
                        snapshot_date=snapshot_date,
                    )
                    s.add(m)
                    s.flush()
                    _ensure_trackings(s, m.id, code, strategy_id, snapshot_date, track_bars)
                s.commit()
                s.refresh(run)
    except Exception as e:  # noqa: BLE001
        run.status = "failed"
        run.message = str(e)
        if persist:
            with get_session() as s:
                s.add(run)
                s.commit()
                s.refresh(run)
        raise

    return run


def _ensure_trackings(s: Session, match_id: int, ts_code: str, strategy_id: int,
                      snapshot_date: str, track_bars: pd.DataFrame) -> None:
    """为某 match 创建 T+0..T+5 跟踪行（已存在则跳过），并尽量填入已到交易日的收盘。"""
    existing = {r.offset for r in s.query(Tracking).filter_by(match_id=match_id).all()}
    days = future_trade_days(snapshot_date, TRACK_DAYS)
    for offset, td in enumerate(days):
        if offset in existing:
            continue
        close = _snapshot_close(track_bars, ts_code, td)
        s.add(Tracking(
            match_id=match_id, ts_code=ts_code, strategy_id=strategy_id,
            buy_price=None, buy_date=snapshot_date, offset=offset,
            trade_date=td, close=close, day_pct=None, cum_pct=None,
        ))


def fill_and_recompute(match_id: Optional[int] = None) -> int:
    """补齐已到交易日的收盘价并重算涨跌。返回处理的 match 数。"""
    with get_session() as s:
        q = s.query(Tracking)
        if match_id is not None:
            match_ids = [match_id]
        else:
            match_ids = [r[0] for r in s.query(Tracking.match_id).distinct().all()]
        # 需要补数据的 (ts_code, trade_date)
        pending = (
            s.query(Tracking.ts_code, Tracking.trade_date)
            .filter(Tracking.close.is_(None))
            .all()
        )
        if pending:
            dates = sorted({d for _, d in pending})
            tushare_api.ensure_bars_for_dates(dates)
            bars = tushare_api.load_bars(dates)
            for ts_code, td in pending:
                c = _snapshot_close(bars, ts_code, td)
                if c is not None:
                    s.query(Tracking).filter(
                        Tracking.ts_code == ts_code, Tracking.trade_date == td, Tracking.close.is_(None)
                    ).update({Tracking.close: c}, synchronize_session=False)
            s.flush()
        for mid in match_ids:
            _recompute_match(s, mid)
        s.commit()
    return len(match_ids)


def _recompute_match(s: Session, match_id: int) -> None:
    rows = s.query(Tracking).filter_by(match_id=match_id).order_by(Tracking.offset.asc()).all()
    if not rows:
        return
    buy = rows[0].buy_price
    closes = {r.offset: r.close for r in rows}
    for r in rows:
        if r.close is None:
            continue
        if buy:
            r.cum_pct = round((r.close - buy) / buy * 100, 2)
        if r.offset == 0:
            r.day_pct = round((r.close - buy) / buy * 100, 2) if buy else None
        else:
            prev = closes.get(r.offset - 1)
            if prev:
                r.day_pct = round((r.close - prev) / prev * 100, 2)


def update_buy_price(match_id: int, buy_price: float) -> None:
    """录入买入价，更新该 match 全部跟踪行并重算。"""
    with get_session() as s:
        rows = s.query(Tracking).filter_by(match_id=match_id).all()
        if not rows:
            return
        for r in rows:
            r.buy_price = buy_price
        _recompute_match(s, match_id)
        s.commit()


def matches_for_run(run_id: int) -> List[Match]:
    with get_session() as s:
        rows = s.query(Match).filter_by(run_id=run_id).all()
        for r in rows:
            _ = r.ts_code
        return rows


def tracking_df(match_id: int) -> pd.DataFrame:
    with get_session() as s:
        rows = s.query(Tracking).filter_by(match_id=match_id).order_by(Tracking.offset.asc()).all()
    return pd.DataFrame([
        {
            "offset": r.offset, "trade_date": r.trade_date, "close": r.close,
            "day_pct": r.day_pct, "cum_pct": r.cum_pct, "buy_price": r.buy_price,
        }
        for r in rows
    ])


def archived_by_buy_date() -> pd.DataFrame:
    """按买入日期归档：返回每个 (buy_date, ts_code, strategy_id) 的跟踪摘要。

    每行含 buy_date, ts_code, buy_price, 各 offset 的 cum_pct。
    """
    with get_session() as s:
        rows = (
            s.query(Tracking)
            .filter(Tracking.buy_price.is_not(None))
            .order_by(Tracking.buy_date.desc(), Tracking.ts_code, Tracking.offset)
            .all()
        )
    if not rows:
        return pd.DataFrame()
    records = {}
    for r in rows:
        key = (r.buy_date, r.ts_code, r.strategy_id)
        rec = records.setdefault(
            key,
            {"buy_date": r.buy_date, "ts_code": r.ts_code, "strategy_id": r.strategy_id,
             "buy_price": r.buy_price, "T0_close": None,
             **{f"T+{i}_cum%": None for i in range(1, TRACK_DAYS + 1)}},
        )
        if r.offset == 0:
            rec["T0_close"] = r.close
        else:
            rec[f"T+{r.offset}_cum%"] = r.cum_pct
    return pd.DataFrame(list(records.values()))
