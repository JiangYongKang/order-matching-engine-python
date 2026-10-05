"""FastAPI 入口：下单、撤单、撮合结果查询、账户快照。

进程启动时自动从 WAL 回放恢复；所有状态均在本地内存，无外部依赖。
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from engine.engine import MatchingEngine
from engine.models import Order

SYMBOLS = ["BTC-USDT", "ETH-USDT"]
WAL_PATH = Path("data/wal.jsonl")

app = FastAPI(title="order-matching-engine")
engine = MatchingEngine(SYMBOLS, WAL_PATH)


class SubmitOrderRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    symbol: str
    side: str          # BUY / SELL
    order_type: str    # LIMIT / MARKET
    price: int = 0     # 限价（分）；市价单忽略
    quantity: int = Field(gt=0)
    time_in_force: str | None = None  # GTC / IOC / FOK；不传=限价 GTC、市价 IOC
    protection_price: int = 0  # 市价单价格保护（分）：买=上限，卖=下限；0=默认口径


class DepositRequest(BaseModel):
    amount: int = 0
    symbol: str = ""
    quantity: int = 0


def order_view(o: Order) -> dict:
    return {
        "order_id": o.order_id,
        "account_id": o.account_id,
        "symbol": o.symbol,
        "side": o.side.value,
        "order_type": o.order_type.value,
        "price": o.price,
        "quantity": o.quantity,
        "remaining": o.remaining,
        "filled": o.quantity - o.remaining,
        "status": o.status.value,
        "reject_reason": o.reject_reason.value,
        "time_in_force": o.time_in_force.value if o.time_in_force else None,
        "protection_price": o.protection_price,
    }


@app.get("/")
def read_root():
    return {"service": "order-matching-engine", "symbols": SYMBOLS}


@app.post("/orders", status_code=200)
def submit_order(req: SubmitOrderRequest):
    """下单。幂等：同一 idempotency_key 重复提交返回首次结论，不重复执行。"""
    result = engine.submit_order(
        req.idempotency_key, req.account_id, req.symbol,
        req.side, req.order_type, req.price, req.quantity,
        time_in_force=req.time_in_force, protection_price=req.protection_price)
    body = {
        "accepted": result.accepted,
        "order_id": result.order_id,
        "status": result.status.value,
        "reject_reason": result.reject_reason.value,
        "duplicate": result.duplicate,
        "trades": [
            {"trade_id": t.trade_id, "price": t.price, "quantity": t.quantity,
             "buy_order_id": t.buy_order_id, "sell_order_id": t.sell_order_id}
            for t in result.trades
        ],
    }
    if not result.accepted and not result.duplicate:
        raise HTTPException(status_code=422, detail=body)
    return body


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str):
    """撤单。已终态订单返回当前终态（幂等），不存在返回 404。"""
    try:
        status = engine.cancel_order(order_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "status": status.value}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = engine.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order_view(order)


@app.get("/trades")
def list_trades(symbol: str | None = None):
    return [
        {"trade_id": t.trade_id, "symbol": t.symbol, "price": t.price,
         "quantity": t.quantity, "buy_order_id": t.buy_order_id,
         "sell_order_id": t.sell_order_id}
        for t in engine.list_trades(symbol)
    ]


@app.post("/accounts/{account_id}/deposit")
def deposit(account_id: str, req: DepositRequest):
    """注入资金/持仓（本地验证用）。"""
    if req.amount > 0:
        engine.deposit_cash(account_id, req.amount)
    if req.symbol and req.quantity > 0:
        engine.deposit_position(account_id, req.symbol, req.quantity)
    return engine.snapshot_account(account_id)


@app.get("/accounts/{account_id}")
def get_account(account_id: str):
    return engine.snapshot_account(account_id)
