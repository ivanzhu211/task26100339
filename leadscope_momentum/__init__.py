"""LeadScope 账户动量模块。

由上层服务直接以库的形式调用，不包含接口层。
数据持久化在本地 SQLite 文件中。
"""

from .impact import Impact
from .store import MomentumStore

__all__ = ["Impact", "MomentumStore"]
