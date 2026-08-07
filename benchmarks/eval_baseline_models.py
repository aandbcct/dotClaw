"""Eval 基线数据模型：BenchmarkSample（单次采样记录）与 BenchmarkSnapshot（汇总快照）。

本模块只定义可序列化的派生测试记录，不承担执行、统计或写盘逻辑。两类模型都复用
Runtime / Eval 既有事实的只读视图，不新增持久化容器，也不内联正文或敏感内容：

- ``BenchmarkSample`` 是单次实验结果的派生记录，按 JSONL 逐条追加，warmup 与
  正式采样均写出并以 ``is_warmup`` 区分；
- ``BenchmarkSnapshot`` 是由同一次实验、提交、环境和采样配置下的非 warmup
  样本聚合而成的对照工件，聚合逻辑见 ``eval_baseline_stats``。

序列化采用严格模式：未知 schema 版本或字段类型错误必须明确失败，禁止把缺失
事实静默当作 0 或成功。并发 / 取消观察字段在 PR1（schema 1.0）样本中缺失时
按 None 处理，不猜测为 0。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Sequence

BENCHMARK_SCHEMA_VERSION: str = "2.0"
"""Benchmark 记录与快照的 schema 版本；读取到其他版本必须明确失败。"""

# PR1 兼容：旧版本 schema 用于反序列化兼容。
_BENCHMARK_SCHEMA_VERSION_V1: str = "1.0"
_SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({_BENCHMARK_SCHEMA_VERSION_V1, BENCHMARK_SCHEMA_VERSION})

SUITE_NAME: str = "runtime_core"
"""PR1 实验族标识：Runtime 核心语义的 Eval 业务回归套件。"""

SUITE_CONCURRENCY: str = "reliability_concurrency"
"""PR3 实验族标识：并发隔离与调度收益套件。"""

SCENARIO_TOOL_SUCCESS: str = "tool_success"
"""PR2 统一业务场景标识：单工具成功（工具调用 → 固定输出 → 最终回答）。"""


class ScheduleMode(StrEnum):
    """PR3 并发 Benchmark 的调度模式。"""

    SESSION_LOCK = "session_lock"
    """当前生产路径：每 Session 异步锁串行化，不同 Session 可并行。"""

    GLOBAL_LOCK = "global_lock"
    """Benchmark 进程内全局锁对照：所有提交串行，仅用于证明调度结构容量影响。"""


class ConcurrencyScenario(StrEnum):
    """PR3 并发场景标识。"""

    FIFO_SAME_SESSION = "fifo_same_session"
    """同 Session FIFO：20 请求并发提交，验证开始/完成/Conversation 顺序。"""

    MULTI_SESSION_ISOLATION = "multi_session_isolation"
    """多 Session 隔离：8×4 请求，验证跨 Session 零串扰。"""

    SESSION_SCALING = "session_scaling"
    """Session 数扩展：1/2/4/8 Session，绘制吞吐随 Session 数变化曲线。"""

    FIXED_CONCURRENCY = "fixed_concurrency"
    """固定并发对照：8×4 请求，Session 锁 vs 全局锁主对照。"""

    MIXED_LONG_SHORT = "mixed_long_short"
    """长短混合：1 长请求 + 7 Session 短请求，证明长任务不阻塞其他 Session。"""

    CANCEL_NON_BLOCKING = "cancel_non_blocking"
    """取消不阻塞：长 Run 持锁期间取消，验证送达/生效时延与锁释放。"""


def compute_fixture_fingerprint(case) -> str:
    """计算 EvalCase 固定夹具的稳定指纹。

    只对决定执行行为的固定夹具部分（LLM / Context / Tool / Approval /
    Delegation Fixture）做确定性哈希，不包含期望、策略或元数据，因此同一
    业务场景定义必然产出同一指纹。当前 Eval 与历史适配器对照时要求两侧
    该指纹一致，以证明执行的是同一固定替身语义。
    """
    payload: Mapping[str, object] = {
        "llm_fixture": case.llm_fixture.to_dict() if case.llm_fixture is not None else None,
        "context_fixtures": [fixture.to_dict() for fixture in case.context_fixtures],
        "tool_fixtures": [fixture.to_dict() for fixture in case.tool_fixtures],
        "approval_fixtures": [fixture.to_dict() for fixture in case.approval_fixtures],
        "delegation_fixtures": [fixture.to_dict() for fixture in case.delegation_fixtures],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return digest.hexdigest()[:16]


class ExecutionSource(StrEnum):
    """采样数据由哪条执行链路产生。"""

    CURRENT_EVAL = "current_eval"
    """当前仓库的 Eval 隔离执行（``ReexecutionRunner``）。"""

    HISTORICAL_ADAPTER = "historical_adapter"
    """历史提交中经外围适配器启动的旧执行链路。"""


class EvidenceKind(StrEnum):
    """语义 / 统计事实取自哪类证据。"""

    RUN_TRACE = "run_trace"
    """当前 Eval 重建出的 RunTrace。"""

    JOURNAL = "journal"
    """历史 Journal 指标。"""

    FINAL_RESULT = "final_result"
    """历史最终结果对象（如 AgentRun 的终态与统计）。"""

    RECORDED_FIXTURE_LOG = "recorded_fixture_log"
    """记录型替身工具 / Fixture 的调用日志。"""


class BenchmarkSchemaError(ValueError):
    """Benchmark 记录的 schema 版本、字段类型或取值不合法。"""


# --------------------------------------------------------------------------- #
# 字段校验助手
# --------------------------------------------------------------------------- #


def _require_str(value: object, label: str, *, allow_empty: bool = True) -> str:
    """读取必填字符串字段。"""
    if not isinstance(value, str):
        raise BenchmarkSchemaError(f"{label} 必须是字符串，实际为 {type(value).__name__}")
    if not allow_empty and not value:
        raise BenchmarkSchemaError(f"{label} 不能为空")
    return value


def _optional_str(value: object, label: str) -> str | None:
    """读取可空字符串字段。"""
    if value is None:
        return None
    return _require_str(value, label)


def _require_int(value: object, label: str) -> int:
    """读取必填整数字段；布尔不被视为整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchmarkSchemaError(f"{label} 必须是整数，实际为 {type(value).__name__}")
    return value


