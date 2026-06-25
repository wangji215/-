"""交易规则仓库：CRUD、5 版本上限、回滚。

仿 core/strategy_repo.py 范式，去掉 active / MAX_ACTIVE（trade_rule 无启用上限概念）。
- 每条规则最多保留 5 个版本；新增第 6 版时删除最旧版并把其余 version_no 前移。
- 回滚 = 把目标版本的 spec 拷为「新版本」写入（保留历史轨迹），并置为当前。
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
    with get_session() as s:
        rule = TradeRule(name=name, description=description or "")
        s.add(rule)
        s.flush()
        v = TradeRuleVersion(rule_id=rule.id, version_no=1, spec_json=spec_json)
        s.add(v)
        s.flush()
        rule.current_version_id = v.id
        s.commit()
        s.refresh(rule)
        _ = rule.name
        return rule


def add_version(rule_id: int, spec) -> TradeRuleVersion:
    """新增版本：超过 MAX_VERSIONS 则淘汰最旧并把后续前移。"""
    spec_json = _normalize_spec(spec)
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
        v = TradeRuleVersion(rule_id=rule_id, version_no=next_no, spec_json=spec_json)
        s.add(v)
        s.flush()
        rule.current_version_id = v.id
        s.commit()
        s.refresh(v)
        _ = v.spec_json
        return v


def rollback_to(rule_id: int, version_id: int) -> TradeRuleVersion:
    """把指定版本的 spec 作为新版本写入并置为当前（保留历史）。"""
    with get_session() as s:
        target = s.query(TradeRuleVersion).filter_by(id=version_id).first()
        if not target or target.rule_id != rule_id:
            raise TradeRuleError("目标版本不存在")
    return add_version(rule_id, target.spec_json)


def delete_trade_rule(rule_id: int) -> None:
    with get_session() as s:
        s.query(TradeRuleVersion).filter_by(rule_id=rule_id).delete()
        s.query(TradeRule).filter_by(id=rule_id).delete()
        s.commit()
