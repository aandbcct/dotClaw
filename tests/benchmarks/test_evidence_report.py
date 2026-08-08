"""PR7 证据资格测试。"""

import json
from pathlib import Path

import pytest

from benchmarks.eval_baseline_stats import build_snapshot
from benchmarks.evidence_report import EvidenceQualificationError, coverage_groups, generate, qualify
from tests.benchmarks.helpers import make_sample


def _write_snapshot(root: Path, samples=(), **snapshot_overrides: object) -> Path:
    """写入一份最小完整快照及其正式 JSONL，供资格拒绝路径复用。"""
    jsonl_samples = tuple(samples) or (make_sample(formal_sampling=True, fixture_fingerprint="fixture-v1"),)
    snapshot_samples = (make_sample(formal_sampling=True, fixture_fingerprint="fixture-v1"),)
    sample_path = root / "samples" / "formal.jsonl"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text("".join(json.dumps(sample.to_dict()) + "\n" for sample in jsonl_samples), encoding="utf-8")
    snapshot = build_snapshot(snapshot_id="snapshot-1", generated_at="2026-08-08T00:00:00Z", git_commit="b6426cc", dataset="runtime_core_v1", environment={"python_version": "3.13.5", "platform": "Windows", "config_hash": "cfg-hash-1", "eval_schema_version": "1.0"}, warmup=0, repeat=len(snapshot_samples), samples=snapshot_samples, samples_path="samples/formal.jsonl", scenario_id="runtime_core_v1", samples_content_summary={"line_count": len(jsonl_samples)})
    payload = snapshot.to_dict()
    payload.update(snapshot_overrides)
    snapshot_path = root / "snapshot.json"
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
    return snapshot_path


def test_coverage_missing_files_rejected() -> None:
    """覆盖率 JSON 缺 files 时明确拒绝。"""
    with pytest.raises(EvidenceQualificationError):
        coverage_groups({})


@pytest.mark.parametrize("override", ({"git_commit": ""}, {"environment": {}}, {"fixture_fingerprints": {}}))
def test_qualification_rejects_missing_git_environment_or_fixture(tmp_path: Path, override: dict[str, object]) -> None:
    """Git、环境或固定 Fixture 缺失时不得进入证据清单。"""
    with pytest.raises(EvidenceQualificationError):
        qualify(_write_snapshot(tmp_path, **override))


def test_qualification_rejects_missing_jsonl(tmp_path: Path) -> None:
    """快照引用不存在的原始正式 JSONL 时明确失败。"""
    snapshot_path = _write_snapshot(tmp_path)
    (tmp_path / "samples" / "formal.jsonl").unlink()
    with pytest.raises(EvidenceQualificationError, match="JSONL"):
        qualify(snapshot_path)


@pytest.mark.parametrize(
    "sample",
    (
        make_sample(is_warmup=True, formal_sampling=False, fixture_fingerprint="fixture-v1"),
        make_sample(git_commit="other-commit", formal_sampling=True, fixture_fingerprint="fixture-v1"),
        make_sample(formal_sampling=True, fixture_fingerprint="fixture-v2"),
    ),
    ids=("warmup", "different_commit", "different_fixture"),
)
def test_qualification_rejects_nonformal_or_inconsistent_samples(tmp_path: Path, sample) -> None:
    """正式 JSONL 混入预热、不同提交或不同 Fixture 时不得形成证据。"""
    with pytest.raises(EvidenceQualificationError):
        qualify(_write_snapshot(tmp_path, (sample,)))


def test_generate_requires_explicit_snapshot_selection(tmp_path: Path) -> None:
    """证据生成器不得递归把目录内所有 JSON 自动视作待收口快照。"""
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps({"files": {}}), encoding="utf-8")
    with pytest.raises(EvidenceQualificationError, match="显式选择"):
        generate(tmp_path, coverage_path, tmp_path / "output")


def test_generate_accepts_explicit_complete_snapshot(tmp_path: Path) -> None:
    """显式选择的完整正式快照可生成可追溯清单。"""
    snapshot_path = _write_snapshot(tmp_path)
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps({"files": {}}), encoding="utf-8")
    manifest_path = generate(tmp_path, coverage_path, tmp_path / "output", (snapshot_path,))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["entries"][0]["formal_sample_count"] == 1