def _require_bool(value: object, label: str) -> bool:
    """读取必填布尔字段。"""
    if not isinstance(value, bool):
        raise BenchmarkSchemaError(f"{label} 必须是布尔值，实际为 {type(value).__name__}")
    return value


def _optional_enum[EnumT: StrEnum](enum_type: type[EnumT], value: object, label: str, default: EnumT) -> EnumT:
    """读取可缺省的枚举字段：缺失时返回默认值，存在但取值非法时明确失败。"""
    if value is None:
        return default
    if not isinstance(value, str):
        raise BenchmarkSchemaError(f"{label} 必须是字符串，实际为 {type(value).__name__}")
    try:
        return enum_type(value)
    except ValueError as error:
        raise BenchmarkSchemaError(f"{label} 取值 {value!r} 不受支持") from error


def _require_float(value: object, label: str) -> float:
    """读取必填数值字段（整数视为合法数值）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkSchemaError(f"{label} 必须是数值，实际为 {type(value).__name__}")
    return float(value)


def _require_json_map(value: object, label: str) -> Mapping[str, object]:
    """读取必填 JSON 兼容对象字段。"""
    if not isinstance(value, Mapping):
        raise BenchmarkSchemaError(f"{label} 必须是对象，实际为 {type(value).__name__}")
    for key, item in value.items():
        if not isinstance(key, str):
            raise BenchmarkSchemaError(f"{label} 的键必须是字符串，实际为 {type(key).__name__}")
        _require_json_value(item, f"{label}.{key}")
    return value


def _require_json_value(value: object, label: str) -> None:
    """递归校验取值可被 JSON 表达。"""
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        _require_json_map(value, label)
        return
    raise BenchmarkSchemaError(f"{label} 不是 JSON 兼容取值：{type(value).__name__}")


def _optional_json_map(value: object, label: str) -> Mapping[str, object] | None:
    """读取可空 JSON 兼容对象字段。"""
    if value is None:
        return None
    return _require_json_map(value, label)


def _optional_int(value: object, label: str) -> int | None:
    """读取可选整数字段；缺失时为 None，存在时校验类型（布尔不算整数）。"""
    if value is None:
        return None
    return _require_int(value, label)


def _optional_float(value: object, label: str) -> float | None:
    """读取可选数值字段；缺失时为 None，存在时校验类型。"""
    if value is None:
        return None
    return _require_float(value, label)


def _optional_bool(value: object, label: str) -> bool | None:
    """读取可选布尔字段；缺失时为 None，存在时校验类型。"""
    if value is None:
        return None
    return _require_bool(value, label)


def _optional_schedule_mode(value: object, label: str) -> ScheduleMode | None:
    """读取可选调度模式枚举；缺失或为 None 时返回 None，存在时校验取值。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise BenchmarkSchemaError(f"{label} 必须是字符串，实际为 {type(value).__name__}")
    try:
        return ScheduleMode(value)
    except ValueError as error:
        raise BenchmarkSchemaError(f"{label} 取值 {value!r} 不受支持") from error


