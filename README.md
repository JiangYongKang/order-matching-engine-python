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
| POST | `/orders` | 下单（需 `idempotency_key`），拒单返回 422 + 拒绝原因 |
| POST | `/orders/{order_id}/cancel` | 撤单；终态订单幂等返回当前状态 |
| GET | `/orders/{order_id}` | 查询订单状态/已成交量 |
| GET | `/trades?symbol=` | 查询成交记录 |
| POST | `/accounts/{id}/deposit` | 注入资金/持仓（本地验证用） |
| GET | `/accounts/{id}` | 账户快照（可用/冻结） |

下单请求体新增可选字段：`time_in_force`（`GTC`/`IOC`/`FOK`）与
`protection_price`（市价单价格保护，分），均不传时行为与旧版本完全一致，详见下文。

## 撮合规则

- **价格优先、时间优先**：买单按价格降序、卖单按价格升序，同价按到达序号（seq）先到先撮合。
- **成交价取挂单价（maker price）**：吃单方按更优价格成交。
- **状态机**：`ACCEPTED → PARTIALLY_FILLED → FILLED / CANCELLED`，`REJECTED` 为终态拒单。
  `FILLED / CANCELLED / REJECTED` 为终态，终态订单不再参与撮合、不可再变更。

### 成交方式（`time_in_force`，下单可选）

| 取值 | 语义 | 适用 |
|---|---|---|
| `GTC` | 立即成交，剩余继续挂单（`ACCEPTED`/`PARTIALLY_FILLED`） | 限价单 |
| `IOC` | 立即成交，剩余部分自动撤销并释放冻结（终态 `CANCELLED`，含部分成交后撤销） | 限价/市价 |
| `FOK` | 立即全部成交，否则**整单不成交**（原子失败，终态 `REJECTED` + `FOK_UNFILLABLE`） | 限价/市价 |

- **不传 `time_in_force` 时与旧行为完全一致**：限价单按 `GTC` 挂盘，市价单按 `IOC`
  立即成交剩余撤销。老的下单调用无需任何改动。
- **FOK 原子性**：撮合前先按当前订单簿做流动性预检（只读，不改状态）。价格边界内
  可成交量不足时整单失败：不产生任何成交、冻结全额释放、账务逐项不变、对手挂单
  不受影响；该结论同样入 WAL 并参与幂等（同一 `idempotency_key` 重提返回同一拒绝结论）。
  预检通过则保证全部成交。

### 市价单价格保护（`protection_price`，下单可选）

- **市价买**：`protection_price` 为可接受的成交价**上限**（分），成交价超过上限的
  档位不吃，剩余按成交方式处理（撤销或 FOK 整单失败）。
- **市价卖**：`protection_price` 为可接受的成交价**下限**，低于下限的档位不吃。
- **默认口径（不传/传 0）**：保护价 = 下单时最优对手价（买=最优卖一，卖=最优买一；
  卖侧无报价时下限为 0 即不设限）。保证市价买成交总额绝不超过下单时锁定额度。
- 仅市价单可用；限价单带 `protection_price`、市价单显式 `GTC`、非法取值均拒单
  `INVALID_ORDER`。
- 订单查询接口（`GET /orders/{id}`）返回实际生效的 `time_in_force` 与
  `protection_price`，以及最终状态与 `reject_reason`。

## 风控与冻结语义

下单时在**同一把全局锁内**完成「检查 + 冻结」，检查与变更原子，并发下不会超卖或超额占用：

- **限价买单**：冻结 `price × quantity` 现金，不足则拒单 `INSUFFICIENT_FUNDS`。
- **市价买单**：按价格保护价（显式上限或默认最优卖一）冻结 `protection_price × quantity`；
  未指定保护价且对手方无报价时拒单 `NO_LIQUIDITY`。成交总额不会超过该锁定额度。
- **卖单**：冻结 `quantity` 持仓，不足则拒单 `INSUFFICIENT_POSITION`。
- **成交结算**：买方按冻结单价扣减、按实际成交价计成本，**差额退回可用**；卖方冻结
  持仓扣减并收款。
