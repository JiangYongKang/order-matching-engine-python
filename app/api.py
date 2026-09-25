"""FastAPI 装配与路由：下单、撤单、撮合结果查询。

数据全部保存在本地内存 + 本地 WAL 文件，不依赖任何外部服务或真实凭据。
WAL 路径由环境变量 MATCHING_ENGINE_WAL 配置（默认 data/events.jsonl）；
交易品种由 MATCHING_ENGINE_SYMBOLS 配置（逗号分隔，默认 AAA,BBB）。
"""

from __future__ import annotations

import os
from decimal import Decimal

from fastapi import FastAPI, HTTPException

from app.engine import MatchingEngine
from app.enums import (
    CancelFailReason,
    OrderRejected,
)
from app.log_store import EventLog
from app.schemas import (
    CancelResponse,
    OrderSubmitRequest,
    OrderView,
    RejectResponse,
)

WAL_PATH = os.environ.get("MATCHING_ENGINE_WAL", "data/events.jsonl")
SYMBOLS = [
    s.strip()
    for s in os.environ.get("MATCHING_ENGINE_SYMBOLS", "AAA,BBB").split(",")
    if s.strip()
]


def create_engine(seed_demo: bool = True) -> MatchingEngine:
    """构造引擎、打开 WAL 并完成事件重放（本地文件，无外部依赖）。"""
    log = EventLog(WAL_PATH).open()
    engine = MatchingEngine(log, SYMBOLS)
    engine.recover()
    if seed_demo:
        _seed_demo_accounts(engine)
    return engine


def _seed_demo_accounts(engine: MatchingEngine) -> None:
    """首次启动时注入演示账户（已存在则忽略），方便本地直接验证。"""
    demo = {
        "ACC-1": (Decimal("100000"), {}),
        "ACC-2": (Decimal("50000"), {s: Decimal("1000") for s in SYMBOLS}),
    }
    for account_id, (cash, positions) in demo.items():
        if engine.accounts.get(account_id) is None:
            engine.create_account(account_id, cash=cash, positions=positions)


def _order_view(o) -> OrderView:
    return OrderView(
        order_id=o.order_id,
        client_order_id=o.client_order_id,
        account_id=o.account_id,
        symbol=o.symbol,
        side=o.side,
        order_type=o.order_type,
        qty=o.qty,
        price=o.price,
        max_price=o.max_price,
        status=o.status,
        filled_qty=o.filled_qty,
        avg_fill_price=o.avg_fill_price,
        remaining_qty=o.remaining_qty,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="local-order-matching-engine", version="0.1.0")
    engine = create_engine()
    app.state.engine = engine

    @app.get("/health")
    def health():
        return {"status": "ok", "symbols": SYMBOLS, "wal": WAL_PATH}

    @app.post("/orders", response_model=OrderView,
              responses={400: {"model": RejectResponse}})
    def submit_order(req: OrderSubmitRequest):
        try:
            order = engine.submit_order(
                client_order_id=req.client_order_id,
                account_id=req.account_id,
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                qty=req.qty,
                price=req.price,
                max_price=req.max_price,
            )
        except OrderRejected as rej:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "ORDER_REJECTED",
                    "reason": rej.reason.value,
                    "detail": rej.detail,
                },
            )
        return _order_view(order)

    @app.post("/orders/{order_id}/cancel", response_model=CancelResponse)
    def cancel_order(order_id: str):
        result = engine.cancel_order(order_id)
        return CancelResponse(
            order_id=result.order_id,
            canceled=result.canceled,
            status=result.status,
            canceled_qty=result.canceled_qty,
            reason=result.reason,
        )

    @app.get("/orders/{order_id}", response_model=OrderView)
    def get_order(order_id: str):
        order = engine.get_order(order_id)
        if order is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "ORDER_NOT_FOUND",
                        "reason": CancelFailReason.ORDER_NOT_FOUND.value},
            )
        return _order_view(order)

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str):
        snap = engine.account_snapshot(account_id)
        if snap is None:
            raise HTTPException(status_code=404,
                                detail={"code": "ACCOUNT_NOT_FOUND"})
        return snap

    @app.get("/books/{symbol}")
    def get_book(symbol: str):
        return engine.book_snapshot(symbol)

    @app.get("/fills")
    def list_fills():
        return {"fills": engine.fills()}

    return app


app = create_app()
