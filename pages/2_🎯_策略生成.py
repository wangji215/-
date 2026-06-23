"""🎯 策略生成页：自然语言对话生成图形策略，管理版本与启用。"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import streamlit as st

from core import llm_client, strategy_repo, tushare_api
from core import strategy_ref
from core.strategy_draft import StrategyGenerationResult
from core.strategy_dsl import StrategyDSL, dsl_to_text

st.set_page_config(page_title="策略生成", page_icon="🎯", layout="wide")
st.title("🎯 策略生成")

# 会话状态：对话历史、待保存 DSL
if "dlg" not in st.session_state:
    st.session_state.dlg = []  # [{"role":"user"/"assistant","content":...}]
if "pending_dsl" not in st.session_state:
    st.session_state.pending_dsl = None
if "pending_generation" not in st.session_state:
    st.session_state.pending_generation = None
if "pending_warning" not in st.session_state:
    st.session_state.pending_warning = ""
if "pending_raw" not in st.session_state:
    st.session_state.pending_raw = ""
if "last_reference" not in st.session_state:
    st.session_state.last_reference = None


def _from_ymd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _render_generation_preview(generation: StrategyGenerationResult | None) -> None:
    if generation is None:
        return
    draft = generation.draft
    st.markdown("**分段理解**")
    for i, seg in enumerate(draft.segments, 1):
        st.markdown(
            f"{i}. **{seg.name}** `{seg.role}` "
            f"[{seg.window.start}, {seg.window.end}]  \n"
            f"意图：{seg.intent or '未填写'}  \n"
            f"技术描述：{seg.technical_description or '未填写'}"
        )


left, right = st.columns([3, 2])

# ---------------- 左：对话 ----------------
with left:
    st.subheader("对话生成")
    st.caption("描述你想要的 K 线图形策略，模型会产出可执行 DSL。需求不清时会先追问。")
    if st.button("清空对话", help="清除当前对话历史和待保存 DSL，避免模型沿用上一轮旧策略。"):
        st.session_state.dlg = []
        st.session_state.pending_dsl = None
        st.session_state.pending_generation = None
        st.session_state.pending_warning = ""
        st.session_state.pending_raw = ""
        st.session_state.last_reference = None
        st.rerun()

    # 参考样本股（可选）：代码 + 自定义时间段 + 形态契合买点
    with st.expander("🔬 参考样本股（可选）", expanded=False):
        st.text_input(
            "代码（逗号分隔，如 600519,300750,贵州茅台）",
            key="sample_codes_input",
            placeholder="留空则不注入参考；填写后取其近期指标注入 prompt",
        )
        latest_cached = tushare_api.latest_cached_trade_date(date.today().strftime("%Y%m%d"))
        max_d = _from_ymd(latest_cached) if latest_cached else date.today()
        d_end = max_d
        d_start = max_d - timedelta(days=10)
        period = st.date_input(
            "参考时间段（小于10个交易日）",
            value=(d_start, d_end),
            max_value=max_d,
            help="框定一段近期行情作参考；超过上限会提示并跳过窗口参考。",
        )
        if isinstance(period, (tuple, list)) and len(period) == 2:
            p_start, p_end = period
        elif isinstance(period, date):
            p_start = p_end = period
        else:
            p_start = p_end = None

        win_dates: list[str] = []
        too_long = False
        if p_start and p_end:
            win_dates = tushare_api.trading_days_between(
                p_start.strftime("%Y%m%d"), p_end.strftime("%Y%m%d"), refresh=False
            )
            too_long = len(win_dates) > strategy_ref.MAX_WINDOW
        if too_long:
            st.warning(
                f"时间段含 {len(win_dates)} 个交易日，超过 {strategy_ref.MAX_WINDOW} 个上限；"
                f"请缩小范围，否则本次不注入窗口参考。"
            )
        if win_dates and not too_long:
            st.selectbox(
                "★ 形态契合买点",
                options=win_dates,
                index=len(win_dates) // 2,
                key="buy_date_sel",
                help="标注窗口内哪一天是理想买点；模型会以其当日形态为目标态优化策略。",
            )

    if st.session_state.get("last_reference"):
        with st.expander("本次注入的参考数据", expanded=False):
            st.text(st.session_state.last_reference)

    # 历史回放
    for msg in st.session_state.dlg:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 待保存的 DSL 预览
    if st.session_state.pending_dsl is not None:
        dsl: StrategyDSL = st.session_state.pending_dsl
        with st.chat_message("assistant", avatar="🧩"):
            st.success("已生成策略 DSL，确认无误后保存为版本：")
            _render_generation_preview(st.session_state.pending_generation)
            if st.session_state.pending_warning:
                st.warning(st.session_state.pending_warning)
            st.markdown(dsl_to_text(dsl))
            with st.expander("查看原始 JSON"):
                st.code(dsl.model_dump_json(indent=2), language="json")
            c1, c2 = st.columns([1, 1])
            name = c1.text_input("策略名称", value=dsl.name, key="save_name")
            desc = c2.text_input("策略说明", value=dsl.description, key="save_desc")
            cs1, cs2 = st.columns([1, 1])
            if cs1.button("💾 保存为新版本", type="primary"):
                # 若已有同名策略则加版本，否则新建
                existing = [s for s in strategy_repo.list_strategies() if s.name == name]
                try:
                    if existing:
                        strategy_repo.add_version(existing[0].id, dsl)
                        strategy_repo.get_strategy(existing[0].id)  # touch
                        # 更新说明
                        st.toast(f"已为「{name}」新增版本")
                    else:
                        strategy_repo.create_strategy(name, desc, dsl)
                        st.toast(f"已创建策略「{name}」")
                    st.session_state.pending_dsl = None
                    st.session_state.pending_generation = None
                    st.session_state.pending_warning = ""
                    st.rerun()
                except strategy_repo.StrategyError as e:
                    st.error(str(e))
            if cs2.button("✏️ 继续修改"):
                st.session_state.pending_dsl = None
                st.session_state.pending_generation = None
                st.session_state.pending_warning = ""
                st.rerun()

    user_input = st.chat_input("描述你的图形策略，例如：均线多头排列且MACD金叉...")
    if user_input:
        # 解析参考样本股（支持中英文逗号/空格/分号分隔）
        raw_codes = [c for c in re.split(r"[,\s，;；]+", st.session_state.get("sample_codes_input", "")) if c.strip()]
        # 决定窗口/买点：窗口超限或为空则退回默认模式（仅代码，60 日末根）；
        # 超限时连代码也不注入（等同不注入参考）。
        window_valid = bool(win_dates) and not too_long
        if window_valid:
            ref_start, ref_end = win_dates[0], win_dates[-1]
            ref_buy = st.session_state.get("buy_date_sel") or win_dates[len(win_dates) // 2]
        else:
            ref_start = ref_end = ref_buy = None
        codes_for_ref = None if too_long else (raw_codes or None)
        try:
            st.session_state.last_reference = (
                strategy_ref.build_sample_reference(
                    raw_codes, start=ref_start, end=ref_end, buy_date=ref_buy
                )
                if codes_for_ref else None
            )
        except tushare_api.TushareError as e:
            st.warning(f"参考样本股已跳过：{e}")
            st.session_state.last_reference = None
            codes_for_ref = None
        st.session_state.dlg.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)
        with st.chat_message("assistant"):
            with st.spinner("模型思考中..."):
                raw, dsl, generation, err = llm_client.generate_strategy(
                    st.session_state.dlg,
                    sample_codes=codes_for_ref,
                    ref_start=ref_start, ref_end=ref_end, ref_buy=ref_buy,
                )
        # 记录助手回复（若有 DSL，回放里展示可读说明而非裸 JSON）
        if dsl is not None:
            st.session_state.dlg.append({"role": "assistant", "content": dsl_to_text(dsl)})
            st.session_state.pending_dsl = dsl
            st.session_state.pending_generation = generation
            st.session_state.pending_warning = err or ""
            st.session_state.pending_raw = raw
        else:
            st.session_state.dlg.append({"role": "assistant", "content": raw})
        if err and dsl is None:
            st.error(f"生成失败：{err}")
        elif err:
            st.warning(err)
        st.rerun()

# ---------------- 右：策略管理 ----------------
with right:
    st.subheader("策略库")
    active = strategy_repo.active_strategies()
    st.caption(f"已启用 **{len(active)}/{strategy_repo.MAX_ACTIVE}** 条（每日分析会对全部启用策略运行）")

    strategies = strategy_repo.list_strategies()
    if not strategies:
        st.info("还没有策略，先在左侧对话生成一个。")

    for st_row in strategies:
        with st.expander(f"{'🟢' if st_row.active else '⚪'} {st_row.name}  ·  {st_row.description or '无说明'}"):
            spec = strategy_repo.get_current_spec(st_row.id)
            try:
                dsl_cur = StrategyDSL.model_validate_json(spec) if spec else None
                if dsl_cur:
                    st.markdown(dsl_to_text(dsl_cur))
            except Exception as e:  # noqa: BLE001
                st.warning(f"当前版本 DSL 解析失败：{e}")

            versions = strategy_repo.get_versions(st_row.id)
            st.markdown(f"**历史版本（最多 {strategy_repo.MAX_VERSIONS}）：**")
            for v in versions:
                is_cur = (st_row.current_version_id == v.id)
                tag = "（当前）" if is_cur else ""
                col_a, col_b = st.columns([3, 1])
                col_a.caption(f"v{v.version_no}{tag}  ·  {v.created_at.strftime('%Y-%m-%d %H:%M')}")
                if col_b.button("回滚", key=f"rb_{v.id}", disabled=is_cur, help="把该版本置为当前（作为新版本写入）"):
                    try:
                        strategy_repo.rollback_to(st_row.id, v.id)
                        st.toast("已回滚")
                        st.rerun()
                    except strategy_repo.StrategyError as e:
                        st.error(str(e))

            bc1, bc2, bc3 = st.columns(3)
            if bc1.button("启用" if not st_row.active else "停用", key=f"tg_{st_row.id}",
                          type="primary" if not st_row.active else "secondary"):
                try:
                    strategy_repo.set_active(st_row.id, not st_row.active)
                    st.rerun()
                except strategy_repo.StrategyError as e:
                    st.error(str(e))
            if bc2.button("复制JSON", key=f"cp_{st_row.id}") and spec:
                st.code(json.dumps(json.loads(spec), ensure_ascii=False, indent=2), language="json")
            if bc3.button("删除", key=f"dl_{st_row.id}"):
                strategy_repo.delete_strategy(st_row.id)
                st.rerun()
