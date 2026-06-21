"""Streamlit 入口（主页）。

多页通过 ``pages/`` 目录自动发现。本文件负责初始化数据库、显示导航与首页。
"""
from __future__ import annotations

import streamlit as st

from core.db import init_db, DB_PATH
from core.config import load_settings

st.set_page_config(
    page_title="股票图形跟踪小助手",
    page_icon="📈",
    layout="wide",
)

init_db()


def main() -> None:
    st.title("📈 股票图形跟踪小助手")
    st.caption("用自然语言生成 K 线图形策略，每日从 A 股 ~6000 只股票中筛选并跟踪后 5 个交易日涨跌。")

    st.markdown(
        """
        **使用流程：**
        1. 左侧 **🌍 环境设置** —— 填入 tushare token 与大模型配置并测试连通性。
        2. **🎯 策略生成** —— 自然语言对话生成图形策略，保存版本并启用（最多同时 3 条）。
        3. **📊 每日筛选分析** —— 选数据时间点运行分析，填买入价，查看 5 日跟踪与归档。

        ---
        左侧边栏选择页面开始。
        """
    )

    # 启动应用内每日调度（进程内幂等；未启用则在配置中关闭）
    try:
        from core import scheduler
        if scheduler.start_if_enabled():
            st.session_state["scheduler_running"] = True
    except Exception as e:  # noqa: BLE001
        st.session_state["scheduler_error"] = str(e)

    with st.sidebar:
        st.divider()
        st.subheader("状态")
        cfg = load_settings()
        ts_ok = "✅ 已配置" if cfg.get("tushare_token") else "❌ 未配置"
        llm_ok = "✅ 已配置" if cfg.get("llm_api_key") else "❌ 未配置"
        st.write(f"tushare：{ts_ok}")
        st.write(f"大模型：{llm_ok}")
        sched_on = "运行中" if st.session_state.get("scheduler_running") else "未运行"
        st.write(f"每日定时：{sched_on}（{cfg.get('daily_run_time', '18:30')}）")
        if st.session_state.get("scheduler_error"):
            st.caption(f"⚠️ 调度器：{st.session_state['scheduler_error']}")
        st.caption(f"数据库：`{DB_PATH}`")


if __name__ == "__main__":
    main()
