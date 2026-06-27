"""数据库引擎与会话工厂。

所有表存于仓库根目录下的 ``data/stock.db`` (SQLite)。
Streamlit 在交互时会重跑脚本，因此关闭 ``check_same_thread``。
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# 仓库根目录（本文件位于 <root>/core/db.py）
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "stock.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def get_session():
    """返回一个新会话，支持 ``with get_session() as s:`` 用法。"""
    return SessionLocal()


def _ensure_column(table: str, column: str, ddl_type: str = "TEXT DEFAULT ''") -> bool:
    """若 table 已存在但缺 column，则 ALTER TABLE 加列；返回是否新增了列。"""
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False  # 表尚未建（create_all 会按最新 schema 建，含新列）
    cols = {c["name"] for c in insp.get_columns(table)}
    if column in cols:
        return False
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
    return True


def _migrate() -> None:
    """轻量幂等迁移：给历史库补版本表的 description 列，并回填策略版本说明。

    新库由 create_all 直接建出含 description 的表，此处对老库做 ALTER + 一次性回填。
    """
    added_sv = _ensure_column("strategy_versions", "description")
    _ensure_column("trade_rule_versions", "description")
    if added_sv:
        # 老数据 description 为 NULL，从 spec_json 顶层 description 回填（spec_json 即 StrategyDSL）
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, spec_json FROM strategy_versions WHERE description IS NULL OR description = ''")
            ).fetchall()
            for vid, spec_json in rows:
                desc = ""
                try:
                    desc = json.loads(spec_json).get("description", "") or ""
                except (ValueError, TypeError):
                    desc = ""
                if desc:
                    conn.execute(
                        text("UPDATE strategy_versions SET description = :d WHERE id = :id"),
                        {"d": desc, "id": vid},
                    )


def init_db() -> None:
    """建表（已存在则跳过）+ 轻量迁移。导入 models 以注册表定义。"""
    from core import models  # noqa: F401  注册 ORM

    Base.metadata.create_all(engine)
    _migrate()
