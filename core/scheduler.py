"""应用内每日定时分析（APScheduler）。

- 用 BackgroundScheduler + SQLAlchemyJobStore（独立 sqlite 文件）持久化。
- 进程级单例，避免 Streamlit 重跑时重复启动。
- 每日到点对全部启用策略在「最近交易日」运行分析并补齐跟踪。

注意：仅在 Streamlit 应用运行期间有效。若需 7×24 兜底，请用
``python -m scripts.run_daily`` 配合 Windows 任务计划程序。
"""
from __future__ import annotations

import atexit
import threading
from datetime import datetime, timedelta

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from core import backtest, strategy_repo, tushare_api
from core import timeutil
from core.config import get_setting
from core.db import DATA_DIR

JOB_ID = "daily_analysis"
_lock = threading.Lock()
_scheduler: BackgroundScheduler | None = None

SCHED_DB = f"sqlite:///{(DATA_DIR / 'scheduler.db')}"


def latest_snapshot() -> str:
    """最近一个 <= 今天的交易日（YYYYMMDD）。"""
    today = timeutil.today_str()
    start = (timeutil.now() - timedelta(days=20)).strftime("%Y%m%d")
    days = tushare_api.trading_days_between(start, today)
    if not days:
        tushare_api.fetch_trade_cal(start, today)
        days = tushare_api.trading_days_between(start, today, refresh=False)
    return days[-1] if days else today


def run_all_active(snapshot_date: str | None = None) -> list[dict]:
    """对全部启用策略运行分析。返回每条策略的运行摘要。"""
    actives = strategy_repo.active_strategies()
    if not actives:
        return []
    snap = snapshot_date or latest_snapshot()
    results = []
    for st_row in actives:
        try:
            run = backtest.run_analysis(st_row.id, snap)
            results.append({"strategy": st_row.name, "run_id": run.id,
                            "matched": run.matched_count, "status": "ok"})
        except Exception as e:  # noqa: BLE001
            results.append({"strategy": st_row.name, "run_id": None,
                            "matched": 0, "status": f"failed: {e}"})
    try:
        backtest.fill_and_recompute()
    except Exception:  # noqa: BLE001
        pass
    return results


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    with _lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(
                jobstores={"default": SQLAlchemyJobStore(url=SCHED_DB)},
                timezone="Asia/Shanghai",
            )
        return _scheduler


def _apply_daily_job() -> None:
    sched = get_scheduler()
    enabled = get_setting("daily_run_enabled", "true") == "true"
    hhmm = get_setting("daily_run_time", "18:30") or "18:30"
    existing = sched.get_job(JOB_ID)
    if not enabled:
        if existing:
            sched.remove_job(JOB_ID)
        return
    try:
        hh, mm = (hhmm.split(":") + ["0", "0"])[:2]
        hour, minute = int(hh), int(mm)
    except ValueError:
        hour, minute = 18, 30
    if existing:
        sched.reschedule_job(JOB_ID, trigger="cron", hour=hour, minute=minute)
    else:
        sched.add_job(run_all_active, "cron", id=JOB_ID, hour=hour, minute=minute,
                      replace_existing=True, misfire_grace_time=3600)


def start_if_enabled() -> bool:
    """启动调度器并按配置安排每日任务。进程内幂等。返回是否已启动。"""
    sched = get_scheduler()
    if not sched.running:
        sched.start()
        atexit.register(lambda: sched.shutdown(wait=False) if sched.running else None)
    _apply_daily_job()
    return sched.running


def reschedule() -> None:
    """配置变更后调用，重排每日任务（调度器需已启动）。"""
    sched = get_scheduler()
    if sched.running:
        _apply_daily_job()
