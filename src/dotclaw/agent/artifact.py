"""Artifact —— Agent 执行产生的产物。

对标 A2A Artifact：由 name + type + content/metadata 组成。
子 Agent 通过 Task.output_artifacts 回传产物给父 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ============================================================================
# ArtifactType
# ============================================================================


class ArtifactType(Enum):
    """产物类型枚举。

    对标 A2A Artifact 的 Part 类型。
    """

    TEXT = "text"
    """纯文本"""

    FILE = "file"
    """文件引用（通过 uri 定位）"""

    JSON = "json"
    """结构化 JSON 数据"""


# ============================================================================
# Artifact
# ============================================================================


@dataclass
class Artifact:
    """Agent 执行产物。

    对标 A2A Artifact。是 Agent 间传递文件/数据的载体。

    字段：
        name: 产物名称
        artifact_type: 产物类型（text/file/json）
        content: 文本内容（text/json 类型时使用）
        mime_type: MIME 类型（file 类型时使用）
        uri: 文件路径引用（file 类型时使用）
        metadata: 附加元数据
    """

    name: str
    """产物名称"""

    artifact_type: ArtifactType = ArtifactType.TEXT
    """产物类型"""

    content: str = ""
    """文本内容"""

    mime_type: str = ""
    """MIME 类型"""

    uri: str = ""
    """文件路径引用"""

    metadata: dict = field(default_factory=dict)
    """附加元数据"""

    # ── 序列化 ──

    def to_dict(self) -> dict:
        """序列化为 dict。artifact_type 存枚举值（字符串）。"""
        return {
            "name": self.name,
            "artifact_type": self.artifact_type.value,
            "content": self.content,
            "mime_type": self.mime_type,
            "uri": self.uri,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Artifact:
        """从 dict 反序列化。"""
        at_raw: str = data.get("artifact_type", "text")
        return cls(
            name=data.get("name", ""),
            artifact_type=ArtifactType(at_raw),
            content=data.get("content", ""),
            mime_type=data.get("mime_type", ""),
            uri=data.get("uri", ""),
            metadata=dict(data.get("metadata", {})),
        )
