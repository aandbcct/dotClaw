"""PR3 并发统计纯函数：吞吐、排队/端到端时延、对照聚合与变化率。

本模块只做无副作用计算，输入为 ``BenchmarkSample`` 序列或已聚合的数值序列。
输出按场景、Session 数和调度模式汇总的统计结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .eval_baseline_models import BenchmarkSample, ScheduleMode
from .eval_baseline_stats import percentile


# --------------------------------------------------------------------------- #
# 并发统计结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConcurrencyLatencyStats:
    """并发实验的时延分布汇总。"""

    sample_count: int
    """有效样本数。"""

    p50_ms: float
    """P50 时延（毫秒）。"""

    p95_ms: float
    """P95 时延（毫秒）。"""

    max_ms: float
    """最大时延（毫秒）。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "sample_count": self.sample_count,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
        }

    @classmethod
    def from_values(cls, values: Sequence[float]) -> ConcurrencyLatencyStats:
        """从一组数值构造时延分布；空序列返回全 0。"""
        if not values:
            return cls(sample_count=0, p50_ms=0.0, p95_ms=0.0, max_ms=0.0)
        return cls(
            sample_count=len(values),
            p50_ms=percentile(values, 50.0),
            p95_ms=percentile(values, 95.0),
            max_ms=max(values),
        )


