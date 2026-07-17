"""文件仓储共享的 JSON 解析与原子写入工具。"""

from __future__ import annotations

import json
import os
import tempfile
from enum import IntEnum, StrEnum
from pathlib import Path

from ..domain.models import JSONMap, JSONValue, require_json_map


class StorageFormatVersion(IntEnum):
    """Runtime v2 文件容器的当前格式版本。"""

    INITIAL = 1


class RunStorageFileName(StrEnum):
    """单个运行目录中使用的固定文件名。"""

    RUN = "run.json"
    EVENTS = "events.jsonl"
    MESSAGES = "messages.json"
    CHECKPOINT = "checkpoint.json"


class SessionStorageFileName(StrEnum):
    """Session 目录中由新仓储负责的固定文件名。"""

    CONVERSATION = "conversation.json"


def validate_path_segment(value: str, field_name: str) -> str:
    """阻止运行标识越过仓储根目录。"""
    candidate: Path = Path(value)
    if not value or candidate.name != value or value in {".", ".."}:
        raise ValueError(f"{field_name} 必须是单个非空路径片段")
    return value


def load_json_map(path: Path) -> JSONMap:
    """读取 JSON 对象文件并校验根节点类型。"""
    raw_text: str = path.read_text(encoding="utf-8")
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
        temporary_path.replace(path)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
