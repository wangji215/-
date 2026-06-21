"""📊 每日筛选分析页：选数据时间点运行策略，填买入价，看 5 日跟踪与归档。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
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


def _name_map(ts_codes: list[str]) -> dict:
    """批量解析 ts_code → 名称，避免逐只开 session。"""
    if not ts_codes:
        return {}
    with get_session() as s:
        rows = s.query(Stock).filter(Stock.ts_code.in_(ts_codes)).all()
    return {r.ts_code: r.name for r in rows}


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


def _render_match(m: Match, lookback_dates: list[str], tracked: bool, name: str = "") -> None:
    if not name:
        name = _stock_name(m.ts_code)
    header = f"{m.ts_code}  {name}"
    with st.expander(header):
        tc1, tc2 = st.columns([2, 3])
        with tc1:
            st.plotly_chart(_kline_fig(m.ts_code, lookback_dates, header), use_container_width=True)
        with tc2:
            if not tracked:
                st.caption("未跟踪 — 在上方表格勾选并点「📊 交易日分析」开始跟踪")
                return
            # 已跟踪：买入价录入 + 跟踪表
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
                # 名称 / 策略名批量解析；已跟踪集合；代码→match_id 映射
                name_map = _name_map([m.ts_code for m in matches])
                strat_map = {s.id: s.name for s in strategy_repo.list_strategies()}
                tracked = backtest.tracked_match_ids(run_id)
                code_to_mid = {m.ts_code: m.id for m in matches}

                dsl = strategy_repo.get_current_dsl(strategy_id) if strategy_id else None
                lookback = dsl.lookback if dsl else 30
                lookback_dates = tushare_api.get_lookback_dates(matches[0].snapshot_date, lookback)

                # 概览（可勾选）+ 交易日分析按钮
                ov = pd.DataFrame([
                    {
                        "选择": False,
                        "代码": m.ts_code,
                        "名称": name_map.get(m.ts_code, ""),
                        "匹配策略": strat_map.get(m.strategy_id, str(m.strategy_id) if m.strategy_id else ""),
                        "状态": "已跟踪" if m.id in tracked else "未跟踪",
                    }
                    for m in matches
                ])
                edited = st.data_editor(
                    ov,
                    column_config={"选择": st.column_config.CheckboxColumn("勾选跟踪", default=False)},
                    disabled=[c for c in ov.columns if c != "选择"],
                    hide_index=True,
                    key=f"sel_{run_id}",
                    use_container_width=True,
                )
                _, btn_col = st.columns([4, 1])
                if btn_col.button("📊 交易日分析", type="primary",
                                  help="对勾选的股票开启购入价/交易日跟踪（T+0..T+5）"):
                    sel_codes = edited.loc[edited["选择"], "代码"].tolist()
                    sel_ids = [code_to_mid[c] for c in sel_codes if c in code_to_mid]
                    if sel_ids:
                        n = backtest.start_tracking(run_id, sel_ids)
                        st.toast(f"已对 {n} 只开启跟踪")
                        st.rerun()
                    else:
                        st.warning("请先在表中勾选至少一只股票。")

                # 逐只详情：K线始终显示；已跟踪才显跟踪表 + 买入价表单
                for m in matches:
                    _render_match(m, lookback_dates, m.id in tracked, name_map.get(m.ts_code, ""))

# ---------------- Tab 2: 历史运行 ----------------
with tab_history:
    strat_map = {s.id: s.name for s in strategy_repo.list_strategies()}
    with get_session() as s:
        runs = s.query(AnalysisRun).order_by(AnalysisRun.id.desc()).limit(50).all()
        data = [
            {
                "run": r.id, "策略": strat_map.get(r.strategy_id, str(r.strategy_id) if r.strategy_id else ""),
                "分析交易日": r.snapshot_date, "匹配数": r.matched_count,
                "状态": r.status, "运行于": r.created_at,
            }
            for r in runs
        ]
    if not data:
        st.info("还没有运行记录。")
    else:
        st.dataframe(pd.DataFrame(data), hide_index=True, use_container_width=True)
        chosen = st.number_input("查看 run 编号", min_value=1, value=data[0]["run"], step=1)
        if st.button("加载该 run 的匹配与跟踪"):
            st.session_state["last_run_id"] = int(chosen)
            st.rerun()

# ---------------- Tab 3: 归档 ----------------
with tab_archive:
    df = backtest.archive_summary()
    if df.empty:
        st.info("还没有跟踪记录——运行分析后，在匹配结果里勾选股票并点「📊 交易日分析」，这里按购入日期 + 策略归档展示。")
    else:
        for buy_date, group in df.groupby("购入日期"):
            with st.expander(f"📅 {buy_date}  ·  {len(group)} 只"):
                st.dataframe(group.drop(columns=["购入日期"]), hide_index=True, use_container_width=True)
