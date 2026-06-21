"""🎯 策略生成页：自然语言对话生成图形策略，管理版本与启用。"""
from __future__ import annotations

import json

import streamlit as st

from core import llm_client, strategy_repo
from core.strategy_dsl import StrategyDSL, dsl_to_text

st.set_page_config(page_title="策略生成", page_icon="🎯", layout="wide")
st.title("🎯 策略生成")

# 会话状态：对话历史、待保存 DSL
if "dlg" not in st.session_state:
    st.session_state.dlg = []  # [{"role":"user"/"assistant","content":...}]
if "pending_dsl" not in st.session_state:
    st.session_state.pending_dsl = None
if "pending_raw" not in st.session_state:
    st.session_state.pending_raw = ""

left, right = st.columns([3, 2])

# ---------------- 左：对话 ----------------
with left:
    st.subheader("对话生成")
    st.caption("描述你想要的 K 线图形策略，模型会产出可执行 DSL。需求不清时会先追问。")

    # 历史回放
    for msg in st.session_state.dlg:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 待保存的 DSL 预览
    if st.session_state.pending_dsl is not None:
        dsl: StrategyDSL = st.session_state.pending_dsl
        with st.chat_message("assistant", avatar="🧩"):
            st.success("已生成策略 DSL，确认无误后保存为版本：")
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
                    st.rerun()
                except strategy_repo.StrategyError as e:
                    st.error(str(e))
            if cs2.button("✏️ 继续修改"):
                st.session_state.pending_dsl = None
                st.rerun()

    user_input = st.chat_input("描述你的图形策略，例如：均线多头排列且MACD金叉...")
    if user_input:
        st.session_state.dlg.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)
        with st.chat_message("assistant"):
            with st.spinner("模型思考中..."):
                raw, dsl, err = llm_client.generate_strategy(st.session_state.dlg)
        # 记录助手回复（若有 DSL，回放里展示可读说明而非裸 JSON）
        if dsl is not None:
            st.session_state.dlg.append({"role": "assistant", "content": dsl_to_text(dsl)})
            st.session_state.pending_dsl = dsl
            st.session_state.pending_raw = raw
        else:
            st.session_state.dlg.append({"role": "assistant", "content": raw})
        if err:
            st.error(f"生成失败：{err}")
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
