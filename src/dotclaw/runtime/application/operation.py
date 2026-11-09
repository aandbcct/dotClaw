"""能力任务的内存值对象和控制异常。"""

import asyncio
from dataclasses import dataclass
from enum import StrEnum


class RunOperationKind(StrEnum):
    """可取消的外部能力种类。"""

    CONTEXT = "context"
    LLM = "llm"
    TOOL = "tool"


@dataclass
class RunOperationHandle:
    """只由取消服务持有和修改的能力任务句柄。"""

    operation_id: str
    run_id: str
    kind: RunOperationKind
    call_id: str | None
    task: asyncio.Task
    cancel_sent: bool = False
    started_monotonic: float = 0.0
    cleanup_error: str | None = None


class RunOperationCancelled(Exception):
    """仅表示带持久化请求的能力子任务已响应取消。"""

    def __init__(self, run_id: str, operation_id: str, request_id: str) -> None:
        super().__init__("能力调用已取消")
        self.run_id = run_id
        self.operation_id = operation_id
        self.request_id = request_id


class RunOperationCleanupError(Exception):
    """能力任务退出仍不能证明资源已回收。"""

    def __init__(self, operation_id: str, diagnostic_code: str, message: str) -> None:
        super().__init__(message)
        self.operation_id = operation_id
        self.diagnostic_code = diagnostic_code
