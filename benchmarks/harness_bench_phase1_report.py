from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


WORKSPACE_TASKS = (
    "001-file",
    "002-exec",
    "003-browser",
    "006-access-bilibili",
    "008-image-recognize",
    "013-image-edit",
    "020-archive-checksum",
    "021-batch-rename-transform",
    "022-local-rest-api-summary",
    "023-web-form-extraction",
    "077-archive-manifest-defense",
    "078-local-api-cursor-retry-ledger",
    "079-smallfile-batch-reject-ledger",
    "080-schema-roundtrip-conversion",
    "081-local-html-dom-form-extract",
)
LONG_RUNNING_TASKS = (
    "007-session-memory",
    "014-task-decomposition",
    "057-interruption-resume",
    "058-multiday-project-state",
    "059-event-update-replan",
    "060-task-cancellation-cleanup",
    "061-periodic-status-rollup",
    "103-policy-update-replan-diff",
    "104-async-ops-window-rollup",
    "105-partial-batch-resume-ledger",
    "106-release-approval-gate-plan",
)
TASKS = WORKSPACE_TASKS + LONG_RUNNING_TASKS
CATEGORY_BY_TASK = {
    **{task: "Workspace, Tool Use & Multimodal Operations" for task in WORKSPACE_TASKS},
    **{task: "Long-running Autonomy & State Adaptation" for task in LONG_RUNNING_TASKS},
}
SCORE_FIELDS = ("completion", "tool_use", "consistency", "robustness", "security", "combined")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def normalize_result(raw: dict[str, Any], harness: str) -> dict[str, Any]:
    """把 Harness-Bench 原始结果收敛为无密钥、可比较的逐题记录。"""
    task_id = str(raw.get("task_id") or "")
    scoring = raw.get("scoring") or {}
    rubric = scoring.get("rubric") or {}
    process_scores = rubric.get("scores") or {}
    usage = raw.get("usage_summary") or {}
    adapters = raw.get("adapter_results") or []
    if not adapters and raw.get("adapter_result"):
        adapters = [raw["adapter_result"]]
    execution_success = bool(adapters) and all(bool(item.get("ok")) for item in adapters)
    timed_out = any(bool((item.get("metadata") or {}).get("timeout")) for item in adapters)
    checks = (raw.get("oracle_result") or {}).get("checks") or []
    failed_checks = [
        {"id": item.get("id"), "label": item.get("label"), "detail": item.get("detail")}
        for item in checks
        if not item.get("pass")
    ]
    completion = _number(scoring.get("outcome_score"))
    return {
        "task_id": task_id,
        "category": CATEGORY_BY_TASK.get(task_id, "unknown"),
        "harness": harness,
        "model": raw.get("api_model_slug"),
        "metrics": {
            "completion": completion,
            "tool_use": _number(process_scores.get("tool_use_appropriate")),
            "consistency": _number(process_scores.get("consistency")),
            "robustness": _number(process_scores.get("robustness")),
            "security": _number(scoring.get("security_score")),
            "combined": _number(scoring.get("combined_score")),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "turns": int(usage.get("request_count") or 0),
            "elapsed_sec": _number(raw.get("elapsed_sec")) or 0.0,
        },
        "execution_success": execution_success,
        "completion_success": completion == 1.0,
        "timed_out": timed_out,
        "usage_available": bool(usage.get("available")),
        "oracle_failed_checks": failed_checks,
        "rubric_notes": rubric.get("notes"),
        "adapter_errors": [str(item.get("stderr") or "").strip() for item in adapters if not item.get("ok")],
    }


