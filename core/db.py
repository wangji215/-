"""数据库引擎与会话工厂。

所有表存于仓库根目录下的 ``data/stock.db`` (SQLite)。
Streamlit 在交互时会重跑脚本，因此关闭 ``check_same_thread``。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
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


def init_db() -> None:
    """建表（已存在则跳过）。导入 models 以注册表定义。"""
    from core import models  # noqa: F401  注册 ORM

    Base.metadata.create_all(engine)
