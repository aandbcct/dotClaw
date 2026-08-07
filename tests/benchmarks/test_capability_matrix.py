"""PR5 安全矩阵读取与固定装配测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.capability_matrix import build_registry, load_matrix


def _matrix_path() -> Path:
    return Path(__file__).resolve().parents[2] / "benchmarks" / "datasets" / "reliability_capability_v1" / "matrix.json"


def test_matrix_is_non_empty_and_case_ids_unique() -> None:
    """Git 跟踪的完整有限矩阵有稳定且唯一的 Case 标识。"""
    cases = load_matrix(_matrix_path())
    assert len(cases) >= 10
    assert len({case.case_id for case in cases}) == len(cases)


def test_matrix_duplicate_case_id_rejected(tmp_path: Path) -> None:
    """重复 Case 标识会拒绝，避免统计分母失真。"""
    payload = json.loads(_matrix_path().read_text(encoding="utf-8"))
    payload["cases"].append(dict(payload["cases"][0]))
    target = tmp_path / "matrix.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_matrix(target)


def test_registry_contains_only_recording_tools() -> None:
    """固定装配包含文件、进程、网络与 MCP 的记录型 Handler。"""
    assert set(build_registry().all_names()) == {
        "cap.file.read", "cap.file.write", "cap.process.exec", "cap.network.tavily",
        "cap.network.bad_host", "cap.mcp.github", "cap.mcp.evil",
    }
