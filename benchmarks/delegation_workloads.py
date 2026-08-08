"""PR7 固定委派工作负载定义；每轮配置独立临时存储根。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class ChildOutcome(StrEnum):
    """受控子 Run 终态（完成、失败、取消、放弃）。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class DelegationWorkloadConfig:
    """一次 PR7 运行的固定 Fixture 配置（不含生产 Runtime 状态）。"""

    fixture_version: str = "delegation-fixture-v1"
    fake_delay_ms: int = 10
    concurrent_parents: int = 8

    def to_dict(self) -> Mapping[str, object]:
        """返回可写入配置工件的稳定字典。"""
        return {"fixture_version": self.fixture_version, "fake_delay_ms": self.fake_delay_ms,
                "concurrent_parents": self.concurrent_parents}


def chain_request_id(parent_index: int, attempt: int) -> str:
    """生成不含业务正文的固定链路标识。"""
    return f"parent-{parent_index}-attempt-{attempt}"
