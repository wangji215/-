"""交易规则仓库冒烟测试。直接运行：python tests/test_trade_rule_repo.py

验证：建表、创建规则、5 版本上限淘汰、回滚、删除、字段往返。
测试结束清理自身创建的数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import trade_rule_repo as R  # noqa: E402
from core.db import get_session, init_db  # noqa: E402
from core.models import TradeRule  # noqa: E402
from core.trade_rule_dsl import TradeRuleSpec  # noqa: E402

init_db()

TEST_NAME_PREFIXES = ("版本测试", "字段测试")

created_ids: list[int] = []


def _cleanup_test_rules() -> None:
    with get_session() as s:
        ids = [
            r.id for r in s.query(TradeRule).all()
            if r.name.startswith(TEST_NAME_PREFIXES)
        ]
    for rid in ids:
        try:
            R.delete_trade_rule(rid)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolate_rule_tests():
    _cleanup_test_rules()
    yield
    _cleanup_test_rules()


def _spec(max_positions: int = 5, stop: float | None = 5.0):
    return {
        "max_positions": max_positions,
        "position_pct": round(1.0 / max_positions, 4),
        "max_holding_days": 10,
        "stop_loss_pct": stop,
        "take_profit_pct": 10.0,
        "time_stop": True,
        "buy_time": "09:35",
        "sell_time": "14:55",
    }


def test_version_limit_and_rollback():
    rule = R.create_trade_rule("版本测试", "验证 5 版上限与回滚", _spec(5))
    created_ids.append(rule.id)
    # 加到第 6 版（含初始 v1，共 11 次写入）
    for n in range(6, 12):
        R.add_version(rule.id, _spec(n))
    versions = R.get_versions(rule.id)
    assert len(versions) == R.MAX_VERSIONS, f"应保留 {R.MAX_VERSIONS} 版，实际 {len(versions)}"
    # 当前版本 max_positions 应为最新写入值
    cur = R.get_current_rule_spec(rule.id)
    assert cur.max_positions == 11
    # 回滚到最旧：指针切换，不新建版本
    oldest = min(versions, key=lambda v: v.version_no)
    rolled = R.rollback_to(rule.id, oldest.id)
    assert rolled.id == oldest.id, "回滚应返回目标版本本身（指针切换），而非新建版本"
    after = R.get_versions(rule.id)
    assert len(after) == len(versions), "回滚不应改变版本数量"
    assert R.get_trade_rule(rule.id).current_version_id == oldest.id
    assert R.get_current_spec(rule.id) == oldest.spec_json
    print(f"[ok] test_version_limit_and_rollback（保留 {len(after)} 版，指针回滚成功）")


def test_update_and_delete_version():
    rule = R.create_trade_rule("版本测试UD", "更新删除", _spec(5))
    created_ids.append(rule.id)
    R.add_version(rule.id, _spec(6), description="v2说明")
    R.add_version(rule.id, _spec(7), description="v3说明")
    by_no = {v.version_no: v for v in R.get_versions(rule.id)}
    assert len(by_no) == 3
    assert by_no[2].description == "v2说明", "每个版本应自带 description"
    # 更新中间版本（v2，非当前）：改 description + spec
    R.update_version(by_no[2].id, spec=_spec(16), description="v2改后")
    refreshed = {v.version_no: v for v in R.get_versions(rule.id)}
    assert refreshed[2].description == "v2改后"
    assert R.get_current_rule_spec(rule.id).max_positions == 7, "更新非当前版本不应改变当前"
    # spec 校验：非法 spec 应抛错
    try:
        R.update_version(by_no[2].id, spec={"buy_time": "25:00"})
        raise AssertionError("应拒绝非法 spec")
    except R.TradeRuleError:
        pass
    # 删除中间版本（v2），剩余重编号
    R.delete_version(rule.id, by_no[2].id)
    after = R.get_versions(rule.id)
    assert len(after) == 2
    assert sorted(v.version_no for v in after) == [1, 2]
    # 不能删唯一版本
    rule_single = R.create_trade_rule("版本测试UD2", "单版", _spec(5))
    created_ids.append(rule_single.id)
    only = R.get_versions(rule_single.id)[0]
    try:
        R.delete_version(rule_single.id, only.id)
        raise AssertionError("应拒绝删除唯一版本")
    except R.TradeRuleError:
        pass
    # 不能删当前版本
    cur_id = R.get_trade_rule(rule.id).current_version_id
    try:
        R.delete_version(rule.id, cur_id)
        raise AssertionError("应拒绝删除当前版本")
    except R.TradeRuleError:
        pass
    print("[ok] test_update_and_delete_version")


def test_field_roundtrip():
    rule = R.create_trade_rule("字段测试", "", _spec(7, stop=None))
    created_ids.append(rule.id)
    spec = R.get_current_rule_spec(rule.id)
    assert isinstance(spec, TradeRuleSpec)
    assert spec.max_positions == 7
    assert spec.stop_loss_pct is None  # None 往返
    assert spec.take_profit_pct == 10.0
    # 校验非法 HH:MM 被拒
    try:
        R.add_version(rule.id, {**_spec(8), "buy_time": "25:00"})
        raise AssertionError("应触发 HH:MM 校验失败")
    except R.TradeRuleError:
        pass
    print("[ok] test_field_roundtrip")


if __name__ == "__main__":
    try:
        test_version_limit_and_rollback()
        test_field_roundtrip()
        test_update_and_delete_version()
        print("\n全部交易规则测试通过 ✅")
    finally:
        for rid in created_ids:
            try:
                R.delete_trade_rule(rid)
            except Exception:
                pass
        print("（已清理测试数据）")
