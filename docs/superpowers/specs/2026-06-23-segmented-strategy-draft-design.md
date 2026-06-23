# 分段策略草稿层设计

## 背景

当前 NL2DSL 流程把用户自然语言直接翻译成 `StrategyDSL.conditions.rules`。这个结构适合表达“当前交易日满足哪些条件”，但不擅长表达图形策略中的时序结构，例如“前期拉升 -> 回踩整理 -> 买点启动”。结果是模型容易把阶段描述压成一组当天均线条件，导致 `description` 看起来正确，真正执行的 `rules` 却没有覆盖“回看 60 日”“前期高点”“回踩区间”“昨日/今日触发”等关键语义。

目标是在现有可执行 DSL 前增加一个可审阅的结构化草稿层，让模型必须先说明：

- 一共有几段走势。
- 每段对应的大致时间窗口。
- 每段的技术指标语义。
- 每段如何映射到可执行条件。

第一版优先提升生成准确性和可解释性，同时兼容现有策略引擎。

## 目标

1. 让自然语言策略先被结构化为多段走势，而不是直接生成扁平条件。
2. 在 UI 中展示模型对“几段、每段时间区间、每段技术描述”的理解，方便用户发现误解。
3. 编译分段草稿为现有 `StrategyDSL`，保持当前回测、筛选、保存、版本管理能力可用。
4. 把“鱼跃龙门”这类经验阈值从全局 prompt 中收敛到草稿参数或策略模板，避免污染其它策略生成。

## 非目标

1. 第一版不重写策略引擎为真正的段内搜索引擎。
2. 第一版不做图像识别或自动从截图提取 K 线。
3. 第一版不保证模型自动找到最优阈值；阈值仍需要通过参考样本和回测调校。
4. 第一版不删除现有 `conditions/rules`，历史策略必须继续可执行。

## 数据结构

新增 `core/strategy_draft.py`，定义 `StrategyDraft` 和 `StrategyGenerationResult`。

`StrategyGenerationResult` 是 LLM 输出的顶层结构：

```json
{
  "draft": {},
  "compiled_dsl": {}
}
```

`StrategyDraft` 只表示模型对图形结构的分段理解：

```json
{
  "name": "鱼跃龙门",
  "description": "前期拉升后回踩，MA5 贴近 MA10 后重新启动",
  "timeframe": "daily",
  "lookback": 60,
  "mode": "trigger",
  "segments": [
    {
      "name": "前期拉升",
      "role": "setup",
      "window": {"start": -60, "end": -20},
      "intent": "出现明显阶段涨幅并形成阶段高点",
      "technical_description": "60 日最高价相对最低价有足够涨幅，最高点距离当前不能过近也不能过远",
      "rules": []
    },
    {
      "name": "回踩整理",
      "role": "pullback",
      "window": {"start": -30, "end": -2},
      "intent": "从前期高点回落，但中期趋势没有破坏",
      "technical_description": "价格不追前高，仍在 MA20/MA60 上方，MA5 靠近 MA10",
      "rules": []
    },
    {
      "name": "启动确认",
      "role": "trigger",
      "window": {"start": -1, "end": 0},
      "intent": "昨日偏弱，今日重新站上短均线",
      "technical_description": "昨日收盘低于昨日 MA10，今日收盘上穿 MA5",
      "rules": []
    }
  ]
}
```

字段说明：

- `mode`: `stage` 或 `trigger`。`stage` 表示当前阶段匹配；`trigger` 表示买点当天触发。
- `window.start/end`: 相对当前交易日的交易日偏移，`0` 为当前日，`-1` 为上一交易日。第一版用于校验和展示，编译时主要转换为 `period`、`offset`、`HHVBARS` 等近似条件。
- `role`: 建议枚举为 `setup`、`advance`、`pullback`、`consolidation`、`trigger`、`filter`。编译器根据 role 使用模板。
- `rules`: 预留给未来段内规则；第一版可以为空，或放与现有 `Rule` 兼容的规则。
- `compiled_dsl`: 不属于 `StrategyDraft`，而是 `StrategyGenerationResult` 的顶层字段。模型可同时输出可执行 DSL；后端会用 schema 校验并做覆盖检查。

## 生成流程

第一版采用“模型输出草稿 + 可执行 DSL”的双输出方式：

1. 用户输入自然语言策略和可选参考样本股。
2. LLM 输出一个 JSON，包含 `draft` 和 `compiled_dsl`。
3. 后端先校验 `draft`：
   - `segments` 非空。
   - `window.start <= window.end <= 0`。
   - `lookback >= abs(min(window.start))`。
   - `trigger` 模式必须至少包含一个 `role=trigger` 段。
4. 后端校验 `compiled_dsl` 是合法 `StrategyDSL`。
5. 后端执行覆盖检查：草稿中的关键时序语义必须落到 `compiled_dsl`：
   - 出现 `window` 或“前期/回看/高点/低点”时，compiled DSL 必须使用 `HHV`、`LLV`、`HHVBARS` 或等价窗口规则。
   - 出现“昨日/前一日”时，compiled DSL 必须使用 `offset`。
   - 出现“买点/触发/首次站上/刚上穿”时，compiled DSL 必须使用 `cross_up` 或明确的 `offset` 触发条件。
