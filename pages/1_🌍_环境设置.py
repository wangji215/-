"""🌍 环境设置页：网络 / tushare / 大模型 / 定时 配置与连通性测试。"""
from __future__ import annotations

import streamlit as st

from core import llm_client, tushare_api
from core.config import load_settings, save_settings

st.set_page_config(page_title="环境设置", page_icon="🌍", layout="wide")
st.title("🌍 环境设置")

cfg = load_settings()


def _save_and_rerun(data: dict) -> None:
    save_settings(data)
    st.toast("已保存")


# ---------------- 网络 ----------------
st.subheader("1. 网络设置")
with st.form("net_form"):
    c1, c2 = st.columns(2)
    http_proxy = c1.text_input("HTTP 代理", value=cfg.get("http_proxy", ""), placeholder="http://127.0.0.1:7890")
    https_proxy = c2.text_input("HTTPS 代理", value=cfg.get("https_proxy", ""), placeholder="http://127.0.0.1:7890")
    if st.form_submit_button("保存网络设置"):
        _save_and_rerun({"http_proxy": http_proxy, "https_proxy": https_proxy})

# ---------------- tushare ----------------
st.subheader("2. tushare 数据接口")
with st.form("ts_form"):
    token = st.text_input("tushare token", value=cfg.get("tushare_token", ""),
                          type="password", help="https://tushare.pro 注册获取")
    c1, c2 = st.columns(2)
    enabled = c1.checkbox("启用 tushare", value=cfg.get("tushare_enabled", "true") == "true")
    rpm = c2.number_input("每分钟最大调用数（限流）", min_value=0, max_value=2000,
                          value=int(cfg.get("tushare_rate_per_min", "200") or 200), step=10)
    saved_ts = st.form_submit_button("保存 tushare 设置")
    if saved_ts:
        _save_and_rerun({
            "tushare_token": token,
            "tushare_enabled": "true" if enabled else "false",
            "tushare_rate_per_min": str(int(rpm)),
        })

tc1, tc2 = st.columns(2)
if tc1.button("🔌 测试 tushare 连接"):
    if not cfg.get("tushare_token") and not token:
        st.error("请先填写并保存 token")
    else:
        with st.spinner("测试中..."):
            res = tushare_api.test_connection()
        if res["ok"]:
            st.success(f"连接成功，在交易股票约 {res['count']} 只")
        else:
            st.error(f"连接失败：{res['error']}")
if tc2.button("🔄 刷新股票列表缓存"):
    with st.spinner("抓取全市场股票列表..."):
        try:
            n = tushare_api.refresh_stocks()
            st.success(f"已缓存 {n} 只股票")
        except Exception as e:  # noqa: BLE001
            st.error(f"失败：{e}")

# ---------------- 数据初始化（回测前置） ----------------
with st.expander("📥 回测数据初始化（按日期范围批量缓存）", expanded=False):
    st.caption(
        "新机器或首次跑 Backtrader 回测前用。逐项补齐：交易日历 → 全市场 daily_bars（后复权 hfq）→ "
        "指数日线（基准）→ 指数成分股权重（动态股票池）。幂等，已缓存的会跳过。"
    )
    bc1, bc2 = st.columns(2)
    bar_from = bc1.text_input("起始日 YYYYMMDD", value="20240601", key="bar_from")
    bar_to = bc2.text_input("结束日 YYYYMMDD", value="20260624", key="bar_to")

    bt_idx = st.text_input(
        "指数 ts_code 列表",
        value="000300.SH,000016.SH,399006.SZ,000905.SH",
        help="逗号分隔；前两个用于基准/池，可任意子集",
    )

    bc3, bc4, bc5 = st.columns(3)
    if bc3.button("📅 拉交易日历", help="fetch_trade_cal，秒级"):
        try:
            n = tushare_api.fetch_trade_cal(bar_from, bar_to)
            st.success(f"交易日历缓存 {n} 行")
        except Exception as e:  # noqa: BLE001
            st.error(f"失败：{e}")

    if bc4.button("📊 拉全市场 daily_bars", help="最重的一步，~2 次/交易日（日线+复权因子），1 年约 500 次调用；后复权 hfq"):
        try:
            days = tushare_api.trading_days_between(bar_from, bar_to)
            if not days:
                st.warning("无交易日，请先拉交易日历")
            else:
                with st.spinner(f"补 {len(days)} 个交易日（每秒 ~3 天）..."):
                    n = tushare_api.ensure_bars_for_dates(days)
                st.success(f"补齐 {n} 个交易日的全市场日线（后复权）")
        except Exception as e:  # noqa: BLE001
            st.error(f"失败：{e}")

    if bc5.button("📈 拉指数日线", help="基准用；4 个指数共 4 次调用"):
        try:
            codes = [c.strip() for c in bt_idx.split(",") if c.strip()]
            msgs = []
            for code in codes:
                n = tushare_api.ensure_index_bars(code, bar_from, bar_to)
                msgs.append(f"{code}: +{n}")
            st.success("；".join(msgs))
        except Exception as e:  # noqa: BLE001
            st.error(f"失败：{e}")

    st.markdown("**🔄 切换复权 / 修正历史**：先清空 daily_bars 再按后复权重抓（首次切换复权方式时必须执行一次，否则新旧数据混存会出错）")
    with st.popover("🔄 清空并按后复权重拉", use_container_width=True):
        st.warning("会先删除全部 daily_bars 再按【后复权】重新抓取，耗时较长。")
        if st.button("确认：清空并重拉", type="primary", key="clear_refetch_bars"):
            try:
                days = tushare_api.trading_days_between(bar_from, bar_to)
                if not days:
                    st.error("无交易日，请先拉交易日历")
                else:
                    cleared = tushare_api.clear_daily_bars()
                    with st.spinner(f"已清空 {cleared:,} 行，重抓 {len(days)} 个交易日（每秒 ~3 天）..."):
                        n = tushare_api.ensure_bars_for_dates(days)
                    st.success(f"已清空旧数据，按后复权重抓 {n} 个交易日")
            except Exception as e:  # noqa: BLE001
                st.error(f"失败：{e}")

    if st.button("🗂 拉指数成分股权重", help="动态池用；每指数 1 次调用，秒级"):
        try:
            codes = [c.strip() for c in bt_idx.split(",") if c.strip()]
            msgs = []
            for code in codes:
                n = tushare_api.ensure_index_weights(code, bar_from, bar_to)
                msgs.append(f"{code}: +{n}")
            st.success("；".join(msgs))
        except Exception as e:  # noqa: BLE001
            st.error(f"失败：{e}")

    st.caption(
        "CLI 等效（适合 CI/批量初始化）：\n"
        "`python -m scripts.bootstrap_backtest_data --from 20240601 --to 20260624`"
    )

