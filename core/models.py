"""ORM 表定义。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from core import timeutil
from core.db import Base


def _now() -> datetime:
    # 统一北京时间（UTC+8，naive），与机器本地时区无关。
    return timeutil.now()


class Setting(Base):
    """key-value 配置表。界面修改即写回。"""

    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, default="")


class Strategy(Base):
    """一条图形策略。可启用，启用上限 3 条（应用层校验）。"""

    __tablename__ = "strategies"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    current_version_id = Column(Integer, ForeignKey("strategy_versions.id", use_alter=True))
    active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)


class StrategyVersion(Base):
    """策略的某一版本 DSL。每条策略最多保留 5 版。

    每个版本自带 description，切换/回滚版本时各自保留说明（不再只存父表一份）。
    """

    __tablename__ = "strategy_versions"
    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)
    version_no = Column(Integer, nullable=False)
    spec_json = Column(Text, nullable=False)
    description = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    __table_args__ = (
        UniqueConstraint("strategy_id", "version_no", name="uq_sv_strategy_version"),
    )


class TradeRule(Base):
    """交易规则（持仓/退出策略）。独立于策略 DSL，回测时按 id 引用。

    由 pages/5_⏰_交易规则.py 表单创建，每条最多保留 5 版。
    与 Strategy 解耦：同一规则可跨策略复用，同一策略也可配多套规则试参数。
    """

    __tablename__ = "trade_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    current_version_id = Column(Integer, ForeignKey("trade_rule_versions.id", use_alter=True))
    created_at = Column(DateTime, default=_now)


class TradeRuleVersion(Base):
    """交易规则的某一版本。spec_json 由 TradeRuleSpec（core/trade_rule_dsl.py）校验。

    每个版本自带 description，切换/回滚时各自保留说明。
    """

    __tablename__ = "trade_rule_versions"
    id = Column(Integer, primary_key=True)
    rule_id = Column(Integer, ForeignKey("trade_rules.id"), nullable=False)
    version_no = Column(Integer, nullable=False)
    spec_json = Column(Text, nullable=False)
    description = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    __table_args__ = (
        UniqueConstraint("rule_id", "version_no", name="uq_trv_rule_version"),
    )


class Stock(Base):
    """缓存 tushare stock_basic。"""

    __tablename__ = "stocks"
    ts_code = Column(String, primary_key=True)
    name = Column(String, default="")
    industry = Column(String, default="")
    market = Column(String, default="")
    list_date = Column(String, default="")
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class TradeCal(Base):
    """交易日历缓存（tushare trade_cal）。"""

    __tablename__ = "trade_cal"
    cal_date = Column(String, primary_key=True)  # YYYYMMDD
    is_open = Column(Boolean, default=True)


class DailyBar(Base):
    """日 K 缓存，按交易日全市场抓取。复合主键 (ts_code, trade_date)。"""

    __tablename__ = "daily_bars"
    ts_code = Column(String, primary_key=True)
    trade_date = Column(String, primary_key=True)  # YYYYMMDD
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    vol = Column(Float)
    amount = Column(Float)
    pct_chg = Column(Float)


class IndexWeight(Base):
    """指数成分股权重快照（tushare pro.index_weight）。

    复合主键 (index_code, trade_date, ts_code)。同一指数在不同快照日成分会变，
    回测时按 trade_date 用 step function（<=当日最新快照）决定可交易池。
    """

    __tablename__ = "index_weights"
    index_code = Column(String, primary_key=True)
    trade_date = Column(String, primary_key=True)  # YYYYMMDD 快照日
    ts_code = Column(String, primary_key=True)
    weight = Column(Float)


class AnalysisRun(Base):
    """一次策略分析运行。"""

    __tablename__ = "analysis_runs"
    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"))
    run_date = Column(String)      # 运行发生日 YYYYMMDD
    snapshot_date = Column(String)  # 分析使用的交易日 YYYYMMDD
    matched_count = Column(Integer, default=0)
    status = Column(String, default="success")  # success / failed
    message = Column(Text, default="")
    created_at = Column(DateTime, default=_now)


class Match(Base):
    """某次运行里命中策略的一只股票。"""

    __tablename__ = "matches"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("analysis_runs.id"))
    ts_code = Column(String)
    strategy_id = Column(Integer)
    snapshot_date = Column(String)
    matched_at = Column(DateTime, default=_now)


class Tracking(Base):
    """匹配股票买入后 T+0..T+5 的跟踪行。"""

    __tablename__ = "trackings"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    ts_code = Column(String)
    strategy_id = Column(Integer)
    buy_price = Column(Float)      # 手填买入价
    buy_date = Column(String)      # = snapshot_date
    offset = Column(Integer)       # 0..5
    trade_date = Column(String)    # T+offset 对应交易日
    close = Column(Float)          # 当日收盘
    day_pct = Column(Float)        # 当日涨跌 %（相对买入价）
    cum_pct = Column(Float)        # 累计涨跌 %（相对买入价）
    created_at = Column(DateTime, default=_now, onupdate=_now)
