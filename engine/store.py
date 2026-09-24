"""事件溯源存储：WAL 追加与回放，用于进程重启后的状态恢复。

事件为 JSON 可序列化 dict，逐行追加（JSON Lines）。回放时按序读取，
由引擎确定性重放，重建订单簿与账务。写盘即 fsync，保证已接受事件不丢。
"""
from __future__ import annotations

import json
from pathlib import Path


class EventStore:
    """JSON Lines 追加式事件日志。path 为 None 时退化为纯内存（不持久化）。"""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def append(self, event: dict) -> None:
        if self._fh is None:
            return
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fh.flush()
        import os
        os.fsync(self._fh.fileno())

    def load_all(self) -> list[dict]:
        if self._path is None or not self._path.exists():
            return []
        events = []
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
