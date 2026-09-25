# 本地内存撮合与风控订单执行引擎（Python）

单进程、零外部依赖的限价/市价订单撮合与风控服务，支持并发安全下单/撤单、
幂等提交、账务冻结与崩溃恢复。金额与数量使用 `Decimal`，账务严格守恒。

详细设计见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 能力概览
- 限价单 / 市价单撮合，**价格优先、时间优先**，允许部分成交，限价剩余继续挂单。
- 账户资金/持仓的**冻结、扣减、释放**，与成交结果严格对齐；现金与持仓总量守恒。
- 一把锁串行化所有写操作：并发下单不超卖/不超额占用；撤单与成交竞争时
  同一订单只有一个终态。
- 幂等键 `(account_id, client_order_id)`：重复提交不重复成交/扣减。
- 仅追加事件日志（WAL，fsync）：重启重放即可重建订单、账务与订单簿。
- 风控拒单原因可区分；单测日志打印订单标识、判定依据与最终状态。

## 目录
- `app/`：`enums / models / accounts / orderbook / log_store / engine / api`
- `tests/`：并发、部分成交、撤单竞争、风控拒单、重复提交、状态恢复
- `docs/DESIGN.md`：撮合规则、风控冻结语义、幂等与恢复保证

## 本地环境
- Python 3.14.4，依赖管理用 [uv](https://docs.astral.sh/uv/)。
- 安装：
  ```bash
  uv sync
  ```

## 运行服务
```bash
uv run fastapi dev main.py        # 或 uv run uvicorn main:app --reload
```
- 默认交易品种 `AAA,BBB`，WAL 文件 `data/events.jsonl`；可用环境变量覆盖：
  `MATCHING_ENGINE_SYMBOLS`、`MATCHING_ENGINE_WAL`。
- 首次启动自动注入演示账户 `ACC-1`（资金）与 `ACC-2`（资金+持仓）。

交互式文档：<http://127.0.0.1:8000/docs>

### 快速手工验证
```bash
curl -s localhost:8000/health
# 卖单
curl -s -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "client_order_id":"w1","account_id":"ACC-2","symbol":"AAA",
  "side":"SELL","order_type":"LIMIT","qty":"5","price":"100"}'
# 买单（立即成交）
curl -s -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "client_order_id":"n1","account_id":"ACC-1","symbol":"AAA",
  "side":"BUY","order_type":"LIMIT","qty":"3","price":"100"}'
# 撤单 / 查询 / 成交
curl -s -X POST localhost:8000/orders/<order_id>/cancel
curl -s localhost:8000/books/AAA
curl -s localhost:8000/fills
```

## 运行测试
```bash
uv run pytest -q
```
测试覆盖：
- `tests/test_matching.py`：价格/时间优先、部分成交、剩余挂单、市价保护价、终态不再撮合；
- `tests/test_risk_accounts.py`：各类拒单原因区分、冻结/释放、价差退回、总量守恒；
- `tests/test_concurrency_idempotency.py`：200 路并发不超卖、撤单/成交竞争终态唯一、重复提交、并发同幂等键；
- `tests/test_recovery.py`：重启后状态逐项一致、幂等映射恢复、恢复后继续撮合。

测试日志会打印订单标识、判定依据与最终状态（logger 名 `app.*`）。
