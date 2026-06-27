"""LLM Provider 注册表

装饰器注册模式：每个 provider 客户端使用 @register("name") 注册自身。
Router 通过 get_provider() 查找并实例化。

自动发现：import 本包时自动导入所有 *_client.py 子模块，
触发 @register 装饰器完成注册。
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Type

logger = logging.getLogger("dotclaw.llm.providers")

from ..base import LLMClient

# ============================================================
# 注册表
# ============================================================

_registry: dict[str, Type[LLMClient]] = {}


def register(provider_name: str):
    """装饰器：将 LLMClient 子类注册为指定 provider 的实现。

    Usage:
        @register("qwen")
        class QwenClient(OpenAICompatibleClient):
            ...
    """
    def decorator(cls: Type[LLMClient]) -> Type[LLMClient]:
        _registry[provider_name] = cls
        logger.debug("注册 provider: %s → %s", provider_name, cls.__name__)
        return cls
    return decorator


def get_provider(provider_name: str) -> Type[LLMClient] | None:
    """根据 provider 名称查找注册的客户端类。"""
    if not _registry:
        _discover()
    return _registry.get(provider_name)


# ============================================================
# 自动发现
# ============================================================

_auto_discovered = False


def _discover():
    """自动导入 providers/ 下所有客户端模块，触发 @register 装饰器执行。"""
    global _auto_discovered
    if _auto_discovered:
        return
    _auto_discovered = True

    current_dir = Path(__file__).parent
    for f in current_dir.glob("*.py"):
        module_name = f.stem
        if module_name.startswith("_") or module_name == "__init__":
            continue
        full_name = f"dotclaw.llm.providers.{module_name}"
        try:
            importlib.import_module(full_name)
        except Exception as exc:
            logger.warning("自动发现 provider 模块 '%s' 失败: %s", full_name, exc)
