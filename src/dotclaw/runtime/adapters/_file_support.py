"""文件仓储共享的 JSON 解析与原子写入工具。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from enum import IntEnum, StrEnum
from pathlib import Path

from ...common.utils import validate_path_segment
from ..domain.facts import JSONMap, JSONValue, require_json_map


_IS_WINDOWS = os.name == "nt"


class StorageFormatVersion(IntEnum):
    """Runtime v4 文件容器的唯一支持格式版本。"""

    CONTEXT_VERSIONS = 4


class RunStorageFileName(StrEnum):
    """单个运行目录中使用的固定文件名。"""

    RUN = "run.json"
    EVENTS = "events.jsonl"
    MESSAGES = "messages.json"
    CHECKPOINT = "checkpoint.json"
    CANCELLATION = "cancellation.json"
    CANCEL_COMMIT = "cancel_commit.json"
    SUCCESS_COMMIT = "success_commit.json"
    """成功提交期间临时存在的可恢复事务意图记录。"""


class SessionStorageFileName(StrEnum):
    """Session 目录中由新仓储负责的固定文件名。"""

    CONVERSATION = "conversation.json"


def load_json_map(path: Path) -> JSONMap:
    """读取 JSON 对象文件并校验根节点类型。"""
    # Windows的CRT读取共享占用可能只有errno13，不能据此当作文件不存在。
    # 只短暂重试目标权限错误；真实权限不足仍在固定上限后抛原异常。
    for attempt in range(5):
        try:
            raw_text: str = path.read_text(encoding="utf-8")
            break
        except PermissionError as error:
            winerror = getattr(error, "winerror", None)
            retryable = _IS_WINDOWS and (winerror in (5, 32, 33) or (winerror is None and error.errno == 13))
            if not retryable or attempt == 4:
                raise
            time.sleep(0.005 * (attempt + 1))
    decoded_value: JSONValue = json.loads(raw_text)
    return require_json_map(decoded_value)


def write_json_atomic(path: Path, payload: JSONMap) -> None:
    """在同目录创建临时文件后原子替换目标 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized_payload: str = json.dumps(payload, ensure_ascii=False, indent=2)
    file_descriptor: int
    temporary_path_text: str
    file_descriptor, temporary_path_text = tempfile.mkstemp(
        prefix=f".{path.stem}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path: Path = Path(temporary_path_text)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(serialized_payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        # Windows 的短暂共享占用不改变原子替换协议；各次尝试复用同一完整临时文件。
        for attempt in range(5):
            try:
                temporary_path.replace(path)
                break
            except PermissionError as error:
                if not _IS_WINDOWS or getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 4:
                    raise
                time.sleep(0.005 * (attempt + 1))
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
