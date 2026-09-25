"""测试夹具：内存 WAL 引擎 + 两个演示账户。"""

from decimal import Decimal as D

import pytest

from app.engine import MatchingEngine
from app.enums import OrderType
from app.log_store import EventLog

SYMBOL = "AAA"


@pytest.fixture
def engine():
    eng = MatchingEngine(EventLog(None).open(), [SYMBOL, "BBB"])
    eng.create_account("BUYER", cash=D("10000"))
    eng.create_account("SELLER", cash=D("1000"), positions={SYMBOL: D("100")})
    eng.create_account("RICH", cash=D("1000000"),
                       positions={SYMBOL: D("100000")})
    return eng


def place(eng, client, account, side, qty, price=None, *,
          otype=OrderType.LIMIT, max_price=None, symbol=SYMBOL):
    return eng.submit_order(
        client_order_id=client, account_id=account, symbol=symbol,
        side=side, order_type=otype, qty=D(qty),
        price=D(price) if price is not None else None,
        max_price=D(max_price) if max_price is not None else None)