@dataclass(frozen=True)
class ScenarioStats:
    """单个场景的并发统计汇总。"""

    scenario_id: str
    """场景标识（ConcurrencyScenario 取值）。"""

    schedule_mode: str
    """调度模式（session_lock / global_lock）。"""

    total_requests: int
    """总请求数。"""

    total_batches: int
    """采样轮数。"""

    # 时延
    queue_wait_ms: ConcurrencyLatencyStats
    """排队等待时延分布。"""

    wall_duration_ms: ConcurrencyLatencyStats
    """端到端时延分布。"""

    cancel_delivery_ms: ConcurrencyLatencyStats
    """取消送达时延分布（仅取消场景）。"""

    cancel_effect_ms: ConcurrencyLatencyStats
    """取消生效时延分布（仅取消场景）。"""

    # 正确性
    fifo_passed_count: int
    """FIFO 顺序通过轮数。"""

    fifo_total_count: int
    """FIFO 顺序总轮数。"""

    isolation_passed_count: int
    """隔离通过轮数。"""

    isolation_total_count: int
    """隔离总轮数。"""

    cancel_passed_count: int
    """取消通过轮数。"""

    cancel_total_count: int
    """取消总轮数。"""

    # 隔离
    message_leak_total: int
    event_leak_total: int
    context_leak_total: int
    tool_leak_total: int
    stream_leak_total: int

    # 吞吐
    throughput_per_sec: float | None = None
    """每秒完成请求数（按批次总耗时计算）。"""

    batch_total_ms: float | None = None
    """批次总耗时（毫秒）。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "scenario_id": self.scenario_id,
            "schedule_mode": self.schedule_mode,
            "total_requests": self.total_requests,
            "total_batches": self.total_batches,
            "queue_wait_ms": self.queue_wait_ms.to_dict(),
            "wall_duration_ms": self.wall_duration_ms.to_dict(),
            "cancel_delivery_ms": self.cancel_delivery_ms.to_dict(),
            "cancel_effect_ms": self.cancel_effect_ms.to_dict(),
            "fifo_passed_count": self.fifo_passed_count,
            "fifo_total_count": self.fifo_total_count,
            "isolation_passed_count": self.isolation_passed_count,
            "isolation_total_count": self.isolation_total_count,
            "cancel_passed_count": self.cancel_passed_count,
            "cancel_total_count": self.cancel_total_count,
            "message_leak_total": self.message_leak_total,
            "event_leak_total": self.event_leak_total,
            "context_leak_total": self.context_leak_total,
            "tool_leak_total": self.tool_leak_total,
            "stream_leak_total": self.stream_leak_total,
            "throughput_per_sec": self.throughput_per_sec,
            "batch_total_ms": self.batch_total_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ScenarioStats:
        """从 JSON 字典反序列化。"""
        return cls(
            scenario_id=str(data["scenario_id"]),
            schedule_mode=str(data["schedule_mode"]),
            total_requests=int(data["total_requests"]),
            total_batches=int(data["total_batches"]),
            queue_wait_ms=ConcurrencyLatencyStats(
                sample_count=int(data["queue_wait_ms"]["sample_count"]),
                p50_ms=float(data["queue_wait_ms"]["p50_ms"]),
                p95_ms=float(data["queue_wait_ms"]["p95_ms"]),
                max_ms=float(data["queue_wait_ms"]["max_ms"]),
            ),
            wall_duration_ms=ConcurrencyLatencyStats(
                sample_count=int(data["wall_duration_ms"]["sample_count"]),
                p50_ms=float(data["wall_duration_ms"]["p50_ms"]),
                p95_ms=float(data["wall_duration_ms"]["p95_ms"]),
                max_ms=float(data["wall_duration_ms"]["max_ms"]),
            ),
            cancel_delivery_ms=ConcurrencyLatencyStats(
                sample_count=int(data["cancel_delivery_ms"]["sample_count"]),
                p50_ms=float(data["cancel_delivery_ms"]["p50_ms"]),
                p95_ms=float(data["cancel_delivery_ms"]["p95_ms"]),
                max_ms=float(data["cancel_delivery_ms"]["max_ms"]),
            ),
            cancel_effect_ms=ConcurrencyLatencyStats(
                sample_count=int(data["cancel_effect_ms"]["sample_count"]),
                p50_ms=float(data["cancel_effect_ms"]["p50_ms"]),
                p95_ms=float(data["cancel_effect_ms"]["p95_ms"]),
                max_ms=float(data["cancel_effect_ms"]["max_ms"]),
            ),
            fifo_passed_count=int(data["fifo_passed_count"]),
            fifo_total_count=int(data["fifo_total_count"]),
            isolation_passed_count=int(data["isolation_passed_count"]),
            isolation_total_count=int(data["isolation_total_count"]),
            cancel_passed_count=int(data["cancel_passed_count"]),
            cancel_total_count=int(data["cancel_total_count"]),
            message_leak_total=int(data["message_leak_total"]),
            event_leak_total=int(data["event_leak_total"]),
            context_leak_total=int(data["context_leak_total"]),
            tool_leak_total=int(data["tool_leak_total"]),
            stream_leak_total=int(data["stream_leak_total"]),
            throughput_per_sec=float(data["throughput_per_sec"]) if data.get("throughput_per_sec") is not None else None,
            batch_total_ms=float(data["batch_total_ms"]) if data.get("batch_total_ms") is not None else None,
        )


@dataclass(frozen=True)
class ConcurrencySnapshot:
    """一次完整并发实验的聚合快照。"""

    suite: str
    """实验族标识。"""

    generated_at: str
    """生成时间（ISO 8601）。"""

    git_commit: str
    """Git 提交号。"""

    warmup: int
    repeat: int
    fake_delay_ms: int

    scenarios: Sequence[ScenarioStats] = ()
    """各场景统计摘要。"""

    workload_configs: Sequence[dict[str, object]] = ()
    """各场景的工作负载配置。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "suite": self.suite,
            "generated_at": self.generated_at,
            "git_commit": self.git_commit,
            "warmup": self.warmup,
            "repeat": self.repeat,
            "fake_delay_ms": self.fake_delay_ms,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "workload_configs": list(self.workload_configs),
        }


# --------------------------------------------------------------------------- #
# 聚合函数
# --------------------------------------------------------------------------- #


