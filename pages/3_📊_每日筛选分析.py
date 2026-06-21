"""📊 每日筛选分析页：选数据时间点运行策略，填买入价，看 5 日跟踪与归档。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import plotly.graph_objects as go
import streamlit as st

from core import backtest, strategy_repo, tushare_api
from core.db import get_session
from core.models import AnalysisRun, Match, Stock

st.set_page_config(page_title="每日筛选分析", page_icon="📊", layout="wide")
st.title("📊 每日筛选分析")


def _to_yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _stock_name(ts_code: str) -> str:
    with get_session() as s:
        row = s.query(Stock).filter_by(ts_code=ts_code).first()
        return row.name if row else ""


def _kline_fig(ts_code: str, trade_dates: list[str], title: str) -> go.Figure:
    import pandas as pd

    bars = tushare_api.load_bars(trade_dates)
    bars = bars[bars["ts_code"] == ts_code].sort_values("trade_date")
    if bars.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{title}（无数据）", height=360)
        return fig
    bars["label"] = bars["trade_date"].astype(str)
    fig = go.Figure(
        data=[
            go.Candlestick(
                x=bars["label"], open=bars["open"], high=bars["high"],
                low=bars["low"], close=bars["close"], increasing_line_color="red",
                decreasing_line_color="green",
            )
        ]
    )
    fig.update_layout(title=title, xaxis_rangeslider_visible=False, height=360,
                      margin=dict(l=20, r=20, t=40, b=20))
    return fig


def _render_match(m: Match, lookback_dates: list[str]) -> None:
    name = _stock_name(m.ts_code)
    header = f"{m.ts_code}  {name}"
    with st.expander(header):
        tc1, tc2 = st.columns([2, 3])
        with tc1:
            st.plotly_chart(_kline_fig(m.ts_code, lookback_dates, header), use_container_width=True)
        with tc2:
            # 买入价录入 + 跟踪表
            df = backtest.tracking_df(m.id)
            st.markdown("**5 交易日跟踪**")
            show = df.rename(columns={
                "offset": "T", "trade_date": "交易日", "close": "收盘",
                "day_pct": "当日涨跌%", "cum_pct": "累计涨跌%",
            })
            show["T"] = show["T"].map(lambda x: f"T+{x}")
            st.dataframe(show[["T", "交易日", "收盘", "当日涨跌%", "累计涨跌%"]], hide_index=True, use_container_width=True)

            with st.form(f"buy_{m.id}"):
                cur = float(df["buy_price"].iloc[0]) if not df.empty and df["buy_price"].iloc[0] else 0.0
                price = st.number_input("当日购入价格", min_value=0.0, value=cur, step=0.01, format="%.2f", key=f"np_{m.id}")
                if st.form_submit_button("保存买入价"):
                    if price > 0:
                        backtest.update_buy_price(m.id, float(price))
                        backtest.fill_and_recompute(m.id)
                        st.toast("已保存买入价并重算")
                        st.rerun()
                    else:
                        st.warning("请输入大于 0 的价格")


# 页面加载时懒计算补齐已到交易日的收盘
try:
    backtest.fill_and_recompute()
except Exception as e:  # noqa: BLE001
    st.warning(f"跟踪数据刷新失败（可能 tushare 未配置）：{e}")

tab_run, tab_history, tab_archive = st.tabs(["🚀 运行分析", "📜 历史运行", "🗄️ 归档（按购买日期）"])

# ---------------- Tab 1: 运行分析 ----------------
with tab_run:
    strategies = strategy_repo.list_strategies()
    if not strategies:
        st.info("还没有策略，请先到「🎯 策略生成」页创建。")
    else:
        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            options = {f"{s.id}: {s.name}{' 🟢' if s.active else ''}": s.id for s in strategies}
            label = st.selectbox("选择策略", list(options.keys()))
            strategy_id = options[label]
        with col2:
            today = date.today()
            default_d = today - timedelta(days=1)
            snap = st.date_input("数据时间点（交易日）", value=default_d, max_value=today, help="选某个交易日的收盘数据进行分析")
            snapshot_date = _to_yyyymmdd(snap)
        with col3:
            st.write("")
            st.write("")
            run_btn = st.button("🚀 运行分析", type="primary")

        if run_btn:
            try:
                with st.spinner("抓取日K并匹配中（首次较慢，已缓存会很快）..."):
                    run = backtest.run_analysis(strategy_id, snapshot_date)
                st.session_state["last_run_id"] = run.id
                st.success(f"完成：匹配 {run.matched_count} 只（run #{run.id}）")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"运行失败：{e}")

        # 展示最近一次运行的匹配
        run_id = st.session_state.get("last_run_id")
        if run_id is None:
            with get_session() as s:
                last = s.query(AnalysisRun).order_by(AnalysisRun.id.desc()).first()
                run_id = last.id if last else None
        if run_id:
            matches = backtest.matches_for_run(run_id)
            st.subheader(f"匹配结果（run #{run_id}，共 {len(matches)} 只）")
            if not matches:
                st.caption("无匹配股票。")
            else:
                dsl = strategy_repo.get_current_dsl(strategy_id) if strategy_id else None
                lookback = dsl.lookback if dsl else 30
                lookback_dates = tushare_api.get_lookback_dates(matches[0].snapshot_date, lookback)
                # 概览表
                overview = [{"代码": m.ts_code, "名称": _stock_name(m.ts_code)} for m in matches]
                st.dataframe(overview, hide_index=True, use_container_width=True)
                for m in matches:
                    _render_match(m, lookback_dates)

# ---------------- Tab 2: 历史运行 ----------------
with tab_history:
    with get_session() as s:
        runs = s.query(AnalysisRun).order_by(AnalysisRun.id.desc()).limit(50).all()
        data = [
            {
                "run": r.id, "策略ID": r.strategy_id, "分析交易日": r.snapshot_date,
                "匹配数": r.matched_count, "状态": r.status, "运行于": r.created_at,
            }
            for r in runs
        ]
    if not data:
        st.info("还没有运行记录。")
    else:
        import pandas as pd

        st.dataframe(pd.DataFrame(data), hide_index=True, use_container_width=True)
        chosen = st.number_input("查看 run 编号", min_value=1, value=data[0]["run"], step=1)
        if st.button("加载该 run 的匹配与跟踪"):
            st.session_state["last_run_id"] = int(chosen)
            st.rerun()

# ---------------- Tab 3: 归档 ----------------
with tab_archive:
    df = backtest.archived_by_buy_date()
    if df.empty:
        st.info("还没有录入买入价的记录。运行分析并在匹配结果里填入「当日购入价格」后，这里按购买日期归档展示。")
    else:
        for buy_date, group in df.groupby("buy_date"):
            with st.expander(f"📅 {buy_date}  ·  {len(group)} 只"):
                g = group.drop(columns=["buy_date"])
                st.dataframe(g, hide_index=True, use_container_width=True)
