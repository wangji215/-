"""一键初始化回测所需数据。

用法：
    python -m scripts.bootstrap_backtest_data --from 20240601 --to 20260624
    python -m scripts.bootstrap_backtest_data --from 20240101 --to 20260624 --indices 000300.SH,000905.SH
    # 切换复权方式时：先清空再按后复权重补全段
    python -m scripts.bootstrap_backtest_data --from 20240601 --to 20260624 --clear-bars

依次补齐：
1. stocks（股票基础信息，用于名称查询）
2. trade_cal（交易日历）
3. daily_bars（全市场**后复权 hfq** 日线，按 trade_date 拉全市场，最大头）
4. 指数日线（daily_bars 表，与个股同 schema；指数无复权）
5. 指数成分股权重（index_weights 表）

幂等：已缓存的不会重复拉。切换复权方式须加 --clear-bars 先清空，避免新旧数据混存。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from core import tushare_api

DEFAULT_INDICES = ["000300.SH", "000016.SH", "399006.SZ", "000905.SH"]


def _parse_args():
    p = argparse.ArgumentParser(description="Bootstrap backtest data from tushare")
    p.add_argument("--from", dest="from_date", required=True, help="YYYYMMDD")
    p.add_argument("--to", dest="to_date", required=True, help="YYYYMMDD")
    p.add_argument(
        "--indices",
        default=",".join(DEFAULT_INDICES),
        help=f"Comma-separated index ts_codes (default: {','.join(DEFAULT_INDICES)})",
    )
    p.add_argument("--skip-stocks", action="store_true", help="Skip stock_basic refresh")
    p.add_argument("--skip-bars", action="store_true", help="Skip daily_bars backfill")
    p.add_argument(
        "--clear-bars",
        action="store_true",
        help="Clear daily_bars before backfill (切换复权方式时用：先清空再按后复权重补全段)",
    )
    p.add_argument("--skip-index-bars", action="store_true", help="Skip index OHLCV backfill")
    p.add_argument("--skip-index-weights", action="store_true", help="Skip index weight backfill")
    return p.parse_args()


def _step(name: str, fn, *args, **kwargs):
    print(f"\n=== {name} ===", flush=True)
    start = time.time()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - start
        print(f"  done in {elapsed:.1f}s  ->  {result}", flush=True)
        return result
    except Exception as exc:
        print(f"  FAILED: {exc}", flush=True)
        return None


def main():
    args = _parse_args()
    start_ts = datetime.strptime(args.from_date, "%Y%m%d")
    end_ts = datetime.strptime(args.to_date, "%Y%m%d")
    if end_ts < start_ts:
        print("--to must be >= --from", file=sys.stderr)
        sys.exit(2)

    indices = [c.strip() for c in args.indices.split(",") if c.strip()]
    print(f"Bootstrap range: {args.from_date} ~ {args.to_date}")
    print(f"Indices: {indices}")

    if not args.skip_stocks:
        _step("refresh stocks (stock_basic)", tushare_api.refresh_stocks)

    _step(
        f"trade_cal [{args.from_date}, {args.to_date}]",
        tushare_api.fetch_trade_cal,
        args.from_date,
        args.to_date,
    )

    if not args.skip_bars:
        if args.clear_bars:
            _step("clear daily_bars", tushare_api.clear_daily_bars)
        trade_days = tushare_api.trading_days_between(args.from_date, args.to_date)
        print(f"  trade_days in range: {len(trade_days)}", flush=True)
        if trade_days:
            _step(
                f"daily_bars ({len(trade_days)} days, hfq)",
                tushare_api.ensure_bars_for_dates,
                trade_days,
            )

    if not args.skip_index_bars:
        for code in indices:
            _step(
                f"index_daily {code}",
                tushare_api.ensure_index_bars,
                code,
                args.from_date,
                args.to_date,
            )

    if not args.skip_index_weights:
        for code in indices:
            _step(
                f"index_weight {code}",
                tushare_api.ensure_index_weights,
                code,
                args.from_date,
                args.to_date,
            )

    print("\n=== summary ===")
    import sqlite3
    with sqlite3.connect("data/stock.db") as conn:
        for table in ["stocks", "trade_cal", "daily_bars", "index_weights"]:
            n = conn.execute(f"select count(*) from {table}").fetchone()[0]
            print(f"  {table}: {n:,} rows")


if __name__ == "__main__":
    main()