# --------------------------------------------------------------------------- #
# BenchmarkSample：单次采样记录
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BenchmarkSample:
    """一次 ``run_case()`` 采样派生的可追溯记录。

    身份与版本环境字段记录采样时的提交与运行条件；语义字段来自 ``EvalResult``；
    时延字段中 ``wall_duration_ms`` 由 Benchmark 外层以 ``perf_counter`` 计量，
    ``trace_metrics`` 与 ``run_statistics`` 是 Trace 的只读视图。``run_statistics``
    中 Fixture 未产生的时延 / token 事实以 ``None`` 表达，绝不猜测为 0。
    """

    dataset: str
    case_id: str
    attempt: int
    is_warmup: bool
    git_commit: str
    python_version: str
    platform: str
    config_hash: str
    eval_schema_version: str
    passed: bool
    failure_kind: str | None
    assertions_passed: int
    assertions_total: int
    trace_available: bool
    wall_duration_ms: float
    run_id: str | None
    schema_version: str = BENCHMARK_SCHEMA_VERSION
    suite: str = SUITE_NAME
    execution_source: ExecutionSource = ExecutionSource.CURRENT_EVAL
    source_commit: str = ""
    scenario_id: str = ""
    evidence_kind: EvidenceKind = EvidenceKind.RUN_TRACE
    fixture_fingerprint: str = ""
    trace_metrics: Mapping[str, object] = field(default_factory=dict)
    run_statistics: Mapping[str, object] = field(default_factory=dict)
    trace_source: Mapping[str, object] | None = None

    # ---- PR3 并发 / 取消观察字段（PR1 样本中为 None，不猜测为 0） ----

    # 工作负载
    schedule_mode: ScheduleMode | None = None
    """调度模式；PR1 Eval 样本为 None。"""
    session_count: int | None = None
    """该轮实验的 Session 数。"""
    requests_per_session: int | None = None
    """每个 Session 的请求数。"""
    fake_delay_ms: int | None = None
    """固定延迟替身的延迟毫秒数。"""

    # 顺序（同 Session FIFO 观察证据）
    accepted_seq: int | None = None
    """Benchmark 入口分配的请求接受序号（1-based）。"""
    execution_started_seq: int | None = None
    """Runtime 真正开始执行的次序。"""
    completed_seq: int | None = None
    """Run 完成的次序。"""
    conversation_commit_seq: int | None = None
    """Conversation 持久化次序。"""

    # 时延
    queue_wait_ms: float | None = None
    """从入口提交到 Runtime 开始执行的排队等待耗时；缺失时为 None，不当作 0。"""
    cancel_delivery_ms: float | None = None
    """取消送达耗时：从调用 cancel() 到取消调用返回。"""
    cancel_effect_ms: float | None = None
    """取消生效耗时：从调用 cancel() 到该 Run 持久化为取消终态。"""

    # 隔离（跨 Session 串扰计数）
    message_leak_count: int | None = None
    """跨 Session 消息串扰数。"""
    event_leak_count: int | None = None
    """跨 Session 事件串扰数。"""
    context_leak_count: int | None = None
    """跨 Session 上下文串��数。"""
    tool_leak_count: int | None = None
    """跨 Session 工具结果串扰数。"""
    stream_leak_count: int | None = None
    """跨 Session 输出串流数。"""

    # 取消路径
    cancellation_delivered: bool | None = None
    """取消信号是否已送达。"""
    cancellation_effective: bool | None = None
    """取消是否已生效（Run 进入取消终态）。"""
    lock_released: bool | None = None
    """取消后 Session 锁是否已释放。"""
    followup_completed: bool | None = None
    """取消后同 Session 后续请求是否完成。"""

    # 证据摘要
    evidence_summary: Mapping[str, object] | None = None
    """运行事实引用与内容摘要（不保存 Prompt、密钥或完整输出正文）。"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典；并发字段为 None 时写入 null。"""
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "suite": self.suite,
            "dataset": self.dataset,
            "case_id": self.case_id,
            "attempt": self.attempt,
            "is_warmup": self.is_warmup,
            "git_commit": self.git_commit,
            "python_version": self.python_version,
            "platform": self.platform,
            "config_hash": self.config_hash,
            "eval_schema_version": self.eval_schema_version,
            "passed": self.passed,
            "failure_kind": self.failure_kind,
            "assertions_passed": self.assertions_passed,
            "assertions_total": self.assertions_total,
            "trace_available": self.trace_available,
            "wall_duration_ms": self.wall_duration_ms,
            "trace_metrics": dict(self.trace_metrics),
            "run_statistics": dict(self.run_statistics),
            "run_id": self.run_id,
            "execution_source": self.execution_source.value,
            "source_commit": self.source_commit,
            "scenario_id": self.scenario_id,
            "evidence_kind": self.evidence_kind.value,
            "fixture_fingerprint": self.fixture_fingerprint,
            "trace_source": None if self.trace_source is None else dict(self.trace_source),
            # PR3 并发 / 取消观察字段
            "schedule_mode": None if self.schedule_mode is None else self.schedule_mode.value,
            "session_count": self.session_count,
            "requests_per_session": self.requests_per_session,
            "fake_delay_ms": self.fake_delay_ms,
            "accepted_seq": self.accepted_seq,
            "execution_started_seq": self.execution_started_seq,
            "completed_seq": self.completed_seq,
            "conversation_commit_seq": self.conversation_commit_seq,
            "queue_wait_ms": self.queue_wait_ms,
            "cancel_delivery_ms": self.cancel_delivery_ms,
            "cancel_effect_ms": self.cancel_effect_ms,
            "message_leak_count": self.message_leak_count,
            "event_leak_count": self.event_leak_count,
            "context_leak_count": self.context_leak_count,
            "tool_leak_count": self.tool_leak_count,
            "stream_leak_count": self.stream_leak_count,
            "cancellation_delivered": self.cancellation_delivered,
            "cancellation_effective": self.cancellation_effective,
            "lock_released": self.lock_released,
            "followup_completed": self.followup_completed,
            "evidence_summary": None if self.evidence_summary is None else dict(self.evidence_summary),
        }
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BenchmarkSample:
        """从 JSON 字典严格反序列化；未知版本或非法字段类型立即失败。

        PR1（schema 1.0）样本缺少并发字段时按 None 处理，不猜测为 0。
        """
        label: str = "benchmark_sample"
        schema_version: str = _require_str(data.get("schema_version"), f"{label}.schema_version")
        if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise BenchmarkSchemaError(
                f"不支持的 sample schema 版本 {schema_version!r}，当前支持 {sorted(_SUPPORTED_SCHEMA_VERSIONS)}"
            )
        return cls(
            schema_version=schema_version,
            suite=_require_str(data.get("suite"), f"{label}.suite", allow_empty=False),
            dataset=_require_str(data.get("dataset"), f"{label}.dataset", allow_empty=False),
            case_id=_require_str(data.get("case_id"), f"{label}.case_id", allow_empty=False),
            attempt=_require_int(data.get("attempt"), f"{label}.attempt"),
            is_warmup=_require_bool(data.get("is_warmup"), f"{label}.is_warmup"),
            git_commit=_require_str(data.get("git_commit"), f"{label}.git_commit"),
            python_version=_require_str(data.get("python_version"), f"{label}.python_version"),
            platform=_require_str(data.get("platform"), f"{label}.platform"),
            config_hash=_require_str(data.get("config_hash"), f"{label}.config_hash"),
            eval_schema_version=_require_str(data.get("eval_schema_version"), f"{label}.eval_schema_version"),
            passed=_require_bool(data.get("passed"), f"{label}.passed"),
            failure_kind=_optional_str(data.get("failure_kind"), f"{label}.failure_kind"),
            assertions_passed=_require_int(data.get("assertions_passed"), f"{label}.assertions_passed"),
            assertions_total=_require_int(data.get("assertions_total"), f"{label}.assertions_total"),
            trace_available=_require_bool(data.get("trace_available"), f"{label}.trace_available"),
            wall_duration_ms=_require_float(data.get("wall_duration_ms"), f"{label}.wall_duration_ms"),
            trace_metrics=_require_json_map(data.get("trace_metrics"), f"{label}.trace_metrics"),
            run_statistics=_require_json_map(data.get("run_statistics"), f"{label}.run_statistics"),
            run_id=_optional_str(data.get("run_id"), f"{label}.run_id"),
            execution_source=_optional_enum(
                ExecutionSource, data.get("execution_source"), f"{label}.execution_source", ExecutionSource.CURRENT_EVAL
            ),
            source_commit=_require_str(data.get("source_commit") or "", f"{label}.source_commit"),
            scenario_id=_require_str(data.get("scenario_id") or "", f"{label}.scenario_id"),
            evidence_kind=_optional_enum(
                EvidenceKind, data.get("evidence_kind"), f"{label}.evidence_kind", EvidenceKind.RUN_TRACE
            ),
            fixture_fingerprint=_require_str(
                data.get("fixture_fingerprint") or "", f"{label}.fixture_fingerprint"
            ),
            trace_source=_optional_json_map(data.get("trace_source"), f"{label}.trace_source"),
            # PR3 并发字段：v1.0 样本缺失时读为 None；v2.0 存在时校验类型
            schedule_mode=_optional_schedule_mode(data.get("schedule_mode"), f"{label}.schedule_mode"),
            session_count=_optional_int(data.get("session_count"), f"{label}.session_count"),
            requests_per_session=_optional_int(data.get("requests_per_session"), f"{label}.requests_per_session"),
            fake_delay_ms=_optional_int(data.get("fake_delay_ms"), f"{label}.fake_delay_ms"),
            accepted_seq=_optional_int(data.get("accepted_seq"), f"{label}.accepted_seq"),
            execution_started_seq=_optional_int(data.get("execution_started_seq"), f"{label}.execution_started_seq"),
            completed_seq=_optional_int(data.get("completed_seq"), f"{label}.completed_seq"),
            conversation_commit_seq=_optional_int(data.get("conversation_commit_seq"), f"{label}.conversation_commit_seq"),
            queue_wait_ms=_optional_float(data.get("queue_wait_ms"), f"{label}.queue_wait_ms"),
            cancel_delivery_ms=_optional_float(data.get("cancel_delivery_ms"), f"{label}.cancel_delivery_ms"),
            cancel_effect_ms=_optional_float(data.get("cancel_effect_ms"), f"{label}.cancel_effect_ms"),
            message_leak_count=_optional_int(data.get("message_leak_count"), f"{label}.message_leak_count"),
            event_leak_count=_optional_int(data.get("event_leak_count"), f"{label}.event_leak_count"),
            context_leak_count=_optional_int(data.get("context_leak_count"), f"{label}.context_leak_count"),
            tool_leak_count=_optional_int(data.get("tool_leak_count"), f"{label}.tool_leak_count"),
            stream_leak_count=_optional_int(data.get("stream_leak_count"), f"{label}.stream_leak_count"),
            cancellation_delivered=_optional_bool(data.get("cancellation_delivered"), f"{label}.cancellation_delivered"),
            cancellation_effective=_optional_bool(data.get("cancellation_effective"), f"{label}.cancellation_effective"),
            lock_released=_optional_bool(data.get("lock_released"), f"{label}.lock_released"),
            followup_completed=_optional_bool(data.get("followup_completed"), f"{label}.followup_completed"),
            evidence_summary=_optional_json_map(data.get("evidence_summary"), f"{label}.evidence_summary"),
        )


