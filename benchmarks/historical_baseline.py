"""PR2 历史基线 CLI：audit / run / compare 三个子命令。

链路（对应开发计划 §4）：

- ``audit``：对显式候选提交执行六道审计门，写出审计报告；通过后其 worktree 与
  独立解释器环境保留在审计输出目录，供 ``run`` 复用；
- ``run``：只接受通过同一审计输出确认的完整提交，执行正式采样并写出历史快照
  （复用 PR1 ``BenchmarkSnapshot`` 格式与基线目录布局）；
- ``compare``：读取当前与历史两份快照，仅在可比条件下生成对照 Markdown。

候选提交必须显式传入，不隐式扫描历史；历史不可复跑时交付可追溯审计报告而非
虚假的性能结论。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from .eval_baseline import write_jsonl
from .eval_baseline_models import (
    SCENARIO_TOOL_SUCCESS,
    BenchmarkSample,
    BenchmarkSnapshot,
    ExecutionSource,
)
from .eval_baseline_stats import build_snapshot
from .historical_audit import (
    AuditError,
    EnvironmentBoundary,
    GitBoundary,
    HistoricalAuditor,
    to_sample_record,
)
from .historical_compare import build_comparison_report, check_comparability
from .historical_legacy_agent_v1 import LegacyAgentV1Adapter

DEFAULT_DATASET: str = "runtime_core_v1"
DEFAULT_CASE: str = "tool_success"
DEFAULT_SCENARIO: str = SCENARIO_TOOL_SUCCESS
DEFAULT_AUDIT_ROOT: Path = Path("benchmarks") / "reports" / "historical-audits"
HISTORICAL_CONFIG_HASH: str = "historical-agent-v1-fixed-fixture"


def _load_audit_report(audit_output: Path) -> dict[str, object]:
    """读取审计报告；缺失或非法时明确失败。"""
    audit_json: Path = Path(audit_output) / "audit.json"
    if not audit_json.exists():
        raise FileNotFoundError(f"审计报告不存在：{audit_json}")
    try:
        return json.loads(audit_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"审计报告无法解析：{audit_json}：{error}") from error


def _require_audited_candidate(report: dict[str, object], candidate: str) -> str:
    """校验候选已通过审计且为完整提交号；不以短哈希代替。"""
    if not report.get("passed"):
        raise ValueError(f"候选 {candidate!r} 未通过审计，不能运行正式基线")
    full_commit: str = str(report.get("full_commit", ""))
    if not full_commit:
        raise ValueError(f"审计报告缺少完整提交号：{report.get('audit_id')}")
    if full_commit != candidate:
        raise ValueError(
            f"候选 {candidate!r} 与审计报告完整提交 {full_commit!r} 不一致，"
            f"run 只接受通过审计的完整提交号"
        )
    return full_commit


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


def cmd_audit(args: argparse.Namespace) -> int:
    """执行一次候选审计（开发期采样），落盘审计报告。"""
    output_root: Path = Path(args.output) if args.output else DEFAULT_AUDIT_ROOT
    adapter = LegacyAgentV1Adapter(
        dataset=args.dataset,
        case_id=args.case,
        scenario_id=args.scenario,
        script_dir=output_root / "scripts",
        subprocess_timeout=args.timeout,
    )
    auditor = HistoricalAuditor(
        repo_root=Path.cwd(),
        output_root=output_root,
        dataset=args.dataset,
        case_id=args.case,
        scenario_id=args.scenario,
        adapter=adapter,
    )
    order: tuple[str, ...] = tuple(args.candidate_order) if args.candidate_order else ()
    try:
        report = asyncio.run(
            auditor.audit(
                args.candidate,
                warmup=args.warmup,
                repeat=args.repeat,
                candidate_order=order,
                selection_note=args.note,
            )
        )
    except AuditError as error:
        print(f"审计失败：{error}", file=sys.stderr)
        print(f"审计报告：{output_root}", file=sys.stderr)
        return 1

    passed_gates = sum(1 for gate in report.gates if gate.passed)
    print("=== 历史候选审计 ===")
    print(f"  候选:      {report.candidate}")
    print(f"  完整提交:  {report.full_commit}")
    print(f"  审计 ID:   {report.audit_id}")
    print(f"  审计门:    {passed_gates}/{len(report.gates)} 通过")
    print(f"  场景:      {report.scenario_id}（warmup={args.warmup}, repeat={args.repeat}）")
    print(f"  worktree:  {report.worktree_path}")
    print(f"  环境:      {report.environment_path}")
    print(f"  采样:      {report.samples_path}")
    print("  结论:      审计通过，可冻结为历史基线" if report.passed else "  结论:      审计失败")
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    """对已审计候选执行正式采样并写出历史快照。"""
    audit_output: Path = Path(args.audit_output)
    report = _load_audit_report(audit_output)
    full_commit: str = _require_audited_candidate(report, args.candidate)

    worktree: Path = Path(str(report.get("worktree_path", "")))
    environment_path: Path = Path(str(report.get("environment_path", "")))
    if not worktree.exists():
        raise FileNotFoundError(f"审计 worktree 不存在：{worktree}")
    if not environment_path.exists():
        raise FileNotFoundError(f"审计环境不存在：{environment_path}")

    env_boundary = EnvironmentBoundary()
    python: Path = env_boundary.python_binary(environment_path / "venv")
    python_version: str = env_boundary.probe_python(python)
    git = GitBoundary(Path.cwd())
    git_commit: str = git.short_commit(full_commit)

    adapter = LegacyAgentV1Adapter(
        dataset=args.dataset,
        case_id=args.case,
        scenario_id=args.scenario,
        script_dir=audit_output / "scripts",
        subprocess_timeout=args.timeout,
    )
    adapter.verify_expected()

    formal_dir: Path = audit_output / "formal"
    all_samples: list[BenchmarkSample] = []
    formal_samples: list[BenchmarkSample] = []

    async def _sample_all() -> None:
        """连续执行正式采样，每轮使用独立临时状态目录。"""
        nonlocal all_samples, formal_samples
        for round_index in range(args.warmup + args.repeat):
            is_warmup: bool = round_index < args.warmup
            attempt: int = round_index if is_warmup else round_index - args.warmup
            scene = await adapter.run_scenario(
                worktree=worktree,
                python=python,
                state_dir=formal_dir / f"state-{round_index}",
                evidence_dir=formal_dir / f"evidence-{round_index}",
                attempt=attempt,
                is_warmup=is_warmup,
            )
            record = to_sample_record(
                scene,
                dataset=args.dataset,
                case_id=args.case,
                scenario_id=args.scenario,
                git_commit=git_commit,
                full_commit=full_commit,
                attempt=attempt,
                is_warmup=is_warmup,
            )
            sample = BenchmarkSample.from_dict(record)
            all_samples.append(sample)
            if not is_warmup:
                formal_samples.append(sample)

    asyncio.run(_sample_all())

    # 正式采样数量校验：每个 Case 恰好 repeat 条
    for case_id in {sample.case_id for sample in all_samples}:
        count = sum(1 for sample in formal_samples if sample.case_id == case_id)
        if count != args.repeat:
            raise ValueError(
                f"Case {case_id!r} 正式采样数为 {count}，与 repeat={args.repeat} 不一致"
            )

    snapshot_id: str = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{git_commit}"
    baseline_dir: Path = Path(args.save_baseline)
    samples_path: str = f"samples/{snapshot_id}.jsonl"
    environment: dict[str, str] = {
        "python_version": python_version,
        "platform": platform.platform(),
        "config_hash": HISTORICAL_CONFIG_HASH,
        "eval_schema_version": "1.0",
    }
    snapshot = build_snapshot(
        snapshot_id=snapshot_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        git_commit=git_commit,
        dataset=args.dataset,
        environment=environment,
        warmup=args.warmup,
        repeat=args.repeat,
        samples=all_samples,
        samples_path=samples_path,
        execution_source=ExecutionSource.HISTORICAL_ADAPTER,
        scenario_id=args.scenario,
        samples_content_summary={},
    )

    baseline_json: Path = baseline_dir / f"{snapshot_id}.json"
    baseline_jsonl: Path = baseline_dir / samples_path
    if baseline_json.exists() or baseline_jsonl.exists():
        raise FileExistsError(f"历史快照或采样已存在，拒绝覆盖：{snapshot_id}")

    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / "samples").mkdir(parents=True, exist_ok=True)
    write_jsonl(baseline_jsonl, all_samples)
    baseline_json.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 历史基线快照 ===")
    print(f"  Snapshot:  {snapshot_id}")
    print(f"  提交:      {full_commit}")
    print(f"  场景:      {args.scenario} | warmup={args.warmup} | repeat={args.repeat}")
    print(f"  成功率:    {snapshot.global_summary.passed_count}/{snapshot.global_summary.sample_count}")
    print(f"  Wall P50:  {snapshot.global_summary.wall_duration_ms.p50_ms:.1f} ms")
    print(f"  基线:      {baseline_json}")
    return 0


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #


def cmd_compare(args: argparse.Namespace) -> int:
    """读取当前与历史快照并生成对照报告。"""
    current = BenchmarkSnapshot.from_dict(
        json.loads(Path(args.current).read_text(encoding="utf-8"))
    )
    historical = BenchmarkSnapshot.from_dict(
        json.loads(Path(args.historical).read_text(encoding="utf-8"))
    )
    comparability = check_comparability(current, historical)
    report_text = build_comparison_report(
        current, historical, shared_scenarios=comparability.shared_scenarios
    )
    output_path: Path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_text, encoding="utf-8")

    print("=== 当前/历史对照 ===")
    if comparability.comparable:
        print(f"  可比:     是（共享场景 {', '.join(comparability.shared_scenarios)}）")
    else:
        print("  可比:     否")
        for reason in comparability.reasons:
            print(f"    - {reason}")
    print(f"  报告:      {output_path}")
    return 0 if comparability.comparable else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _add_common(parser: argparse.ArgumentParser) -> None:
    """公共参数：Dataset、Case 与场景。"""
    parser.add_argument("--dataset", type=str, default=DEFAULT_DATASET, help="Dataset 名称")
    parser.add_argument("--case", type=str, default=DEFAULT_CASE, help="Case 标识")
    parser.add_argument("--scenario", type=str, default=DEFAULT_SCENARIO, help="业务场景标识")


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(description="dotClaw PR2：历史基线可复跑与对照")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="对候选提交执行可复跑性审计")
    audit.add_argument("--candidate", type=str, required=True, help="候选提交（完整或短哈希）")
    _add_common(audit)
    audit.add_argument("--warmup", type=int, default=1, help="开发期预热采样数（默认 1）")
    audit.add_argument("--repeat", type=int, default=10, help="开发期正式采样数（默认 10）")
    audit.add_argument("--output", type=str, default=None, help="审计输出根目录")
    audit.add_argument("--candidate-order", type=str, nargs="*", default=None, help="审计的候选序列（首个通过者依据）")
    audit.add_argument("--note", type=str, default="", help="选择该候选的理由")
    audit.add_argument("--timeout", type=float, default=180.0, help="场景子进程超时秒数")
    audit.set_defaults(handler=cmd_audit)

    run = sub.add_parser("run", help="对已审计候选执行正式采样并生成历史快照")
    run.add_argument("--candidate", type=str, required=True, help="已审计通过的完整提交号")
    _add_common(run)
    run.add_argument("--warmup", type=int, default=5, help="正式预热采样数（默认 5）")
    run.add_argument("--repeat", type=int, default=30, help="正式采样数（默认 30）")
    run.add_argument("--audit-output", type=str, required=True, help="审计输出目录（含 audit.json）")
    run.add_argument("--save-baseline", type=str, required=True, help="历史基线保存目录")
    run.add_argument("--timeout", type=float, default=180.0, help="场景子进程超时秒数")
    run.set_defaults(handler=cmd_run)

    compare = sub.add_parser("compare", help="生成当前/历史对照报告")
    compare.add_argument("--current", type=str, required=True, help="当前快照 JSON 路径")
    compare.add_argument("--historical", type=str, required=True, help="历史快照 JSON 路径")
    compare.add_argument("--output", type=str, required=True, help="对照报告 Markdown 路径")
    compare.set_defaults(handler=cmd_compare)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "warmup", 0) < 0:
        parser.error("--warmup 必须大于等于 0")
    if getattr(args, "repeat", 1) <= 0:
        parser.error("--repeat 必须大于 0")

    try:
        return int(args.handler(args))
    except (AuditError, FileExistsError, FileNotFoundError, ValueError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
