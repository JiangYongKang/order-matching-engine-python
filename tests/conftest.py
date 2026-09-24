import logging
import sys

import pytest

from engine.engine import MatchingEngine

SYMBOL = "BTC-USDT"


@pytest.fixture(autouse=True)
def _log_to_stdout(caplog):
    """单测日志输出订单标识、判定依据与最终状态。"""
    logging.getLogger("matching").setLevel(logging.INFO)
    with caplog.at_level(logging.INFO, logger="matching"):
        yield


@pytest.fixture
def engine():
    return MatchingEngine([SYMBOL])  # 纯内存，无 WAL


@pytest.fixture
def funded(engine):
    engine.deposit_cash("alice", 1_000_000)
    engine.deposit_cash("bob", 1_000_000)
    engine.deposit_position("carol", SYMBOL, 1_000)
    engine.deposit_position("dave", SYMBOL, 1_000)
    return engine
