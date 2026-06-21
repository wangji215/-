"""样本股参考数据：把用户感兴趣的股票近期 K 线 + 指标快照渲染成纯文本，
作为参考注入策略生成 prompt，让 LLM 据真实样本校准阈值与形态。

定位为 advisory（参考）：只读 ``daily_bars`` 缓存，不联网、不写库，毫秒级。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from core import indicators, tushare_api

LOOKBACK = 60  # 取最近 60 个交易日，足以稳定 MA20 / MACD(26) / BOLL(20)
MIN_BARS = 30  # 少于则视为数据不足


def build_sample_reference(codes: list[str], asof: Optional[str] = None) -> Optional[str]:
    """构建样本股参考文本块。

    Args:
        codes: 用户原始输入列表，元素可为 ``600519`` / ``600519.SH`` / ``贵州茅台``。
        asof: 截至交易日(YYYYMMDD)；默认取缓存里最近一个有数据的交易日。

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


def _render_stock(ts_code: str, name: str, sub: pd.DataFrame) -> str:
    title = f"{ts_code} {name}:" if name else f"{ts_code}:"
    if len(sub) < MIN_BARS:
        return f"{title}\n  数据不足（仅 {len(sub)} 个交易日），已跳过指标"

    df = sub.reset_index(drop=True)
    close = float(df["close"].iloc[-1])

    ma5 = indicators.MA(df, 5).iloc[-1]
    ma10 = indicators.MA(df, 10).iloc[-1]
    ma20 = indicators.MA(df, 20).iloc[-1]
    arrange = _ma_arrange(ma5, ma10, ma20)

    dif, dea, _ = indicators.MACD(df)
    dif_v, dea_v = float(dif.iloc[-1]), float(dea.iloc[-1])
    cross_kind, cross_n = _macd_cross(dif - dea)

    rsi = float(indicators.RSI(df, 14).iloc[-1])
    rsi_tag = "超买" if rsi > 70 else "超卖" if rsi < 30 else "中性"

    k, d, j = indicators.KDJ(df)
    kv, dv, jv = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])

    upper, mid, lower = indicators.BOLL(df, 20)
    boll_tag = _boll_pos(close, upper.iloc[-1], mid.iloc[-1], lower.iloc[-1])

    mavol5 = indicators.MA_VOL(df, 5).iloc[-1]
    vol_ratio = float(df["vol"].iloc[-1]) / mavol5 if mavol5 and not pd.isna(mavol5) else float("nan")
    vol_tag = "放量" if vol_ratio >= 1.2 else "缩量" if not pd.isna(vol_ratio) and vol_ratio < 0.8 else "温和"

    ret5 = _ret_n(df["close"], 5)
    patterns = _last_bar_patterns(df)

    line1 = (f"收盘{_fmt(close)} | MA5 {_fmt(ma5)} / MA10 {_fmt(ma10)} / MA20 {_fmt(ma20)} → {arrange}")
    macd_str = f"MACD DIF {_fmt(dif_v)} {'>' if dif_v >= dea_v else '<'} DEA {_fmt(dea_v)}"
    if cross_kind:
        macd_str += f"，{cross_kind}约{cross_n}日"
    macd_str += f" | RSI(14)={_fmt(rsi)}({rsi_tag}) | KDJ K{_fmt(kv)}/D{_fmt(dv)}/J{_fmt(jv)}"
    line3 = (f"BOLL {boll_tag} | 量比{_fmt(vol_ratio)}({vol_tag}) | 近5日{_signed(ret5)}% "
             f"| 末根：{patterns or '无明显形态'}")
    return title + "\n  " + line1 + "\n  " + macd_str + "\n  " + line3


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
    checks = [
        ("十字星", indicators.doji(df)),
        ("锤子线", indicators.hammer(df)),
        ("阳包阴", indicators.engulfing_bull(df)),
        ("阴包阳", indicators.engulfing_bear(df)),
        ("三连阳", indicators.consecutive_up(df, 3)),
        ("三连阴", indicators.consecutive_down(df, 3)),
    ]
    hit = [name for name, s in checks if bool(s.fillna(False).iloc[-1])]
    return "、".join(hit)


def _fmt(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    return f"{x:.2f}"


def _signed(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    return f"+{x:.1f}" if x >= 0 else f"{x:.1f}"
