"""tushare pro 接入与日 K 缓存。

设计要点：
- 股票列表缓存到 ``stocks`` 表。
- 日 K 按 ``trade_date`` 全市场抓取（``pro.daily(trade_date=...)`` 一次返回当日全部股票），
  缓存到 ``daily_bars``（**后复权 hfq**：价格 × ``pro.adj_factor``，pct_chg 按后复权 close 重算；
  vol/amount 保留真实成交），命中则不重复抓，避免按票逐个调用。
- 令牌桶限流，超限自动等待。
- 交易日历缓存到 ``trade_cal``，用于回看窗口与 T+1..T+5 偏移计算。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import List

import pandas as pd
import tushare as ts
from requests.exceptions import RequestException
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.config import apply_proxies, get_setting
from core.db import get_session
from core.models import DailyBar, IndexWeight, Stock, TradeCal

_pro = None
_pro_token = None
_pro_timeout = None
_last_call = 0.0


class TushareError(RuntimeError):
    pass


def _timeout_setting() -> int:
    """单次请求超时（秒），从配置取，兜底 120。"""
    try:
        return max(10, int(get_setting("tushare_timeout", "120")))
    except (TypeError, ValueError):
        return 120


def _client():
    global _pro, _pro_token, _pro_timeout
    token = get_setting("tushare_token")
    if not token:
        raise TushareError("未配置 tushare token，请到「环境设置」页填写。")
    apply_proxies()
    to = _timeout_setting()
    if _pro is None or token != _pro_token or to != _pro_timeout:
        ts.set_token(token)
        _pro = ts.pro_api(timeout=to)
        _pro_token = token
        _pro_timeout = to
    return _pro


def _call_with_retry(fn, *args, tries: int = 3, **kwargs):
    """带重试地调用 tushare 接口：全市场查询偶发 read timeout / 连接错误，重试通常即可通过。"""
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except RequestException as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))  # 2s、4s 退避
    raise last


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
    cols = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg", "adj_factor"]
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


def _prev_trade_date(d: str) -> str | None:
    """返回 trade_cal 中 < d 的最大开市日（YYYYMMDD），无则 None。"""
    with get_session() as s:
        row = (
            s.query(TradeCal.cal_date)
            .filter(TradeCal.cal_date < d, TradeCal.is_open.is_(True))
            .order_by(TradeCal.cal_date.desc())
            .first()
        )
    return row[0] if row else None


def _recompute_pct_chg(df: pd.DataFrame, d: str) -> pd.Series:
    """用上一交易日缓存的 hfq close 重算当日 pct_chg（%）。无上一交易日的票返回 NaN。

    df 的 close 此时已是当日 hfq close；prev_close 取自缓存（同为 hfq），比值即真实日涨跌。
    """
    d_prev = _prev_trade_date(d)
    nan = pd.Series([float("nan")] * len(df), index=df.index)
    if d_prev is None:
        return nan
    with get_session() as s:
        prev_rows = s.query(DailyBar.ts_code, DailyBar.close).filter(DailyBar.trade_date == d_prev).all()
    if not prev_rows:
        return nan
    prev_close = pd.DataFrame(prev_rows, columns=["ts_code", "close"]).rename(columns={"close": "prev_close"})
    merged = df[["ts_code", "close"]].merge(prev_close, on="ts_code", how="left")
    return (merged["close"].astype(float) / merged["prev_close"] - 1.0) * 100.0


def ensure_bars_for_dates(trade_dates: List[str]) -> int:
    """确保给定交易日（YYYYMMDD）的日 K 已缓存（后复权 hfq），返回新抓取的天数。

    每个交易日：``pro.daily``（不复权 OHLC，整市场）× ``pro.adj_factor``（整市场复权因子）→
    价格后复权化（vol/amount 不动）；pct_chg 按当日 hfq close / 上一交易日 hfq close 重算，
    使除权日不产生跳空。missing 按日期升序处理，保证算 pct_chg 时上一交易日已缓存。
    """
    trade_dates = [d for d in trade_dates if d]
    if not trade_dates:
        return 0
    cached = _cached_trade_dates()
    missing = sorted(d for d in trade_dates if d not in cached)
    if not missing:
        return 0
    pro = _client()
    for d in missing:
        _rate_limit()
        df = _call_with_retry(pro.daily, trade_date=d)
        if df is None or df.empty:
            continue
        # 整市场复权因子；缺失填 1.0（极个别无因子的票按不复权处理）
        _rate_limit()
        af = _call_with_retry(pro.adj_factor, trade_date=d)
        if af is not None and not af.empty:
            df = df.merge(af[["ts_code", "adj_factor"]], on="ts_code", how="left")
        else:
            df["adj_factor"] = 1.0
        df["adj_factor"] = df["adj_factor"].fillna(1.0)
        # 后复权化价格（量额保留真实成交，不动）
        for col in ("open", "high", "low", "close"):
            df[col] = (df[col].astype(float) * df["adj_factor"]).round(4)
        # 重算 pct_chg 与 hfq close 一致
        df["pct_chg"] = _recompute_pct_chg(df, d)
        _persist_bars(df)
    # 抓取后即使某日无数据（停市），也以调用次数为准返回
    return len(missing)


def clear_daily_bars() -> int:
    """清空 daily_bars 缓存（保留表结构），返回删除行数。

    切换复权方式后必须先清空再重拉，否则新旧数据（不复权 / 后复权）混在同一张表会得到错误结果。
    """
    with get_session() as s:
        n = s.query(DailyBar).count()
        s.query(DailyBar).delete()
        s.commit()
    return n


def fetch_index_daily(ts_code: str, start_date: str, end_date: str) -> int:
    """抓取单只指数日线（YYYYMMDD 闭区间），写入 ``daily_bars`` 复用同一张表。

    tushare ``pro.index_daily`` 必须按 ts_code 单只拉（不能像 ``pro.daily`` 那样按 trade_date 全市场）。
    返回写入行数。字段与 ``pro.daily`` 同名同义，``_persist_bars`` 直接复用。
    """
    pro = _client()
    _rate_limit()
    df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    return _persist_bars(df)


def _cached_index_range(ts_code: str) -> tuple[str | None, str | None]:
    """返回某 ts_code（含指数）已缓存的 (min_date, max_date)；无则 (None, None)。"""
    with get_session() as s:
        rows = (
            s.query(DailyBar.trade_date)
            .filter(DailyBar.ts_code == ts_code)
            .order_by(DailyBar.trade_date.asc())
            .all()
        )
    if not rows:
        return None, None
    return rows[0][0], rows[-1][0]


def ensure_index_bars(ts_code: str, start_date: str, end_date: str) -> int:
    """确保 [start_date, end_date] 内的指数日线已缓存，返回新写入行数。

    只补缺口：若已缓存区间完全覆盖请求区间则直接返回 0；否则按缺口段拉取。
    """
    lo, hi = _cached_index_range(ts_code)
    if lo is not None and hi is not None and lo <= start_date and hi >= end_date:
        return 0
    # 简化：只要不完全覆盖就重拉整段（指数单只单段，开销可接受）
    return fetch_index_daily(ts_code, start_date, end_date)


def fetch_index_weights(index_code: str, start_date: str, end_date: str) -> int:
    """抓取指数成分股权重快照（tushare pro.index_weight），返回写入行数。

    ``pro.index_weight`` 按 (index_code, start/end_date) 返回每日成分股 + 权重。
    复合主键 (index_code, trade_date, ts_code) 自动幂等 upsert。
    """
    pro = _client()
    _rate_limit()
    df = pro.index_weight(index_code=index_code, start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return 0
    # tushare index_weight 返回列：index_code, con_code, trade_date, weight
    # con_code 即成分股 ts_code，统一写为 ts_code 入库
    df = df.rename(columns={"con_code": "ts_code"})
    cols = ["index_code", "trade_date", "ts_code", "weight"]
    rows = [{c: r.get(c) for c in cols} for _, r in df.iterrows()]
    BATCH = 100
    total = len(rows)
    with get_session() as s:
        for i in range(0, total, BATCH):
            batch = rows[i:i + BATCH]
            stmt = sqlite_insert(IndexWeight.__table__).values(batch)
            update_cols = {c: stmt.excluded[c] for c in cols if c not in ("index_code", "trade_date", "ts_code")}
            stmt = stmt.on_conflict_do_update(index_elements=["index_code", "trade_date", "ts_code"], set_=update_cols)
            s.execute(stmt)
        s.commit()
    return total


def _cached_weight_snapshot_range(index_code: str) -> tuple[str | None, str | None]:
    """返回某指数已缓存的 (最早快照日, 最晚快照日)；无则 (None, None)。"""
    with get_session() as s:
        rows = (
            s.query(IndexWeight.trade_date)
            .filter(IndexWeight.index_code == index_code)
            .order_by(IndexWeight.trade_date.asc())
            .all()
        )
    if not rows:
        return None, None
    return rows[0][0], rows[-1][0]


def ensure_index_weights(index_code: str, start_date: str, end_date: str) -> int:
    """确保 [start_date, end_date] 内指数成分权重已缓存，返回新写入行数。

    与 ensure_index_bars 同款语义：完全覆盖则跳过；否则重拉整段。
    """
    lo, hi = _cached_weight_snapshot_range(index_code)
    if lo is not None and hi is not None and lo <= start_date and hi >= end_date:
        return 0
    return fetch_index_weights(index_code, start_date, end_date)


def index_snapshot_dates(index_code: str, start_date: str | None = None, end_date: str | None = None) -> List[str]:
    """返回已缓存的快照日期升序列表（可按 [start_date, end_date] 过滤）。"""
    with get_session() as s:
        q = s.query(IndexWeight.trade_date).filter(IndexWeight.index_code == index_code)
        if start_date:
            q = q.filter(IndexWeight.trade_date >= start_date)
        if end_date:
            q = q.filter(IndexWeight.trade_date <= end_date)
        rows = q.distinct().order_by(IndexWeight.trade_date.asc()).all()
    return [r[0] for r in rows]


def constituents_on(index_code: str, on_date: str) -> List[str]:
    """返回 <= on_date 的最新一次快照成分股代码列表（无快照则空列表）。"""
    with get_session() as s:
        snap = (
            s.query(IndexWeight.trade_date)
            .filter(IndexWeight.index_code == index_code, IndexWeight.trade_date <= on_date)
            .order_by(IndexWeight.trade_date.desc())
            .first()
        )
        if snap is None:
            return []
        rows = (
            s.query(IndexWeight.ts_code)
            .filter(IndexWeight.index_code == index_code, IndexWeight.trade_date == snap[0])
            .all()
        )
    return [r[0] for r in rows]


def build_pool_map(index_code: str, trade_dates: List[str]) -> dict:
    """对每个 trade_date，返回 <= 当日最新快照的成分股列表。

    单次 SQL 拉所有相关快照，内存里 step function 查找。trade_dates 必须升序。
    """
    if not trade_dates:
        return {}
    lo = min(trade_dates)
    hi = max(trade_dates)
    with get_session() as s:
        rows = (
            s.query(IndexWeight.trade_date, IndexWeight.ts_code)
            .filter(
                IndexWeight.index_code == index_code,
                IndexWeight.trade_date >= lo,
                IndexWeight.trade_date <= hi,
            )
            .order_by(IndexWeight.trade_date.asc())
            .all()
        )
    snap_to_codes: dict[str, list[str]] = {}
    for snap_date, ts_code in rows:
        snap_to_codes.setdefault(snap_date, []).append(ts_code)
    sorted_snaps = sorted(snap_to_codes.keys())

    pool_map: dict[str, list[str]] = {}
    current_codes: list[str] = []
    snap_idx = 0
    for td in trade_dates:
        while snap_idx < len(sorted_snaps) and sorted_snaps[snap_idx] <= td:
            current_codes = snap_to_codes[sorted_snaps[snap_idx]]
            snap_idx += 1
        pool_map[td] = current_codes
    return pool_map


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
