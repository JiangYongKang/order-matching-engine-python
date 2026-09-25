# 本地内存撮合与风控订单执行引擎 — 设计说明

本服务在**单进程本地内存**中提供限价/市价单的下单、撤单与撮合结果查询，
配合**仅追加事件日志（WAL）**实现幂等与崩溃恢复。全部数据保存在本地，
**不依赖任何外部服务或真实凭据**。

- 金额与数量一律使用 `Decimal`，杜绝浮点误差，账务可严格守恒校验。
- 所有写操作由同一把可重入锁串行化，读操作在同锁内取一致性快照。

---

## 1. 撮合规则

### 1.1 价格优先、时间优先
订单簿（`app/orderbook.py`）对每个品种维护两本队列：

- 买单 bids：**价格高者优先**，同价按下单调配序号（`created_at_ns`）**早者优先**；
- 卖单 asks：**价格低者优先**，同价**早者优先**。

新到订单（taker）从对手方队首依次吃单，一旦队首价格不再满足即停止，
因为后续价位只会更差。成交价取 **maker（挂单方）价格**。

### 1.2 限价单与市价单
- **限价单**：买单 `limit >= 卖一价` 可成交；卖单 `limit <= 买一价` 可成交。
- **市价卖单**：无价格上限/下限，只要有买一就成交。
- **市价买单**：**必须提供保护价 `max_price`**，仅在 `卖一价 <= max_price`
  时成交；否则直接拒单（原因 `MARKET_BUY_NEEDS_PROTECTION`），
  防止无保护市价单在空簿/异常价格下超预期扣划资金。

### 1.3 部分成交与剩余量
- 允许部分成交；每笔成交更新双方 `filled_qty` 与成交量加权均价
  `avg_fill_price`。
- **限价单**未成交的剩余量继续挂簿（状态 `PARTIALLY_FILLED` / `NEW`）。
- **市价单**不挂簿；流动性不足导致的剩余量直接作废（状态置 `CANCELED`，
  事件 reason=`MARKET_REMAINDER_EXPIRED`），并释放对应冻结。

### 1.4 确定的状态流转
```
NEW ──成交──────► PARTIALLY_FILLED ──成交至满──► FILLED      （终态）
NEW/PARTIALLY_FILLED ──撤单或市价剩余作废──► CANCELED        （终态）
风控不通过 ───────────────────────────────► REJECTED        （终态，不生成在簿订单）
```
- 终态集合：`FILLED / CANCELED / REJECTED`。
- **已终止订单不再参与撮合**：撮合迭代会跳过并清理终态/零剩余订单；
  对终态订单撤单返回 `canceled=false`，原因 `ALREADY_TERMINAL`。
- 每次状态推进都自增 `version`，便于审计与乐观判定。

---

## 2. 风控与冻结语义

### 2.1 下单即冻结（判定与扣减原子完成）
在同一临界区内完成「风控判定 → 冻结 → 落盘接受」：

- **买单冻结资金** = `qty × freeze_price`
  - 限价单 `freeze_price = limit`；市价买 `freeze_price = max_price`。
- **卖单冻结持仓** = `qty`（按品种）。

冻结动作先检查可用侧是否足额，不足则抛 `OrderRejected` 且**不产生任何
冻结/订单**。由于检查与扣减在同一把锁内，**并发下单不可能超额占用资金或
造成超卖**。

### 2.2 账户不变式（任意时刻成立）
```
总现金   = cash_available + cash_frozen            （>=0，不可为负）
品种持仓 = position_available[s] + position_frozen[s]
```

### 2.3 成交记账（与成交结果严格对齐）
一笔成交同时记买卖双方账：
- 买方：按 `qty × 买方冻结价` 从冻结解冻；其中 `qty × 成交价` 划给卖方，
  **冻结价与成交价之间的价差（价格保护盈余）退回买方可用资金**；
  买方可用持仓增加 `qty`。
- 卖方：冻结持仓减少 `qty`，可用资金增加 `qty × 成交价`。

> 说明：买单按限价/保护价冻结，实际成交价可能更优。退回价差保证
> `cash_available + cash_frozen` 恒等于现金总额，冻结不会虚增。

### 2.4 撤单 / 剩余作废释放
撤单（或市价剩余作废）按**剩余量**原路释放：买退资金、卖退持仓。
已成交部分不释放（其资金/持仓已在成交时结清）。

### 2.5 可区分的拒绝原因
| reason | 含义 |
| --- | --- |
| `UNKNOWN_ACCOUNT` | 账户不存在 |
| `UNKNOWN_SYMBOL` | 品种不在白名单 |
| `INVALID_PRICE` | 限价单价格缺失或非正 |
| `INVALID_QTY` | 委托数量非正 |
| `MARKET_BUY_NEEDS_PROTECTION` | 市价买单缺少正保护价 |
| `INSUFFICIENT_AVAILABLE_CASH` | 可用资金不足以冻结 |
| `INSUFFICIENT_AVAILABLE_POSITION` | 可用持仓不足以冻结 |

撤单失败另有 `ORDER_NOT_FOUND` / `ALREADY_TERMINAL` / `NO_REMAINING_QTY`。

---

## 3. 幂等与状态恢复

