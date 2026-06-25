"""Backtrader 回测页：聚宽风格策略总览。"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from sqlalchemy import func

from core import strategy_repo, trade_rule_repo, tushare_api
from core.db import DB_PATH, get_session
from core.models import DailyBar, Stock
from core.strategy_dsl import dsl_to_text
from core.trade_rule_dsl import rule_to_text
from scripts.backtrader_four_stage_backtest import discover_db_codes, run_dsl_backtest


st.set_page_config(page_title="Backtrader回测", page_icon="📈", layout="wide")
st.title("📈 Backtrader回测")


BENCHMARK_OPTIONS = {
    "沪深300": "000300.SH",
    "上证50": "000016.SH",
    "创业板指": "399006.SZ",
    "中证500": "000905.SH",
    "无基准": None,
}

POOL_OPTIONS = {
    "全市场": None,
    "沪深300": "000300.SH",
    "上证50": "000016.SH",
    "创业板指": "399006.SZ",
    "中证500": "000905.SH",
}


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y%m%d").date()


def _to_ts(value: date | None) -> pd.Timestamp | None:
    if value is None:
        return None
    return pd.Timestamp(value)


def _db_summary() -> dict:
    with get_session() as s:
        rows = s.query(DailyBar).count()
        codes = s.query(DailyBar.ts_code).distinct().count()
        dates = s.query(DailyBar.trade_date).distinct().count()
        min_date = s.query(DailyBar.trade_date).order_by(DailyBar.trade_date.asc()).first()
        max_date = s.query(DailyBar.trade_date).order_by(DailyBar.trade_date.desc()).first()
    return {
        "rows": rows,
        "codes": codes,
        "dates": dates,
        "min_date": min_date[0] if min_date else None,
        "max_date": max_date[0] if max_date else None,
    }


@st.cache_data(ttl=300)
def _stock_options() -> pd.DataFrame:
    with get_session() as s:
        rows = s.query(Stock.ts_code, Stock.name).order_by(Stock.ts_code.asc()).all()
    return pd.DataFrame(rows, columns=["ts_code", "name"])


def _flatten(value):
    if hasattr(value, "items"):
        return {k: _flatten(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)) and value and hasattr(value[0], "items"):
        return [_flatten(v) for v in value]
    return value


def _nested(data, *keys, default=None):
    cur = data
    for key in keys:
        if not hasattr(cur, "get"):
            return default
        cur = cur.get(key, default)
    return cur


def _fmt_pct(x: float, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x * 100:+.{digits}f}%"


def _fmt_num(x: float, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x:.{digits}f}"


def _equity_fig(strategy_series, benchmark_series) -> go.Figure:
    fig = go.Figure()
    if strategy_series:
        df = pd.DataFrame(strategy_series)
        fig.add_trace(go.Scatter(x=df["date"], y=df["value"], mode="lines", name="策略净值", line=dict(color="#1f77b4", width=2)))
    if benchmark_series:
        dfb = pd.DataFrame(benchmark_series)
        fig.add_trace(go.Scatter(x=dfb["date"], y=dfb["value"], mode="lines", name="基准净值", line=dict(color="#888", width=1.5, dash="dot")))
    fig.update_layout(
        height=420,
        margin=dict(l=20, r=20, t=30, b=20),
        xaxis_title="日期",
        yaxis_title="归一化净值",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    return fig


def _drawdown_fig(drawdown_series) -> go.Figure:
    fig = go.Figure()
    if drawdown_series:
        df = pd.DataFrame(drawdown_series)
        df["dd_pct"] = df["drawdown"] * 100
        fig.add_trace(go.Scatter(
            x=df["date"], y=df["dd_pct"], mode="lines", name="回撤",
            line=dict(color="#d62728", width=1.2), fill="tozeroy", fillcolor="rgba(214,39,40,0.18)",
        ))
    fig.update_layout(
        height=200,
        margin=dict(l=20, r=20, t=10, b=20),
        xaxis_title="日期",
        yaxis_title="回撤%",
        hovermode="x unified",
    )
    return fig


def _monthly_heatmap(monthly_returns) -> go.Figure:
    fig = go.Figure()
    if monthly_returns:
        df = pd.DataFrame(monthly_returns)
        pivot = df.pivot_table(index="year", columns="month", values="pct", aggfunc="first")
        labels = ["%d月" % m for m in range(1, 13)]
        fig.add_trace(go.Heatmap(
            z=pivot.values * 100,
            x=labels,
            y=[str(y) for y in pivot.index],
            colorscale="RdYlGn",
            zmid=0,
            colorbar=dict(title="%", ticksuffix="%"),
            hovertemplate="年份=%{y}<br>月份=%{x}<br>收益=%{z:.2f}%<extra></extra>",
            text=[[f"{v:.1f}%" if not pd.isna(v) else "" for v in row] for row in pivot.values * 100],
            texttemplate="%{text}",
        ))
    fig.update_layout(height=320, margin=dict(l=20, r=20, t=10, b=20), xaxis_title="", yaxis_title="")
    return fig


def _daily_hist(strategy_series) -> go.Figure:
    fig = go.Figure()
    if strategy_series and len(strategy_series) > 1:
        df = pd.DataFrame(strategy_series)
        rets = df["value"].pct_change().dropna() * 100
        fig.add_trace(go.Histogram(x=rets, nbinsx=40, marker_color="#9467bd", name="日收益%"))
        fig.update_layout(
            height=320,
            margin=dict(l=20, r=20, t=10, b=20),
            xaxis_title="日收益%",
            yaxis_title="天数",
            bargap=0.05,
        )
    return fig


def _contribution_bar(stock_summary) -> go.Figure:
    fig = go.Figure()
    if stock_summary:
        df = pd.DataFrame(stock_summary).sort_values("contribution_pct")
        df["label"] = df["name"].where(df["name"] != df["code"], df["code"])
        df["contrib_pct"] = df["contribution_pct"] * 100
        colors = ["#2ca02c" if v >= 0 else "#d62728" for v in df["contrib_pct"]]
        fig.add_trace(go.Bar(
            y=df["label"], x=df["contrib_pct"], orientation="h",
            marker_color=colors, hovertemplate="%{y}<br>贡献=%{x:.2f}%<extra></extra>",
        ))
        fig.update_layout(
            height=max(300, len(df) * 22 + 60),
            margin=dict(l=20, r=20, t=10, b=20),
            xaxis_title="贡献占比%",
            yaxis_title="",
        )
    return fig


summary = _db_summary()
if not summary["rows"]:
    st.warning("本地日线缓存为空，请先补齐 daily_bars。")
    st.stop()

min_d = _to_date(summary["min_date"])
max_d = _to_date(summary["max_date"])

top = st.columns(4)
top[0].metric("日线行数", f"{summary['rows']:,}")
top[1].metric("股票数", f"{summary['codes']:,}")
top[2].metric("交易日", f"{summary['dates']:,}")
top[3].metric("日期范围", f"{summary['min_date']} - {summary['max_date']}")

strategies = strategy_repo.list_strategies()
strategy_options = {f"{row.name} · ID {row.id}": row.id for row in strategies}
if not strategy_options:
    st.warning("还没有保存策略，请先在策略生成页面保存一个 DSL。")
    st.stop()

st.subheader("时间范围与股票池")
d1, d2 = st.columns([1, 1])
from_date = d1.date_input("开始日期", value=min_d, min_value=min_d, max_value=max_d)
to_date = d2.date_input("结束日期", value=max_d, min_value=min_d, max_value=max_d)

p1, p2 = st.columns([1, 2])
pool_label = p1.selectbox("股票池", options=list(POOL_OPTIONS), index=0, help="动态池：成分股按指数快照生效；券池独立于基准")
pool_index_code = POOL_OPTIONS[pool_label]
code_text = p2.text_input(
    "指定代码（仅「全市场」或与池子取交集）",
    placeholder="如 600885.SH,002631.SZ；留空走池子规则",
)

l1, l2 = st.columns([1, 2])
run_all = l1.checkbox("加载全部股票", value=False)
max_codes = l2.slider(
    "加载股票数（仅全市场模式生效）",
    min_value=50,
    max_value=max(50, int(summary["codes"])),
    value=min(300, int(summary["codes"])),
    step=50,
)

if pool_index_code and (from_date and to_date):
    pool_fetched = st.session_state.get("pool_fetched")
    if st.button("📥 拉取/更新成分股", help=f"从 tushare index_weight 缓存 {pool_index_code} 成分股快照"):
        try:
            n = tushare_api.ensure_index_weights(
                pool_index_code,
                from_date.strftime("%Y%m%d"),
                to_date.strftime("%Y%m%d"),
            )
            st.session_state["pool_fetched"] = {"code": pool_index_code, "rows": n}
            st.success(f"成分股 {pool_index_code} 缓存完成（新增 {n} 行）。")
        except Exception as exc:  # noqa: BLE001
            st.error(f"拉取成分股失败：{exc}")
    if pool_fetched and pool_fetched.get("code") == pool_index_code:
        st.caption(f"成分股 {pool_fetched['code']} 上次拉取新增 {pool_fetched['rows']} 行。")

if st.button("🔍 预览股票池", help="按当前条件查询将加载的股票，不跑回测"):
    selected_input = code_text.strip() or None
    from_str = from_date.strftime("%Y%m%d")
    to_str = to_date.strftime("%Y%m%d")
    if pool_index_code:
        snaps = tushare_api.index_snapshot_dates(pool_index_code, from_str, to_str)
        if not snaps:
            st.warning(f"尚未缓存 {pool_index_code} 的成分股，请先点「拉取/更新成分股」。")
            st.stop()
        pool_map_preview = tushare_api.build_pool_map(
            pool_index_code,
            tushare_api.trading_days_between(from_str, to_str) or [from_str],
        )
        raw_codes = sorted({c for codes in pool_map_preview.values() for c in codes})
        if selected_input:
            user_codes = {c.strip() for c in selected_input.split(",") if c.strip()}
            raw_codes = [c for c in raw_codes if c in user_codes]
    elif selected_input:
        raw_codes = [c.strip() for c in selected_input.split(",") if c.strip()]
    else:
        limit = None if run_all else int(max_codes)
        raw_codes = discover_db_codes(
            str(DB_PATH),
            fromdate=from_date,
            todate=to_date,
            max_codes=limit,
        )
    stocks = _stock_options()
    pool_df = pd.DataFrame(raw_codes, columns=["ts_code"])
    if not stocks.empty:
        pool_df = pool_df.merge(stocks, on="ts_code", how="left")
    dropped = 0
    if not pool_df.empty:
        with get_session() as s:
            rows = (
                s.query(DailyBar.ts_code, func.min(DailyBar.trade_date))
                .filter(DailyBar.ts_code.in_(list(pool_df["ts_code"])))
                .group_by(DailyBar.ts_code)
                .all()
            )
        first_dates = {code: str(d) for code, d in rows}
        pool_df["首日"] = pool_df["ts_code"].map(first_dates)
        tradeable_mask = pool_df["首日"].astype(str) <= from_str
        dropped = int((~tradeable_mask).sum())
        pool_df = pool_df[tradeable_mask].copy()
    st.session_state["pool_preview"] = pool_df
    st.session_state["pool_dropped"] = dropped

preview = st.session_state.get("pool_preview")
if preview is not None:
    dropped = st.session_state.get("pool_dropped", 0)
    if preview.empty:
        st.warning("股票池为空，请检查条件。")
    else:
        msg = f"将加载 {len(preview)} 只股票"
        if dropped:
            msg += f"（已剔除 {dropped} 只新股，首日 > 开始日期）"
        st.success(msg)
        show_df = preview.rename(columns={"ts_code": "代码", "name": "名称"})
        show_df = show_df.reindex(columns=["代码", "名称", "首日"])
        st.dataframe(show_df, hide_index=True, width="stretch")

with st.form("bt_form"):
    default_label = next(
        (label for label, sid in strategy_options.items() if any(row.id == sid and row.active for row in strategies)),
        next(iter(strategy_options)),
    )
    strategy_label = st.selectbox(
        "回测策略",
        options=list(strategy_options),
        index=list(strategy_options).index(default_label),
    )
    selected_strategy_id = strategy_options[strategy_label]
    selected_dsl = strategy_repo.get_current_dsl(selected_strategy_id)
    if selected_dsl is None:
        st.error("所选策略没有可用 DSL 版本。")
        st.stop()
    st.caption(dsl_to_text(selected_dsl))

    c1, c2, c3, c4, c5 = st.columns(5)
    cash = c1.number_input("初始资金", min_value=1000.0, value=100000.0, step=10000.0, format="%.0f")
    commission = c2.number_input(
        "佣金比例（双边）",
        min_value=0.0, max_value=0.01, value=0.0003, step=0.0001, format="%.4f",
        help="买卖双向收取，A 股券商通常万二~万三",
    )
    stamp_duty = c3.number_input(
        "印花税（仅卖）",
        min_value=0.0, max_value=0.005, value=0.0005, step=0.0001, format="%.4f",
        help="卖出单边收取，2023-08 起 A 股 0.05%",
    )
    slippage = c4.number_input(
        "滑点比例",
        min_value=0.0, max_value=0.02, value=0.001, step=0.0005, format="%.4f",
        help="百分比滑点，模拟成交价偏离（涨跌停/低流动性更显著）。默认 0.1%",
    )
    cash_buffer = c5.number_input(
        "保护金比例",
        min_value=0.0, max_value=0.2, value=0.05, step=0.01, format="%.2f",
        help="始终保留不投入的现金比例。A 股次日跳空高开 + 佣金可能导致满仓后下一笔被 Margin 拒单，留 5% 缓冲。",
    )
    st.caption(f"策略回看 {selected_dsl.lookback} 日")

    # 交易规则：max_positions 由规则决定，不再单独暴露
    trade_rules = trade_rule_repo.list_trade_rules()
    rule_options = {f"📋 {r.name}": r.id for r in trade_rules}
    rule_options["使用内置默认（不保存）"] = None
    rule_label = st.selectbox(
        "交易规则",
        options=list(rule_options),
        index=0,
        help="持仓/退出规则（max_positions、止损、止盈、持股时间）。在「交易规则」页建规则。",
    )
    selected_rule_id = rule_options[rule_label]
    selected_rule_spec = None
    if selected_rule_id is not None:
        try:
            selected_rule_spec = trade_rule_repo.get_current_rule_spec(selected_rule_id)
            if selected_rule_spec:
                st.caption(f"规则：{rule_to_text(selected_rule_spec)}")
        except Exception as exc:  # noqa: BLE001
            st.warning(f"规则加载失败：{exc}")
    # 选了规则时：max_positions 由规则决定（run_dsl_backtest 内 strategy 从 rule 取）
    # 未选规则时：fallback 给 param.max_positions
    max_positions = selected_rule_spec.max_positions if selected_rule_spec else 5

    benchmark_label = st.selectbox("基准", options=list(BENCHMARK_OPTIONS), index=0)
    benchmark_code = BENCHMARK_OPTIONS[benchmark_label]

    submitted = st.form_submit_button("运行 Backtrader 回测", type="primary")

if benchmark_code and (from_date and to_date):
    fetched = st.session_state.get("bench_fetched")
    if st.button("📥 拉取/更新基准数据", help="从 tushare index_daily 补齐基准指数日线到本地缓存"):
        try:
            n = tushare_api.ensure_index_bars(
                benchmark_code,
                from_date.strftime("%Y%m%d"),
                to_date.strftime("%Y%m%d"),
            )
            st.session_state["bench_fetched"] = {"code": benchmark_code, "rows": n}
            st.success(f"基准 {benchmark_code} 缓存完成（新增 {n} 行）。")
        except Exception as exc:  # noqa: BLE001
            st.error(f"拉取基准失败：{exc}")
    if fetched and fetched.get("code") == benchmark_code:
        st.caption(f"基准 {fetched['code']} 上次拉取新增 {fetched['rows']} 行。")

if submitted:
    if from_date > to_date:
        st.error("开始日期不能晚于结束日期。")
        st.stop()

    selected_codes = code_text.strip() or None
    limit = None if run_all or selected_codes else int(max_codes)

    with st.spinner("Backtrader 回测运行中..."):
        try:
            trade_rule_dict = selected_rule_spec.model_dump() if selected_rule_spec else None
            # 选了规则时，max_positions 由规则决定，不透传避免重复（D1）
            kwargs = dict(
                strategy_id=selected_strategy_id,
                db_path=str(DB_PATH),
                codes=selected_codes,
                cash=float(cash),
                commission=float(commission),
                stamp_duty=float(stamp_duty),
                slippage=float(slippage),
                cash_buffer=float(cash_buffer),
                fromdate=_to_ts(from_date),
                todate=_to_ts(to_date),
                max_codes=limit,
                benchmark_code=benchmark_code,
                pool_index_code=pool_index_code,
                trade_rule_spec=trade_rule_dict,
                printlog=False,
            )
            if trade_rule_dict is None:
                kwargs["max_positions"] = int(max_positions)
            result = run_dsl_backtest(**kwargs)
        except Exception as exc:  # noqa: BLE001
            st.error(f"回测失败：{exc}")
            st.stop()

    st.session_state["bt_result"] = result
    st.session_state["bt_params"] = {
        "strategy": strategy_label,
        "from": from_date.strftime("%Y-%m-%d"),
        "to": to_date.strftime("%Y-%m-%d"),
        "codes": selected_codes or ("全部" if run_all else f"前 {limit} 只"),
        "benchmark": benchmark_label,
        "pool": pool_label,
    }

result = st.session_state.get("bt_result")
if not result:
    st.info("设置参数后运行回测。默认只加载前 300 只股票，确认流程后再跑全量。")
    st.stop()

params = st.session_state.get("bt_params", {})
loaded = result["loaded"]
counts = [count for _, count in loaded]
returns = _flatten(result["returns"])
drawdown = _flatten(result["drawdown"])
trades = _flatten(result["trades"])
orders = pd.DataFrame(result["orders"])
portfolio = pd.DataFrame(result["portfolio_value"])
signals = pd.DataFrame(result.get("signals", []))
metrics = result.get("metrics", {})
strategy_series = result.get("strategy_series", [])
benchmark_series = result.get("benchmark_series", [])
drawdown_series = result.get("drawdown_series", [])
monthly_returns = result.get("monthly_returns", [])
trade_pairs = pd.DataFrame(result.get("trade_pairs", []))
stock_summary = pd.DataFrame(result.get("stock_summary", []))

st.subheader("回测结果")
st.caption(
    f"{params.get('strategy')}；{params.get('from')} 至 {params.get('to')}；"
    f"范围：{params.get('codes')}；基准：{params.get('benchmark', '—')}；池：{params.get('pool', '—')}"
)

m1, m2, m3, m4, m5, m6 = st.columns(6)
start_value = result["start_value"]
end_value = result["end_value"]
total_return = (end_value / start_value - 1.0) if start_value else 0.0
m1.metric("起始资金", f"{start_value:,.0f}")
m2.metric("期末资金", f"{end_value:,.0f}", f"{total_return * 100:+.2f}%")
m3.metric("年化收益", _fmt_pct(metrics.get("annual_return")))
m4.metric("最大回撤", _fmt_pct(-abs(metrics.get("max_drawdown", 0))))
m5.metric("夏普", _fmt_num(metrics.get("sharpe"), 3))
m6.metric("Sortino", _fmt_num(metrics.get("sortino"), 3))

m7, m8, m9, m10, m11, m12 = st.columns(6)
m7.metric("Calmar", _fmt_num(metrics.get("calmar"), 3))
m8.metric("胜率", _fmt_pct(metrics.get("win_rate")))
m9.metric("盈亏比", _fmt_num(metrics.get("profit_loss_ratio"), 2))
m10.metric("平均持仓(天)", _fmt_num(metrics.get("avg_hold_days"), 1))
m11.metric("交易次数", f"{int(metrics.get('total_trades', 0))}")
m12.metric("命中信号", f"{int(result.get('total_signals', 0)):,}")

st.caption(
    "载入 bar 数：min=%s, max=%s, >=70根=%s"
    % (
        min(counts) if counts else 0,
        max(counts) if counts else 0,
        sum(1 for count in counts if count >= 70),
    )
)

if not portfolio.empty:
    st.plotly_chart(_equity_fig(strategy_series, benchmark_series), width="stretch")
else:
    st.warning("没有组合净值序列。")

if drawdown_series:
    st.plotly_chart(_drawdown_fig(drawdown_series), width="stretch")

heat_col, hist_col = st.columns(2)
with heat_col:
    st.caption("月度收益热力图")
    st.plotly_chart(_monthly_heatmap(monthly_returns), width="stretch")
with hist_col:
    st.caption("每日收益分布")
    st.plotly_chart(_daily_hist(strategy_series), width="stretch")

tab_orders, tab_pairs, tab_signals, tab_contrib, tab_feeds, tab_raw = st.tabs(
    ["成交记录", "交易对", "每日信号", "持仓贡献", "载入股票", "指标原始值"]
)

with tab_orders:
    if orders.empty:
        st.caption("没有成交记录。")
    else:
        if "status" not in orders.columns:
            orders["status"] = "Completed"
        else:
            orders["status"] = orders["status"].fillna("Completed")
        show = orders.rename(columns={
            "date": "日期", "code": "代码", "side": "方向", "size": "数量",
            "price": "价格", "value": "成交额", "commission": "佣金", "status": "状态",
        })
        st.dataframe(show, hide_index=True, width="stretch")

with tab_pairs:
    if trade_pairs.empty:
        st.caption("没有可配对的交易。")
    else:
        pairs_show = trade_pairs.copy()
        pairs_show["状态"] = pairs_show["sell_date"].isna().map({True: "未平仓", False: "已平仓"})
        pairs_show = pairs_show.rename(columns={
            "code": "代码", "buy_date": "买入日", "buy_price": "买入价",
            "sell_date": "卖出日", "sell_price": "卖出价", "size": "数量",
            "pnl": "盈亏", "pnl_pct": "盈亏%", "hold_days": "持仓天",
        })
        st.dataframe(pairs_show, hide_index=True, width="stretch")

with tab_signals:
    if signals.empty:
        st.caption("没有每日信号记录。")
    else:
        signal_show = signals.rename(columns={"date": "日期", "matched": "命中数量", "targets": "目标持仓"})
        st.dataframe(signal_show, hide_index=True, width="stretch")

with tab_contrib:
    if stock_summary.empty:
        st.caption("没有已平仓个股。")
    else:
        contrib_show = stock_summary.copy()
        contrib_show["贡献%"] = (contrib_show["contribution_pct"] * 100).round(2)
        contrib_show["总盈亏"] = contrib_show["total_pnl"].round(2)
        contrib_show["平均持仓天"] = contrib_show["avg_hold_days"].round(1)
        contrib_show = contrib_show[["code", "name", "trades", "总盈亏", "平均持仓天", "贡献%"]].rename(columns={
            "code": "代码", "name": "名称", "trades": "交易次数",
        })
        st.plotly_chart(_contribution_bar(result.get("stock_summary", [])), width="stretch")
        st.dataframe(contrib_show, hide_index=True, width="stretch")

with tab_feeds:
    feed_df = pd.DataFrame(loaded, columns=["代码", "bar数量"])
    stocks = _stock_options()
    if not stocks.empty:
        feed_df = feed_df.merge(stocks.rename(columns={"ts_code": "代码", "name": "名称"}), on="代码", how="left")
        feed_df = feed_df[["代码", "名称", "bar数量"]]
    st.dataframe(feed_df, hide_index=True, width="stretch")

with tab_raw:
    st.json({
        "metrics": metrics,
        "returns": returns,
        "drawdown": drawdown,
        "trades": trades,
        "calmar": _flatten(result.get("calmar")),
        "annual_return": _flatten(result.get("annual_return")),
        "period_stats": _flatten(result.get("period_stats")),
        "benchmark_code": result.get("benchmark_code"),
        "signal_days": result.get("signal_days", 0),
        "total_signals": result.get("total_signals", 0),
    })
