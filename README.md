# 股票图形跟踪小助手 📈

根据股票 K 线数据从 A 股 ~6000 家上市公司中筛选出符合「K 线图形策略」的股票，并跟踪其后 5 个交易日的涨跌，用于复盘优化投资策略。

- **策略生成**：自然语言对话生成可执行图形策略（LLM → 结构化 DSL → 确定性引擎评估）。
- **数据来源**：tushare pro 日 K（按交易日全市场抓取并缓存）。
- **大模型**：OpenAI 兼容接口，可随时切换 DeepSeek / 智谱 / 通义 / Kimi / OpenAI / 本地 Ollama。
- **回测跟踪**：手动填买入价，自动统计 T+1..T+5 每日涨跌与累计涨跌，按购买日期归档。

## 技术栈

Streamlit + pandas + SQLAlchemy(SQLite) + APScheduler + openai + tushare。

## 安装与运行

> 需要 Python 3.10+。

```bash
# 1. （建议）创建虚拟环境
python -m venv .venv
# Windows:
.venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
streamlit run app.py
```

浏览器自动打开后，按左侧页面顺序配置即可。

## 使用流程

1. **🌍 环境设置**：填写 tushare token、大模型 base_url / api_key / model，分别点「测试连接」。
2. **🎯 策略生成**：用自然语言描述策略（如「均线多头排列且 MACD 金叉」），确认 DSL 后保存为版本，并**启用**（最多同时 3 条）。
3. **📊 每日筛选分析**：选数据时间点 → 运行分析 → 在匹配结果里填「当日购入价格」 → 查看 5 日跟踪与归档。

## 每日自动运行（两种方式）

- **应用内（推荐先试）**：Streamlit 开启时，APScheduler 按环境设置里的时间（默认 18:30）对全部启用策略自动运行。
- **7×24 兜底**：用 Windows 任务计划程序定时执行 CLI，即使应用没开也会跑：

```bash
python -m scripts.run_daily            # 用最近交易日
python -m scripts.run_daily 20240603   # 指定交易日
```

**Windows 任务计划程序注册**：`操作` → `创建基本任务` → 触发器选「每天 18:30」→ 操作选「启动程序」→ 程序填 `python`（或虚拟环境的 `python.exe` 全路径），参数填 `-m scripts.run_daily`，起始于填本项目根目录。

## 策略 DSL 说明

策略是结构化 JSON（不是可执行代码），由内置引擎在日 K 上确定性求值：

```json
{
  "name": "金叉+放量+站上20日线",
  "lookback": 40,
  "conditions": [
    { "logic": "and", "rules": [
      { "left": {"ind": "MA", "period": 5}, "op": "cross_up", "right": {"ind": "MA", "period": 10}},
      { "left": {"ind": "VOL"}, "op": ">", "right": {"ind": "MA_VOL", "period": 5}, "multiplier": 1.5},
      { "left": {"ind": "CLOSE"}, "op": ">", "right": {"ind": "MA", "period": 20}}
    ]}
  ]
}
```

- 指标：`MA EMA MA_VOL RSI MACD KDJ BOLL` 及原生字段 `CLOSE/OPEN/HIGH/LOW/VOL/AMOUNT/PCT_CHG`。
- 形态：`PATTERN` + `field` ∈ `doji / hammer / engulfing_bull / engulfing_bear / consecutive_up / consecutive_down`。
- 运算：`> < >= <= cross_up(金叉) cross_down(死叉) between is_true`。
- 顶层多个条件组之间为「且」，组内规则按 `logic`(and/or) 组合。

## 自测

```bash
python tests/test_engine.py
```

## 目录结构

```
app.py                       # Streamlit 入口
pages/                       # 三个界面（环境设置/策略生成/每日筛选）
core/                        # db/models/config/tushare_api/indicators/strategy_dsl/
                             #   strategy_engine/llm_client/strategy_repo/backtest/scheduler
scripts/run_daily.py         # 每日兜底 CLI
tests/test_engine.py         # 引擎自测
data/                        # SQLite（stock.db / scheduler.db，自动生成，已 gitignore）
```

## 说明

- tushare `daily` 接口可调用频率取决于账户积分；首次全市场回看较慢，之后走缓存很快。
- 本工具仅用于学习与研究，不构成投资建议。