def load_results(directory: Path, harness: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for task_id in TASKS:
        path = directory / f"{task_id}.json"
        if not path.is_file():
            missing.append(task_id)
            continue
        records.append(normalize_result(json.loads(path.read_text(encoding="utf-8")), harness))
    if missing:
        raise ValueError(f"{harness} 缺少正式结果: {', '.join(missing)}")
    return records


def _mean(values: Iterable[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return statistics.fmean(clean) if clean else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    def section(items: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "task_count": len(items),
            "execution_success_count": sum(bool(item["execution_success"]) for item in items),
            "completion_success_count": sum(bool(item["completion_success"]) for item in items),
        }
        for field in SCORE_FIELDS:
            values = [item["metrics"][field] for item in items]
            summary[f"mean_{field}"] = _mean(values)
            summary[f"reported_{field}_count"] = sum(value is not None for value in values)
        for field in ("input_tokens", "output_tokens", "total_tokens", "turns", "elapsed_sec"):
            summary[f"mean_{field}"] = _mean(item["metrics"][field] for item in items)
            summary[f"total_{field}"] = sum(item["metrics"][field] for item in items)
        return summary

    return {
        "overall": section(records),
        "by_category": {
            category: section([item for item in records if item["category"] == category])
            for category in dict.fromkeys(CATEGORY_BY_TASK.values())
        },
    }


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _metric_row(label: str, key: str, left: dict[str, Any], right: dict[str, Any]) -> str:
    a = left.get(key)
    b = right.get(key)
    if key.startswith("mean_") and key.removeprefix("mean_") in SCORE_FIELDS:
        difference = "N/A" if a is None or b is None else f"{(a - b) * 100:+.2f} pp"
        return f"| {label} | {_percent(a)} | {_percent(b)} | {difference} |"
    if a is None or b is None:
        difference = "N/A"
    elif b == 0:
        difference = f"{a - b:+.2f} (relative N/A)"
    else:
        difference = f"{a - b:+.2f} ({(a - b) / b * 100:+.2f}%)"
    return f"| {label} | {a:.2f} | {b:.2f} | {difference} |" if a is not None and b is not None else f"| {label} | N/A | N/A | N/A |"


def comparison_markdown(dotclaw: dict[str, Any], nanobot: dict[str, Any]) -> str:
    blocks = ["# Harness-Bench Phase 1 Comparison", ""]
    sections = [("Overall", dotclaw["overall"], nanobot["overall"])]
    sections.extend(
        (category, dotclaw["by_category"][category], nanobot["by_category"][category])
        for category in dotclaw["by_category"]
    )
    metrics = (
        ("Completion", "mean_completion"),
        ("Tool Use", "mean_tool_use"),
        ("Consistency", "mean_consistency"),
        ("Robustness", "mean_robustness"),
        ("Security", "mean_security"),
        ("Combined", "mean_combined"),
        ("Avg Tokens", "mean_total_tokens"),
        ("Avg Turns", "mean_turns"),
    )
    for title, left, right in sections:
        blocks.extend([f"## {title}", "", "| Metric | dotClaw | NanoBot | Difference |", "|---|---:|---:|---:|"])
        blocks.extend(_metric_row(label, key, left, right) for label, key in metrics)
        blocks.append("")
    blocks.extend(
        [
            "## 口径",
            "",
            "- Completion、Tool Use、Consistency、Robustness、Security、Combined 的差值使用百分点（pp）。",
            "- Turns 使用官方 usage proxy 的 request_count；Tokens 使用同一 proxy 的 total_tokens。",
            "- 本表是每个 task 单次正式运行的描述性结果，不提供置信区间，也不把随机差异解释为稳定提升。",
            "",
        ]
    )
    return "\n".join(blocks)


def failures_markdown(dotclaw: list[dict[str, Any]], nanobot: list[dict[str, Any]]) -> str:
    lines = ["# Harness-Bench Phase 1 Failures", ""]
    failures = [item for item in [*dotclaw, *nanobot] if not item["execution_success"] or not item["completion_success"]]
    if not failures:
        return "\n".join([*lines, "没有执行失败或未满分任务。", ""])
    for item in failures:
        metrics = item["metrics"]
        evidence_text = json.dumps(
            [item["oracle_failed_checks"], item["adapter_errors"], item["rubric_notes"]], ensure_ascii=False
        ).lower()
        missing_capability = item["harness"] == "dotClaw" and item["task_id"] in {
            "008-image-recognize",
            "013-image-edit",
        }
        cause_flags = {
            "oracle_failure": bool(item["oracle_failed_checks"]) or metrics["completion"] != 1.0,
            "process_failure": not item["execution_success"] or item["timed_out"],
            "tool_failure": metrics["tool_use"] is not None and metrics["tool_use"] < 1.0,
            "state_failure": any(word in evidence_text for word in ("state", "session", "resume", "ledger", "状态")),
            "permission_failure": any(word in evidence_text for word in ("permission", "approval", "denied", "权限")),
            "missing_capability": missing_capability,
        }
        cause_flags["other"] = not any(cause_flags.values())
        lines.extend(
            [
                f"## {item['harness']} / {item['task_id']}",
                "",
                f"- Combined: {metrics['combined']}",
                f"- Completion: {metrics['completion']}",
                f"- 原因证据信号: `{json.dumps(cause_flags, ensure_ascii=False)}`",
                f"- Timeout: {item['timed_out']}",
                f"- Oracle failed checks: `{json.dumps(item['oracle_failed_checks'], ensure_ascii=False)}`",
                f"- Adapter errors: `{json.dumps(item['adapter_errors'], ensure_ascii=False)}`",
                f"- Rubric notes: {item['rubric_notes'] or 'N/A'}",
                "",
            ]
        )
    return "\n".join(lines)


def _git_sha(repo: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def generate(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    dotclaw = load_results(args.dotclaw_results.resolve(), "dotClaw")
    nanobot = load_results(args.nanobot_results.resolve(), "NanoBot")
    dotclaw_summary = summarize(dotclaw)
    nanobot_summary = summarize(nanobot)
    _write_jsonl(output / "dotclaw-results.jsonl", dotclaw)
    _write_jsonl(output / "nanobot-results.jsonl", nanobot)
    _write_json(output / "dotclaw-summary.json", dotclaw_summary)
    _write_json(output / "nanobot-summary.json", nanobot_summary)
    (output / "comparison.md").write_text(comparison_markdown(dotclaw_summary, nanobot_summary), encoding="utf-8")
    (output / "failures.md").write_text(failures_markdown(dotclaw, nanobot), encoding="utf-8")
    environment = {
        "harness_bench_commit": args.harness_commit,
        "dotclaw_commit": args.dotclaw_commit or _git_sha(Path.cwd()),
        "dotclaw_worktree_dirty_during_run": True,
        "nanobot_version": args.nanobot_version,
        "python_version": sys.version.split()[0],
        "os": platform.platform(),
        "model": args.model,
        "provider": args.provider,
        "endpoint_identifier": args.endpoint_identifier,
        "model_parameters": {"temperature": args.temperature, "max_tokens": args.max_tokens},
        "timeout": "official per-task task.yaml timeout_sec",
        "budget": {"dotclaw_max_tool_iterations": args.dotclaw_max_tool_iterations},
        "report_generated_at": datetime.now(timezone.utc).isoformat(),
        "run_timestamp": {"start": args.run_start, "end": args.run_end},
        "dotclaw_diff_sha256": args.dotclaw_diff_sha256,
        "harness_bench_worktree_patch_sha256": args.harness_patch_sha256,
        "task_list": list(TASKS),
        "sample_count_per_harness": len(TASKS),
        "attempts_per_task": 1,
        "secrets_persisted": False,
    }
    _write_json(output / "environment.json", environment)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Harness-Bench 第一阶段正式报告")
    parser.add_argument("--dotclaw-results", type=Path, required=True)
    parser.add_argument("--nanobot-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--harness-commit", required=True)
    parser.add_argument("--dotclaw-commit", default="")
    parser.add_argument("--nanobot-version", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--endpoint-identifier", required=True)
    parser.add_argument("--temperature", type=float, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--dotclaw-max-tool-iterations", type=int, required=True)
    parser.add_argument("--run-start", required=True)
    parser.add_argument("--run-end", required=True)
    parser.add_argument("--dotclaw-diff-sha256", required=True)
    parser.add_argument("--harness-patch-sha256", required=True)
    return parser


def main() -> None:
    generate(build_parser().parse_args())


if __name__ == "__main__":
    main()