### 3.1 幂等
幂等键为 `(account_id, client_order_id)`：
- 首次请求：风控 → 冻结 → 撮合 → 落盘，返回订单结论；
- **重复请求（即使参数不同）直接返回首次的同一订单**，不会重复冻结、
  重复成交或重复扣减；
- 并发使用相同幂等键时，因写操作串行化，只有一笔被接受，其余返回该单。

### 3.2 事件日志（WAL）
事件以 JSON Lines 追加写入本地文件，每条 `flush + fsync`，做到
**“已响应即已落盘”**。事件类型：

- `ACCOUNT_OPENED`：开户（初始资金/持仓）
- `ORDER_ACCEPTED`：订单通过风控并完成冻结（含订单快照与冻结额）
- `ORDER_FILLED`：一次成交（双方账户、成交价量、双方冻结价）
- `ORDER_CANCELED`：撤单或市价剩余作废（剩余量与冻结价）
- `ORDER_REJECTED`：风控拒单（可区分原因，仅审计，不改余额）

### 3.3 崩溃恢复
进程重启后调用 `engine.recover()`，**按事件原始 seq 单遍顺序重放**，
在每个订单“接受”的同一时间点立即重放撮合（二者在实时路径本就处于同一
临界区），在“撤单”事件的真实时间点释放剩余冻结：

1. 重放 `ACCOUNT_OPENED` 重建账户初始总额；
2. 遇到 `ORDER_ACCEPTED`：以零成交 NEW 重建订单、按冻结快照迁移
   可用→冻结，并用**当时的订单簿**立即重放撮合：
   - 推导出的成交若已存在于日志（键 `(taker, maker, price, qty)` 命中），
     只做一次账务与状态推进，**不重复落盘**；
   - 若日志中不存在（即“接受已 fsync、成交未及 fsync”的崩溃窗口），
     则按相同的价格/时间优先确定性补记账，并以 `recovered=true` 补落盘，
     结论与崩溃前内存中已发生的成交逐笔一致；
3. 遇到 `ORDER_CANCELED`（含市价剩余作废）：在该时间点释放剩余冻结、
   移出订单簿并置 `CANCELED`；已终态订单跳过；
4. 限价单撮合后的剩余量在重放时挂回订单簿；
5. 重建 `(账户, client_order_id) -> order_id` 幂等映射。

因此**重启后已接受订单与账务状态与重启前逐项一致**，恢复后可继续正常
撮合与撤单。`recover()` 可安全重复调用（结果幂等）；崩溃窗口补单在再次
重启时因已落盘而不会被重复记账。

> 一致性说明：采用“先落盘再应答 + 单线程串行撮合 + 全量事件单遍重放”的
> 简化模型，不依赖两阶段提交等分布式机制。成交事件对买卖双方是**同一条
> 原子记录**，因此不会出现只持久化单边成交的中间态；极端的“接受已落盘、
> 成交未落盘”窗口由上面的确定性补单收敛，账务不重不漏、总量守恒。

---

## 4. 并发与终态一致性

- 下单、撤单、撮合、记账均在 `MatchingEngine._lock`（`RLock`）临界区内，
  因而“冻结检查与扣减”“撤单释放与成交扣减”都不会交织。
- **撤单与成交竞争**同一订单时，先抢到临界区的操作决定命运：
  - 撤单先到：订单置 `CANCELED` 并移出订单簿，随后成交迭代看不到它；
  - 成交先到且打满：订单置 `FILLED`，随后撤单得到 `ALREADY_TERMINAL`。
  两者互斥，且恒有 `成交量 + 撤销量 = 原始委托量`。
- 所有金额/数量断言可用作回归：跨账户现金总量、持仓总量在任何场景守恒。

---

## 5. 模块结构
```
app/enums.py      枚举：方向/类型/状态/事件/拒绝原因，OrderRejected
app/models.py     Order / Fill / CancelResult 数据模型（Decimal）
app/accounts.py   Account / AccountBook：冻结、成交记账、释放与守恒快照
app/orderbook.py  价格-时间优先订单簿与惰性撮合序列
app/log_store.py  仅追加 JSONL 事件日志（fsync）与读取
app/engine.py     串行化引擎：风控、冻结、撮合、撤单、幂等、恢复
app/schemas.py    HTTP Pydantic 模型
app/api.py        FastAPI 路由装配 / 健康检查 / 演示账户
main.py           服务入口
tests/            并发、部分成交、撤单竞争、风控、幂等、恢复测试
```

## 6. HTTP 接口
- `GET  /health`
- `POST /orders`                     下单（body 见下）
- `POST /orders/{order_id}/cancel`   撤单
- `GET  /orders/{order_id}`          查询订单
- `GET  /accounts/{account_id}`      查询账户资金/持仓快照
- `GET  /books/{symbol}`             查询订单簿价位
- `GET  /fills`                      查询全部成交

下单请求示例：
```json
{
  "client_order_id": "c-001",
  "account_id": "ACC-1",
  "symbol": "AAA",
  "side": "BUY",
  "order_type": "LIMIT",
  "qty": "10",
  "price": "100.00",
  "max_price": null
}
```
风控拒单返回 `400`，body 的 `detail.reason` 给出上表中的可区分原因。
