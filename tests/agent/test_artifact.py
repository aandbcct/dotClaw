"""测试 Artifact —— Agent 执行产物的纯数据类。"""

import pytest

from dotclaw.agent.artifact import Artifact, ArtifactType


class TestArtifactType:
    """ArtifactType 枚举。"""

    def test_text_value(self) -> None:
        """ArtifactType.TEXT 值为 "text"。"""
        assert ArtifactType.TEXT.value == "text"

    def test_file_value(self) -> None:
        """ArtifactType.FILE 值为 "file"。"""
        assert ArtifactType.FILE.value == "file"

    def test_json_value(self) -> None:
        """ArtifactType.JSON 值为 "json"。"""
        assert ArtifactType.JSON.value == "json"


class TestArtifact:
    """Artifact dataclass 构造和序列化。"""

    def test_basic_text_artifact(self) -> None:
        """基本 text 类型构造。"""
        a = Artifact(name="summary", artifact_type=ArtifactType.TEXT, content="hello")
        assert a.name == "summary"
        assert a.artifact_type == ArtifactType.TEXT
        assert a.content == "hello"
        assert a.mime_type == ""
        assert a.uri == ""
        assert a.metadata == {}

    def test_file_artifact(self) -> None:
        """file 类型带 mime_type 和 uri。"""
        a = Artifact(
            name="report",
            artifact_type=ArtifactType.FILE,
            mime_type="application/pdf",
            uri="/tmp/report.pdf",
        )
        assert a.artifact_type == ArtifactType.FILE
        assert a.mime_type == "application/pdf"
        assert a.uri == "/tmp/report.pdf"

    def test_json_artifact_with_metadata(self) -> None:
        """json 类型带 metadata。"""
        a = Artifact(
            name="analysis",
            artifact_type=ArtifactType.JSON,
            content='{"score": 95}',
            metadata={"source": "llm", "version": 2},
        )
        assert a.artifact_type == ArtifactType.JSON
        assert a.metadata == {"source": "llm", "version": 2}

    def test_to_dict_text(self) -> None:
        """序列化 text artifact。"""
        a = Artifact(name="log", artifact_type=ArtifactType.TEXT, content="done")
        d = a.to_dict()
        assert d == {
            "name": "log",
            "artifact_type": "text",
            "content": "done",
            "mime_type": "",
            "uri": "",
            "metadata": {},
        }

    def test_from_dict_text(self) -> None:
        """反序列化 text artifact。"""
        d = {
            "name": "log",
            "artifact_type": "text",
            "content": "done",
        }
        a = Artifact.from_dict(d)
        assert a.name == "log"
        assert a.artifact_type == ArtifactType.TEXT
        assert a.content == "done"
        assert a.mime_type == ""
        assert a.uri == ""
        assert a.metadata == {}

    def test_roundtrip(self) -> None:
        """序列化再反序列化保持一致。"""
        a = Artifact(
            name="data",
            artifact_type=ArtifactType.JSON,
            content='{"a":1}',
            metadata={"k": "v"},
        )
        a2 = Artifact.from_dict(a.to_dict())
        assert a2.name == a.name
        assert a2.artifact_type == a.artifact_type
        assert a2.content == a.content
        assert a2.metadata == a.metadata

    def test_default_values(self) -> None:
        """默认值 — 仅需 name 即可构造最小 Artifact。"""
        a = Artifact(name="minimal")
        assert a.name == "minimal"
        assert a.artifact_type == ArtifactType.TEXT
        assert a.content == ""
        assert a.mime_type == ""
        assert a.uri == ""
        assert a.metadata == {}
