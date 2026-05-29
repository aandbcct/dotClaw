"""单例工具

提供 SingletonMeta 元类和 @singleton 装饰器，支持 reset() 用于测试。
"""

from __future__ import annotations

from threading import Lock


class SingletonMeta(type):
    """线程安全的单例元类"""

    _instances: dict[type, object] = {}
    _lock = Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]

    @classmethod
    def reset(cls, target_cls: type | None = None):
        """重置单例实例（用于测试）。不传参重置全部。"""
        if target_cls is not None:
            cls._instances.pop(target_cls, None)
        else:
            cls._instances.clear()
