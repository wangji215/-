"""⏰ 交易规则页：持仓/退出策略的结构化表单配置。

与策略 DSL 解耦：
- 入场信号仍由策略 DSL（pages/2）管
- 本页只管持仓管理 / 止损止盈 / 持股时间等执行层规则
- 回测时选规则应用，规则可跨策略复用
"""
from __future__ import annotations

import json

import streamlit as st

from core import trade_rule_repo
from core.db import init_db
from core.trade_rule_dsl import TradeRuleSpec, rule_to_text

init_db()

st.set_page_config(page_title="交易规则", page_icon="⏰", layout="wide")
st.title("⏰ 交易规则")
st.caption(
    "配置持仓/退出规则（最多同时持几只、止损/止盈、最大持股时间）。"
    "回测页选规则后，规则驱动卖出决策；规则与策略 DSL 解耦，可跨策略复用。"
)

left, right = st.columns([3, 2])


# ---------------- 左：编辑表单 ----------------
with left:
    st.subheader("新建 / 编辑")
    with st.form("rule_form"):
        name = st.text_input("名称", placeholder="如：保守 / 激进 / 默认")
        description = st.text_input("说明", placeholder="可选")

        st.markdown("**仓位**")
        c1, c2 = st.columns(2)
        max_positions = c1.number_input("最多同时持有", min_value=1, max_value=50, value=5, step=1, help="回测时同时持有的最大股票数")
        position_pct = c2.slider("单只仓位 %", min_value=1, max_value=100, value=20, step=1,
                                 help="等权时建议 = 100/max_positions")

        st.markdown("**持股时间**")
        c3, c4 = st.columns(2)
        max_holding_days = c3.number_input("最大持股（交易日）", min_value=1, max_value=250, value=10, step=1,
                                           help="到则强平（time_stop 开启时生效）")
        time_stop = c4.checkbox("到期强平", value=True, help="到达最大持股日时自动卖出")

        st.markdown("**止损止盈**（勾选「关闭」表示不启用；用正数 magnitude）")
        c5, c6, c7, c8 = st.columns(4)
        sl_off = c5.checkbox("止损关闭", value=False)
        stop_loss_pct = c6.number_input("止损 %", min_value=0.0, max_value=50.0, value=5.0, step=0.5,
                                        disabled=sl_off, help="跌幅达到该值时卖出")
        tp_off = c7.checkbox("止盈关闭", value=False)
        take_profit_pct = c8.number_input("止盈 %", min_value=0.0, max_value=200.0, value=10.0, step=1.0,
                                          disabled=tp_off, help="涨幅达到该值时卖出")

        st.markdown(
            "**执行时点**（⚠️ 回测忽略这两个字段——日线数据无日内价，"
            "所有订单统一按 backtrader 默认撮合：T 日 close 决策 → T+1 日 open 成交。"
            "字段仅记录实盘意图，未来接实盘时用。）"
        )
        c9, c10 = st.columns(2)
        buy_time = c9.text_input("买入时间 HH:MM", value="09:35")
        sell_time = c10.text_input("卖出时间 HH:MM", value="14:55")

        save_clicked = st.form_submit_button("保存（新建或新增版本）")

    if save_clicked:
        if not name.strip():
            st.error("请填写名称")
        else:
            spec_dict = {
                "max_positions": int(max_positions),
                "position_pct": float(position_pct) / 100.0,
                "max_holding_days": int(max_holding_days),
                "stop_loss_pct": None if sl_off else float(stop_loss_pct),
                "take_profit_pct": None if tp_off else float(take_profit_pct),
                "time_stop": bool(time_stop),
                "buy_time": buy_time.strip(),
                "sell_time": sell_time.strip(),
            }
            try:
                spec = TradeRuleSpec(**spec_dict)
                existing = [r for r in trade_rule_repo.list_trade_rules() if r.name == name.strip()]
                if existing:
                    trade_rule_repo.add_version(existing[0].id, spec)
                    st.toast(f"已为「{name}」新增版本")
                else:
                    rule = trade_rule_repo.create_trade_rule(name.strip(), description, spec)
                    st.toast(f"已创建规则 #{rule.id}")
                st.rerun()
            except trade_rule_repo.TradeRuleError as e:
                st.error(str(e))
            except Exception as e:  # noqa: BLE001
                st.error(f"保存失败：{e}")


# ---------------- 右：规则库 ----------------
with right:
    st.subheader("规则库")
    rules = trade_rule_repo.list_trade_rules()
    if not rules:
        st.info("还没有规则。先在左侧建一条，回测页才能选具体规则（不建则用内置默认）。")

    for r in rules:
        with st.expander(f"📋 {r.name}  ·  {r.description or '无说明'}"):
            spec_json = trade_rule_repo.get_current_spec(r.id)
            try:
                spec = TradeRuleSpec.model_validate_json(spec_json) if spec_json else None
                if spec:
                    st.markdown(rule_to_text(spec))
                    with st.expander("查看 JSON"):
                        st.code(json.dumps(json.loads(spec_json), ensure_ascii=False, indent=2), language="json")
            except Exception as e:  # noqa: BLE001
                st.warning(f"当前版本解析失败：{e}")

            versions = trade_rule_repo.get_versions(r.id)
            st.markdown(f"**历史版本（最多 {trade_rule_repo.MAX_VERSIONS}）：**")
            for v in versions:
                is_cur = (r.current_version_id == v.id)
                tag = "（当前）" if is_cur else ""
                ca, cb = st.columns([3, 1])
                ca.caption(f"v{v.version_no}{tag}  ·  {v.created_at.strftime('%Y-%m-%d %H:%M')}")
                if cb.button("回滚", key=f"rbr_{v.id}", disabled=is_cur, help="把该版本置为当前（作为新版本写入）"):
                    try:
                        trade_rule_repo.rollback_to(r.id, v.id)
                        st.toast("已回滚")
                        st.rerun()
                    except trade_rule_repo.TradeRuleError as e:
                        st.error(str(e))

            if st.button("删除", key=f"rdl_{r.id}"):
                trade_rule_repo.delete_trade_rule(r.id)
                st.rerun()
