"""交易规则仓库：CRUD、5 版本上限、回滚。

仿 core/strategy_repo.py 范式，去掉 active / MAX_ACTIVE（trade_rule 无启用上限概念）。
- 每条规则最多保留 5 个版本；新增第 6 版时删除最旧版并把其余 version_no 前移。
- 回滚 = 直接把 current_version_id 指针切到目标版本（不新建版本、不改 version_no）。
- 每个版本自带 description；父表 TradeRule.description 始终同步为「当前版本」的说明。
"""
from __future__ import annotations

import json
from typing import List, Optional

from pydantic import ValidationError

from core.db import get_session
from core.models import TradeRule, TradeRuleVersion
from core.trade_rule_dsl import TradeRuleSpec

MAX_VERSIONS = 5


class TradeRuleError(RuntimeError):
    pass


def _normalize_spec(spec) -> str:
    """接受 dict / TradeRuleSpec / JSON 字符串，统一成紧凑 JSON 字符串。"""
    if isinstance(spec, TradeRuleSpec):
        return spec.model_dump_json()
    if isinstance(spec, dict):
        try:
            return TradeRuleSpec(**spec).model_dump_json()
        except ValidationError as e:
            raise TradeRuleError(f"spec 字段非法：{e}") from e
    if isinstance(spec, str):
        try:
            return TradeRuleSpec.model_validate_json(spec).model_dump_json()
        except (ValidationError, ValueError) as e:
            raise TradeRuleError(f"spec 不是合法的 TradeRuleSpec JSON：{e}") from e
    raise TradeRuleError(f"不支持的 spec 类型: {type(spec)}")


def list_trade_rules() -> List[TradeRule]:
    with get_session() as s:
        rows = s.query(TradeRule).order_by(TradeRule.created_at.desc()).all()
        for r in rows:
            _ = r.name
        return rows


def get_trade_rule(rule_id: int) -> Optional[TradeRule]:
    with get_session() as s:
        row = s.query(TradeRule).filter_by(id=rule_id).first()
        if row:
            _ = row.name
        return row


def get_versions(rule_id: int) -> List[TradeRuleVersion]:
    with get_session() as s:
        rows = (
            s.query(TradeRuleVersion)
            .filter_by(rule_id=rule_id)
            .order_by(TradeRuleVersion.version_no.desc())
            .all()
        )
        for r in rows:
            _ = r.spec_json
        return rows


def get_current_spec(rule_id: int) -> Optional[str]:
    rule = get_trade_rule(rule_id)
    if not rule or not rule.current_version_id:
        return None
    with get_session() as s:
        v = s.query(TradeRuleVersion).filter_by(id=rule.current_version_id).first()
        return v.spec_json if v else None


def get_current_rule_spec(rule_id: int) -> Optional[TradeRuleSpec]:
    spec = get_current_spec(rule_id)
    if not spec:
        return None
    return TradeRuleSpec.model_validate_json(spec)


def create_trade_rule(name: str, description: str, spec) -> TradeRule:
    spec_json = _normalize_spec(spec)
    desc = description or ""
    with get_session() as s:
        rule = TradeRule(name=name, description=desc)
        s.add(rule)
        s.flush()
        v = TradeRuleVersion(rule_id=rule.id, version_no=1, spec_json=spec_json, description=desc)
        s.add(v)
        s.flush()
        rule.current_version_id = v.id
        s.commit()
        s.refresh(rule)
        _ = rule.name
        return rule


