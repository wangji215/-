"""tushare 接入层测试（不访问网络）。直接运行：python tests/test_tushare_api.py"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tushare_api  # noqa: E402


def test_client_rebuilds_when_token_changes():
    """Streamlit 进程内修改 token 后，应重建 tushare client，避免继续用旧 token。"""
    old_client = object()
    new_client = object()
    tushare_api._pro = None

    with (
        patch.object(tushare_api, "get_setting", side_effect=["old-token", "new-token"]),
        patch.object(tushare_api.ts, "set_token") as set_token,
        patch.object(tushare_api.ts, "pro_api", side_effect=[old_client, new_client]),
    ):
        assert tushare_api._client() is old_client
        assert tushare_api._client() is new_client
        assert [c.args[0] for c in set_token.call_args_list] == ["old-token", "new-token"]

    tushare_api._pro = None
    print("[ok] test_client_rebuilds_when_token_changes")


if __name__ == "__main__":
    test_client_rebuilds_when_token_changes()
    print("\n全部 tushare 接入层测试通过 ✅")
