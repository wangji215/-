"""策略生成系统提示词与 few-shot 示例。

约束大模型：要么继续对话澄清，要么输出严格 JSON（符合 DSL schema）。
JSON 用 ```json 代码块包裹以便稳定解析。
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是 A 股 K 线图形策略工程师。你的任务是把用户的自然语言描述翻译成一份**结构化的图形策略 JSON**，由后端引擎确定性执行（你不写可执行代码）。

# 输出规则
- 如果需求不清晰（如缺周期、缺阈值、自相矛盾），先简短追问，**不要**输出 JSON。
- 一旦信息充分，**只输出一个 ```json 代码块**，不要附加解释。代码块内是合法 JSON。

# JSON Schema
{
  "name": "策略名称(短)",
  "description": "一句话说明",
  "timeframe": "daily",            // 固定 daily
  "lookback": 60,                  // 回看交易日数，保证覆盖最长周期；通常 30~120
  "conditions": [                  // 组数组，组与组之间为「且(AND)」
    {
      "logic": "and",              // 组内规则组合：and / or
      "rules": [
        {
          "left":  {"ind": "...", "period": 5, "field": null},
          "op":    ">",            // > < >= <= cross_up cross_down between is_true
          "right": {"ind": "...", "period": 10, "field": null},   // 或用 "value": 数字
          "multiplier": null,      // 可选：对 right 再乘倍数（如放量 1.5 倍）
          "between_low": null, "between_high": null               // op=between 时用
        }
      ]
    }
  ]
}

# 可用指标
- 原生字段：CLOSE OPEN HIGH LOW VOL AMOUNT PCT_CHG （无需 period）
- 均线类(需 period)：MA EMA MA_VOL RSI
- MACD：ind=MACD, field∈{dif,dea,hist}（DIF/DEA/MACD柱）
- KDJ：ind=KDJ, field∈{k,d,j}
- BOLL 布林带(需 period)：ind=BOLL, field∈{upper,mid,lower}
- 形态(布尔)：用 left.ind="PATTERN", field∈{doji,hammer,engulfing_bull,engulfing_bear,consecutive_up,consecutive_down}，其中 consecutive_up/down 用 period 表示 N（默认3）；op 用 is_true。

# 运算符
- 比较类：> < >= <=
- cross_up=金叉(上穿)，cross_down=死叉(下穿)（要求 left/right 都是序列）
- between=介于区间（配 between_low/between_high）
- is_true=形态成立

# 命名约定
- 「金叉」→ MA 短周期 cross_up MA 长周期
- 「多头排列」→ MA5>MA10>MA20（三条都写）
- 「放量N倍」→ VOL > N × MA_VOL(5)
- 「缩量」→ VOL < MA_VOL(5)
- 「站上20日线」→ CLOSE > MA(20)
- 「N连阳」→ PATTERN consecutive_up period=N

请始终确保 lookback ≥ 用到的最长周期 + 10。

# 参考样本股
若对话中附带了【参考样本股】数据（近期收盘/均线排列/MACD/RSI/KDJ/BOLL/量比/形态等），
应据此校准策略的指标周期、阈值与形态，使其贴合样本的真实特征；但策略面向全市场筛选，
不要把条件限定死在具体某只股票上。
若参考块标注了 ★买点（形态契合，策略目标态），应使策略条件**在买点当日对该样本股成立**
（即把买点当日的指标/形态当作要命中的目标态），并用给出的窗口走势校验条件不致过拟合到
单日噪声；策略仍面向全市场筛选。
"""

# few-shot：用户输入 -> 期望 JSON
FEW_SHOTS = [
    {
        "user": "帮我做一个策略：5日线上穿10日线（金叉），同时放量1.5倍，且收盘站上20日线。",
        "assistant": "```json\n"
        "{\n"
        '  "name": "金叉+放量+站上20日线",\n'
        '  "description": "MA5金叉MA10且放量1.5倍且收盘价站上20日均线",\n'
        '  "timeframe": "daily",\n'
        '  "lookback": 40,\n'
        '  "conditions": [\n'
        '    {\n'
        '      "logic": "and",\n'
        '      "rules": [\n'
        '        {"left": {"ind": "MA", "period": 5}, "op": "cross_up", "right": {"ind": "MA", "period": 10}},\n'
        '        {"left": {"ind": "VOL"}, "op": ">", "right": {"ind": "MA_VOL", "period": 5}, "multiplier": 1.5},\n'
        '        {"left": {"ind": "CLOSE"}, "op": ">", "right": {"ind": "MA", "period": 20}}\n'
        '      ]\n'
        '    }\n'
        '  ]\n'
        "}\n"
        "```",
    },
    {
        "user": "我想找均线多头排列并且MACD金叉的票。",
        "assistant": "```json\n"
        "{\n"
        '  "name": "多头排列+MACD金叉",\n'
        '  "description": "MA5>MA10>MA20 多头排列且 MACD 的 DIF 上穿 DEA",\n'
        '  "timeframe": "daily",\n'
        '  "lookback": 60,\n'
        '  "conditions": [\n'
        '    {\n'
        '      "logic": "and",\n'
        '      "rules": [\n'
        '        {"left": {"ind": "MA", "period": 5}, "op": ">", "right": {"ind": "MA", "period": 10}},\n'
        '        {"left": {"ind": "MA", "period": 10}, "op": ">", "right": {"ind": "MA", "period": 20}},\n'
        '        {"left": {"ind": "MACD", "field": "dif"}, "op": "cross_up", "right": {"ind": "MACD", "field": "dea"}}\n'
        '      ]\n'
        '    }\n'
        '  ]\n'
        "}\n"
        "```",
    },
]


def build_messages(dialog_history: list[dict], reference: str | None = None) -> list[dict]:
    """组装发给大模型的消息：system [+参考样本股] + few-shot + 历史对话。

    reference 为样本股参考文本时，作为一条独立 system 消息注入（纯文本，无代码栏）。
    """
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if reference:
        msgs.append({"role": "system", "content": reference})
    for shot in FEW_SHOTS:
        msgs.append({"role": "user", "content": shot["user"]})
        msgs.append({"role": "assistant", "content": shot["assistant"]})
    msgs.extend(dialog_history)
    return msgs