# --------------------------------------------------------------------------- #
# 汇总结构：时延分布、Case 汇总、全局汇总
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LatencyStats:
    """一组时延样本的分布汇总（毫秒）。"""

    sample_count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "sample_count": self.sample_count,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> LatencyStats:
        """从 JSON 字典严格反序列化。"""
        label: str = "latency_stats"
        return cls(
            sample_count=_require_int(data.get("sample_count"), f"{label}.sample_count"),
            p50_ms=_require_float(data.get("p50_ms"), f"{label}.p50_ms"),
            p95_ms=_require_float(data.get("p95_ms"), f"{label}.p95_ms"),
            p99_ms=_require_float(data.get("p99_ms"), f"{label}.p99_ms"),
            max_ms=_require_float(data.get("max_ms"), f"{label}.max_ms"),
        )


@dataclass(frozen=True)
class CaseSummary:
    """单个 Case 在本次实验中的聚合结果。"""

    case_id: str
    sample_count: int
    passed_count: int
    failed_count: int
    success_rate: float
    wall_duration_ms: LatencyStats
    failure_kinds: Mapping[str, int] = field(default_factory=dict)
    trace_metrics_ms: Mapping[str, LatencyStats] = field(default_factory=dict)
    llm_call_count_total: int = 0
    tool_call_count_total: int = 0
    trace_available_count: int = 0
    trace_missing_count: int = 0
    failed_tool_count_total: int = 0

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "case_id": self.case_id,
            "sample_count": self.sample_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "success_rate": self.success_rate,
            "failure_kinds": dict(self.failure_kinds),
            "wall_duration_ms": self.wall_duration_ms.to_dict(),
            "trace_metrics_ms": {key: value.to_dict() for key, value in self.trace_metrics_ms.items()},
            "llm_call_count_total": self.llm_call_count_total,
            "tool_call_count_total": self.tool_call_count_total,
            "trace_available_count": self.trace_available_count,
            "trace_missing_count": self.trace_missing_count,
            "failed_tool_count_total": self.failed_tool_count_total,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CaseSummary:
        """从 JSON 字典严格反序列化。"""
        label: str = "case_summary"
        return cls(
            case_id=_require_str(data.get("case_id"), f"{label}.case_id", allow_empty=False),
            sample_count=_require_int(data.get("sample_count"), f"{label}.sample_count"),
            passed_count=_require_int(data.get("passed_count"), f"{label}.passed_count"),
            failed_count=_require_int(data.get("failed_count"), f"{label}.failed_count"),
            success_rate=_require_float(data.get("success_rate"), f"{label}.success_rate"),
            failure_kinds=_require_json_map(data.get("failure_kinds"), f"{label}.failure_kinds"),
            wall_duration_ms=LatencyStats.from_dict(
                _require_json_map(data.get("wall_duration_ms"), f"{label}.wall_duration_ms")
            ),
            trace_metrics_ms={
                str(key): LatencyStats.from_dict(_require_json_map(value, f"{label}.trace_metrics_ms.{key}"))
                for key, value in _require_json_map(
                    data.get("trace_metrics_ms"), f"{label}.trace_metrics_ms"
                ).items()
            },
            llm_call_count_total=_require_int(data.get("llm_call_count_total"), f"{label}.llm_call_count_total"),
            tool_call_count_total=_require_int(data.get("tool_call_count_total"), f"{label}.tool_call_count_total"),
            trace_available_count=_require_int(data.get("trace_available_count"), f"{label}.trace_available_count"),
            trace_missing_count=_require_int(data.get("trace_missing_count"), f"{label}.trace_missing_count"),
            failed_tool_count_total=_require_int(data.get("failed_tool_count_total"), f"{label}.failed_tool_count_total"),
        )


