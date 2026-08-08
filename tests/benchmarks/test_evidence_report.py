"""PR7 证据资格测试。"""

import pytest

from benchmarks.evidence_report import EvidenceQualificationError, coverage_groups


def test_coverage_missing_files_rejected() -> None:
    """覆盖率 JSON 缺 files 时明确拒绝。"""
    with pytest.raises(EvidenceQualificationError):
        coverage_groups({})
