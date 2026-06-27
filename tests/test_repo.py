"""数据层与策略仓库冒烟测试。直接运行：python tests/test_repo.py

验证：建表、创建策略、5 版本上限淘汰、回滚、启用上限(3)、删除。
测试结束会清理自身创建的数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import strategy_repo as R  # noqa: E402
from core.db import get_session, init_db  # noqa: E402
from core.models import Strategy  # noqa: E402
from core.strategy_dsl import StrategyDSL  # noqa: E402

init_db()

created_ids: list[int] = []
TEST_NAME_PREFIXES = ("版本测试", "启用测试", "DSL往返")


def _cleanup_test_strategies() -> None:
    with get_session() as s:
        ids = [
            st.id for st in s.query(Strategy).all()
            if st.name.startswith(TEST_NAME_PREFIXES)
        ]
    for sid in ids:
        try:
            R.delete_strategy(sid)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolate_repo_tests():
    _cleanup_test_strategies()
    yield
    _cleanup_test_strategies()


def _spec(period: int):
    return {
        "name": f"测试{period}",
        "lookback": 30 + period,
        "conditions": [{
            "logic": "and",
            "rules": [
                {"left": {"ind": "MA", "period": period}, "op": ">", "right": {"ind": "MA", "period": period + 5}}
            ],
        }],
    }


def test_version_limit_and_rollback():
    st = R.create_strategy("版本测试", "验证 5 版上限与回滚", _spec(5))
    created_ids.append(st.id)
    # 加到第 6 版
    for p in range(6, 12):
        R.add_version(st.id, _spec(p))
    versions = R.get_versions(st.id)
    assert len(versions) == R.MAX_VERSIONS, f"应保留 {R.MAX_VERSIONS} 版，实际 {len(versions)}"
    # 回滚到最旧版本：指针切换，不新建版本
    oldest = min(versions, key=lambda v: v.version_no)
    rolled = R.rollback_to(st.id, oldest.id)
    assert rolled.id == oldest.id, "回滚应返回目标版本本身（指针切换），而非新建版本"
    after = R.get_versions(st.id)
    assert len(after) == len(versions), "回滚不应改变版本数量"
    # 当前指针已切到 oldest，spec 一致
    assert R.get_strategy(st.id).current_version_id == oldest.id
    assert R.get_current_spec(st.id) == oldest.spec_json
    # 父表 description 同步为该版本说明
    assert R.get_strategy(st.id).description == (oldest.description or "")
    print(f"[ok] test_version_limit_and_rollback（保留 {len(after)} 版，指针回滚成功）")


def test_update_and_delete_version():
    st = R.create_strategy("版本测试UD", "更新删除", _spec(5))
    created_ids.append(st.id)
    R.add_version(st.id, _spec(6), description="v2说明")
    R.add_version(st.id, _spec(7), description="v3说明")
    by_no = {v.version_no: v for v in R.get_versions(st.id)}
    assert len(by_no) == 3
    assert by_no[2].description == "v2说明", "每个版本应自带 description"
    # 更新中间版本（v2，非当前）：改 description + spec
    R.update_version(by_no[2].id, spec=_spec(16), description="v2改后")
    refreshed = {v.version_no: v for v in R.get_versions(st.id)}
    assert refreshed[2].description == "v2改后"
    # spec 校验：非法 spec 应抛错
    try:
        R.update_version(by_no[2].id, spec="not-a-json")
        raise AssertionError("应拒绝非法 spec")
    except R.StrategyError:
        pass
    # 删除中间版本（v2），剩余按 version_no 重编号为 1..N
    R.delete_version(st.id, by_no[2].id)
    after = R.get_versions(st.id)
    assert len(after) == 2
    assert sorted(v.version_no for v in after) == [1, 2]
    # 不能删唯一版本
    st_single = R.create_strategy("版本测试UD2", "单版", _spec(5))
    created_ids.append(st_single.id)
    only = R.get_versions(st_single.id)[0]
    try:
        R.delete_version(st_single.id, only.id)
        raise AssertionError("应拒绝删除唯一版本")
    except R.StrategyError:
        pass
    # 不能删当前版本
    cur_id = R.get_strategy(st.id).current_version_id
    try:
        R.delete_version(st.id, cur_id)
        raise AssertionError("应拒绝删除当前版本")
    except R.StrategyError:
        pass
    print("[ok] test_update_and_delete_version")


def test_active_limit():
    ids = []
    for i in range(R.MAX_ACTIVE):
        s = R.create_strategy(f"启用测试{i}", "", _spec(5 + i))
        created_ids.append(s.id)
        ids.append(s.id)
        R.set_active(s.id, True)
    active = R.active_strategies()
    assert len(active) == R.MAX_ACTIVE
    # 第 4 条应启用失败
    extra = R.create_strategy("启用测试溢出", "", _spec(99))
    created_ids.append(extra.id)
    try:
        R.set_active(extra.id, True)
        raise AssertionError("应触发启用上限异常")
    except R.StrategyError:
        pass
    # 停用一条后可启用溢出那条
    R.set_active(ids[0], False)
    R.set_active(extra.id, True)
    assert len(R.active_strategies()) == R.MAX_ACTIVE
    print("[ok] test_active_limit")


def test_dsl_roundtrip():
    st = R.create_strategy("DSL往返", "", _spec(7))
    created_ids.append(st.id)
    dsl = R.get_current_dsl(st.id)
    assert isinstance(dsl, StrategyDSL)
    assert dsl.lookback == 37
    print("[ok] test_dsl_roundtrip")


if __name__ == "__main__":
    try:
        test_version_limit_and_rollback()
        test_active_limit()
        test_dsl_roundtrip()
        test_update_and_delete_version()
        print("\n全部数据层测试通过 ✅")
    finally:
        for sid in created_ids:
            try:
                R.delete_strategy(sid)
            except Exception:
                pass
        print("（已清理测试数据）")