def add_version(rule_id: int, spec, description: Optional[str] = None) -> TradeRuleVersion:
    """新增版本：超过 MAX_VERSIONS 则淘汰最旧并把后续前移。

    description 为 None 时默认空串（TradeRuleSpec 无 description 字段）。新版本成为当前版本，
    父表 TradeRule.description 同步为该版本说明。
    """
    spec_json = _normalize_spec(spec)
    desc = (description or "") if description is not None else ""
    with get_session() as s:
        rule = s.query(TradeRule).filter_by(id=rule_id).first()
        if not rule:
            raise TradeRuleError("交易规则不存在")
        versions = (
            s.query(TradeRuleVersion)
            .filter_by(rule_id=rule_id)
            .order_by(TradeRuleVersion.version_no.asc())
            .all()
        )
        if len(versions) >= MAX_VERSIONS:
            oldest = versions[0]
            if rule.current_version_id == oldest.id:
                raise TradeRuleError("当前正在使用最旧版本，无法自动淘汰，请先回滚到较新版本")
            s.delete(oldest)
            s.flush()
            versions = versions[1:]
            for i, v in enumerate(versions, start=1):
                v.version_no = i
            s.flush()
        next_no = (versions[-1].version_no + 1) if versions else 1
        v = TradeRuleVersion(rule_id=rule_id, version_no=next_no, spec_json=spec_json, description=desc)
        s.add(v)
        s.flush()
        rule.current_version_id = v.id
        rule.description = desc
        s.commit()
        s.refresh(v)
        _ = v.spec_json
        return v


def rollback_to(rule_id: int, version_id: int) -> TradeRuleVersion:
    """把当前版本指针切到目标版本（不新建版本、不改 version_no），并同步父表说明。"""
    with get_session() as s:
        rule = s.query(TradeRule).filter_by(id=rule_id).first()
        if not rule:
            raise TradeRuleError("交易规则不存在")
        target = s.query(TradeRuleVersion).filter_by(id=version_id).first()
        if not target or target.rule_id != rule_id:
            raise TradeRuleError("目标版本不存在")
        rule.current_version_id = target.id
        rule.description = target.description or ""
        s.commit()
        s.refresh(target)
        _ = target.spec_json
        return target


def update_version(version_id: int, spec=None, description: Optional[str] = None) -> TradeRuleVersion:
    """就地更新某版本的 spec 和/或 description。

    spec 非 None 时经 _normalize_spec 校验后覆盖；description 非 None 时覆盖。
    若该版本是某规则的当前版本，同步父表 TradeRule.description。
    """
    spec_json = _normalize_spec(spec) if spec is not None else None
    with get_session() as s:
        v = s.query(TradeRuleVersion).filter_by(id=version_id).first()
        if not v:
            raise TradeRuleError("版本不存在")
        if spec_json is not None:
            v.spec_json = spec_json
        if description is not None:
            v.description = description
        rule = s.query(TradeRule).filter_by(current_version_id=version_id).first()
        if rule and (description is not None or spec_json is not None):
            rule.description = v.description or ""
        s.commit()
        s.refresh(v)
        _ = v.spec_json
        return v


def delete_version(rule_id: int, version_id: int) -> None:
    """删除某个非当前版本，并把剩余版本按 version_no 升序重编号为 1..N。

    至少保留 1 个版本；不能删除当前启用版本（请先回滚到其他版本）。
    """
    with get_session() as s:
        rule = s.query(TradeRule).filter_by(id=rule_id).first()
        if not rule:
            raise TradeRuleError("交易规则不存在")
        v = s.query(TradeRuleVersion).filter_by(id=version_id).first()
        if not v or v.rule_id != rule_id:
            raise TradeRuleError("版本不存在")
        versions = (
            s.query(TradeRuleVersion)
            .filter_by(rule_id=rule_id)
            .order_by(TradeRuleVersion.version_no.asc())
            .all()
        )
        if len(versions) <= 1:
            raise TradeRuleError("至少保留一个版本，无法删除")
        if rule.current_version_id == version_id:
            raise TradeRuleError("不能删除当前启用版本，请先回滚到其他版本")
        s.delete(v)
        s.flush()
        remaining = (
            s.query(TradeRuleVersion)
            .filter_by(rule_id=rule_id)
            .order_by(TradeRuleVersion.version_no.asc())
            .all()
        )
        for i, rv in enumerate(remaining, start=1):
            rv.version_no = i
        s.commit()


def delete_trade_rule(rule_id: int) -> None:
    with get_session() as s:
        s.query(TradeRuleVersion).filter_by(rule_id=rule_id).delete()
        s.query(TradeRule).filter_by(id=rule_id).delete()
        s.commit()
