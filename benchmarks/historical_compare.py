"""PR2 当前/历史对照纯函数：快照可比性检查与 Markdown 对照报告。

对比规则（对应开发计划 §4.3 / §6）：

- 两份快照必须具有相同 Dataset、共享场景、正式 repeat、warmup、机器标识、
  Python 主/次版本和固定替身配置；任一条件不一致时报告不可比原因，不计算变化率；
- 对比仅在两侧均具备非空测量值时计算 ``(current - historical) / historical``；
  历史值为 0、缺失或语义不一致时仅列原值和不可比原因；
- 成功率报告成功数/总数、Wilson 95% 区间和绝对错误数；时延报告样本数、
  P50/P95/P99、最大值和变化率；调用数报告均值及分布。

本模块只做无副作用的计算与报告文本生成，不读写文件。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .eval_baseline_models import (
    CaseSummary,
    ExecutionSource,
    LatencyStats,
    BenchmarkSnapshot,
)

_WILSON_Z: float = 1.959963984540054
"""95% 置信度对应的正态分位数。"""


@dataclass(frozen=True)
class ComparabilityResult:
    """两份快照的可比性结论。"""

    comparable: bool
    reasons: Sequence[str] = ()
    shared_scenarios: Sequence[str] = ()

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "comparable": self.comparable,
            "reasons": list(self.reasons),
            "shared_scenarios": list(self.shared_scenarios),
        }


def _python_major_minor(snapshot: BenchmarkSnapshot) -> str:
    """取 Python 主/次版本（如 3.13）；无法解析时返回空串。"""
    version = str(snapshot.environment.get("python_version", ""))
    parts = version.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return ""


def _scenario_set(scenario_id: str) -> frozenset[str]:
    """把快照场景标识解析为场景集合（空标识视为空集）。"""
    if not scenario_id:
        return frozenset()
    return frozenset(item for item in scenario_id.split(",") if item)


def _scenario_case_consistency(snapshot: BenchmarkSnapshot) -> bool:
    """快照声明场景集合与 Case 列表一致，防止内容摘要被篡改。"""
    declared = _scenario_set(snapshot.scenario_id)
    actual = frozenset(case.case_id for case in snapshot.cases)
    return declared == actual


def check_comparability(
    current: BenchmarkSnapshot,
    historical: BenchmarkSnapshot,
) -> ComparabilityResult:
    """检查两份快照是否满足对照条件。

    只允许当前 Eval 快照与历史适配快照对照；任一环境或采样条件不一致、
    无共享场景、或场景标识与 Case 列表不一致（疑似篡改）时判为不可比。
    """
    reasons: list[str] = []

    if current.execution_source is not ExecutionSource.CURRENT_EVAL:
        reasons.append(f"当前快照执行来源应为 current_eval，实际 {current.execution_source.value}")
    if historical.execution_source is not ExecutionSource.HISTORICAL_ADAPTER:
        reasons.append(f"历史快照执行来源应为 historical_adapter，实际 {historical.execution_source.value}")

    if current.dataset != historical.dataset:
        reasons.append(
            f"Dataset 不一致：当前 {current.dataset!r}，历史 {historical.dataset!r}"
        )
    if current.repeat != historical.repeat:
        reasons.append(f"正式 repeat 不一致：当前 {current.repeat}，历史 {historical.repeat}")
    if current.warmup != historical.warmup:
        reasons.append(f"warmup 不一致：当前 {current.warmup}，历史 {historical.warmup}")

    current_platform = str(current.environment.get("platform", ""))
    historical_platform = str(historical.environment.get("platform", ""))
    if current_platform != historical_platform:
        reasons.append(
            f"机器标识不一致：当前 {current_platform!r}，历史 {historical_platform!r}"
        )
    current_python = _python_major_minor(current)
    historical_python = _python_major_minor(historical)
    if current_python and historical_python and current_python != historical_python:
        reasons.append(f"Python 主/次版本不一致：当前 {current_python}，历史 {historical_python}")
    elif not current_python or not historical_python:
        reasons.append("两侧 Python 版本标识缺失，无法确认环境一致")

    # 固定替身配置：两侧都必须有已固定的配置哈希记录
    if str(current.environment.get("config_hash", "unknown")) == "unknown":
        reasons.append("当前配置哈希缺失，无法确认配置固定")
    if str(historical.environment.get("config_hash", "unknown")) == "unknown":
        reasons.append("历史替身配置哈希缺失，无法确认配置固定")

    # 场景语义：共享场景必须非空，且两侧场景声明与 Case 列表一致（防篡改）
    current_scenarios = _scenario_set(current.scenario_id)
    historical_scenarios = _scenario_set(historical.scenario_id)
    if not _scenario_case_consistency(current):
        reasons.append("当前快照场景标识与 Case 列表不一致（疑似篡改）")
    if not _scenario_case_consistency(historical):
        reasons.append("历史快照场景标识与 Case 列表不一致（疑似篡改）")
    shared: list[str] = sorted(current_scenarios & historical_scenarios)
    if not shared:
        reasons.append(
            f"无共享场景：当前 {sorted(current_scenarios)}，历史 {sorted(historical_scenarios)}"
        )

    return ComparabilityResult(
        comparable=not reasons,
        reasons=tuple(reasons),
        shared_scenarios=tuple(shared),
    )


def percent_change(current: float, historical: float) -> float | None:
    """计算 ``(current - historical) / historical``；历史为 0 或缺失时返回 None。"""
    if historical == 0:
        return None
    return (current - historical) / historical


def wilson_interval(passed: int, total: int) -> tuple[float, float]:
    """计算成功率（分子/分母）的 Wilson 95% 置信区间。"""
    if total <= 0:
        raise ValueError(f"总样本数必须大于 0，实际 {total}")
    proportion: float = passed / total
    denominator: float = 1.0 + _WILSON_Z * _WILSON_Z / total
    centre: float = (proportion + _WILSON_Z * _WILSON_Z / (2.0 * total)) / denominator
    half: float = (
        _WILSON_Z
        * math.sqrt(proportion * (1.0 - proportion) / total + _WILSON_Z * _WILSON_Z / (4.0 * total * total))
        / denominator
    )
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class MetricComparison:
    """一项指标的当前/历史对照。"""

    metric: str
    current_value: object
    historical_value: object
    change_pct: float | None = None
    note: str = ""


def _latency_comparisons(
    current: LatencyStats,
    historical: LatencyStats,
    prefix: str = "",
) -> list[MetricComparison]:
    """对时延分布的 P50/P95/P99/Max 逐项构造对照；历史为 0 时仅列原值。"""
    rows: list[MetricComparison] = []
    for key in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
        current_value = getattr(current, key)
        historical_value = getattr(historical, key)
        change = percent_change(current_value, historical_value)
        note = ""
        if change is None and historical_value == 0:
            note = "历史值为 0，不计算变化率"
        rows.append(
            MetricComparison(
                metric=f"{prefix}{key}",
                current_value=round(current_value, 3),
                historical_value=round(historical_value, 3),
                change_pct=change,
                note=note,
            )
        )
    return rows


def _case_comparisons(current: CaseSummary, historical: CaseSummary) -> list[MetricComparison]:
    """对单个共享 Case 构造全部可比指标对照。"""
    rows: list[MetricComparison] = []

    current_rate = current.success_rate
    historical_rate = historical.success_rate
    rows.append(
        MetricComparison(
            metric="成功率",
            current_value=f"{current.passed_count}/{current.sample_count} ({current_rate:.2%})",
            historical_value=f"{historical.passed_count}/{historical.sample_count} ({historical_rate:.2%})",
            change_pct=percent_change(current_rate, historical_rate),
        )
    )
    rows.extend(_latency_comparisons(current.wall_duration_ms, historical.wall_duration_ms, "wall_"))
    current_llm_mean = current.llm_call_count_total / current.sample_count if current.sample_count else 0
    historical_llm_mean = historical.llm_call_count_total / historical.sample_count if historical.sample_count else 0
    rows.append(
        MetricComparison(
            metric="LLM 调用均值",
            current_value=round(current_llm_mean, 3),
            historical_value=round(historical_llm_mean, 3),
            change_pct=percent_change(current_llm_mean, historical_llm_mean),
        )
    )
    current_tool_mean = current.tool_call_count_total / current.sample_count if current.sample_count else 0
    historical_tool_mean = historical.tool_call_count_total / historical.sample_count if historical.sample_count else 0
    rows.append(
        MetricComparison(
            metric="Tool 调用均值",
            current_value=round(current_tool_mean, 3),
            historical_value=round(historical_tool_mean, 3),
            change_pct=percent_change(current_tool_mean, historical_tool_mean),
        )
    )
    return rows


def _format_change(change_pct: float | None, note: str) -> str:
    """格式化变化率；不可比时仅列原因。"""
    if change_pct is None:
        return note or "—（无法计算）"
    return f"{change_pct:+.2%}"


def build_comparison_report(
    current: BenchmarkSnapshot,
    historical: BenchmarkSnapshot,
    *,
    shared_scenarios: Sequence[str],
) -> str:
    """生成当前/历史对照的 Markdown 报告文本。"""
    lines: list[str] = [
        "# dotClaw 当前/历史 Eval 基线对照",
        "",
        f"> 当前：`{current.snapshot_id}`（{current.git_commit}，{current.execution_source.value}）",
        f"> 历史：`{historical.snapshot_id}`（{historical.git_commit}，{historical.execution_source.value}）",
        f"> Dataset：`{current.dataset}`",
        f"> 采样：warmup={current.warmup}，repeat={current.repeat}",
        "",
        "## 可比性检查",
        "",
    ]
    comparability = check_comparability(current, historical)
    if comparability.comparable:
        lines.append(f"- **可比**：共享场景 {', '.join(comparability.shared_scenarios)}")
    else:
        lines.append("- **不可比**，不计算任何变化率：")
        for reason in comparability.reasons:
            lines.append(f"  - {reason}")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "",
            "## 共享场景对照",
            "",
            "| 场景 | 指标 | 当前 | 历史 | 变化率 |",
            "|---|---|---|---|---|",
        ]
    )
    shared_set = set(shared_scenarios)
    for case in current.cases:
        if case.case_id not in shared_set:
            continue
        historical_case = next(
            (item for item in historical.cases if item.case_id == case.case_id), None
        )
        if historical_case is None:
            continue
        rows = _case_comparisons(case, historical_case)
        for row in rows:
            lines.append(
                f"| `{case.case_id}` | {row.metric} | {row.current_value} | {row.historical_value} "
                f"| {_format_change(row.change_pct, row.note)} |"
            )

    lines.extend(["", "## 成功率区间（Wilson 95%）", "", "| 场景 | 当前 | 历史 |", "|---|---|---|"])
    for case in current.cases:
        if case.case_id not in shared_set:
            continue
        historical_case = next(
            (item for item in historical.cases if item.case_id == case.case_id), None
        )
        if historical_case is None:
            continue
        current_lo, current_hi = wilson_interval(case.passed_count, case.sample_count)
        historical_lo, historical_hi = wilson_interval(
            historical_case.passed_count, historical_case.sample_count
        )
        lines.append(
            f"| `{case.case_id}` | {case.passed_count}/{case.sample_count} "
            f"({current_lo:.1%}~{current_hi:.1%}) | {historical_case.passed_count}/{historical_case.sample_count} "
            f"({historical_lo:.1%}~{historical_hi:.1%}) |"
        )

    lines.extend(
        [
            "",
            "## 不可比指标",
            "",
            "- 历史链路没有 Trace / token / 内部阶段时延（记录为 `null`），不参与变化率计算。",
            "- `wall_duration_ms` 为端到端口径，Trace 关键路径仅用于解释内部耗时，两者不可互相替代。",
            "- 成功率是隔离 Fixture / 固定脚本 LLM 下的语义通过率，不代表真实模型线上成功率。",
            "- P50/P95/P99 仅在同机、同环境、同 Dataset、同配置、同 repeat 下可比。",
        ]
    )
    lines.append("")
    return "\n".join(lines)
