"""策略与版本仓库：CRUD、5 版本上限、回滚、启用管理。

- 每条策略最多保留 5 个版本；新增第 6 版时删除最旧版并把其余 version_no 前移。
- 回滚 = 把目标版本的 spec 拷为「新版本」写入（保留历史轨迹），并置为当前。
- 启用上限 3 条（MAX_ACTIVE）。
"""
from __future__ import annotations

import json
from typing import List, Optional

from sqlalchemy import func

from core.db import get_session
from core.models import Strategy, StrategyVersion
from core.strategy_dsl import StrategyDSL

MAX_VERSIONS = 5
MAX_ACTIVE = 3


class StrategyError(RuntimeError):
    pass


def _normalize_spec(spec) -> str:
    """接受 dict / StrategyDSL / JSON 字符串，统一成紧凑 JSON 字符串。"""
    if isinstance(spec, StrategyDSL):
        return spec.model_dump_json()
    if isinstance(spec, dict):
        return json.dumps(spec, ensure_ascii=False)
    if isinstance(spec, str):
        # 规范化：解析再序列化，便于比较与存储
        try:
            return json.dumps(json.loads(spec), ensure_ascii=False)
        except json.JSONDecodeError:
            raise StrategyError("spec 不是合法 JSON")
    raise StrategyError(f"不支持的 spec 类型: {type(spec)}")


def list_strategies() -> List[Strategy]:
    with get_session() as s:
        rows = s.query(Strategy).order_by(Strategy.created_at.desc()).all()
        # 触发加载，避免分离后访问
        for r in rows:
            _ = r.name
        return rows


def get_strategy(strategy_id: int) -> Optional[Strategy]:
    with get_session() as s:
        row = s.query(Strategy).filter_by(id=strategy_id).first()
        if row:
            _ = row.name
        return row


def get_versions(strategy_id: int) -> List[StrategyVersion]:
    with get_session() as s:
        rows = (
            s.query(StrategyVersion)
            .filter_by(strategy_id=strategy_id)
            .order_by(StrategyVersion.version_no.desc())
            .all()
        )
        for r in rows:
            _ = r.spec_json
        return rows


def get_current_spec(strategy_id: int) -> Optional[str]:
    st = get_strategy(strategy_id)
    if not st or not st.current_version_id:
        return None
    with get_session() as s:
        v = s.query(StrategyVersion).filter_by(id=st.current_version_id).first()
        return v.spec_json if v else None


def get_current_dsl(strategy_id: int) -> Optional[StrategyDSL]:
    spec = get_current_spec(strategy_id)
    if not spec:
        return None
    return StrategyDSL.model_validate_json(spec)


def create_strategy(name: str, description: str, spec) -> Strategy:
    spec_json = _normalize_spec(spec)
    with get_session() as s:
        st = Strategy(name=name, description=description or "", active=False)
        s.add(st)
        s.flush()
        v = StrategyVersion(strategy_id=st.id, version_no=1, spec_json=spec_json)
        s.add(v)
        s.flush()
        st.current_version_id = v.id
        s.commit()
        s.refresh(st)
        _ = st.name
        return st


def add_version(strategy_id: int, spec) -> StrategyVersion:
    """新增版本：超过 MAX_VERSIONS 则淘汰最旧并把后续前移。"""
    spec_json = _normalize_spec(spec)
    with get_session() as s:
        st = s.query(Strategy).filter_by(id=strategy_id).first()
        if not st:
            raise StrategyError("策略不存在")
        versions = (
            s.query(StrategyVersion)
            .filter_by(strategy_id=strategy_id)
            .order_by(StrategyVersion.version_no.asc())
            .all()
        )
        if len(versions) >= MAX_VERSIONS:
            # 删除最旧
            oldest = versions[0]
            if st.current_version_id == oldest.id:
                raise StrategyError("当前正在使用最旧版本，无法自动淘汰，请先回滚到较新版本")
            s.delete(oldest)
            s.flush()
            versions = versions[1:]
            # 前移 version_no
            for i, v in enumerate(versions, start=1):
                v.version_no = i
            s.flush()
        next_no = (versions[-1].version_no + 1) if versions else 1
        v = StrategyVersion(strategy_id=strategy_id, version_no=next_no, spec_json=spec_json)
        s.add(v)
        s.flush()
        st.current_version_id = v.id
        s.commit()
        s.refresh(v)
        _ = v.spec_json
        return v


def rollback_to(strategy_id: int, version_id: int) -> StrategyVersion:
    """把指定版本的 spec 作为新版本写入并置为当前（保留历史）。"""
    with get_session() as s:
        target = s.query(StrategyVersion).filter_by(id=version_id).first()
        if not target or target.strategy_id != strategy_id:
            raise StrategyError("目标版本不存在")
    return add_version(strategy_id, target.spec_json)


def set_active(strategy_id: int, active: bool) -> None:
    with get_session() as s:
        st = s.query(Strategy).filter_by(id=strategy_id).first()
        if not st:
            raise StrategyError("策略不存在")
        if active and not st.active:
            cnt = s.query(func.count(Strategy.id)).filter(Strategy.active.is_(True)).scalar() or 0
            if cnt >= MAX_ACTIVE:
                raise StrategyError(f"最多同时启用 {MAX_ACTIVE} 条策略，请先停用其他策略")
        st.active = bool(active)
        s.commit()


def active_strategies() -> List[Strategy]:
    with get_session() as s:
        rows = s.query(Strategy).filter(Strategy.active.is_(True)).all()
        for r in rows:
            _ = r.name
        return rows


def delete_strategy(strategy_id: int) -> None:
    with get_session() as s:
        s.query(StrategyVersion).filter_by(strategy_id=strategy_id).delete()
        s.query(Strategy).filter_by(id=strategy_id).delete()
        s.commit()
