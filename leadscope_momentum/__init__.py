"""LeadScope 账户动量模块。

以库的形式提供给上层服务调用，数据持久化在本地 SQLite 文件中。
"""

from .engine import Impact, MomentumEngine

__all__ = ["Impact", "MomentumEngine"]