6. UI 展示草稿段落和 compiled DSL，用户确认后保存 compiled DSL。

这样第一版不需要把编译器做到完全智能，但能把模型的理解暴露出来，并用校验防止“描述正确、规则漏掉”的问题。

## 编译策略

新增 `core/strategy_compiler.py`，提供两个能力：

1. `validate_draft_coverage(draft, dsl) -> list[str]`
   返回缺失的关键语义，例如“前期拉升段缺少 HHV/LLV 规则”。

2. `compile_draft_defaults(draft) -> StrategyDSL`
   当模型没有输出 compiled DSL，或用户选择“按草稿自动生成”时，使用通用模板生成可执行 DSL。

默认模板只包含通用映射，不包含某只股票的特殊阈值：

- `setup/advance`:
  - `HHV(HIGH, lookback) > k * LLV(LOW, lookback)`。
  - `HHVBARS(HIGH, lookback)` 在合理区间。
- `pullback/consolidation`:
  - `CLOSE <= upper * HHV(HIGH, lookback)`，避免追到前高。
  - `CLOSE >= lower * HHV(HIGH, lookback)`，避免回撤过深。
  - 均线贴近用上下界表达。
- `trigger`:
  - 昨日条件用 `offset=1`。
  - 当日重新站上用 `cross_up`。
  - 强收盘可用 `CLOSE > x * HIGH`，但只在草稿或用户明确要求时加入。

阈值来源优先级：

1. 用户自然语言明确给出。
2. 参考样本股统计。
3. 策略模板默认值。
4. 缺失时追问，不硬猜。

## Prompt 改造

`core/strategy_prompt.py` 不再把“鱼跃龙门”的具体阈值写成全局规则。全局 prompt 改为要求模型输出：

- `draft.segments`：分段理解。
- `compiled_dsl`：可执行 DSL。
- 每段必须说明 `window`、`intent`、`technical_description`。
- 如果描述中有阶段语义，不能只输出扁平当天条件。

示例 prompt 重点展示结构，而不是固定某个阈值。

## UI 改造

策略生成页面新增“分段理解”预览：

```text
分段理解
1. 前期拉升 [-60, -20]
   意图：出现明显阶段涨幅并形成阶段高点
   技术描述：HHV/LLV 有足够涨幅，HHVBARS 限制高点位置

2. 回踩整理 [-30, -2]
   意图：从高点回落但趋势未破坏
   技术描述：价格不贴近前高，均线仍多头，MA5 靠近 MA10

3. 启动确认 [-1, 0]
   意图：昨日弱，今日重新站上短均线
   技术描述：昨日收盘低于昨日 MA10，今日收盘上穿 MA5
```

保存策略时仍保存 `compiled_dsl`，避免影响现有策略列表和每日筛选。

## 测试计划

新增测试：

1. `StrategyDraft` schema 校验：
   - 合法分段通过。
   - window 越界或顺序错误失败。
   - trigger 模式无 trigger 段失败。

2. 覆盖检查：
   - draft 有前期拉升段但 dsl 没有 HHV/LLV/HHVBARS 时返回问题。
   - draft 有昨日语义但 dsl 没有 offset 时返回问题。
   - draft 有买点触发但 dsl 没有 cross_up/offset 触发时返回问题。

3. 编译默认模板：
   - 三段鱼跃龙门草稿能生成合法 `StrategyDSL`。
   - 生成结果包含窗口指标、offset、触发条件。

4. 兼容性：
   - 旧 `StrategyDSL` 保存、读取、执行不受影响。
   - 当前 engine 测试继续通过。

## 风险与缓解

- 风险：模型输出 draft 和 compiled DSL 不一致。
  - 缓解：增加覆盖检查，UI 显示 warning，不允许直接保存明显缺失关键语义的 DSL。

- 风险：segments 第一版只是近似编译，并非真正段内搜索。
  - 缓解：文案明确“第一版为结构化生成与可解释编译”；后续再升级 engine 支持直接按窗口求值。

- 风险：全局 prompt 再次被具体策略阈值污染。
  - 缓解：全局 prompt 只写映射原则；具体阈值放到草稿参数、策略模板或参考样本统计中。

- 风险：UI 复杂度增加。
  - 缓解：默认折叠 raw JSON，只展示分段摘要和最终可执行条件。

## 实施顺序

1. 新增 `StrategyDraft` schema。
2. 新增 draft 解析、覆盖检查和默认编译器。
3. 调整 prompt 输出 `draft + compiled_dsl`。
4. 调整 LLM 解析逻辑，优先解析 `StrategyGenerationResult`，同时兼容旧格式只返回 `StrategyDSL`。
5. 调整策略生成页面，展示分段理解和覆盖检查结果。
6. 补测试并跑现有测试。

## 验收标准

1. 对“鱼跃龙门”描述，模型输出至少三段：前期拉升、回踩整理、启动确认。
2. UI 能显示每段时间窗口、意图、技术描述。
3. compiled DSL 中包含窗口指标、offset 和触发条件。
4. 若 compiled DSL 缺少关键语义，页面提示并阻止静默保存。
5. 旧策略仍可保存、回滚、执行。
6. 全部相关测试通过。