@dataclass(frozen=True)
class GlobalSummary:
    """全部 Case 同口径聚合与失败归因计数。"""

    sample_count: int
    passed_count: int
    failed_count: int
    success_rate: float
    wall_duration_ms: LatencyStats
    failure_kinds: Mapping[str, int] = field(default_factory=dict)
    llm_call_count_total: int = 0
    tool_call_count_total: int = 0
    trace_available_count: int = 0
    trace_missing_count: int = 0

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "sample_count": self.sample_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "success_rate": self.success_rate,
            "failure_kinds": dict(self.failure_kinds),
            "wall_duration_ms": self.wall_duration_ms.to_dict(),
            "llm_call_count_total": self.llm_call_count_total,
            "tool_call_count_total": self.tool_call_count_total,
            "trace_available_count": self.trace_available_count,
            "trace_missing_count": self.trace_missing_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> GlobalSummary:
        """从 JSON 字典严格反序列化。"""
        label: str = "global_summary"
        return cls(
            sample_count=_require_int(data.get("sample_count"), f"{label}.sample_count"),
            passed_count=_require_int(data.get("passed_count"), f"{label}.passed_count"),
            failed_count=_require_int(data.get("failed_count"), f"{label}.failed_count"),
            success_rate=_require_float(data.get("success_rate"), f"{label}.success_rate"),
            failure_kinds=_require_json_map(data.get("failure_kinds"), f"{label}.failure_kinds"),
            wall_duration_ms=LatencyStats.from_dict(
                _require_json_map(data.get("wall_duration_ms"), f"{label}.wall_duration_ms")
            ),
            llm_call_count_total=_require_int(data.get("llm_call_count_total"), f"{label}.llm_call_count_total"),
            tool_call_count_total=_require_int(data.get("tool_call_count_total"), f"{label}.tool_call_count_total"),
            trace_available_count=_require_int(data.get("trace_available_count"), f"{label}.trace_available_count"),
            trace_missing_count=_require_int(data.get("trace_missing_count"), f"{label}.trace_missing_count"),
        )


