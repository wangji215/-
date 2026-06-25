"""Backtrader port of the four-stage pullback strategy.

Usage:
    python scripts/backtrader_four_stage_backtest.py --data data/csv_daily
    python scripts/backtrader_four_stage_backtest.py --data data/600885.SH.csv

CSV files should contain daily OHLC columns. Supported date columns:
date, datetime, trade_date. If volume/openinterest are missing they are filled
with 0 for Backtrader compatibility.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import backtrader as bt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import strategy_engine, strategy_repo, tushare_api
from core.strategy_dsl import StrategyDSL


REQUIRED_COLUMNS = ["open", "high", "low", "close"]


def _date_from_data(data):
    return bt.num2date(data.datetime[0]).date()


class FourStagePullbackStrategy(bt.Strategy):
    params = dict(
        lookback=90,
        max_positions=5,
        cash_buffer=0.05,
        printlog=True,
    )

    def __init__(self):
        self.order_records = []

    def log(self, message):
        if self.p.printlog and self.datas:
            print("%s %s" % (_date_from_data(self.datas[0]), message))

    def _line_values(self, line, size):
        values = np.array(line.get(size=size), dtype=float)
        if values.size < size:
            return None
        return values

    def _match_four_stage_signal(self, data):
        count = min(len(data), self.p.lookback)
        if count < 70:
            return False, 999.0

        close = self._line_values(data.close, count)
        high = self._line_values(data.high, count)
        low = self._line_values(data.low, count)
        if close is None or high is None or low is None:
            return False, 999.0

        ma5 = pd.Series(close).rolling(5).mean().to_numpy()
        ma13 = pd.Series(close).rolling(13).mean().to_numpy()
        ma21 = pd.Series(close).rolling(21).mean().to_numpy()
        ma30 = pd.Series(close).rolling(30).mean().to_numpy()
        ma60 = pd.Series(close).rolling(60).mean().to_numpy()

        ma_last = [ma5[-1], ma13[-1], ma21[-1], ma30[-1], ma60[-1]]
        if any(pd.isna(x) for x in ma_last):
            return False, 999.0

        high60 = high[-60:]
        low60 = low[-60:]
        hhv60 = float(np.max(high60))
        llv60 = float(np.min(low60))
        hhv_pos = np.flatnonzero(high60 == hhv60)
        hhvbars = len(high60) - 1 - int(hhv_pos[-1])

        close_0 = float(close[-1])
        close_1 = float(close[-2])
        close_2 = float(close[-3])
        ma5_0 = float(ma5[-1])
        ma5_1 = float(ma5[-2])
        ma5_8 = float(ma5[-9])
        ma13_0 = float(ma13[-1])
        ma13_3 = float(ma13[-4])
        ma13_8 = float(ma13[-9])
        ma21_0 = float(ma21[-1])
        ma21_2 = float(ma21[-3])
        ma21_3 = float(ma21[-4])
        ma21_8 = float(ma21[-9])
        ma30_8 = float(ma30[-9])
        ma60_0 = float(ma60[-1])

        rules = [
            ma13_0 >= ma13_3,
            ma21_0 >= ma21_3,
            close_0 > ma21_0,
            close_0 > ma60_0,
            hhvbars >= 3,
            hhvbars <= 30,
            hhv60 > 1.2 * llv60,
            close_0 <= 0.97 * hhv60,
            close_0 >= 0.78 * hhv60,
            close_2 > ma21_2,
            ma5_0 > ma5_1,
            ma5_0 < 1.015 * ma13_0,
            ma5_0 > 1.002 * ma13_0,
            ma5_0 >= ma13_0,
            ma5_8 > ma13_8,
            ma13_8 > ma21_8,
            ma21_8 > ma30_8,
            bool(np.any(ma5[-10:] > ma13[-10:])),
            bool(np.all(ma5[-10:] >= ma13[-10:])),
            close_0 > ma5_0,
            close_1 <= ma5_1,
        ]
        for passed in rules:
            if not bool(passed):
                return False, 999.0

        closeness = abs(ma5_0 / ma13_0 - 1.0)
        recency_penalty = hhvbars / 100.0
        return True, float(closeness + recency_penalty)

    def next(self):
        hits = []
        for data in self.datas:
            ok, score = self._match_four_stage_signal(data)
            if ok:
                hits.append((data, score))

        hits.sort(key=lambda item: item[1])
        target_datas = [data for data, _ in hits[: self.p.max_positions]]
        target_names = [data._name for data in target_datas]
        self.log("matched=%d targets=%s" % (len(hits), target_names))

        target_set = set(target_datas)
        for data in self.datas:
            position = self.getposition(data)
            if position.size and data not in target_set:
                self.order_target_value(data=data, target=0.0)

        if not target_datas:
            return

        target_value = self.broker.getvalue() * (1.0 - self.p.cash_buffer) / len(target_datas)
        for data in target_datas:
            self.order_target_value(data=data, target=target_value)

    def notify_order(self, order):
        if order.status in [order.Completed]:
            side = "BUY" if order.isbuy() else "SELL"
            self.order_records.append(
                {
                    "date": _date_from_data(order.data).strftime("%Y-%m-%d"),
                    "code": order.data._name,
                    "side": side,
                    "size": float(order.executed.size),
                    "price": float(order.executed.price),
                    "value": float(order.executed.value),
                    "commission": float(order.executed.comm),
                }
            )
            self.log(
                "%s %s size=%s price=%.3f value=%.2f"
                % (side, order.data._name, order.executed.size, order.executed.price, order.executed.value)
            )
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log("ORDER %s %s" % (order.data._name, order.getstatusname()))


class DslSignalStrategy(bt.Strategy):
    params = dict(
        signal_map=None,
        max_positions=5,
        cash_buffer=0.05,
        trade_start=None,
        trade_end=None,
        printlog=True,
    )

    def __init__(self):
        self.order_records = []
        self.signal_records = []
        self._data_by_name = {data._name: data for data in self.datas}

    def log(self, message):
        if self.p.printlog and self.datas:
            print("%s %s" % (_date_from_data(self.datas[0]), message))

    def _is_current_bar(self, data, trade_date):
        return _date_from_data(data).strftime("%Y%m%d") == trade_date

    def next(self):
        if not self.datas:
            return
        current = _date_from_data(self.datas[0])
        trade_date = current.strftime("%Y%m%d")

        if self.p.trade_start and current < self.p.trade_start:
            return
        if self.p.trade_end and current > self.p.trade_end:
            return

        signal_map = self.p.signal_map or {}
        target_names = list(signal_map.get(trade_date, []))[: self.p.max_positions]
        target_datas = [
            self._data_by_name[name]
            for name in target_names
            if name in self._data_by_name and self._is_current_bar(self._data_by_name[name], trade_date)
        ]
        target_set = set(target_datas)

        self.signal_records.append(
            {
                "date": current.strftime("%Y-%m-%d"),
                "matched": len(signal_map.get(trade_date, [])),
                "targets": ",".join(target_names),
            }
        )
        self.log("matched=%d targets=%s" % (len(signal_map.get(trade_date, [])), target_names))

        for data in self.datas:
            position = self.getposition(data)
            if position.size and data not in target_set and self._is_current_bar(data, trade_date):
                self.order_target_value(data=data, target=0.0)

        if not target_datas:
            return

        target_value = self.broker.getvalue() * (1.0 - self.p.cash_buffer) / len(target_datas)
        for data in target_datas:
            self.order_target_value(data=data, target=target_value)

    def notify_order(self, order):
        if order.status in [order.Completed]:
            side = "BUY" if order.isbuy() else "SELL"
            self.order_records.append(
                {
                    "date": _date_from_data(order.data).strftime("%Y-%m-%d"),
                    "code": order.data._name,
                    "side": side,
                    "size": float(order.executed.size),
                    "price": float(order.executed.price),
                    "value": float(order.executed.value),
                    "commission": float(order.executed.comm),
                }
            )
            self.log(
                "%s %s size=%s price=%.3f value=%.2f"
                % (side, order.data._name, order.executed.size, order.executed.price, order.executed.value)
            )
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.order_records.append(
                {
                    "date": _date_from_data(order.data).strftime("%Y-%m-%d"),
                    "code": order.data._name,
                    "side": "BUY" if order.isbuy() else "SELL",
                    "size": float(order.created.size),
                    "price": float(order.created.price),
                    "value": float(order.created.value),
                    "commission": 0.0,
                    "status": order.getstatusname(),
                }
            )
            self.log("ORDER %s %s" % (order.data._name, order.getstatusname()))


class PortfolioValueAnalyzer(bt.Analyzer):
    def start(self):
        self.rows = []

    def next(self):
        if not self.strategy.datas:
            return
        self.rows.append(
            {
                "date": _date_from_data(self.strategy.datas[0]).strftime("%Y-%m-%d"),
                "value": float(self.strategy.broker.getvalue()),
                "cash": float(self.strategy.broker.getcash()),
            }
        )

    def get_analysis(self):
        return self.rows


def read_daily_csv(path):
    df = pd.read_csv(path)
    df.columns = [str(col).strip().lower() for col in df.columns]

    date_col = None
    for candidate in ["date", "datetime", "trade_date"]:
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None:
        raise ValueError("%s has no date/datetime/trade_date column" % path)

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError("%s missing columns: %s" % (path, ",".join(missing)))

    df[date_col] = pd.to_datetime(df[date_col].astype(str))
    df = df.sort_values(date_col).set_index(date_col)
    if "volume" not in df.columns:
        df["volume"] = 0
    if "openinterest" not in df.columns:
        df["openinterest"] = 0

    keep = ["open", "high", "low", "close", "volume", "openinterest"]
    return df[keep].astype(float)


def read_daily_bars_from_db(db_path, ts_code, fromdate=None, todate=None):
    query = (
        "select trade_date, open, high, low, close, vol as volume "
        "from daily_bars where ts_code = ?"
    )
    params = [ts_code]
    if fromdate is not None:
        query += " and trade_date >= ?"
        params.append(fromdate.strftime("%Y%m%d"))
    if todate is not None:
        query += " and trade_date <= ?"
        params.append(todate.strftime("%Y%m%d"))
    query += " order by trade_date asc"

    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df

    df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str))
    df = df.set_index("trade_date")
    df["openinterest"] = 0
    keep = ["open", "high", "low", "close", "volume", "openinterest"]
    return df[keep].astype(float)


def read_daily_bars_frame_from_db(db_path, codes=None, fromdate=None, todate=None):
    query = (
        "select ts_code, trade_date, open, high, low, close, vol, amount, pct_chg "
        "from daily_bars where 1=1"
    )
    params = []
    if codes:
        placeholders = ",".join(["?"] * len(codes))
        query += " and ts_code in (%s)" % placeholders
        params.extend(codes)
    if fromdate is not None:
        query += " and trade_date >= ?"
        params.append(fromdate.strftime("%Y%m%d"))
    if todate is not None:
        query += " and trade_date <= ?"
        params.append(todate.strftime("%Y%m%d"))
    query += " order by ts_code asc, trade_date asc"

    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(query, conn, params=params)


def discover_csv_files(data_path):
    path = Path(data_path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError("data path does not exist: %s" % data_path)
    return sorted(path.glob("*.csv"))


def add_data_feeds(cerebro, data_path, fromdate=None, todate=None, max_files=None):
    files = discover_csv_files(data_path)
    if max_files:
        files = files[:max_files]
    if not files:
        raise ValueError("no csv files found under %s" % data_path)

    loaded = []
    for file_path in files:
        df = read_daily_csv(file_path)
        if fromdate is not None:
            df = df[df.index >= fromdate]
        if todate is not None:
            df = df[df.index <= todate]
        if df.empty:
            continue

        data = bt.feeds.PandasData(dataname=df)
        cerebro.adddata(data, name=file_path.stem)
        loaded.append((file_path.stem, len(df)))
    return loaded


def discover_db_codes(db_path, fromdate=None, todate=None, max_codes=None):
    query = "select ts_code, count(*) as n from daily_bars where 1=1"
    params = []
    if fromdate is not None:
        query += " and trade_date >= ?"
        params.append(fromdate.strftime("%Y%m%d"))
    if todate is not None:
        query += " and trade_date <= ?"
        params.append(todate.strftime("%Y%m%d"))
    query += " group by ts_code order by ts_code asc"
    if max_codes:
        query += " limit ?"
        params.append(int(max_codes))

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [row[0] for row in rows]


def warmup_start_date(db_path, fromdate, lookback):
    if fromdate is None:
        return None
    query = (
        "select distinct trade_date from daily_bars "
        "where trade_date <= ? order by trade_date desc limit ?"
    )
    params = [fromdate.strftime("%Y%m%d"), int(lookback) + 1]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        return fromdate
    return pd.to_datetime(rows[-1][0])


def add_db_data_feeds(cerebro, db_path, codes=None, fromdate=None, todate=None, max_codes=None):
    if codes:
        selected_codes = [code.strip() for code in codes.split(",") if code.strip()]
    else:
        selected_codes = discover_db_codes(db_path, fromdate=fromdate, todate=todate, max_codes=max_codes)
    if max_codes and codes:
        selected_codes = selected_codes[:max_codes]
    if not selected_codes:
        raise ValueError("no codes found in %s" % db_path)

    loaded = []
    for code in selected_codes:
        df = read_daily_bars_from_db(db_path, code, fromdate=fromdate, todate=todate)
        if df.empty:
            continue
        data = bt.feeds.PandasData(dataname=df)
        cerebro.adddata(data, name=code)
        loaded.append((code, len(df)))
    if not loaded:
        raise ValueError("no usable daily bars loaded from %s" % db_path)
    return loaded


def add_db_data_feeds_from_frame(cerebro, bars):
    if bars.empty:
        raise ValueError("no usable daily bars loaded from database")

    loaded = []
    for code, df in bars.groupby("ts_code", sort=False):
        feed = df.sort_values("trade_date").copy()
        feed["trade_date"] = pd.to_datetime(feed["trade_date"].astype(str))
        feed = feed.set_index("trade_date")
        feed = feed.rename(columns={"vol": "volume"})
        feed["openinterest"] = 0
        keep = ["open", "high", "low", "close", "volume", "openinterest"]
        feed = feed[keep].astype(float)
        if feed.empty:
            continue
        data = bt.feeds.PandasData(dataname=feed)
        cerebro.adddata(data, name=str(code))
        loaded.append((str(code), len(feed)))
    if not loaded:
        raise ValueError("no usable daily bars loaded from database")
    return loaded


def build_signal_map(dsl: StrategyDSL, bars: pd.DataFrame, fromdate=None, todate=None):
    history = strategy_engine.evaluate_history(dsl, bars)
    if history.empty:
        return {}, history
    matched = history[history["matched"].astype(bool)].copy()
    if fromdate is not None:
        matched = matched[matched["trade_date"].astype(str) >= fromdate.strftime("%Y%m%d")]
    if todate is not None:
        matched = matched[matched["trade_date"].astype(str) <= todate.strftime("%Y%m%d")]

    signal_map = {}
    for trade_date, group in matched.groupby("trade_date", sort=True):
        signal_map[str(trade_date)] = list(group.sort_values("ts_code")["ts_code"].astype(str))
    return signal_map, history


def print_feed_summary(loaded, min_bars):
    counts = [count for _, count in loaded]
    enough = [name for name, count in loaded if count >= min_bars]
    print(
        "Loaded feeds: %d, bars min/max: %d/%d, feeds with >=%d bars: %d"
        % (len(loaded), min(counts), max(counts), min_bars, len(enough))
    )
    if len(enough) == 0:
        print("Warning: no feed has enough bars for this strategy; expect zero trades.")


def _extra_analyzers(cerebro):
    """补齐聚宽风格的 analyzer 套件。

    - Sharpe/Sortino：backtrader 1.9.78.123 的 SharpeRatio 在窗口短时返回 0，且无 Sortino，
      统一在 :func:`_compute_metrics` 里自算。
    - Calmar / AnnualReturn / PeriodStats：analyzer 直接用，便于 cross-check。
    """
    cerebro.addanalyzer(bt.analyzers.Calmar, _name="calmar")
    cerebro.addanalyzer(bt.analyzers.AnnualReturn, _name="annual_return")
    cerebro.addanalyzer(bt.analyzers.PeriodStats, _name="period_stats")
    return cerebro


def build_cerebro(
    cash=100000.0,
    commission=0.0003,
    lookback=90,
    max_positions=5,
    cash_buffer=0.05,
    printlog=True,
):
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=commission)
    cerebro.addstrategy(
        FourStagePullbackStrategy,
        lookback=lookback,
        max_positions=max_positions,
        cash_buffer=cash_buffer,
        printlog=printlog,
    )
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(PortfolioValueAnalyzer, _name="portfolio_value")
    _extra_analyzers(cerebro)
    return cerebro


def build_dsl_cerebro(
    signal_map,
    cash=100000.0,
    commission=0.0003,
    max_positions=5,
    cash_buffer=0.05,
    trade_start=None,
    trade_end=None,
    printlog=True,
):
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=commission)
    cerebro.addstrategy(
        DslSignalStrategy,
        signal_map=signal_map,
        max_positions=max_positions,
        cash_buffer=cash_buffer,
        trade_start=trade_start.date() if hasattr(trade_start, "date") else trade_start,
        trade_end=trade_end.date() if hasattr(trade_end, "date") else trade_end,
        printlog=printlog,
    )
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(PortfolioValueAnalyzer, _name="portfolio_value")
    _extra_analyzers(cerebro)
    return cerebro


def _pair_orders_to_trades(order_records):
    """把订单层记录按 code FIFO 配对成交易对。

    输出 list[{code, buy_date, buy_price, sell_date, sell_price, size, pnl, pnl_pct, hold_days}]。
    回测结束时未平仓的 BUY 输出为 sell_date/sell_price/pnl/hold_days = None，UI 标「未平仓」。
    部分 SELL 超 BUY（数据异常）跳过。
    """
    by_code: dict[str, list[dict]] = {}
    for o in order_records:
        by_code.setdefault(o["code"], []).append(o)

    trades: list[dict] = []
    for code, orders in by_code.items():
        orders = sorted(orders, key=lambda x: (x["date"], 0 if x.get("side") == "BUY" else 1))
        buy_queue: list[dict] = []
        for o in orders:
            if o.get("side") == "BUY":
                buy_queue.append({"date": o["date"], "price": float(o["price"]), "size": float(abs(o["size"]))})
                continue
            remaining = float(abs(o.get("size", 0)))
            if remaining <= 0:
                continue
            sell_date = o["date"]
            sell_price = float(o["price"])
            while remaining > 1e-9 and buy_queue:
                buy = buy_queue[0]
                matched = min(buy["size"], remaining)
                if matched <= 0:
                    buy_queue.pop(0)
                    continue
                pnl = (sell_price - buy["price"]) * matched
                pnl_pct = (sell_price / buy["price"] - 1.0) if buy["price"] else 0.0
                try:
                    hold_days = int((pd.to_datetime(sell_date) - pd.to_datetime(buy["date"])).days)
                except Exception:
                    hold_days = None
                trades.append({
                    "code": code,
                    "buy_date": buy["date"],
                    "buy_price": float(buy["price"]),
                    "sell_date": sell_date,
                    "sell_price": float(sell_price),
                    "size": float(matched),
                    "pnl": float(pnl),
                    "pnl_pct": float(pnl_pct),
                    "hold_days": hold_days,
                })
                buy["size"] -= matched
                remaining -= matched
                if buy["size"] <= 1e-9:
                    buy_queue.pop(0)
        for buy in buy_queue:
            trades.append({
                "code": code,
                "buy_date": buy["date"],
                "buy_price": float(buy["price"]),
                "sell_date": None,
                "sell_price": None,
                "size": float(buy["size"]),
                "pnl": None,
                "pnl_pct": None,
                "hold_days": None,
            })
    return trades


def _safe_float(obj, *keys, default=0.0) -> float:
    """从 analyzer 输出里逐层取值；空/异常返回 default。"""
    cur = obj
    for k in keys:
        if cur is None:
            return default
        try:
            cur = cur.get(k) if hasattr(cur, "get") else getattr(cur, k, None)
        except Exception:
            return default
    try:
        return float(cur) if cur is not None else default
    except (TypeError, ValueError):
        return default


def _compute_metrics(strategy, trade_pairs, portfolio_value, start_value, end_value) -> dict:
    """聚宽风格指标表：analyzer + 自算 cross-check，全除零保护。"""
    closed = [t for t in trade_pairs if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    total_pnl = sum(t["pnl"] for t in closed)
    win_rate = (len(wins) / len(closed)) if closed else 0.0
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss_abs = (abs(sum(t["pnl"] for t in losses)) / len(losses)) if losses else 0.0
    profit_loss_ratio = (avg_win / avg_loss_abs) if avg_loss_abs else 0.0
    avg_hold_days = (sum(t["hold_days"] for t in closed if t["hold_days"] is not None) / len(closed)) if closed else 0.0

    values = [row["value"] for row in portfolio_value] if portfolio_value else []
    max_dd = 0.0
    peak = None
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    annual_return = 0.0
    if portfolio_value and len(portfolio_value) >= 2 and start_value > 0:
        try:
            days = (pd.to_datetime(portfolio_value[-1]["date"]) - pd.to_datetime(portfolio_value[0]["date"])).days
            if days > 0:
                annual_return = (end_value / start_value) ** (365.0 / days) - 1.0
        except Exception:
            annual_return = 0.0

    sharpe = 0.0
    sortino = 0.0
    if len(values) >= 2:
        series = pd.Series(values, dtype=float)
        daily_ret = series.pct_change().dropna()
        if len(daily_ret) > 0:
            mean_ret = float(daily_ret.mean())
            std = float(daily_ret.std(ddof=0))
            if std > 0:
                sharpe = mean_ret / std * (250 ** 0.5)
            downside = daily_ret.clip(upper=0.0)
            dd_var = float((downside ** 2).mean())
            if dd_var > 0:
                sortino = mean_ret / (dd_var ** 0.5) * (250 ** 0.5)

    calmar = _safe_float(strategy.analyzers.calmar.get_analysis(), "calmar")
    if calmar == 0.0 and annual_return != 0.0 and max_dd > 0:
        # backtrader Calmar 在窗口短时（< 1 年）返回 0；自算补 fallback。
        calmar = abs(annual_return) / max_dd

    return {
        "annual_return": float(annual_return),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(calmar),
        "max_drawdown": float(max_dd),
        "win_rate": float(win_rate),
        "profit_loss_ratio": float(profit_loss_ratio),
        "avg_hold_days": float(avg_hold_days),
        "total_trades": int(len(closed)),
        "total_pnl": float(total_pnl),
    }


def _normalize_strategy_series(portfolio_value):
    """[{date, value, cash}] → [{date, value}]，归一化到 1.0。"""
    if not portfolio_value:
        return []
    base = portfolio_value[0]["value"]
    if not base:
        return [{"date": r["date"], "value": 1.0} for r in portfolio_value]
    return [{"date": r["date"], "value": float(r["value"] / base)} for r in portfolio_value]


def _benchmark_series_from_bars(bars_df, dates):
    """从 daily_bars DataFrame 切出对应日期，归一化到 1.0。"""
    if bars_df is None or bars_df.empty or not dates:
        return []
    norm_dates = set()
    for d in dates:
        s = str(d).replace("-", "")
        norm_dates.add(s)
    sub = bars_df[bars_df["trade_date"].astype(str).isin(norm_dates)]
    sub = sub.sort_values("trade_date")
    if sub.empty:
        return []
    base = float(sub.iloc[0]["close"])
    if not base:
        return []
    out = []
    for _, row in sub.iterrows():
        d = str(row["trade_date"])
        d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        out.append({"date": d_fmt, "value": float(row["close"]) / base})
    return out


def _drawdown_series_from_equity(series):
    """[{date, value}] → [{date, drawdown}]（drawdown <= 0）。"""
    if not series:
        return []
    out = []
    peak = series[0]["value"]
    for r in series:
        peak = max(peak, r["value"])
        dd = (r["value"] / peak - 1.0) if peak > 0 else 0.0
        out.append({"date": r["date"], "drawdown": float(dd)})
    return out


def _monthly_returns_from_equity(series):
    """[{date, value}] → [{year, month, pct}]（每月复利收益）。"""
    if len(series) < 2:
        return []
    df = pd.DataFrame(series)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    monthly = df["value"].resample("ME").last().dropna()
    if len(monthly) < 2:
        return []
    pct = monthly.pct_change().dropna()
    out = []
    for ts, v in pct.items():
        out.append({"year": int(ts.year), "month": int(ts.month), "pct": float(v)})
    return out


def _stock_summary(trade_pairs, stock_names):
    """按个股聚合：交易次数、总盈亏、平均持仓天数、贡献占比。"""
    by_code: dict[str, list[dict]] = {}
    for t in trade_pairs:
        if t.get("pnl") is None:
            continue
        by_code.setdefault(t["code"], []).append(t)
    if not by_code:
        return []
    total_pnl_all = sum(sum(t["pnl"] for t in lst) for lst in by_code.values())
    out = []
    for code, lst in by_code.items():
        pnl = sum(t["pnl"] for t in lst)
        holds = [t["hold_days"] for t in lst if t["hold_days"] is not None]
        avg_hold = (sum(holds) / len(holds)) if holds else 0.0
        out.append({
            "code": code,
            "name": stock_names.get(code, code),
            "trades": int(len(lst)),
            "total_pnl": float(pnl),
            "avg_hold_days": float(avg_hold),
            "contribution_pct": float(pnl / total_pnl_all) if total_pnl_all else 0.0,
        })
    out.sort(key=lambda x: x["total_pnl"], reverse=True)
    return out


def _load_stock_names(db_path, codes):
    """从 stocks 表查名称，返回 {ts_code: name}。失败返回空 dict。"""
    if not codes or not db_path:
        return {}
    placeholders = ",".join(["?"] * len(codes))
    query = "select ts_code, name from stocks where ts_code in (%s)" % placeholders
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(query, list(codes)).fetchall()
    except Exception:
        return {}
    return {r[0]: (r[1] or r[0]) for r in rows}


def run_backtest(
    data_path=None,
    db_path=None,
    codes=None,
    cash=100000.0,
    commission=0.0003,
    lookback=90,
    max_positions=5,
    cash_buffer=0.05,
    fromdate=None,
    todate=None,
    max_files=None,
    max_codes=None,
    printlog=False,
):
    if bool(data_path) == bool(db_path):
        raise ValueError("Provide exactly one of data_path or db_path")

    cerebro = build_cerebro(
        cash=cash,
        commission=commission,
        lookback=lookback,
        max_positions=max_positions,
        cash_buffer=cash_buffer,
        printlog=printlog,
    )

    if data_path:
        loaded = add_data_feeds(cerebro, data_path, fromdate=fromdate, todate=todate, max_files=max_files)
    else:
        loaded = add_db_data_feeds(
            cerebro,
            db_path,
            codes=codes,
            fromdate=fromdate,
            todate=todate,
            max_codes=max_codes,
        )

    start_value = cerebro.broker.getvalue()
    results = cerebro.run()
    end_value = cerebro.broker.getvalue()
    strategy = results[0]

    return {
        "loaded": loaded,
        "start_value": float(start_value),
        "end_value": float(end_value),
        "returns": strategy.analyzers.returns.get_analysis(),
        "drawdown": strategy.analyzers.drawdown.get_analysis(),
        "trades": strategy.analyzers.trades.get_analysis(),
        "portfolio_value": strategy.analyzers.portfolio_value.get_analysis(),
        "orders": strategy.order_records,
    }


def run_dsl_backtest(
    strategy_id=None,
    dsl=None,
    db_path=None,
    codes=None,
    cash=100000.0,
    commission=0.0003,
    max_positions=5,
    cash_buffer=0.05,
    fromdate=None,
    todate=None,
    max_codes=None,
    benchmark_code=None,
    pool_index_code=None,
    printlog=False,
):
    if not db_path:
        raise ValueError("db_path is required")
    if dsl is None:
        if strategy_id is None:
            raise ValueError("strategy_id or dsl is required")
        dsl = strategy_repo.get_current_dsl(int(strategy_id))
    if dsl is None:
        raise ValueError("strategy has no current DSL")

    lookback = int(dsl.lookback)
    user_codes = [code.strip() for code in codes.split(",") if code.strip()] if codes else None

    pool_map = None
    pool_set = None
    if pool_index_code:
        from_str_pool = fromdate.strftime("%Y%m%d")
        to_str_pool = todate.strftime("%Y%m%d")
        tushare_api.ensure_index_weights(pool_index_code, from_str_pool, to_str_pool)
        with sqlite3.connect(db_path) as conn:
            trade_days = [
                r[0]
                for r in conn.execute(
                    "select distinct trade_date from daily_bars where trade_date >= ? and trade_date <= ? order by trade_date asc",
                    [from_str_pool, to_str_pool],
                ).fetchall()
            ]
        pool_map = tushare_api.build_pool_map(pool_index_code, trade_days)
        pool_codes = sorted({c for codes_on_day in pool_map.values() for c in codes_on_day})
        if not pool_codes:
            raise ValueError("no constituents cached for %s" % pool_index_code)
        pool_set = set(pool_codes)

    if user_codes:
        if pool_set is not None:
            selected_codes = [c for c in user_codes if c in pool_set]
        elif max_codes:
            selected_codes = user_codes[:max_codes]
        else:
            selected_codes = user_codes
    elif pool_set is not None:
        selected_codes = pool_codes
    else:
        selected_codes = discover_db_codes(db_path, fromdate=fromdate, todate=todate, max_codes=max_codes)
    if not selected_codes:
        raise ValueError("no codes found in %s" % db_path)

    load_fromdate = warmup_start_date(db_path, fromdate, lookback)
    bars = read_daily_bars_frame_from_db(
        db_path,
        codes=selected_codes,
        fromdate=load_fromdate,
        todate=todate,
    )
    if bars.empty:
        raise ValueError("no usable daily bars loaded from %s" % db_path)

    signal_map, history = build_signal_map(dsl, bars, fromdate=fromdate, todate=todate)

    from_str = fromdate.strftime("%Y%m%d") if fromdate is not None else None
    if from_str is not None and not bars.empty:
        first_dates = bars.groupby("ts_code")["trade_date"].min().astype(str)
        keep_codes = first_dates[first_dates <= from_str].index.tolist()
    else:
        keep_codes = list(bars["ts_code"].unique()) if not bars.empty else []
    if keep_codes and len(keep_codes) < bars["ts_code"].nunique():
        bars_for_bt = bars[bars["ts_code"].isin(keep_codes)]
        signal_map = {
            d: [c for c in codes if c in set(keep_codes)]
            for d, codes in signal_map.items()
        }
        signal_map = {d: codes for d, codes in signal_map.items() if codes}
    else:
        bars_for_bt = bars

    if pool_map:
        signal_map = {
            d: [c for c in codes if c in set(pool_map.get(d, []))]
            for d, codes in signal_map.items()
        }
        signal_map = {d: codes for d, codes in signal_map.items() if codes}

    cerebro = build_dsl_cerebro(
        signal_map,
        cash=cash,
        commission=commission,
        max_positions=max_positions,
        cash_buffer=cash_buffer,
        trade_start=fromdate,
        trade_end=todate,
        printlog=printlog,
    )
    loaded = add_db_data_feeds_from_frame(cerebro, bars_for_bt)

    start_value = cerebro.broker.getvalue()
    results = cerebro.run()
    end_value = cerebro.broker.getvalue()
    strategy = results[0]

    total_signals = sum(len(codes) for codes in signal_map.values())
    portfolio_value = strategy.analyzers.portfolio_value.get_analysis()
    order_records = list(strategy.order_records)

    strategy_series = _normalize_strategy_series(portfolio_value)
    drawdown_series = _drawdown_series_from_equity(strategy_series)
    monthly_returns = _monthly_returns_from_equity(strategy_series)
    trade_pairs = _pair_orders_to_trades(order_records)
    metrics = _compute_metrics(strategy, trade_pairs, portfolio_value, float(start_value), float(end_value))

    benchmark_series = []
    if benchmark_code:
        bench_codes = list({benchmark_code})
        bench_bars = read_daily_bars_frame_from_db(
            db_path,
            codes=bench_codes,
            fromdate=load_fromdate,
            todate=todate,
        )
        dates = [r["date"] for r in strategy_series]
        benchmark_series = _benchmark_series_from_bars(bench_bars, dates)

    traded_codes = list({t["code"] for t in trade_pairs})
    stock_names = _load_stock_names(db_path, traded_codes)
    stock_summary = _stock_summary(trade_pairs, stock_names)

    return {
        "strategy_name": dsl.name,
        "lookback": lookback,
        "loaded": loaded,
        "start_value": float(start_value),
        "end_value": float(end_value),
        "returns": strategy.analyzers.returns.get_analysis(),
        "drawdown": strategy.analyzers.drawdown.get_analysis(),
        "trades": strategy.analyzers.trades.get_analysis(),
        "portfolio_value": portfolio_value,
        "orders": order_records,
        "signals": strategy.signal_records,
        "signal_days": len(signal_map),
        "total_signals": total_signals,
        "metrics": metrics,
        "strategy_series": strategy_series,
        "benchmark_series": benchmark_series,
        "benchmark_code": benchmark_code,
        "drawdown_series": drawdown_series,
        "monthly_returns": monthly_returns,
        "trade_pairs": trade_pairs,
        "stock_summary": stock_summary,
        "calmar": strategy.analyzers.calmar.get_analysis(),
        "annual_return": strategy.analyzers.annual_return.get_analysis(),
        "period_stats": strategy.analyzers.period_stats.get_analysis(),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Run four-stage pullback strategy on Backtrader.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", help="CSV file or directory of CSV files")
    source.add_argument("--db", help="SQLite database containing daily_bars")
    parser.add_argument("--codes", help="Comma-separated ts_code list when using --db")
    parser.add_argument("--cash", type=float, default=100000.0)
    parser.add_argument("--commission", type=float, default=0.0003)
    parser.add_argument("--lookback", type=int, default=90)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--fromdate", help="YYYY-MM-DD")
    parser.add_argument("--todate", help="YYYY-MM-DD")
    parser.add_argument("--max-files", type=int, help="Limit number of CSV files loaded")
    parser.add_argument("--max-codes", type=int, help="Limit number of DB codes loaded")
    parser.add_argument("--benchmark-code", help="Benchmark ts_code (e.g. 000300.SH for 沪深300) loaded from daily_bars")
    parser.add_argument("--pool-index-code", help="Dynamic stock pool from index_weight (e.g. 000300.SH = 沪深300 constituents)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--strategy-id", type=int, help="Run saved DSL strategy instead of built-in four-stage strategy")
    return parser.parse_args()


def main():
    args = parse_args()
    fromdate = pd.to_datetime(args.fromdate) if args.fromdate else None
    todate = pd.to_datetime(args.todate) if args.todate else None

    if args.strategy_id:
        if not args.db:
            raise ValueError("--strategy-id requires --db")
        result = run_dsl_backtest(
            strategy_id=args.strategy_id,
            db_path=args.db,
            codes=args.codes,
            cash=args.cash,
            commission=args.commission,
            max_positions=args.max_positions,
            fromdate=fromdate,
            todate=todate,
            max_codes=args.max_codes,
            benchmark_code=args.benchmark_code,
            pool_index_code=args.pool_index_code,
            printlog=not args.quiet,
        )
    else:
        result = run_backtest(
            data_path=args.data,
            db_path=args.db,
            codes=args.codes,
            cash=args.cash,
            commission=args.commission,
            lookback=args.lookback,
            max_positions=args.max_positions,
            fromdate=fromdate,
            todate=todate,
            max_files=args.max_files,
            max_codes=args.max_codes,
            printlog=not args.quiet,
        )
    loaded = result["loaded"]
    print_feed_summary(loaded, min_bars=70)

    print("Start Portfolio Value: %.2f" % result["start_value"])
    print("Final Portfolio Value: %.2f" % result["end_value"])
    print("Returns:", result["returns"])
    print("DrawDown:", result["drawdown"])
    print("Trades:", result["trades"])
    if "metrics" in result:
        m = result["metrics"]
        print(
            "Metrics: annual_return=%.4f sharpe=%.3f sortino=%.3f calmar=%.3f "
            "max_drawdown=%.4f win_rate=%.4f pl_ratio=%.3f avg_hold=%.1fd trades=%d"
            % (
                m["annual_return"], m["sharpe"], m["sortino"], m["calmar"],
                m["max_drawdown"], m["win_rate"], m["profit_loss_ratio"],
                m["avg_hold_days"], m["total_trades"],
            )
        )
    if "signal_days" in result:
        print("Signal days: %d, total signals: %d" % (result["signal_days"], result["total_signals"]))

    if args.plot:
        cerebro = build_cerebro(
            cash=args.cash,
            commission=args.commission,
            lookback=args.lookback,
            max_positions=args.max_positions,
            printlog=not args.quiet,
        )
        if args.data:
            add_data_feeds(cerebro, args.data, fromdate=fromdate, todate=todate, max_files=args.max_files)
        else:
            add_db_data_feeds(cerebro, args.db, codes=args.codes, fromdate=fromdate, todate=todate, max_codes=args.max_codes)
        cerebro.run()
        cerebro.plot()


if __name__ == "__main__":
    main()
