"""配置读写。

统一走 ``settings`` 表（界面修改即持久化）。首次/兜底从 ``.env`` 与内置默认值取。
任何模块需要配置时调用 :func:`get_setting` / :func:`load_settings` / :func:`save_settings`。
"""
from __future__ import annotations

import os
from typing import Dict

from dotenv import load_dotenv

from core.db import get_session
from core.models import Setting

load_dotenv()  # 若存在 .env 则载入环境变量

# 内置默认值；带 os.getenv 的项支持 .env 兜底
DEFAULTS: Dict[str, str] = {
    # 网络
    "http_proxy": os.getenv("HTTP_PROXY", ""),
    "https_proxy": os.getenv("HTTPS_PROXY", ""),
    # tushare
    "tushare_token": os.getenv("TUSHARE_TOKEN", ""),
    "tushare_enabled": "true",
    "tushare_rate_per_min": "200",
    # 大模型
    "llm_base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    "llm_api_key": os.getenv("LLM_API_KEY", ""),
    "llm_model": os.getenv("LLM_MODEL", "deepseek-chat"),
    "llm_temperature": "0.2",
    "llm_max_tokens": "2000",
    # 定时
    "daily_run_enabled": "true",
    "daily_run_time": "18:30",  # HH:MM
}


def get_setting(key: str, default: str | None = None) -> str:
    """读取单项配置：DB > 默认值 > default。"""
    with get_session() as s:
        row = s.query(Setting).filter_by(key=key).first()
        if row is not None and row.value is not None:
            return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    return "" if default is None else default


def set_setting(key: str, value) -> None:
    """写入单项配置（强制 str）。"""
    val = "" if value is None else str(value)
    with get_session() as s:
        row = s.query(Setting).filter_by(key=key).first()
        if row is None:
            s.add(Setting(key=key, value=val))
        else:
            row.value = val
        s.commit()


def load_settings() -> Dict[str, str]:
    """读取全部配置（DB 覆盖默认值），返回 dict。"""
    out = dict(DEFAULTS)
    with get_session() as s:
        for row in s.query(Setting).all():
            out[row.key] = row.value if row.value is not None else ""
    return out


def save_settings(data: Dict[str, str]) -> None:
    """批量写入配置。"""
    with get_session() as s:
        for key, value in data.items():
            val = "" if value is None else str(value)
            row = s.query(Setting).filter_by(key=key).first()
            if row is None:
                s.add(Setting(key=key, value=val))
            else:
                row.value = val
        s.commit()


def apply_proxies() -> None:
    """把代理设置写入 os.environ，供 tushare(requests) / openai(httpx) 使用。"""
    http_p = get_setting("http_proxy")
    https_p = get_setting("https_proxy")
    if http_p:
        os.environ["HTTP_PROXY"] = http_p
    else:
        os.environ.pop("HTTP_PROXY", None)
    if https_p:
        os.environ["HTTPS_PROXY"] = https_p
    else:
        os.environ.pop("HTTPS_PROXY", None)