# --------------------------------------------------------------------------- #
# BenchmarkSnapshot：当前基线快照
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BenchmarkSnapshot:
    """一次完整基准运行的非 warmup 样本聚合快照。

    快照不是 ``EvalResult``、``RegressionReport`` 或 Runtime 事实的替代品，
    不进入 CI Gate。``samples_path`` 是原始 JSONL 相对于快照自身目录的路径，
    由 ``EvalBaselineRunner`` 在写盘时确定，保证证据不越出输出目录。
    """

    snapshot_id: str
    generated_at: str
    git_commit: str
    dataset: str
    warmup: int
    repeat: int
    global_summary: GlobalSummary
    samples_path: str
    schema_version: str = BENCHMARK_SCHEMA_VERSION
    execution_source: ExecutionSource = ExecutionSource.CURRENT_EVAL
    scenario_id: str = ""
    fixture_fingerprints: Mapping[str, str] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    cases: Sequence[CaseSummary] = ()
    samples_content_summary: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "generated_at": self.generated_at,
            "git_commit": self.git_commit,
            "dataset": self.dataset,
            "environment": dict(self.environment),
            "warmup": self.warmup,
            "repeat": self.repeat,
            "cases": [case.to_dict() for case in self.cases],
            "global_summary": self.global_summary.to_dict(),
            "samples_path": self.samples_path,
            "execution_source": self.execution_source.value,
            "scenario_id": self.scenario_id,
            "fixture_fingerprints": dict(self.fixture_fingerprints),
            "samples_content_summary": dict(self.samples_content_summary),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BenchmarkSnapshot:
        """从 JSON 字典严格反序列化。"""
        label: str = "benchmark_snapshot"
        schema_version: str = _require_str(data.get("schema_version"), f"{label}.schema_version")
        if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise BenchmarkSchemaError(
                f"不支持的 snapshot schema 版本 {schema_version!r}，当前支持 {sorted(_SUPPORTED_SCHEMA_VERSIONS)}"
            )
        return cls(
            schema_version=schema_version,
            snapshot_id=_require_str(data.get("snapshot_id"), f"{label}.snapshot_id", allow_empty=False),
            generated_at=_require_str(data.get("generated_at"), f"{label}.generated_at"),
            git_commit=_require_str(data.get("git_commit"), f"{label}.git_commit"),
            dataset=_require_str(data.get("dataset"), f"{label}.dataset", allow_empty=False),
            environment=_require_json_map(data.get("environment"), f"{label}.environment"),
            warmup=_require_int(data.get("warmup"), f"{label}.warmup"),
            repeat=_require_int(data.get("repeat"), f"{label}.repeat"),
            cases=tuple(
                CaseSummary.from_dict(_require_json_map(item, f"{label}.cases[{index}]"))
                for index, item in enumerate(data.get("cases") or ())
            ),
            global_summary=GlobalSummary.from_dict(
                _require_json_map(data.get("global_summary"), f"{label}.global_summary")
            ),
            samples_path=_require_str(data.get("samples_path"), f"{label}.samples_path"),
            execution_source=_optional_enum(
                ExecutionSource, data.get("execution_source"), f"{label}.execution_source", ExecutionSource.CURRENT_EVAL
            ),
            scenario_id=_require_str(data.get("scenario_id") or "", f"{label}.scenario_id"),
            fixture_fingerprints=_require_json_map(
                data.get("fixture_fingerprints") or {}, f"{label}.fixture_fingerprints"
            ),
            samples_content_summary=_require_json_map(
                data.get("samples_content_summary"), f"{label}.samples_content_summary"
            ),
        )
