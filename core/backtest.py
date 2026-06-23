"""每日分析与 5 交易日跟踪服务。

- :func:`run_analysis` ：对一条策略在某 snapshot_date 跑引擎，落 AnalysisRun + Matches（**只筛选**，不自动跟踪）。
- :func:`start_tracking` ：对勾选的 match 开启 opt-in 跟踪（T+0..T+5）。
- :func:`fill_and_recompute` ：补齐已到交易日的收盘价并重算涨跌（懒计算）。
- :func:`update_buy_price` ：录入买入价后重算。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from core import strategy_engine, strategy_repo, tushare_api
from core import timeutil
from core.db import get_session
from core.models import AnalysisRun, Match, Strategy, Stock, Tracking

TRACK_DAYS = 5  # 跟踪后 5 个交易日

# A 股板块（按 ts_code 归类）。科创板(688/689) 属上交所但单列；
# 深证含主板/中小板/创业板；北证即北交所(.BJ)。
BOARDS = ["上证", "深证", "北证", "科创板"]


def classify_board(ts_code: str) -> str:
    """根据 ts_code 归类板块：上证 / 深证 / 北证 / 科创板 / 其他。

    按 ts_code 后缀（交易所）+ 前缀（688/689 科创板）判定，不依赖 stock 缓存。
    """
    code, _, suffix = str(ts_code).partition(".")
    if suffix == "BJ":
        return "北证"
    if suffix == "SH":
        return "科创板" if code.startswith(("688", "689")) else "上证"
    if suffix == "SZ":
        return "深证"
    return "其他"


def _filter_bars_by_boards(bars: pd.DataFrame, boards: Optional[List[str]]) -> pd.DataFrame:
    """仅保留属于指定板块的股票的日 K。全选（或 None）则原样返回。"""
    if not boards:
        return bars
    if set(BOARDS).issubset(set(boards)):
        return bars  # 四个板块都勾选 = 不过滤
    keep = set(boards)
    codes = [c for c in bars["ts_code"].astype(str).unique() if classify_board(c) in keep]
    if not codes:
        return bars.iloc[0:0]
    return bars[bars["ts_code"].isin(codes)]


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


def run_analysis(
    strategy_id: int,
    snapshot_date: str,
    persist: bool = True,
    boards: Optional[List[str]] = None,
) -> AnalysisRun:
    """对一条策略在 snapshot_date 运行分析，返回 AnalysisRun（已持久化）。

    Args:
        boards: 仅分析这些板块（``BOARDS`` 的子集，如 ``["上证", "科创板"]``）。
            None 或全选表示不过滤（全市场）。过滤在引擎求值前完成，可显著提速。
    """
    dsl = strategy_repo.get_current_dsl(strategy_id)
    if dsl is None:
        raise ValueError("策略无可用 DSL 版本")

    today = timeutil.today_str()
    run = AnalysisRun(
        strategy_id=strategy_id,
        run_date=today,
        snapshot_date=snapshot_date,
        matched_count=0,
        status="success",
    )

    try:
        # 数据准备：所有 tushare 取数 / 写缓存必须在 analysis_runs 事务之外完成。
        # 否则 SQLite 在已持有写锁的事务内，再开新连接写 trade_cal / daily_bars
        # 会触发 "database is locked"（同线程自死锁）。
        lookback_dates = tushare_api.get_lookback_dates(snapshot_date, dsl.lookback)
        tushare_api.ensure_bars_for_dates(lookback_dates)
        bars = tushare_api.load_bars(lookback_dates)
        # 仅对勾选板块求值：减少引擎分组的股票数，提速。
        bars = _filter_bars_by_boards(bars, boards)
        matched_codes = strategy_engine.evaluate(dsl, bars, snapshot_date)
        run.matched_count = len(matched_codes)

        if persist:
            # 落库阶段：仅做 INSERT（run / matches）。跟踪(Tracking)改为按需 opt-in
            # （见 start_tracking）——运行分析不再对每只命中股自动建跟踪。
            with get_session() as s:
                s.add(run)
                s.flush()
                for code in matched_codes:
                    s.add(Match(
                        run_id=run.id,
                        ts_code=code,
                        strategy_id=strategy_id,
                        snapshot_date=snapshot_date,
                    ))
                s.commit()
                s.refresh(run)
    except Exception as e:  # noqa: BLE001
        # 记录失败：必须新建对象写入 failed 记录。切勿复用可能已被 flush 过
        # （带 id 但事务已回滚）的 run，否则会发 UPDATE 匹配 0 行 → StaleDataError，
        # 反而掩盖真实错误。
        run.status = "failed"
        run.message = str(e)
        if persist:
            try:
                with get_session() as s:
                    s.add(AnalysisRun(
                        strategy_id=strategy_id,
                        run_date=today,
                        snapshot_date=snapshot_date,
                        matched_count=run.matched_count,
                        status="failed",
                        message=str(e),
                    ))
                    s.commit()
            except Exception:  # noqa: BLE001
                pass  # 记录失败本身出错也不能掩盖原始错误
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


def start_tracking(run_id: int, match_ids: List[int]) -> int:
    """对勾选的 match 开启 opt-in 跟踪：建 T+0..T+5 跟踪行并尽量填已到交易日收盘。

    ``run_analysis`` 现仅筛选不自动跟踪；用户在界面勾选后调用本函数。
    幂等：已存在的 offset 由 ``_ensure_trackings`` 跳过。返回实际处理的 match 数。
    """
    if not match_ids:
        return 0
    with get_session() as s:
        rows = (
            s.query(Match)
            .filter(Match.id.in_(match_ids), Match.run_id == run_id)
            .all()
        )
        if not rows:
            return 0
        snap = rows[0].snapshot_date
        targets = [(r.id, r.ts_code, r.strategy_id) for r in rows]

    # 事务外抓取未来交易日 K（写 daily_bars 不能嵌套在下面写事务内，否则 database is locked）
    track_days = future_trade_days(snap)
    tushare_api.ensure_bars_for_dates(track_days)
    track_bars = tushare_api.load_bars(track_days)

    with get_session() as s:
        for mid, code, sid in targets:
            _ensure_trackings(s, mid, code, sid, snap, track_bars)
        s.commit()
    return len(targets)


def tracked_match_ids(run_id: int) -> set:
    """返回该 run 中已有 Tracking 行的 match_id 集合（供 UI 标注「已跟踪」）。"""
    with get_session() as s:
        rows = (
            s.query(Tracking.match_id)
            .join(Match, Match.id == Tracking.match_id)
            .filter(Match.run_id == run_id)
            .distinct()
            .all()
        )
    return {r[0] for r in rows}


def fill_and_recompute(match_id: Optional[int] = None) -> int:
    """补齐已到交易日的收盘价并重算涨跌。返回处理的 match 数。"""
    # 1) 读：取待处理的 match 与需要补收盘价的 (ts_code, trade_date)。
    with get_session() as s:
        if match_id is not None:
            match_ids = [match_id]
        else:
            match_ids = [r[0] for r in s.query(Tracking.match_id).distinct().all()]
        pending = (
            s.query(Tracking.ts_code, Tracking.trade_date)
            .filter(Tracking.close.is_(None))
            .all()
        )

    # 2) 事务外抓取并缓存日 K：ensure_bars_for_dates 会写 daily_bars，
    #    不能嵌套在下面的写事务内，否则同样 "database is locked"。
    closes = {}
    if pending:
        dates = sorted({d for _, d in pending})
        tushare_api.ensure_bars_for_dates(dates)
        bars = tushare_api.load_bars(dates)
        for ts_code, td in pending:
            closes[(ts_code, td)] = _snapshot_close(bars, ts_code, td)

    # 3) 写：回填 close 并重算涨跌。
    with get_session() as s:
        for (ts_code, td), c in closes.items():
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


def archive_summary() -> pd.DataFrame:
    """已跟踪股票归档摘要：每行对应一个 (buy_date, ts_code, strategy_id) 跟踪组。

    列含：购入日期 / 运行日期(分析运行日) / 归档时间(进入跟踪的时间) /
    代码 / 名称 / 策略 / 策略ID / 买入价 / T0收盘 / T+1..T+5累计%。
    展示**全部已跟踪**记录（买入价未填亦列出）。
    """
    with get_session() as s:
        # left join 到 Match→AnalysisRun 取「运行日期」；缺 run 也不丢跟踪行
        rows = (
            s.query(Tracking, AnalysisRun.run_date)
            .outerjoin(Match, Match.id == Tracking.match_id)
            .outerjoin(AnalysisRun, AnalysisRun.id == Match.run_id)
            .order_by(Tracking.buy_date.desc(), Tracking.ts_code, Tracking.offset)
            .all()
        )
        if not rows:
            return pd.DataFrame()
        # 批量解析名称 / 策略名，避免逐行查询
        codes = {t.ts_code for (t, _) in rows}
        sids = {t.strategy_id for (t, _) in rows if t.strategy_id is not None}
        name_by_code = {r.ts_code: r.name for r in s.query(Stock).filter(Stock.ts_code.in_(codes)).all()}
        name_by_sid = {r.id: r.name for r in s.query(Strategy).filter(Strategy.id.in_(sids)).all()}

    records = {}
    for t, run_date in rows:
        key = (t.buy_date, t.ts_code, t.strategy_id)
        rec = records.setdefault(
            key,
            {
                "购入日期": t.buy_date,
                "运行日期": run_date or "",
                "归档时间": t.created_at,
                "代码": t.ts_code,
                "名称": name_by_code.get(t.ts_code, ""),
                "策略": name_by_sid.get(t.strategy_id, str(t.strategy_id)) if t.strategy_id is not None else "",
                "策略ID": t.strategy_id,
                "买入价": t.buy_price,
                "T0收盘": None,
                **{f"T+{i}累计%": None for i in range(1, TRACK_DAYS + 1)},
            },
        )
        # 运行日期 / 归档时间 取组内最新（同一记录可能跨多次运行/补算）
        if run_date and (not rec["运行日期"] or run_date > rec["运行日期"]):
            rec["运行日期"] = run_date
        if t.created_at and (not rec["归档时间"] or t.created_at > rec["归档时间"]):
            rec["归档时间"] = t.created_at
        if t.offset == 0:
            rec["T0收盘"] = t.close
        else:
            rec[f"T+{t.offset}累计%"] = t.cum_pct
    return pd.DataFrame(list(records.values()))


def delete_tracking_groups(keys: List[tuple]) -> int:
    """批量删除归档记录：``keys`` 为 [(buy_date, ts_code, strategy_id), ...]。

    每个三元组定位一组 T+0..T+5 跟踪行（与 :func:`archive_summary` 的分组键一致），
    删除其全部跟踪数据。返回实际删除的跟踪行数。Matches（筛选命中）保留不动。
    """
    if not keys:
        return 0
    total = 0
    with get_session() as s:
        for buy_date, ts_code, strategy_id in keys:
            q = s.query(Tracking).filter(
                Tracking.buy_date == buy_date,
                Tracking.ts_code == ts_code,
            )
            if strategy_id is None:
                q = q.filter(Tracking.strategy_id.is_(None))
            else:
                q = q.filter(Tracking.strategy_id == strategy_id)
            total += q.delete(synchronize_session=False)
        s.commit()
    return total
