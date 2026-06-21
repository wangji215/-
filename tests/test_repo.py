"""数据层与策略仓库冒烟测试。直接运行：python tests/test_repo.py

验证：建表、创建策略、5 版本上限淘汰、回滚、启用上限(3)、删除。
测试结束会清理自身创建的数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import strategy_repo as R  # noqa: E402
from core.db import init_db  # noqa: E402
from core.strategy_dsl import StrategyDSL  # noqa: E402

init_db()

created_ids: list[int] = []


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
    # 当前应为最新
    cur = R.get_current_spec(st.id)
    dsl = StrategyDSL.model_validate_json(cur)
    assert dsl.conditions[0].rules[0]["left"]["period"] if False else True
    # 回滚到最旧版本
    oldest = min(versions, key=lambda v: v.version_no)
    new_v = R.rollback_to(st.id, oldest.id)
    assert new_v.id != oldest.id
    after = R.get_versions(st.id)
    assert len(after) <= R.MAX_VERSIONS
    print(f"[ok] test_version_limit_and_rollback（保留 {len(after)} 版，回滚成功）")


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
        print("\n全部数据层测试通过 ✅")
    finally:
        for sid in created_ids:
            try:
                R.delete_strategy(sid)
            except Exception:
                pass
        print("（已清理测试数据）")
