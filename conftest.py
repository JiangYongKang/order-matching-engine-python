"""pytest 根配置：使项目根在 sys.path，并统一打印判定日志。"""

import logging
import sys

# 让单测运行时也能在 stdout 看到订单判定日志（订单标识/判定依据/最终状态）。
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
)
logging.getLogger("app").addHandler(_handler)
logging.getLogger("app").setLevel(logging.INFO)