# ---------------- 大模型 ----------------
st.subheader("3. 大模型（OpenAI 兼容接口）")
with st.form("llm_form"):
    c1, c2 = st.columns(2)
    base_url = c1.text_input("Base URL", value=cfg.get("llm_base_url", ""))
    model = c2.text_input("模型名", value=cfg.get("llm_model", ""))
    api_key = st.text_input("API Key", value=cfg.get("llm_api_key", ""), type="password")
    c3, c4 = st.columns(2)
    temp = c3.slider("temperature", 0.0, 1.5, float(cfg.get("llm_temperature", "0.2") or 0.2), 0.1)
    max_tokens = c4.number_input("max_tokens", min_value=256, max_value=8192,
                                 value=int(cfg.get("llm_max_tokens", "2000") or 2000), step=128)
    saved_llm = st.form_submit_button("保存大模型设置")
    if saved_llm:
        _save_and_rerun({
            "llm_base_url": base_url, "llm_model": model, "llm_api_key": api_key,
            "llm_temperature": str(temp), "llm_max_tokens": str(int(max_tokens)),
        })

with st.expander("常见平台配置参考"):
    st.markdown(
        """
        | 平台 | Base URL | 模型示例 |
        |---|---|---|
        | DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
        | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
        | 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
        | Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
        | OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
        | 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5` |
        """
    )

if st.button("🔌 测试大模型对话"):
    if not (cfg.get("llm_api_key") or api_key):
        st.error("请先填写并保存 API Key")
    else:
        with st.spinner("请求中..."):
            res = llm_client.test_connection()
        if res["ok"]:
            st.success(f"回复：{res['reply']}")
        else:
            st.error(f"失败：{res['error']}")

# ---------------- 定时 ----------------
st.subheader("4. 每日自动分析")
with st.form("sched_form"):
    c1, c2 = st.columns(2)
    daily_enabled = c1.checkbox("启用每日自动运行", value=cfg.get("daily_run_enabled", "true") == "true")
    daily_time = c2.text_input("运行时间 HH:MM", value=cfg.get("daily_run_time", "18:30"))
    if st.form_submit_button("保存定时设置"):
        _save_and_rerun({"daily_run_enabled": "true" if daily_enabled else "false", "daily_run_time": daily_time})
        # 重新应用每日任务（幂等；若未启动则按当前开关决定是否生效）
        try:
            from core import scheduler
            scheduler.start_if_enabled()
        except Exception as e:  # noqa: BLE001
            st.warning(f"调度器更新失败：{e}")
st.caption(
    "说明：应用开启时由 APScheduler 按时对全部启用策略运行分析；"
    "若 Streamlit 未常开，可用 `python -m scripts.run_daily` 配合 Windows 任务计划程序兜底（详见 README）。"
)