def aggregate_scenario_stats(
    scenario_id: str,
    schedule_mode: str,
    samples: Sequence[BenchmarkSample],
    total_batches: int,
) -> ScenarioStats:
    """从一组 BenchmarkSample 聚合单个场景的统计。

    仅统计 is_warmup=False 的正式采样。
    """
    # 过滤正式采样
    formal = [s for s in samples if not s.is_warmup]
    total_requests = len(formal)

    # 排队等待时延
    queue_values: list[float] = [
        s.queue_wait_ms for s in formal
        if s.queue_wait_ms is not None and s.queue_wait_ms > 0
    ]

    # 端到端时延
    wall_values: list[float] = [s.wall_duration_ms for s in formal]

    # 取消时延
    cancel_delivery_values: list[float] = [
        s.cancel_delivery_ms for s in formal
        if s.cancel_delivery_ms is not None
    ]
    cancel_effect_values: list[float] = [
        s.cancel_effect_ms for s in formal
        if s.cancel_effect_ms is not None
    ]

    # 正确性统计
    fifo_passed: int = sum(
        1 for s in formal
        if s.accepted_seq is not None and s.execution_started_seq == s.accepted_seq
    )

    # 隔离统计
    message_leak_total: int = sum(
        s.message_leak_count for s in formal if s.message_leak_count is not None
    )
    event_leak_total: int = sum(
        s.event_leak_count for s in formal if s.event_leak_count is not None
    )
    context_leak_total: int = sum(
        s.context_leak_count for s in formal if s.context_leak_count is not None
    )
    tool_leak_total: int = sum(
        s.tool_leak_count for s in formal if s.tool_leak_count is not None
    )
    stream_leak_total: int = sum(
        s.stream_leak_count for s in formal if s.stream_leak_count is not None
    )

    # 取消统计
    cancel_passed: int = sum(
        1 for s in formal
        if s.cancellation_delivered is True
        and s.cancellation_effective is True
        and s.lock_released is True
        and s.followup_completed is True
    )

    return ScenarioStats(
        scenario_id=scenario_id,
        schedule_mode=schedule_mode,
        total_requests=total_requests,
        total_batches=total_batches,
        queue_wait_ms=ConcurrencyLatencyStats.from_values(queue_values),
        wall_duration_ms=ConcurrencyLatencyStats.from_values(wall_values),
        cancel_delivery_ms=ConcurrencyLatencyStats.from_values(cancel_delivery_values),
        cancel_effect_ms=ConcurrencyLatencyStats.from_values(cancel_effect_values),
        fifo_passed_count=fifo_passed,
        fifo_total_count=len(formal),
        isolation_passed_count=len(formal) - message_leak_total - event_leak_total - context_leak_total - tool_leak_total - stream_leak_total,
        isolation_total_count=len(formal),
        cancel_passed_count=cancel_passed,
        cancel_total_count=len(formal),
        message_leak_total=message_leak_total,
        event_leak_total=event_leak_total,
        context_leak_total=context_leak_total,
        tool_leak_total=tool_leak_total,
        stream_leak_total=stream_leak_total,
    )


def compute_throughput(
    total_requests: int,
    batch_total_ms: float,
) -> float:
    """计算吞吐量（每秒完成请求数）。

    参数：
        total_requests: 完成的请求总数。
        batch_total_ms: 批次总耗时（毫秒）。
    """
    if batch_total_ms <= 0:
        return 0.0
    return total_requests / (batch_total_ms / 1000.0)


def compute_change_rate(
    session_value: float,
    global_value: float,
) -> float | None:
    """计算 Session 锁相对全局锁的变化率。

    公式：``(session - global) / global``
    返回 None 表示不可比（global 为 0 或缺失）。
    """
    if global_value == 0.0:
        return None
    return (session_value - global_value) / global_value


def compare_schedule_modes(
    session_stats: ScenarioStats,
    global_stats: ScenarioStats,
) -> dict[str, object]:
    """对照两种调度模式的统计结果。

    仅计算两侧共享且条件一致的指标；0 基线、缺失值时不计算变化率。
    """
    comparison: dict[str, object] = {
        "scenario_id": session_stats.scenario_id,
        "session_lock": session_stats.to_dict(),
        "global_lock": global_stats.to_dict(),
    }

    # 吞吐变化率
    if session_stats.throughput_per_sec and global_stats.throughput_per_sec:
        comparison["throughput_change_rate"] = compute_change_rate(
            session_stats.throughput_per_sec, global_stats.throughput_per_sec
        )

    # P95 排队等待变化率
    if session_stats.queue_wait_ms.sample_count > 0 and global_stats.queue_wait_ms.sample_count > 0:
        comparison["queue_wait_p95_change_rate"] = compute_change_rate(
            session_stats.queue_wait_ms.p95_ms, global_stats.queue_wait_ms.p95_ms
        )

    # P95 端到端变化率
    if session_stats.wall_duration_ms.sample_count > 0 and global_stats.wall_duration_ms.sample_count > 0:
        comparison["wall_p95_change_rate"] = compute_change_rate(
            session_stats.wall_duration_ms.p95_ms, global_stats.wall_duration_ms.p95_ms
        )

    comparison["note"] = "变化率仅在同负载的两种模式间计算；Session 锁模式（当前生产）vs 全局锁（Benchmark 对照）；0 基线或缺失时不计算。"

    return comparison
