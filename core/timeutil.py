"""北京时间统一工具。

本应用面向 A 股市场（北京时间），所有「当前时间 / 当前日期」一律按 UTC+8 计算，
**与运行机器的本地时区无关**：即使服务器部署在 UTC 时区，也能得到正确的北京时间，
避免数据库时间戳（旧实现用 ``datetime.utcnow()``，UTC）与业务日期
（``datetime.now()``，机器本地时区）混用导致前后不一致。

约定：返回 naive 的 datetime / date（无 tzinfo），与现有 ORM 列
（``created_at`` 等历史存的都是 naive datetime）保持一致，避免
SQLAlchemy / SQLite 写入带时区信息。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Asia/Shanghai：UTC+8，中国不实行夏令时，全年固定偏移。
_BJT = timezone(timedelta(hours=8))


def now() -> datetime:
    """当前北京时间（naive datetime，无 tzinfo）。"""
    return datetime.now(_BJT).replace(tzinfo=None)


def today() -> date:
    """当前北京日期。"""
    return now().date()


def today_str(fmt: str = "%Y%m%d") -> str:
    """当前北京日期字符串，默认 ``YYYYMMDD``。"""
    return now().strftime(fmt)
