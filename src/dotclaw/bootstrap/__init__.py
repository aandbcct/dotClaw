"""应用组合根。

``ApplicationHost`` 是唯一公开启动对象（``dotclaw.bootstrap.ApplicationHost``）；
``runtime_factory`` 与 ``_host_components`` 为其私有装配实现，不再经由本包公开导出。
"""

from .application_host import ApplicationHost

__all__ = ["ApplicationHost"]

