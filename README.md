# order-matching-engine-python

本地内存撮合与风控的订单执行服务。提供下单、撤单、撮合结果查询与账户快照，
不依赖任何外部服务或真实凭据，全部状态在进程内存中，辅以本地 WAL 文件做恢复。

## 运行

```bash
uv sync
uv run uvicorn main:app --reload
```

标的：`BTC-USDT`、`ETH-USDT`。价格单位为「分」（整数），数量为整数股。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/orders` | 下单（需 `idempotency_key`；可选 `time_in_force`、`protection_price`），拒单返回 422 + 拒绝原因 |
| POST | `/orders/{order_id}/cancel` | 撤单；终态订单幂等返回当前状态 |
| GET | `/orders/{order_id}` | 查询订单状态/已成交量 |
| GET | `/trades?symbol=` | 查询成交记录 |
| POST | `/accounts/{id}/deposit` | 注入资金/持仓（本地验证用） |
| GET | `/accounts/{id}` | 账户快照（可用/冻结） |

## 撮合规则

- **价格优先、时间优先**：买单按价格降序、卖单按价格升序，同价按到达序号（seq）先到先撮合。
- **成交价取挂单价（maker price）**：吃单方按更优价格成交。
- **状态机**：`ACCEPTED → PARTIALLY_FILLED → FILLED / CANCELLED`，`REJECTED` 为下单即拒。
  `FILLED / CANCELLED / REJECTED` 为终态，终态订单不再参与撮合、不可再变更。

### 成交方式（time_in_force）

下单可选传 `time_in_force`，取值与语义：

| 取值 | 语义 | 剩余量处理 |
|---|---|---|
| `GTC` | 立即成交，剩余挂入订单簿（仅限价单） | 挂单，状态 `ACCEPTED` / `PARTIALLY_FILLED` |
| `IOC` | 立即成交，剩余部分自动撤销 | 撤销并释放冻结，终态 `CANCELLED` |
| `FOK` | 立即全部成交，否则整单不成交 | 无剩余：要么 `FILLED`，要么下单即拒 |

- **不传 `time_in_force` 时保持旧行为**：限价单按 `GTC`、市价单按 `IOC` 处理，
  老的下单调用不用改也能照常工作。
- **FOK 整单失败**：撮合前先做只读预检，盘口（含价格约束下）可立即成交量不足时
  整单拒单，原因 `FOK_NOT_FULLY_FILLABLE`；拒单发生在冻结之前，不会留下半成交、
  冻结残留或任何账务改动，同一幂等键重试返回同一结论。
- `MARKET + GTC` 视为非法（市价单必须立即成交），拒单 `INVALID_ORDER`。

### 市价单价格保护（protection_price）

市价单可选传 `protection_price`（分）：**买单=可接受的最高成交价，卖单=可接受的
最低成交价**。吃单过程中遇到超出边界的档位即停止，剩余量按成交方式处理
（IOC/默认 → 撤销；FOK 预检即计入该约束）。

- **市价买不传保护价的安全默认**：有效上限 = 下单时最优卖价（即冻结单价），
  不会向上吃更贵的档，保证**成交总额不超过下单时锁定的额度**。
- **市价卖不传保护价的默认**：有效下限 = 0（不约束），可向下吃完所有档位。
- `protection_price` 仅适用于市价单，限价单传该参数拒单 `INVALID_ORDER`。
- 新参数与最终结论（`FILLED` / `CANCELLED`(部分成交后撤销) / `REJECTED`+原因 /
  挂单）均可通过 `GET /orders/{order_id}` 与下单响应查看。

## 风控与冻结语义

下单时在**同一把全局锁内**完成「检查 + 冻结」，检查与变更原子，并发下不会超卖或超额占用：

- **限价买单**：冻结 `price × quantity` 现金，不足则拒单 `INSUFFICIENT_FUNDS`。
- **市价买单**：按有效价格上限冻结 `上限 × quantity`（上限 = 保护价，未指定时为
  最优卖价）；对手方无报价拒单 `NO_LIQUIDITY`。
- **卖单**：冻结 `quantity` 持仓，不足则拒单 `INSUFFICIENT_POSITION`。
- **成交结算**：买方按冻结单价扣减、按实际成交价计成本，**差额退回可用**；卖方冻结
  持仓扣减并收款。
- **冻结释放**：撤单、IOC 剩余撤销时释放剩余冻结；FOK 拒单发生在冻结之前，无冻结可留；
  FOK 成交与 IOC 成交部分的冻结在结算中扣减。任何路径下可用资金/冻结均不为负。
- **守恒不变量**：任意时刻全系统 `现金(可用+冻结)` 与 `持仓(可用+冻结)` 总量守恒，
  测试中对每次场景做断言。
- 拒绝原因可区分：`INVALID_ORDER / UNKNOWN_SYMBOL / INSUFFICIENT_FUNDS /
  INSUFFICIENT_POSITION / NO_LIQUIDITY / FOK_NOT_FULLY_FILLABLE /
  DUPLICATE_IDEMPOTENCY_KEY`。

## 幂等与状态恢复

- **幂等**：每个请求携带 `idempotency_key`。重复提交（含并发重复、拒单重试）直接返回
  首次结论（`duplicate=true`），不重新冻结、不重复成交、不重复扣减。
- **恢复**：所有改变状态的操作（入金、订单接受、成交、撤单、拒单）先追加到
  `data/wal.jsonl`（JSON Lines，写后 fsync）再生效。进程重启时按序回放事件，
  确定性重建订单簿与账务，与重启前结论一致；序号（seq）从 WAL 恢复，不会冲突。
- **兼容**：旧版 WAL（无 `time_in_force` / `protection_price` 字段）按原语义读入：
  缺省成交方式按旧默认归一化（LIMIT→GTC，MARKET→IOC），缺省保护价按 0（未指定），
  历史订单结论不因新增字段而改变。
- **并发**：引擎内所有变更在一把可重入锁下串行执行，撤单与成交竞争时同一订单只有
  一个终态，且撤单返回值与最终态一致。

## 本地验证

```bash
uv run pytest -q                          # 全部测试
uv run pytest -o log_cli=true --log-cli-level=INFO   # 查看订单标识/判定依据/最终状态日志
```

测试覆盖：价格/时间优先、部分成交与剩余挂单、市价 IOC、终态不再撮合、
FOK 整单成交/整单失败无副作用、IOC 剩余撤销、市价买/卖价格保护与安全默认、
非法参数组合、并发买入不超额冻结、并发卖出不超卖、并发 FOK 抢单原子性、
混合成交方式并发守恒、撤单与成交竞争唯一终态（200 轮对抗）、
并发重复提交单次执行、拒绝原因可区分、重启恢复一致性（含新参数）、
旧版 WAL 兼容读入、恢复后幂等仍生效、全场景账务总量守恒。

手工验证示例：

```bash
curl -X POST localhost:8000/accounts/alice/deposit -H 'content-type: application/json' -d '{"amount": 1000000}'
curl -X POST localhost:8000/accounts/carol/deposit -H 'content-type: application/json' -d '{"symbol":"BTC-USDT","quantity":100}'
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{"idempotency_key":"s1","account_id":"carol","symbol":"BTC-USDT","side":"SELL","order_type":"LIMIT","price":500,"quantity":5}'
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{"idempotency_key":"b1","account_id":"alice","symbol":"BTC-USDT","side":"BUY","order_type":"LIMIT","price":600,"quantity":5}'
# FOK：立即全部成交，否则整单拒单
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{"idempotency_key":"f1","account_id":"alice","symbol":"BTC-USDT","side":"BUY","order_type":"LIMIT","price":500,"quantity":10,"time_in_force":"FOK"}'
# 市价买 + 价格保护：成交价不超过 550，剩余自动撤销
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{"idempotency_key":"m1","account_id":"alice","symbol":"BTC-USDT","side":"BUY","order_type":"MARKET","quantity":5,"protection_price":550}'
curl localhost:8000/trades
# 重启进程后再次查询，订单与账务状态一致
```

## 代码结构

```
engine/models.py     订单/成交/状态/拒绝原因 数据模型
engine/orderbook.py  订单簿（价格优先、时间优先）
engine/ledger.py     账户账务：冻结/扣减/释放/结算，守恒校验
engine/engine.py     撮合引擎：风控、撮合、幂等、WAL 回放恢复（全局锁串行化）
engine/store.py      JSON Lines WAL（追加 + fsync + 回放）
main.py              FastAPI 接口层
tests/               撮合规则 / 并发 / 幂等恢复 / API 测试
```
