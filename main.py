"""本地内存撮合与风控订单执行服务入口。

运行：uv run fastapi dev main.py  或  uv run uvicorn main:app
"""

from app.api import app

__all__ = ["app"]