- **释放规则**：撤单、IOC/市价剩余撤销、FOK 整单失败时，剩余冻结（现金或持仓）
  全额释放回可用；FOK 失败等价于「冻结后立即全额解冻」，账务净变化为零。
- **守恒不变量**：任意时刻全系统 `现金(可用+冻结)` 与 `持仓(可用+冻结)` 总量守恒，
  可用与冻结均不为负，测试中对每次场景做断言。
- 拒绝原因可区分：`INVALID_ORDER / UNKNOWN_SYMBOL / INSUFFICIENT_FUNDS /
  INSUFFICIENT_POSITION / NO_LIQUIDITY / FOK_UNFILLABLE / DUPLICATE_IDEMPOTENCY_KEY`。

## 幂等与状态恢复

- **幂等**：每个请求携带 `idempotency_key`。重复提交（含并发重复、拒单重试）直接返回
  首次结论（`duplicate=true`），不重新冻结、不重复成交、不重复扣减。
- **恢复**：所有改变状态的操作（入金、订单接受、成交、撤单、拒单）先追加到
  `data/wal.jsonl`（JSON Lines，写后 fsync）再生效。进程重启时按序回放事件，
  确定性重建订单簿与账务，与重启前结论一致；序号（seq）从 WAL 恢复，不会冲突。
- **兼容**：旧格式 WAL（无 `time_in_force` / `protection_price` 字段、拒单事件无
  `order_id`）按原语义读入：限价单解析为 GTC、市价单为 IOC、保护价为默认口径，
  历史结论不因新增字段而改变。
- **并发**：引擎内所有变更在一把可重入锁下串行执行，撤单与成交竞争时同一订单只有
  一个终态，且撤单返回值与最终态一致。

## 本地验证

```bash
uv run pytest -q                          # 全部测试
uv run pytest -o log_cli=true --log-cli-level=INFO   # 查看订单标识/判定依据/最终状态日志
```

测试覆盖：价格/时间优先、部分成交与剩余挂单、市价 IOC、终态不再撮合、
FOK 全成/整单原子失败（无半成交、无冻结残留、账务不变）、IOC 剩余撤销、
市价买/卖价格保护与默认口径、成交总额不超锁定额度、FOK 失败幂等重放、
并发 FOK 原子性、并发买入不超额冻结、并发卖出不超卖、撤单与成交竞争唯一终态
（200 轮对抗）、并发重复提交单次执行、拒绝原因可区分、重启恢复一致性（含新字段）、
旧格式 WAL 兼容、恢复后幂等仍生效、全场景账务总量守恒。

手工验证示例：

```bash
curl -X POST localhost:8000/accounts/alice/deposit -H 'content-type: application/json' -d '{"amount": 1000000}'
curl -X POST localhost:8000/accounts/carol/deposit -H 'content-type: application/json' -d '{"symbol":"BTC-USDT","quantity":100}'
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{"idempotency_key":"s1","account_id":"carol","symbol":"BTC-USDT","side":"SELL","order_type":"LIMIT","price":500,"quantity":5}'
# FOK：要么全成要么整单失败
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{"idempotency_key":"b1","account_id":"alice","symbol":"BTC-USDT","side":"BUY","order_type":"LIMIT","price":600,"quantity":5,"time_in_force":"FOK"}'
# 市价买 + 价格保护上限 550 分
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{"idempotency_key":"m1","account_id":"alice","symbol":"BTC-USDT","side":"BUY","order_type":"MARKET","quantity":3,"protection_price":550}'
curl localhost:8000/trades
# 重启进程后再次查询，订单与账务状态一致
```

## 代码结构

```
engine/models.py     订单/成交/状态/拒绝原因/成交方式 数据模型
engine/orderbook.py  订单簿（价格优先、时间优先）
engine/ledger.py     账户账务：冻结/扣减/释放/结算，守恒校验
engine/engine.py     撮合引擎：风控、成交方式、价格保护、幂等、WAL 回放恢复（全局锁串行化）
engine/store.py      JSON Lines WAL（追加 + fsync + 回放）
main.py              FastAPI 接口层
tests/               撮合规则 / 成交方式与价格保护 / 并发 / 幂等恢复 / API 测试
```
