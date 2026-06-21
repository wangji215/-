"""每日分析兜底 CLI，供 Windows 任务计划程序调用。

用法：
    python -m scripts.run_daily            # 用最近交易日，对全部启用策略运行
    python -m scripts.run_daily 20240603   # 指定分析交易日（YYYYMMDD）

注册到 Windows 任务计划程序后，即使 Streamlit 未开启也能按时跑分析。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保可被 ``python -m scripts.run_daily`` 直接运行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import scheduler  # noqa: E402
from core.db import init_db  # noqa: E402


def main(argv: list[str]) -> int:
    init_db()
    snapshot = argv[1] if len(argv) > 1 else None
    results = scheduler.run_all_active(snapshot)
    if not results:
        print("没有启用的策略，请在「策略生成」页启用后再运行。")
        return 0
    print(f"分析交易日: {snapshot or '最近交易日'}")
    for r in results:
        print(f"  - {r['strategy']}: 匹配 {r['matched']} 只 [{r['status']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
