"""样本股参考数据：把用户感兴趣的股票近期 K 线 + 指标快照渲染成纯文本，
作为参考注入策略生成 prompt，让 LLM 据真实样本校准阈值与形态。

两种模式：
- **窗口+买点模式**（``start``/``end``/``buy_date`` 任一给出）：渲染买点当日的完整
  指标/形态快照（策略目标态）+ 窗口走势 + 买点后累计，作监督信号优化策略。
- **默认模式**（上述全 None）：取截至最新缓存日的 60 个交易日，渲染末根快照（旧行为）。

定位为 advisory（参考）：只读 ``daily_bars`` 缓存，不联网、不写库，毫秒级。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from core import indicators, tushare_api

LOOKBACK = 60  # 取最近 60 个交易日，足以稳定 MA20 / MACD(26) / BOLL(20)
MIN_BARS = 30  # 少于则视为数据不足
MAX_WINDOW = 9  # 窗口模式最多展示的交易日数（"<10 个交易日"）


def build_sample_reference(
    codes: list[str],
    asof: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    buy_date: Optional[str] = None,
) -> Optional[str]:
    """构建样本股参考文本块。

    Args:
        codes: 用户原始输入列表，元素可为 ``600519`` / ``600519.SH`` / ``贵州茅台``。
        asof: 默认模式截至交易日(YYYYMMDD)；窗口模式下忽略。默认取缓存最近有数据日。
        start / end: 窗口模式的时间段(YYYYMMDD，闭区间)；任一给出即进入窗口模式。
        buy_date: 窗口内某交易日(YYYYMMDD)为「形态契合买点」；缺省取窗口中间日。

    Returns:
        纯文本参考块（无 ``` 代码栏，避免干扰 JSON 解析）；无任何有效样本时返回 None。
    """
    raw = [c for c in (codes or []) if c and c.strip()]
    if not raw:
        return None
    resolved, unknown = _resolve_codes(raw)
    if not resolved:
        return None

    today = datetime.now().strftime("%Y%m%d")
    use_window = bool(start or end or buy_date)

    if use_window:
        # 锚点取窗口/买点中最晚者，并夹到最近有缓存的交易日，确保窗口落在缓存区间内。
        anchor_raw = max(d for d in (start, end, buy_date, today) if d)
        anchor = tushare_api.latest_cached_trade_date(anchor_raw) or anchor_raw
        dates = tushare_api.get_lookback_dates(anchor, LOOKBACK)
        bars = tushare_api.load_bars(dates)

        win_start = start or buy_date or end
        win_end = end or buy_date or start
        # 窗口真实交易日：直接从已加载的缓存日里筛（纯离线，不查交易日历）
        loaded = sorted(str(d) for d in bars["trade_date"].unique()) if not bars.empty else []
        window_dates = [d for d in loaded if win_start <= d <= win_end]
        if not window_dates:
            window_dates = sorted({d for d in (win_start, win_end, buy_date) if d})

        if buy_date and buy_date in window_dates:
            bd = buy_date
        elif window_dates:
            bd = window_dates[len(window_dates) // 2]
        else:
            bd = buy_date or today

        lines = [f"【参考样本股 · 买点 {bd}（形态契合，策略目标态）】"]
        for ts_code, name in resolved:
            sub = bars[bars["ts_code"] == ts_code].sort_values("trade_date")
            lines.append(_render_stock_window(ts_code, name, sub, window_dates, bd))
        lines.append("请使策略条件在买点当日对样本股成立，以窗口走势校验、避免过拟合单日噪声；策略仍面向全市场。")
    else:
        asof = asof or tushare_api.latest_cached_trade_date(today) or today
        dates = tushare_api.get_lookback_dates(asof, LOOKBACK)
        bars = tushare_api.load_bars(dates)

        lines = [f"【参考样本股 截至 {asof}】"]
        for ts_code, name in resolved:
            sub = bars[bars["ts_code"] == ts_code].sort_values("trade_date")
            lines.append(_render_stock(ts_code, name, sub))
        lines.append("请据此校准指标周期/阈值/形态，使策略贴合上述样本的真实特征，但不限定于这些个股。")

    if unknown:
        lines.append("（未识别的输入：" + "、".join(unknown) + "，已忽略）")
    return "\n".join(lines)


def _resolve_codes(raw_codes: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """把用户输入归一化为 [(ts_code, name), ...]，未命中的收集到 unknown。"""
    stocks = tushare_api.list_stocks()
    by_full: dict[str, str] = {}            # ts_code -> name
    by_bare: dict[str, tuple[str, str]] = {}  # 6 位代码 -> (ts_code, name)
    by_name: dict[str, tuple[str, str]] = {}  # 名称 -> (ts_code, name)
    for _, r in stocks.iterrows():
        ts, nm = r["ts_code"], str(r["name"] or "")
        by_full[ts] = nm
        by_bare.setdefault(ts.split(".")[0], (ts, nm))
        if nm:
            by_name.setdefault(nm, (ts, nm))

    resolved: list[tuple[str, str]] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for raw in raw_codes:
        key = raw.strip()
        if not key:
            continue
        match: Optional[tuple[str, str]] = None
        if key in by_full:
            match = (key, by_full[key])
        elif key in by_bare:
            match = by_bare[key]
        elif key in by_name:
            match = by_name[key]
        else:  # 名称包含匹配（取第一个命中）
            hit = stocks[stocks["name"].astype(str).str.contains(key, na=False)]
            if not hit.empty:
                r0 = hit.iloc[0]
                match = (r0["ts_code"], str(r0["name"] or ""))
        if match and match[0] not in seen:
            resolved.append(match)
            seen.add(match[0])
        elif not match:
            unknown.append(key)
    return resolved, unknown


def _snapshot_body(df: pd.DataFrame) -> Optional[str]:
    """对 df（末根为目标日）计算指标，返回 3 行缩进正文（无标题）。

    数据不足（< MIN_BARS）返回 None。
    """
    if len(df) < MIN_BARS:
        return None
    d = df.reset_index(drop=True)
    close = float(d["close"].iloc[-1])

    ma5 = indicators.MA(d, 5).iloc[-1]
    ma10 = indicators.MA(d, 10).iloc[-1]
    ma20 = indicators.MA(d, 20).iloc[-1]
    arrange = _ma_arrange(ma5, ma10, ma20)

    dif, dea, _ = indicators.MACD(d)
    dif_v, dea_v = float(dif.iloc[-1]), float(dea.iloc[-1])
    cross_kind, cross_n = _macd_cross(dif - dea)

    rsi = float(indicators.RSI(d, 14).iloc[-1])
    rsi_tag = "超买" if rsi > 70 else "超卖" if rsi < 30 else "中性"

    k, dser, j = indicators.KDJ(d)
    kv, dv, jv = float(k.iloc[-1]), float(dser.iloc[-1]), float(j.iloc[-1])

    upper, mid, lower = indicators.BOLL(d, 20)
    boll_tag = _boll_pos(close, upper.iloc[-1], mid.iloc[-1], lower.iloc[-1])

    mavol5 = indicators.MA_VOL(d, 5).iloc[-1]
    vol_ratio = float(d["vol"].iloc[-1]) / mavol5 if mavol5 and not pd.isna(mavol5) else float("nan")
    vol_tag = "放量" if vol_ratio >= 1.2 else "缩量" if not pd.isna(vol_ratio) and vol_ratio < 0.8 else "温和"

    ret5 = _ret_n(d["close"], 5)
    patterns = _last_bar_patterns(d)

    line1 = (f"收盘{_fmt(close)} | MA5 {_fmt(ma5)} / MA10 {_fmt(ma10)} / MA20 {_fmt(ma20)} → {arrange}")
    macd_str = f"MACD DIF {_fmt(dif_v)} {'>' if dif_v >= dea_v else '<'} DEA {_fmt(dea_v)}"
    if cross_kind:
        macd_str += f"，{cross_kind}约{cross_n}日"
    macd_str += f" | RSI(14)={_fmt(rsi)}({rsi_tag}) | KDJ K{_fmt(kv)}/D{_fmt(dv)}/J{_fmt(jv)}"
    line3 = (f"BOLL {boll_tag} | 量比{_fmt(vol_ratio)}({vol_tag}) | 近5日{_signed(ret5)}% "
             f"| 末根：{patterns or '无明显形态'}")
    return "  " + line1 + "\n  " + macd_str + "\n  " + line3


def _render_stock(ts_code: str, name: str, sub: pd.DataFrame) -> str:
    """默认模式：渲染末根快照。"""
    title = f"{ts_code} {name}:" if name else f"{ts_code}:"
    body = _snapshot_body(sub)
    if body is None:
        return f"{title}\n  数据不足（仅 {len(sub)} 个交易日），已跳过指标"
    return title + "\n" + body


def _render_stock_window(ts_code: str, name: str, sub: pd.DataFrame,
                         window_dates: list[str], buy_date: str) -> str:
    """窗口模式：买点当日目标快照 + 窗口走势 + 买点后累计。"""
    nm = f"{ts_code} {name}" if name else ts_code
    if window_dates:
        title = f"{nm}（窗口 {window_dates[0]}–{window_dates[-1]}，共 {len(window_dates)} 个交易日）:"
    else:
        title = f"{nm}:"

    buy_sub = sub[sub["trade_date"].astype(str) <= buy_date] if buy_date else sub
    body = _snapshot_body(buy_sub)
    if body is None:
        snap = f"  ★买点当日({_md(buy_date)}) 数据不足，无法计算指标"
    else:
        snap = f"  ★买点当日({_md(buy_date)}) 目标形态（策略应在本日选出此票）：\n" + body
    walk = _window_walk(sub, window_dates, buy_date)
    return title + "\n" + snap + "\n" + walk


def _window_walk(sub: pd.DataFrame, window_dates: list[str], buy_date: str) -> str:
    """窗口各交易日 收盘/涨跌% 压缩展示，买点行加 ★；附买点后累计涨跌作确认。"""
    if not window_dates:
        return "  窗口内无缓存数据"
    by_date = {str(r["trade_date"]): r for _, r in sub.iterrows()}

    parts: list[str] = []
    for d in window_dates:
        r = by_date.get(d)
        star = "★" if d == buy_date else ""
        if r is None:
            parts.append(f"{_md(d)} 无数据{star}".rstrip())
            continue
        close = float(r["close"])
        pct = r.get("pct_chg")
        if pct is None or pd.isna(pct):
            parts.append(f"{_md(d)} {_fmt(close)}{star}".rstrip())
        else:
            parts.append(f"{_md(d)} {_fmt(close)}({_signed(float(pct))}%){star}")
    # 每 3 个一行，避免单行过长
    chunks = [parts[i:i + 3] for i in range(0, len(parts), 3)]
    walk_line = "  窗口走势：\n    " + "\n    ".join("  ".join(c) for c in chunks)

    after = [d for d in window_dates if d > buy_date]
    if after:
        buy_row = by_date.get(buy_date)
        last_row = by_date.get(after[-1])
        if buy_row is not None and last_row is not None:
            cum = (float(last_row["close"]) / float(buy_row["close"]) - 1) * 100
            tag = "确认有效" if cum > 0 else "走弱"
            walk_line += f"\n  买点后 T+1..T+{len(after)} 累计 {_signed(cum)}%（{tag}）"
    return walk_line


def _md(date_str: str) -> str:
    """20260515 → 05-15；异常原样返回。"""
    s = str(date_str)
    return f"{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _ma_arrange(ma5: float, ma10: float, ma20: float) -> str:
    vals = [ma5, ma10, ma20]
    if any(pd.isna(v) for v in vals):
        return "均线数据不足"
    if ma5 > ma10 > ma20:
        return "多头排列"
    if ma5 < ma10 < ma20:
        return "空头排列"
    return "均线纠缠"


def _macd_cross(diff_dea: pd.Series) -> tuple[str, int]:
    """识别最近一次金叉/死叉及距今交易日数；无则 ("", 0)。"""
    s = diff_dea.dropna().reset_index(drop=True)
    if len(s) < 2:
        return ("", 0)
    sign = (s > 0).astype(int)
    change_idx = sign.diff().fillna(0)[lambda x: x != 0].index
    if len(change_idx) == 0:
        return ("", 0)
    n = len(s) - 1 - change_idx[-1]
    kind = "金叉" if s.iloc[-1] > 0 else "死叉"
    return (kind, max(int(n), 1))


def _boll_pos(close: float, upper: float, mid: float, lower: float) -> str:
    width = upper - lower
    if any(pd.isna(v) for v in (upper, mid, lower)) or not width:
        return "未知"
    pos = (close - lower) / width
    if pos >= 0.8:
        return "近上轨"
    if pos <= 0.2:
        return "近下轨"
    return "中轨"


def _ret_n(close: pd.Series, n: int) -> float:
    if len(close) <= n:
        return float("nan")
    return float(close.iloc[-1] / close.iloc[-1 - n] - 1) * 100


def _last_bar_patterns(df: pd.DataFrame) -> str:
    # doji/hammer/engulfing 内部对 high==low 的一字板会用 pd.NA，导致
    # `bool(NA)` ambiguous 报错；这里把每个形态单独包起来，异常/NA 一律视为未命中。
    checks = [
        ("十字星", lambda: indicators.doji(df)),
        ("锤子线", lambda: indicators.hammer(df)),
        ("阳包阴", lambda: indicators.engulfing_bull(df)),
        ("阴包阳", lambda: indicators.engulfing_bear(df)),
        ("三连阳", lambda: indicators.consecutive_up(df, 3)),
        ("三连阴", lambda: indicators.consecutive_down(df, 3)),
    ]
    hit = []
    for name, fn in checks:
        try:
            val = fn().fillna(False).iloc[-1]
            if bool(val):
                hit.append(name)
        except Exception:  # noqa: BLE001  NA/计算异常 → 该形态未命中
            continue
    return "、".join(hit)


def _fmt(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    return f"{x:.2f}"


def _signed(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    return f"+{x:.1f}" if x >= 0 else f"{x:.1f}"
