# Backtrader 策略 DSL 回测

## 安装

项目依赖已固定：

```bash
.venv/bin/pip install -r requirements.txt
```

Backtrader 版本：

```text
backtrader==1.9.78.123
```

## 运行已保存策略 DSL

Backtrader 页面默认使用数据库里保存的策略 DSL，并通过
`core.strategy_engine.evaluate_history()` 生成每日信号。这个信号判断逻辑
和“每日筛选分析”一致。

先查看页面里的策略 ID，或从数据库查看：

```bash
sqlite3 data/stock.db "select id,name,current_version_id from strategies order by id"
```

运行指定策略：

```bash
.venv/bin/python scripts/backtrader_four_stage_backtest.py \
  --db data/stock.db \
  --strategy-id 5 \
  --cash 100000 \
  --fromdate 2026-03-13 \
  --todate 2026-06-22
```

限制股票数量做快速 smoke test：

```bash
.venv/bin/python scripts/backtrader_four_stage_backtest.py \
  --db data/stock.db \
  --strategy-id 5 \
  --max-codes 20 \
  --quiet
```

只跑指定股票：

```bash
.venv/bin/python scripts/backtrader_four_stage_backtest.py \
  --db data/stock.db \
  --strategy-id 5 \
  --codes 600885.SH,002631.SZ
```

## 兼容：运行固定四段式脚本

```bash
.venv/bin/python scripts/backtrader_four_stage_backtest.py \
  --data data/csv_daily
```

CSV 必须包含：

```text
date,open,high,low,close
```

`volume` 和 `openinterest` 可缺省，脚本会补 0。

不传 `--strategy-id` 时，脚本仍会运行内置的 `FourStagePullbackStrategy`。
这个模式用于和 JoinQuant 四段式脚本做迁移对照，不再是 Streamlit 页面的默认模式。

## 回测语义

DSL 模式：

- 使用所选策略当前版本的 `lookback` 和 `conditions`。
- 信号由 `strategy_engine.evaluate_history()` 计算。
- 当日收盘后生成信号，Backtrader 市价单在下一根 bar 执行。
- 多只股票同时命中时，按代码顺序取前 `max_positions` 只。
- 默认预留 5% 现金，避免下一交易日跳空上涨和佣金导致 Backtrader `Margin` 拒单。

持仓规则：

- 每根日线 bar 调用一次 `next()`。
- 读取当天 DSL 命中信号。
- 最多持有 `max_positions` 只。
- 不在目标列表中的持仓清仓。
- 目标列表等权调仓。

## 基准 (benchmark)

页面右侧 `基准` 下拉可选沪深300 / 上证50 / 创业板指 / 中证500 / 无基准。
选定后点表单下方的 `📥 拉取/更新基准数据`，调 `tushare_api.ensure_index_bars(code, start, end)`
从 `pro.index_daily` 拉指数日线缓存到 `daily_bars` 表（指数 ts_code 与个股共用同一张表，
schema 兼容）。回测时策略净值与基准净值都归一化到 1.0 画双线对比。

CLI 等效：

```bash
.venv/bin/python scripts/backtrader_four_stage_backtest.py \
  --db data/stock.db --strategy-id 5 \
  --benchmark-code 000300.SH \
  --fromdate 2024-01-01 --todate 2024-06-01
```

注意：**CLI 不自动拉基准**，需先在页面或手动调 `ensure_index_bars` 拉好缓存。

## 策略总览指标

| 指标 | 来源 | 说明 |
|---|---|---|
| 年化收益 | 自算 | `(end/start)^(365/days) - 1` |
| 最大回撤 | 自算 | 组合 value 的滚动 max → drawdown 取最大 |
| 夏普 Sharpe | 自算 | `mean(daily_ret) / std(daily_ret) * sqrt(250)`，因 backtrader 内置 SharpeRatio 在窗口短时返回 0 |
| Sortino | 自算 | 同 Sharpe 但分母用 downside deviation（仅负收益）；backtrader 1.9.78.123 无内置 Sortino analyzer |
| Calmar | analyzer + fallback | backtrader `Calmar`；窗口短返回 0 时 fallback 为 `abs(annual)/max_dd` |
| 胜率/盈亏比/平均持仓天数 | 自算 | 由 `_pair_orders_to_trades` 配对的交易对聚合 |
| 交易次数 | 自算 | 已平仓交易对数 |

零成交回测（如窗口太短或策略太严）下所有指标安全返回 0，不抛除零。

## 交易对与持仓贡献

`_pair_orders_to_trades` 把订单层记录按 ts_code FIFO 配对成完整交易：
回测结束时仍有未平仓 BUY，输出一条 `sell_date=None` 的记录，UI 标「未平仓」。
持仓贡献 tab 按个股聚合 `交易次数 / 总盈亏 / 平均持仓天 / 贡献占比`，并按贡献排序画横向 bar。

## 当前本地数据注意

本地 `daily_bars` 是 Tushare 日线缓存，不包含聚宽的指数成分、停牌状态、涨跌停、
ST 状态、前复权字段。当前页面优先保证信号判断和项目内 DSL 引擎一致；和聚宽实盘
撮合细节仍会有差异。
