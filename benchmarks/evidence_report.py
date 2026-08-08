"""PR7 证据清单 CLI：拒绝不完整快照与不可追溯样本。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from .eval_baseline_models import BenchmarkSample, BenchmarkSnapshot


class EvidenceQualificationError(ValueError):
    """证据资格（可进入 PR8 收口的完整可追溯条件）不满足。"""


def _read_json(path: Path) -> Mapping[str, object]:
    """读取 JSON 对象，根不是对象时显式失败。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvidenceQualificationError(f"{path} JSON 根必须是对象")
    return value


def load_samples(snapshot_path: Path, snapshot: BenchmarkSnapshot) -> tuple[BenchmarkSample, ...]:
    """按快照相对路径读取 JSONL，拒绝目录逃逸、空行和损坏记录。"""
    sample_path = (snapshot_path.parent / snapshot.samples_path).resolve()
    if snapshot_path.parent.resolve() not in sample_path.parents:
        raise EvidenceQualificationError(f"样本路径逃出快照目录：{snapshot.samples_path}")
    if not sample_path.is_file():
        raise EvidenceQualificationError(f"缺少原始 JSONL：{sample_path}")
    lines = sample_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise EvidenceQualificationError(f"原始 JSONL 为空：{sample_path}")
    return tuple(BenchmarkSample.from_dict(json.loads(line)) for line in lines)


def qualify(snapshot_path: Path) -> dict[str, object]:
    """校验单个快照与其正式样本是否可被 PR8 消费。"""
    snapshot = BenchmarkSnapshot.from_dict(_read_json(snapshot_path))
    if not snapshot.git_commit or snapshot.git_commit == "unknown":
        raise EvidenceQualificationError(f"{snapshot_path} 缺少固定 Git 提交")
    required_environment = {"python_version", "platform", "config_hash", "eval_schema_version"}
    if not required_environment.issubset(snapshot.environment):
        raise EvidenceQualificationError(f"{snapshot_path} 缺少环境或配置追溯字段")
    samples = load_samples(snapshot_path, snapshot)
    formal = [item for item in samples if not item.is_warmup]
    if not formal:
        raise EvidenceQualificationError(f"{snapshot_path} 不含正式样本")
    for sample in formal:
        if sample.git_commit != snapshot.git_commit:
            raise EvidenceQualificationError(f"{snapshot_path} 混入不同 Git 提交样本")
        if sample.formal_sampling is not True:
            raise EvidenceQualificationError(f"{snapshot_path} 包含未明确标记为正式的样本")
    return {"snapshot": str(snapshot_path), "suite": snapshot.dataset, "git_commit": snapshot.git_commit,
            "samples_path": snapshot.samples_path, "formal_sample_count": len(formal),
            "fixture_fingerprints": dict(snapshot.fixture_fingerprints), "environment": dict(snapshot.environment)}


def coverage_groups(coverage: Mapping[str, object]) -> dict[str, dict[str, int | str]]:
    """从 pytest-cov JSON 按真实源路径汇总指定目录；未命中明确写不适用。"""
    files = coverage.get("files")
    if not isinstance(files, dict):
        raise EvidenceQualificationError("coverage.json 缺少 files 对象")
    groups = {"Runtime": "src/dotclaw/runtime/", "Tool": "src/dotclaw/tools/", "Context": "src/dotclaw/context/", "Orchestration": "src/dotclaw/orchestration/", "LLM": "src/dotclaw/llm/"}
    result: dict[str, dict[str, int | str]] = {}
    for name, prefix in groups.items():
        summaries = [value.get("summary") for path, value in files.items() if isinstance(path, str) and path.replace("\\", "/").endswith(prefix.rstrip("/")) is False and prefix in path.replace("\\", "/") and isinstance(value, dict)]
        if not summaries:
            result[name] = {"status": "不适用/未覆盖"}
            continue
        covered = sum(int(item.get("covered_lines", 0)) for item in summaries if isinstance(item, dict))
        total = sum(int(item.get("num_statements", 0)) for item in summaries if isinstance(item, dict))
        covered_branches = sum(int(item.get("covered_branches", 0)) for item in summaries if isinstance(item, dict))
        total_branches = sum(int(item.get("num_branches", 0)) for item in summaries if isinstance(item, dict))
        result[name] = {"covered_lines": covered, "num_statements": total, "covered_branches": covered_branches, "num_branches": total_branches}
    return result


def generate(snapshot_root: Path, coverage_path: Path, output: Path, selected_snapshots: Sequence[Path] = ()) -> Path:
    """生成 JSON 清单和 Markdown 覆盖率报告；不会改写 README。"""
    candidates = tuple(selected_snapshots) if selected_snapshots else tuple(sorted(snapshot_root.rglob("*.json")))
    entries = [qualify(path) for path in candidates if path.suffix == ".json" and "/v1.0/" not in path.as_posix()]
    if not entries:
        raise EvidenceQualificationError("未找到可资格校验的快照")
    coverage = _read_json(coverage_path)
    groups = coverage_groups(coverage)
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence-manifest.json").write_text(json.dumps({"entries": entries, "coverage_groups": groups}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# PR7 证据清单", "", "仅完整正式样本可供 PR8 消费。", "", "## 覆盖率分层", "", "| 范围 | 已覆盖行 | 语句数 | 已覆盖分支 | 分支数 |", "|---|---:|---:|---:|---:|"]
    for name, value in groups.items():
        covered = value.get("covered_lines", value.get("status", "不适用/未覆盖"))
        lines.append(f"| {name} | {covered} | {value.get('num_statements', '')} | {value.get('covered_branches', '')} | {value.get('num_branches', '')} |")
    (output / "coverage.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output / "evidence-manifest.json"


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行并写出 PR7 证据工件。"""
    parser = argparse.ArgumentParser(description="PR7 正式证据清单")
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    generate(args.snapshots, args.coverage, args.output, args.snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
