"""策略与版本仓库：CRUD、5 版本上限、回滚、启用管理。

- 每条策略最多保留 5 个版本；新增第 6 版时删除最旧版并把其余 version_no 前移。
- 回滚 = 直接把 current_version_id 指针切到目标版本（不新建版本、不改 version_no）。
- 每个版本自带 description；父表 Strategy.description 始终同步为「当前版本」的说明。
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


def _extract_description(spec) -> str:
    """从 spec 提取 description：StrategyDSL 取 .description；dict/JSON 字符串取顶层 description。"""
    if isinstance(spec, StrategyDSL):
        return spec.description or ""
    if isinstance(spec, dict):
        return str(spec.get("description") or "")
    if isinstance(spec, str):
        try:
            return str(json.loads(spec).get("description") or "")
        except (ValueError, TypeError):
            return ""
    return ""


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
    desc = (description or _extract_description(spec)) or ""
    with get_session() as s:
        st = Strategy(name=name, description=desc, active=False)
        s.add(st)
        s.flush()
        v = StrategyVersion(strategy_id=st.id, version_no=1, spec_json=spec_json, description=desc)
        s.add(v)
        s.flush()
        st.current_version_id = v.id
        s.commit()
        s.refresh(st)
        _ = st.name
        return st


def add_version(strategy_id: int, spec, description: Optional[str] = None) -> StrategyVersion:
    """新增版本：超过 MAX_VERSIONS 则淘汰最旧并把后续前移。

    description 为 None 时从 spec 提取（StrategyDSL.description）。新版本成为当前版本，
    父表 Strategy.description 同步为该版本说明。
    """
    spec_json = _normalize_spec(spec)
    desc = description if description is not None else _extract_description(spec)
    desc = desc or ""
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
        v = StrategyVersion(strategy_id=strategy_id, version_no=next_no, spec_json=spec_json, description=desc)
        s.add(v)
        s.flush()
        st.current_version_id = v.id
        st.description = desc
        s.commit()
        s.refresh(v)
        _ = v.spec_json
        return v


def rollback_to(strategy_id: int, version_id: int) -> StrategyVersion:
    """把当前版本指针切到目标版本（不新建版本、不改 version_no），并同步父表说明。"""
    with get_session() as s:
        st = s.query(Strategy).filter_by(id=strategy_id).first()
        if not st:
            raise StrategyError("策略不存在")
        target = s.query(StrategyVersion).filter_by(id=version_id).first()
        if not target or target.strategy_id != strategy_id:
            raise StrategyError("目标版本不存在")
        st.current_version_id = target.id
        st.description = target.description or ""
        s.commit()
        s.refresh(target)
        _ = target.spec_json
        return target


def update_version(version_id: int, spec=None, description: Optional[str] = None) -> StrategyVersion:
    """就地更新某版本的 spec 和/或 description。

    spec 非 None 时经 _normalize_spec 校验后覆盖；description 非 None 时覆盖。
    若该版本是某策略的当前版本，同步父表 Strategy.description。
    """
    spec_json = _normalize_spec(spec) if spec is not None else None
    with get_session() as s:
        v = s.query(StrategyVersion).filter_by(id=version_id).first()
        if not v:
            raise StrategyError("版本不存在")
        if spec_json is not None:
            v.spec_json = spec_json
        if description is not None:
            v.description = description
        # 若是当前版本，同步父表说明
        st = s.query(Strategy).filter_by(current_version_id=version_id).first()
        if st and (description is not None or spec_json is not None):
            # 优先用新 description；否则维持原版本 description（spec 改了但说明没给时不变）
            st.description = v.description or ""
        s.commit()
        s.refresh(v)
        _ = v.spec_json
        return v


def delete_version(strategy_id: int, version_id: int) -> None:
    """删除某个非当前版本，并把剩余版本按 version_no 升序重编号为 1..N。

    至少保留 1 个版本；不能删除当前启用版本（请先回滚到其他版本）。
    """
    with get_session() as s:
        st = s.query(Strategy).filter_by(id=strategy_id).first()
        if not st:
            raise StrategyError("策略不存在")
        v = s.query(StrategyVersion).filter_by(id=version_id).first()
        if not v or v.strategy_id != strategy_id:
            raise StrategyError("版本不存在")
        versions = (
            s.query(StrategyVersion)
            .filter_by(strategy_id=strategy_id)
            .order_by(StrategyVersion.version_no.asc())
            .all()
        )
        if len(versions) <= 1:
            raise StrategyError("至少保留一个版本，无法删除")
        if st.current_version_id == version_id:
            raise StrategyError("不能删除当前启用版本，请先回滚到其他版本")
        s.delete(v)
        s.flush()
        remaining = (
            s.query(StrategyVersion)
            .filter_by(strategy_id=strategy_id)
            .order_by(StrategyVersion.version_no.asc())
            .all()
        )
        for i, rv in enumerate(remaining, start=1):
            rv.version_no = i
        s.commit()


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
