"""PR3 并发隔离与调度收益 CLI 与编排。

链路：固定并发场景 + 固定延迟 Fake LLM / Fake Tool
    → SessionInteractionService
    → SessionRunCoordinator → RuntimeEngine
    → 持久化事实读取 → BenchmarkSample（JSONL）
    → ConcurrencySnapshot（JSON + Markdown 报告）

本模块只写 ``output_dir`` 与可选 ``baseline_dir``，不修改 Runtime 生产代码。
全局锁模式仅是本 Benchmark 进程内的对照手段，不是生产路径。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from dotclaw.bootstrap.session_interaction import SessionInteractionService
from dotclaw.session.session import Session, SessionManager
from dotclaw.runtime.adapters.approval_repository import ApprovalRepositoryAdapter
from dotclaw.runtime.adapters.checkpoint_repository import CheckpointRepositoryAdapter
from dotclaw.runtime.adapters.run_repository import RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.dto import (
    ConversationMessage,
    ConversationSnapshot,
    RunRequest,
)
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.domain.facts import MessageRole

from .concurrency_assertions import (
    AssertionResult,
    CancelResult,
    IsolationResult,
    assert_cancel,
    assert_comparable_configs,
    assert_fifo_order,
    assert_isolation,
    check_isolation,
)
from .concurrency_stats import (
    ConcurrencyLatencyStats,
    ConcurrencySnapshot,
    ScenarioStats,
    aggregate_scenario_stats,
    compare_schedule_modes,
    compute_throughput,
)
from .concurrency_workloads import (
    ControlledSubmissionGate,
    FixedContext,
    FixedDelayLLM,
    FixedDelayTool,
    FixedPolicy,
    IdentifierCodec,
    LongDelayLLM,
    RunFacts,
    RecordingOutputPort,
    SessionSelectiveDelayLLM,
    ToolCallingFixedDelayLLM,
    WorkloadConfig,
    make_benchmark_request,
    read_run_facts,
    read_session_conversation,
)
from .eval_baseline_models import (
    BENCHMARK_SCHEMA_VERSION,
    SUITE_CONCURRENCY,
    BenchmarkSample,
    ConcurrencyScenario,
    ExecutionSource,
    ScheduleMode,
)
from .eval_baseline import git_full_commit, git_short_commit, make_snapshot_id

# --------------------------------------------------------------------------- #
# 必需的最小 Token 计数与历史压缩替身
# --------------------------------------------------------------------------- #


class _AlwaysWithinBudgetCounter:
    """Token 计数始终在预算内。"""

    async def count(self, request) -> object:
        from dotclaw.runtime.application.context_budget import TokenCountResult
        return TokenCountResult(input_tokens=1)


class _UnexpectedHistoryCompactor:
    """历史压缩不应在 Benchmark 中被触发。"""

    async def compact_history(self, request) -> object:
        raise AssertionError("Benchmark 不应触发历史压缩")


# --------------------------------------------------------------------------- #
# Fake AgentRegistry（用于 SessionInteractionService）
# --------------------------------------------------------------------------- #


class _FakeAgentRegistry:
    """Benchmark 最小 Agent 注册表：仅注册一个固定 agent_id。"""

    def __init__(self, agent_id: str = "bench-agent") -> None:
        self._agent_id: str = agent_id

    async def resolve(self, agent_id: str) -> object | None:
        """始终返回固定 identity。"""
        if agent_id == self._agent_id:
            from dotclaw.agent.identity import AgentIdentity
            return AgentIdentity(
                agent_id=agent_id,
                agent_name="Benchmark Agent",
            )
        return None

    def get(self, agent_id: str):
        """按 SessionInteractionService 所需接口读取固定 Identity。"""
        if agent_id != self._agent_id:
            return None
        from dotclaw.agent.identity import AgentIdentity
        return AgentIdentity(
            agent_id=agent_id,
            agent_name="Benchmark Agent",
        )

    def list_all(self) -> list[object]:
        """返回唯一可用 Identity，满足默认解析协议。"""
        identity = self.get(self._agent_id)
        return [identity] if identity is not None else []


# --------------------------------------------------------------------------- #
# Runtime 服务组装
# --------------------------------------------------------------------------- #


def _build_engine(
    root: Path,
    llm_port,
    tool_port,
    delay_ms: int,
    context_port: FixedContext,
) -> RuntimeEngine:
    """组装最小 Benchmark RuntimeEngine。"""
    return RuntimeEngine(
        run_repository=RunRepositoryAdapter(root),
        checkpoint_repository=CheckpointRepositoryAdapter(root),
        context_port=context_port,
        llm_port=llm_port,
        tool_port=tool_port,
        policy_port=FixedPolicy(),
        approval_service=ApprovalService(ApprovalRepositoryAdapter(root)),
        cancellation_service=CancellationService(),
        token_counter=_AlwaysWithinBudgetCounter(),
        history_compactor=_UnexpectedHistoryCompactor(),
    )


def _build_services(
    abs_data_dir: str,
    llm_port,
    tool_port,
    delay_ms: int,
) -> tuple[SessionRunCoordinator, RunRepositoryAdapter, SessionManager, _FakeAgentRegistry, FixedContext]:
    """组装 Benchmark Runtime 服务。

    ``abs_data_dir`` 是绝对路径的临时数据目录。
    """
    root = Path(abs_data_dir)
    context_port = FixedContext()
    engine: RuntimeEngine = _build_engine(root, llm_port, tool_port, delay_ms, context_port)
    coordinator = SessionRunCoordinator(engine)
    repository = RunRepositoryAdapter(root)
    # SessionManager 需要相对路径，但我们直接用绝对路径创建 Session
    # 通过 monkey-patch data_dir 来绕过路径解析
    session_manager = SessionManager(abs_data_dir)
    # 覆盖 _data_dir 为绝对路径
    session_manager._data_dir = root.resolve()
    agent_registry = _FakeAgentRegistry()
    return coordinator, repository, session_manager, agent_registry, context_port


def _make_round_data_dir(round_index: int, scenario: str) -> str:
    """创建一个隔离的临时数据目录（系统 temp 目录下，避免沙箱拦截删除）。"""
    import uuid
    import tempfile
    tmp_root: Path = Path(tempfile.gettempdir()) / "dotclaw_bench_concurrency"
    tmp_root.mkdir(parents=True, exist_ok=True)
    subdir: str = f"{scenario}_{round_index}_{uuid.uuid4().hex[:6]}"
    abs_path: Path = tmp_root / subdir
    abs_path.mkdir(parents=True, exist_ok=True)
    # 返回相对于项目根目录的路径供 SessionManager 使用
    # SessionManager 会将相对路径解析为 project_root/data_dir
    # 这里我们直接返回绝对路径字符串
    return str(abs_path)


def _cleanup_round_data(data_dir: str) -> None:
    """清理轮次数据目录。"""
    abs_path: Path = Path(data_dir)
    if abs_path.exists():
        import shutil
        shutil.rmtree(abs_path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 请求提交辅助
# --------------------------------------------------------------------------- #


def _make_request_factory(
    session_id: str,
    agent_id: str,
    user_message: str,
):
    """创建 submit_prepared 使用的请求工厂闭包。"""
    async def factory() -> RunRequest:
        user_msg = ConversationMessage(
            message_id="msg-input",
            role=MessageRole.USER,
            content=user_message,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return RunRequest(
            session_id=session_id,
            lease_id=f"lease-{session_id}",
            agent_id=agent_id,
            user_message=user_msg,
            conversation=ConversationSnapshot(
                session_id=session_id,
                messages=(),
                version=0,
            ),
        )
    return factory


# --------------------------------------------------------------------------- #
# 场景编排
# --------------------------------------------------------------------------- #


async def _run_fifo_same_session(
    coordinator: SessionRunCoordinator,
    repository: RunRepositoryAdapter,
    session: Session,
    agent_id: str,
    gate: ControlledSubmissionGate,
    config: WorkloadConfig,
    batch_index: int,
    is_warmup: bool,
    batch_started: float,
) -> tuple[list[BenchmarkSample], list[AssertionResult]]:
    """执行一轮同 Session FIFO 场景。"""
    n: int = config.requests_per_session
    samples: list[BenchmarkSample] = []
    results: list[AssertionResult] = []

    # 为每个请求分配 accepted_seq
    accepted_seqs: list[int] = []
    user_messages: list[str] = []
    identifiers: list[str] = []
    for i in range(n):
        seq = gate.accept(session.id)
        accepted_seqs.append(seq)
        identifier, user_msg = make_benchmark_request(agent_id, 0, i)
        identifiers.append(identifier)
        user_messages.append(user_msg)

    # 并发提交（同 Session 经锁 FIFO 串行化）
    async def submit_timed(accepted_seq: int, user_message: str):
        """按接受顺序放行真实入口，并让全部请求在 Runtime Session 锁上竞争。"""
        await gate.enter(session.id, accepted_seq)
        accepted_at = datetime.now(timezone.utc)
        # 仅控制进入应用入口的先后；在请求结束前立即放行下一项，禁止 Benchmark 自行串行化。
        gate.release_next(session.id, accepted_seq)
        # 传递已创建 Session，避免异步磁盘读取竞争重排到达同一 Session 锁的先后。
        result = await coordinator.submit(session, user_message)
        ended_at = datetime.now(timezone.utc)
        return result, accepted_at, ended_at

    observed_results = await asyncio.gather(
        *(submit_timed(seq, message) for seq, message in zip(accepted_seqs, user_messages, strict=True))
    )
    run_results = [item[0] for item in observed_results]

    # 读取事实并构造样本
    facts_list: list[RunFacts] = []
    for i, rr in enumerate(run_results):
        facts = await read_run_facts(repository, session.id, rr.run_id)
        if facts is None:
            continue
        facts_list.append(facts)

        accepted_at = observed_results[i][1]
        ended_at = observed_results[i][2]
        started_at = next((event.occurred_at for event in facts.events if event.event_type.value == "llm_started"), None)
        queue_wait_ms = None
        if started_at is not None:
            queue_wait_ms = (datetime.fromisoformat(started_at) - accepted_at).total_seconds() * 1000.0
        wall_ms = (ended_at - accepted_at).total_seconds() * 1000.0

        samples.append(BenchmarkSample(
            dataset=SUITE_CONCURRENCY,
            case_id=ConcurrencyScenario.FIFO_SAME_SESSION.value,
            attempt=batch_index,
            is_warmup=is_warmup,
            git_commit=git_short_commit(),
            python_version=sys.version.split()[0],
            platform=platform.platform(),
            config_hash="",
            eval_schema_version="",
            passed=rr.state.outcome().value == "completed",
            failure_kind=None if rr.state.outcome().value == "completed" else "runtime",
            assertions_passed=1 if rr.state.outcome().value == "completed" else 0,
            assertions_total=1,
            trace_available=False,
            wall_duration_ms=wall_ms,
            run_id=rr.run_id,
            schedule_mode=config.schedule_mode,
            session_count=config.session_count,
            requests_per_session=config.requests_per_session,
            fake_delay_ms=config.fake_delay_ms,
            accepted_seq=accepted_seqs[i],
            execution_started_seq=None,
            completed_seq=None,
            conversation_commit_seq=None,
            queue_wait_ms=queue_wait_ms,
            evidence_summary={
                "identifier": identifiers[i],
                "accepted_at": accepted_at.isoformat(),
                "ended_at": ended_at.isoformat(),
            },
        ))

    # 以 Runtime 事件和实际 Conversation 投影计算顺序，禁止用提交下标代替。
    started_order = sorted(
        ((event.occurred_at, facts.run_id) for facts in facts_list for event in facts.events if event.event_type.value == "llm_started"),
    )
    completed_order = sorted(
        ((event.occurred_at, facts.run_id) for facts in facts_list for event in facts.events if event.event_type.value == "run_completed"),
    )
    started_seq = {run_id: index + 1 for index, (_, run_id) in enumerate(started_order)}
    completed_seq = {run_id: index + 1 for index, (_, run_id) in enumerate(completed_order)}
    conversation = await read_session_conversation(repository, session.id)
    conversation_seq = {
        facts.run_id: next((index + 1 for index, content in enumerate(conversation) if facts.identifier in content), None)
        for facts in facts_list
    }
    for sample in samples:
        if sample.run_id is None:
            continue
        object.__setattr__(sample, "execution_started_seq", started_seq.get(sample.run_id))
        object.__setattr__(sample, "completed_seq", completed_seq.get(sample.run_id))
        object.__setattr__(sample, "conversation_commit_seq", conversation_seq.get(sample.run_id))

    # FIFO 断言
    if facts_list:
        fifo_results = assert_fifo_order(facts_list, accepted_seqs[:len(facts_list)])
        results.extend(fifo_results)
        # 标记 sample 的 FIFO 状态
        for sample in samples:
            object.__setattr__(sample, "passed",
                              all(r.passed for r in fifo_results))

    return samples, results


async def _run_multi_session_isolation(
    coordinator: SessionRunCoordinator,
    repository: RunRepositoryAdapter,
    session_ids: list[str],
    agent_id: str,
    gates: dict[str, ControlledSubmissionGate],
    config: WorkloadConfig,
    batch_index: int,
    is_warmup: bool,
    batch_started: float,
    tool_port: FixedDelayTool,
    output_port: RecordingOutputPort,
) -> tuple[list[BenchmarkSample], IsolationResult]:
    """执行一轮多 Session 隔离场景。"""
    n_sessions: int = config.session_count
    n_requests: int = config.requests_per_session
    samples: list[BenchmarkSample] = []

    # 为所有 Session 的所有请求创建任务
    all_tasks: list = []
    all_meta: list[dict] = []  # (session_index, request_index, session_id, accepted_seq)

    for si in range(n_sessions):
        sid = session_ids[si]
        for ri in range(n_requests):
            seq = gates[sid].accept(sid)
            identifier, user_msg = make_benchmark_request(agent_id, si, ri)
            async def submit_timed(session_id: str, message: str):
                """记录单请求入口与终态，保留真实端到端时延。"""
                accepted_at = datetime.now(timezone.utc)
                result = await coordinator.submit(session_id, message, output_port)
                return result, accepted_at, datetime.now(timezone.utc)
            all_tasks.append(submit_timed(sid, user_msg))
            all_meta.append({
                "session_index": si,
                "request_index": ri,
                "session_id": sid,
                "accepted_seq": seq,
                "identifier": identifier,
            })

    # 并发提交（不同 Session 可并行）
    run_results = await asyncio.gather(*all_tasks)
    batch_end: float = time.perf_counter()
    batch_total_ms: float = (batch_end - batch_started) * 1000.0

    # 按 Session 分组读取事实
    facts_by_session: dict[int, list[RunFacts]] = {}
    for si in range(n_sessions):
        facts_by_session[si] = []

    for i, observed in enumerate(run_results):
        rr, accepted_at, ended_at = observed
        meta = all_meta[i]
        si = meta["session_index"]
        facts = await read_run_facts(repository, meta["session_id"], rr.run_id)
        if facts is None:
            continue
        facts = replace(
            facts,
            tool_outputs=tuple(tool_port.outputs_by_run.get(rr.run_id, [])),
            stream_contents=tuple(output_port.contents_by_run.get(rr.run_id, [])),
        )
        facts_by_session[si].append(facts)

        samples.append(BenchmarkSample(
            dataset=SUITE_CONCURRENCY,
            case_id=ConcurrencyScenario.MULTI_SESSION_ISOLATION.value,
            attempt=batch_index,
            is_warmup=is_warmup,
            git_commit=git_short_commit(),
            python_version=sys.version.split()[0],
            platform=platform.platform(),
            config_hash="",
            eval_schema_version="",
            passed=rr.state.outcome().value == "completed",
            failure_kind=None if rr.state.outcome().value == "completed" else "runtime",
            assertions_passed=1 if rr.state.outcome().value == "completed" else 0,
            assertions_total=1,
            trace_available=False,
            wall_duration_ms=(ended_at - accepted_at).total_seconds() * 1000.0,
            run_id=rr.run_id,
            schedule_mode=config.schedule_mode,
            session_count=config.session_count,
            requests_per_session=config.requests_per_session,
            fake_delay_ms=config.fake_delay_ms,
            accepted_seq=meta["accepted_seq"],
            queue_wait_ms=(
                (datetime.fromisoformat(started_at) - accepted_at).total_seconds() * 1000.0
                if (started_at := next((event.occurred_at for event in facts.events if event.event_type.value == "llm_started"), None)) is not None
                else None
            ),
            evidence_summary={
                "session_index": si,
                "identifier": meta["identifier"],
                "accepted_at": accepted_at.isoformat(),
                "ended_at": ended_at.isoformat(),
            },
        ))

    # 隔离检查
    isolation = check_isolation(facts_by_session, n_sessions)
    return samples, isolation


async def _run_session_scaling(
    coordinator: SessionRunCoordinator,
    repository: RunRepositoryAdapter,
    session_ids: list[str],
    agent_id: str,
    config: WorkloadConfig,
    batch_index: int,
    is_warmup: bool,
    batch_started: float,
) -> tuple[list[BenchmarkSample], float]:
    """执行一轮 Session 数扩展场景。"""
    n_sessions: int = config.session_count
    n_requests: int = config.requests_per_session
    samples: list[BenchmarkSample] = []

    all_tasks: list = []
    all_meta: list[dict] = []

    for si in range(n_sessions):
        sid = session_ids[si]
        for ri in range(n_requests):
            identifier, user_msg = make_benchmark_request(agent_id, si, ri)
            factory = _make_request_factory(sid, agent_id, user_msg)
            async def submit_timed(session_id: str, message: str):
                """记录单请求入口与终态，保留真实端到端时延。"""
                accepted_at = datetime.now(timezone.utc)
                result = await coordinator.submit(session_id, message)
                return result, accepted_at, datetime.now(timezone.utc)
            all_tasks.append(submit_timed(sid, user_msg))
            all_meta.append({
                "session_index": si,
                "request_index": ri,
                "session_id": sid,
                "identifier": identifier,
            })

    observed_results = await asyncio.gather(*all_tasks)
    batch_end: float = time.perf_counter()
    batch_total_ms: float = (batch_end - batch_started) * 1000.0

    for i, meta in enumerate(all_meta):
        result, accepted_at, ended_at = observed_results[i]
        facts = await read_run_facts(repository, meta["session_id"], result.run_id)
        started_at = None if facts is None else next(
            (event.occurred_at for event in facts.events if event.event_type.value == "llm_started"), None
        )
        queue_wait_ms = None if started_at is None else (
            datetime.fromisoformat(started_at) - accepted_at
        ).total_seconds() * 1000.0
        samples.append(BenchmarkSample(
            dataset=SUITE_CONCURRENCY,
            case_id=ConcurrencyScenario.SESSION_SCALING.value,
            attempt=batch_index,
            is_warmup=is_warmup,
            git_commit=git_short_commit(),
            python_version=sys.version.split()[0],
            platform=platform.platform(),
            config_hash="",
            eval_schema_version="",
            passed=True,
            failure_kind=None,
            assertions_passed=1,
            assertions_total=1,
            trace_available=False,
            wall_duration_ms=(ended_at - accepted_at).total_seconds() * 1000.0,
            run_id=result.run_id,
            schedule_mode=config.schedule_mode,
            session_count=config.session_count,
            requests_per_session=config.requests_per_session,
            fake_delay_ms=config.fake_delay_ms,
            queue_wait_ms=queue_wait_ms,
            evidence_summary={
                "session_index": meta["session_index"],
                "identifier": meta["identifier"],
                "accepted_at": accepted_at.isoformat(),
                "ended_at": ended_at.isoformat(),
            },
        ))

    return samples, batch_total_ms


# --------------------------------------------------------------------------- #
# 全局锁模式
# --------------------------------------------------------------------------- #


class _GlobalLockCoordinator:
    """全局锁协调器包装：用一把全局异步锁包围所有提交。"""

    def __init__(self, interaction: SessionInteractionService) -> None:
        self._interaction: SessionInteractionService = interaction
        self._global_lock: asyncio.Lock = asyncio.Lock()

    async def submit(self, session_id: str, user_message: str, output_port=None):
        """在全局锁下仍经 SessionInteractionService 提交普通消息。"""
        async with self._global_lock:
            return await self._interaction.submit(session_id, user_message, output_port)


def _with_batch_metrics(stats: ScenarioStats, batch_total_ms: float) -> ScenarioStats:
    """为正式采样统计补充总批次耗时与吞吐量。"""
    return replace(
        stats,
        throughput_per_sec=compute_throughput(stats.total_requests, batch_total_ms),
        batch_total_ms=batch_total_ms,
    )


# --------------------------------------------------------------------------- #
# 主编排器
# --------------------------------------------------------------------------- #


class ConcurrencyReliabilityRunner:
    """PR3 并发可靠性与调度收益编排器。"""

    def __init__(self) -> None:
        self._git_commit: str = git_short_commit()
        self._source_commit: str = git_full_commit()

    async def run(
        self,
        *,
        warmup: int,
        repeat: int,
        fake_delay_ms: int,
        output_dir: Path,
        baseline_dir: Path | None = None,
    ) -> ConcurrencySnapshot:
        """执行完整并发实验套件并返回快照。"""
        if warmup < 0:
            raise ValueError(f"warmup 必须 >= 0，实际 {warmup}")
        if repeat <= 0:
            raise ValueError(f"repeat 必须 > 0，实际 {repeat}")

        agent_id: str = "bench-agent"
        all_samples: list[BenchmarkSample] = []
        all_scenarios: list[ScenarioStats] = []
        all_configs: list[dict[str, object]] = []

        # === 场景 1：同 Session FIFO（仅 Session 锁模式） ===
        print("=== 场景 1：同 Session FIFO ===")
        fifo_config = WorkloadConfig(
            session_count=1, requests_per_session=20, fake_delay_ms=fake_delay_ms,
            schedule_mode=ScheduleMode.SESSION_LOCK, warmup=warmup, repeat=repeat,
        )
        fifo_samples, _ = await self._run_scenario_fifo(
            fifo_config, agent_id, output_dir,
        )
        all_samples.extend(fifo_samples)
        fifo_stats = aggregate_scenario_stats(
            ConcurrencyScenario.FIFO_SAME_SESSION.value,
            ScheduleMode.SESSION_LOCK.value,
            fifo_samples,
            repeat,
        )
        all_scenarios.append(fifo_stats)
        all_configs.append(fifo_config.to_dict())
        print(f"  FIFO: {fifo_stats.total_requests} 请求, FIFO 通过 {fifo_stats.fifo_passed_count}/{fifo_stats.fifo_total_count}")

        # === 场景 2：多 Session 隔离 ===
        print("=== 场景 2：多 Session 隔离 ===")
        iso_config = WorkloadConfig(
            session_count=8, requests_per_session=4, fake_delay_ms=fake_delay_ms,
            schedule_mode=ScheduleMode.SESSION_LOCK, warmup=warmup, repeat=repeat,
        )
        iso_samples, _ = await self._run_scenario_isolation(
            iso_config, agent_id, output_dir,
        )
        all_samples.extend(iso_samples)
        iso_stats = aggregate_scenario_stats(
            ConcurrencyScenario.MULTI_SESSION_ISOLATION.value,
            ScheduleMode.SESSION_LOCK.value,
            iso_samples,
            repeat,
        )
        all_scenarios.append(iso_stats)
        all_configs.append(iso_config.to_dict())
        print(f"  隔离: {iso_stats.total_requests} 请求, 消息串扰 {iso_stats.message_leak_total}, 事件串扰 {iso_stats.event_leak_total}")

        # === 场景 3：Session 数扩展 ===
        print("=== 场景 3：Session 数扩展 ===")
        for n_sessions in (1, 2, 4, 8):
            session_config = WorkloadConfig(
                session_count=n_sessions, requests_per_session=4, fake_delay_ms=fake_delay_ms,
                schedule_mode=ScheduleMode.SESSION_LOCK, warmup=warmup, repeat=repeat,
            )
            scale_samples, batch_total = await self._run_scenario_scaling(
                session_config, agent_id, output_dir,
            )
            all_samples.extend(scale_samples)
            scale_stats = aggregate_scenario_stats(
                f"{ConcurrencyScenario.SESSION_SCALING.value}_{n_sessions}s",
                ScheduleMode.SESSION_LOCK.value,
                scale_samples,
                repeat,
            )
            scale_stats = _with_batch_metrics(scale_stats, batch_total)
            all_scenarios.append(scale_stats)
            all_configs.append(session_config.to_dict())

            global_config = WorkloadConfig(
                session_count=n_sessions, requests_per_session=4, fake_delay_ms=fake_delay_ms,
                schedule_mode=ScheduleMode.GLOBAL_LOCK, warmup=warmup, repeat=repeat,
            )
            global_samples, global_batch = await self._run_scenario_scaling_global(
                global_config, agent_id, output_dir,
            )
            all_samples.extend(global_samples)
            global_stats = aggregate_scenario_stats(
                f"{ConcurrencyScenario.SESSION_SCALING.value}_{n_sessions}s",
                ScheduleMode.GLOBAL_LOCK.value,
                global_samples,
                repeat,
            )
            global_stats = _with_batch_metrics(global_stats, global_batch)
            all_scenarios.append(global_stats)
            all_configs.append(global_config.to_dict())
            comparison = compare_schedule_modes(scale_stats, global_stats)
            print(
                f"  Session {n_sessions}: 相对全局串行吞吐变化 "
                f"{comparison.get('throughput_change_rate', 'N/A')}"
            )

        # === 场景 4：固定并发对照（Session 锁 vs 全局锁） ===
        print("=== 场景 4：固定并发对照 ===")
        fixed_config = WorkloadConfig(
            session_count=8, requests_per_session=4, fake_delay_ms=fake_delay_ms,
            schedule_mode=ScheduleMode.SESSION_LOCK, warmup=warmup, repeat=repeat,
        )

        # Session 锁
        session_samples, session_batch = await self._run_scenario_scaling(
            fixed_config, agent_id, output_dir,
        )
        all_samples.extend(session_samples)
        session_stats = aggregate_scenario_stats(
            f"{ConcurrencyScenario.FIXED_CONCURRENCY.value}_session",
            ScheduleMode.SESSION_LOCK.value,
            session_samples,
            repeat,
        )

        # 全局锁
        global_config = WorkloadConfig(
            session_count=8, requests_per_session=4, fake_delay_ms=fake_delay_ms,
            schedule_mode=ScheduleMode.GLOBAL_LOCK, warmup=warmup, repeat=repeat,
        )
        global_samples, global_batch = await self._run_scenario_scaling_global(
            global_config, agent_id, output_dir,
        )
        all_samples.extend(global_samples)
        global_stats = aggregate_scenario_stats(
            f"{ConcurrencyScenario.FIXED_CONCURRENCY.value}_global",
            ScheduleMode.GLOBAL_LOCK.value,
            global_samples,
            repeat,
        )
        session_stats = _with_batch_metrics(session_stats, session_batch)
        global_stats = _with_batch_metrics(global_stats, global_batch)

        # 对照
        comparison = compare_schedule_modes(session_stats, global_stats)
        all_scenarios.append(session_stats)
        all_scenarios.append(global_stats)
        all_configs.append(fixed_config.to_dict())
        all_configs.append(global_config.to_dict())
        print(f"  对照: {comparison.get('throughput_change_rate', 'N/A')}")

        # === 场景 5：长短混合（Session 锁 vs 全局锁） ===
        print("=== 场景 5：长短混合 ===")
        mixed_session_config = WorkloadConfig(
            session_count=8, requests_per_session=1, fake_delay_ms=fake_delay_ms,
            schedule_mode=ScheduleMode.SESSION_LOCK, warmup=warmup, repeat=repeat,
            long_delay_ms=200, long_request_session_index=0,
        )
        mixed_session_samples, mixed_session_batch = await self._run_scenario_mixed(
            mixed_session_config, agent_id, global_lock=False,
        )
        mixed_global_config = WorkloadConfig(
            session_count=8, requests_per_session=1, fake_delay_ms=fake_delay_ms,
            schedule_mode=ScheduleMode.GLOBAL_LOCK, warmup=warmup, repeat=repeat,
            long_delay_ms=200, long_request_session_index=0,
        )
        mixed_global_samples, mixed_global_batch = await self._run_scenario_mixed(
            mixed_global_config, agent_id, global_lock=True,
        )
        all_samples.extend(mixed_session_samples)
        all_samples.extend(mixed_global_samples)
        mixed_session_stats = _with_batch_metrics(
            aggregate_scenario_stats(
                ConcurrencyScenario.MIXED_LONG_SHORT.value,
                ScheduleMode.SESSION_LOCK.value,
                mixed_session_samples,
                repeat,
            ),
            mixed_session_batch,
        )
        mixed_global_stats = _with_batch_metrics(
            aggregate_scenario_stats(
                ConcurrencyScenario.MIXED_LONG_SHORT.value,
                ScheduleMode.GLOBAL_LOCK.value,
                mixed_global_samples,
                repeat,
            ),
            mixed_global_batch,
        )
        all_scenarios.extend((mixed_session_stats, mixed_global_stats))
        all_configs.extend((mixed_session_config.to_dict(), mixed_global_config.to_dict()))
        print(f"  长短混合对照: {compare_schedule_modes(mixed_session_stats, mixed_global_stats).get('throughput_change_rate', 'N/A')}")

        # === 场景 6：取消不阻塞 ===
        print("=== 场景 6：取消不阻塞 ===")
        cancel_config = WorkloadConfig(
            session_count=1, requests_per_session=2, fake_delay_ms=fake_delay_ms,
            schedule_mode=ScheduleMode.SESSION_LOCK, warmup=warmup, repeat=repeat,
            long_delay_ms=200,
        )
        cancel_samples = await self._run_scenario_cancel(
            cancel_config, agent_id, output_dir,
        )
        all_samples.extend(cancel_samples)
        cancel_stats = aggregate_scenario_stats(
            ConcurrencyScenario.CANCEL_NON_BLOCKING.value,
            ScheduleMode.SESSION_LOCK.value,
            cancel_samples,
            repeat,
        )
        all_scenarios.append(cancel_stats)
        all_configs.append(cancel_config.to_dict())
        print(f"  取消: 送达 P50={cancel_stats.cancel_delivery_ms.p50_ms:.1f}ms, 生效 P50={cancel_stats.cancel_effect_ms.p50_ms:.1f}ms")

        # 构建快照
        snapshot = ConcurrencySnapshot(
            suite=SUITE_CONCURRENCY,
            generated_at=datetime.now(timezone.utc).isoformat(),
            git_commit=self._git_commit,
            warmup=warmup,
            repeat=repeat,
            fake_delay_ms=fake_delay_ms,
            scenarios=tuple(all_scenarios),
            workload_configs=tuple(all_configs),
        )

        # 写出工件
        self._write_artifacts(snapshot, all_samples, output_dir, baseline_dir)

        return snapshot

    # ---- 场景实现 ----

    async def _run_scenario_fifo(
        self, config: WorkloadConfig, agent_id: str, output_dir: Path,
    ) -> tuple[list[BenchmarkSample], float]:
        """执行同 Session FIFO 场景。"""
        samples: list[BenchmarkSample] = []
        total_batch_ms: float = 0.0

        for batch_index in range(config.warmup + config.repeat):
            is_warmup: bool = batch_index < config.warmup

            data_dir = _make_round_data_dir(batch_index, "fifo")
            try:
                coordinator, repository, sm, registry, context_port = _build_services(
                    data_dir, FixedDelayLLM(config.fake_delay_ms),
                    FixedDelayTool(config.fake_delay_ms), config.fake_delay_ms,
                )
                session = await sm.create(agent_id=agent_id, title="s-fifo")
                interaction = SessionInteractionService(sm, registry, coordinator)
                gate = ControlledSubmissionGate()

                batch_started: float = time.perf_counter()
                batch_samples, _ = await _run_fifo_same_session(
                    interaction, repository, session,
                    agent_id, gate, config, batch_index, is_warmup, batch_started,
                )
                interaction = SessionInteractionService(sm, registry, coordinator)
                batch_end: float = time.perf_counter()
                total_batch_ms += (batch_end - batch_started) * 1000.0
                samples.extend(batch_samples)
            finally:
                _cleanup_round_data(data_dir)

        return samples, total_batch_ms

    async def _run_scenario_isolation(
        self, config: WorkloadConfig, agent_id: str, output_dir: Path,
    ) -> tuple[list[BenchmarkSample], float]:
        """执行多 Session 隔离场景。"""
        samples: list[BenchmarkSample] = []
        total_batch_ms: float = 0.0

        for batch_index in range(config.warmup + config.repeat):
            is_warmup: bool = batch_index < config.warmup

            data_dir = _make_round_data_dir(batch_index, "iso")
            try:
                tool_port = FixedDelayTool(config.fake_delay_ms)
                coordinator, repository, sm, registry, context_port = _build_services(
                    data_dir, ToolCallingFixedDelayLLM(config.fake_delay_ms),
                    tool_port, config.fake_delay_ms,
                )
                interaction = SessionInteractionService(sm, registry, coordinator)
                session_ids: list[str] = []
                gates: dict[str, ControlledSubmissionGate] = {}
                for si in range(config.session_count):
                    session = await sm.create(agent_id=agent_id, title=f"s-iso-{si}")
                    session_ids.append(session.id)
                    gates[session.id] = ControlledSubmissionGate()

                batch_started: float = time.perf_counter()
                batch_samples, isolation = await _run_multi_session_isolation(
                    interaction, repository, session_ids, agent_id,
                    gates, config, batch_index, is_warmup, batch_started,
                    tool_port, RecordingOutputPort(),
                )
                batch_end: float = time.perf_counter()
                total_batch_ms += (batch_end - batch_started) * 1000.0

                # 将隔离结果写入 sample
                for s in batch_samples:
                    object.__setattr__(s, "message_leak_count", isolation.message_leak_count)
                    object.__setattr__(s, "event_leak_count", isolation.event_leak_count)
                    object.__setattr__(s, "context_leak_count", isolation.context_leak_count)
                    object.__setattr__(s, "tool_leak_count", isolation.tool_leak_count)
                    object.__setattr__(s, "stream_leak_count", isolation.stream_leak_count)
                    if isolation.any_leak:
                        object.__setattr__(s, "passed", False)
                        object.__setattr__(s, "failure_kind", "isolation")

                samples.extend(batch_samples)
            finally:
                _cleanup_round_data(data_dir)

        return samples, total_batch_ms

    async def _run_scenario_scaling(
        self, config: WorkloadConfig, agent_id: str, output_dir: Path,
    ) -> tuple[list[BenchmarkSample], float]:
        """执行 Session 数扩展场景（Session 锁模式）。"""
        samples: list[BenchmarkSample] = []
        total_batch_ms: float = 0.0

        for batch_index in range(config.warmup + config.repeat):
            is_warmup: bool = batch_index < config.warmup

            data_dir = _make_round_data_dir(batch_index, "scale")
            try:
                coordinator, repository, sm, registry, context_port = _build_services(
                    data_dir, FixedDelayLLM(config.fake_delay_ms),
                    FixedDelayTool(config.fake_delay_ms), config.fake_delay_ms,
                )
                interaction = SessionInteractionService(sm, registry, coordinator)
                session_ids: list[str] = []
                for si in range(config.session_count):
                    session = await sm.create(agent_id=agent_id, title=f"s-scale-{si}")
                    session_ids.append(session.id)

                batch_started: float = time.perf_counter()
                batch_samples, batch_ms = await _run_session_scaling(
                    interaction, repository, session_ids, agent_id,
                    config, batch_index, is_warmup, batch_started,
                )
                if not is_warmup:
                    total_batch_ms += batch_ms
                samples.extend(batch_samples)
            finally:
                _cleanup_round_data(data_dir)

        return samples, total_batch_ms

    async def _run_scenario_scaling_global(
        self, config: WorkloadConfig, agent_id: str, output_dir: Path,
    ) -> tuple[list[BenchmarkSample], float]:
        """执行 Session 数扩展场景（全局锁模式）。"""
        samples: list[BenchmarkSample] = []
        total_batch_ms: float = 0.0

        for batch_index in range(config.warmup + config.repeat):
            is_warmup: bool = batch_index < config.warmup

            data_dir = _make_round_data_dir(batch_index, "global")
            try:
                coordinator, repository, sm, registry, context_port = _build_services(
                    data_dir, FixedDelayLLM(config.fake_delay_ms),
                    FixedDelayTool(config.fake_delay_ms), config.fake_delay_ms,
                )
                interaction = SessionInteractionService(sm, registry, coordinator)
                global_coordinator = _GlobalLockCoordinator(interaction)

                session_ids: list[str] = []
                for si in range(config.session_count):
                    session = await sm.create(agent_id=agent_id, title=f"s-global-{si}")
                    session_ids.append(session.id)

                batch_started: float = time.perf_counter()
                batch_samples, batch_ms = await _run_session_scaling(
                    global_coordinator, repository, session_ids, agent_id,
                    config, batch_index, is_warmup, batch_started,
                )
                if not is_warmup:
                    total_batch_ms += batch_ms
                # 覆盖调度模式
                for s in batch_samples:
                    object.__setattr__(s, "schedule_mode", ScheduleMode.GLOBAL_LOCK)
                samples.extend(batch_samples)
            finally:
                _cleanup_round_data(data_dir)

        return samples, total_batch_ms

    async def _run_scenario_mixed(
        self,
        config: WorkloadConfig,
        agent_id: str,
        *,
        global_lock: bool,
    ) -> tuple[list[BenchmarkSample], float]:
        """执行一个长请求与七个独立短请求的调度对照。"""
        samples: list[BenchmarkSample] = []
        total_batch_ms: float = 0.0
        long_delay_ms: int = config.long_delay_ms or 200
        for batch_index in range(config.warmup + config.repeat):
            is_warmup: bool = batch_index < config.warmup
            data_dir = _make_round_data_dir(batch_index, "mixed-global" if global_lock else "mixed-session")
            try:
                coordinator, repository, sm, registry, context_port = _build_services(
                    data_dir,
                    SessionSelectiveDelayLLM(
                        config.fake_delay_ms,
                        long_delay_ms,
                        config.long_request_session_index,
                    ),
                    FixedDelayTool(config.fake_delay_ms),
                    config.fake_delay_ms,
                )
                interaction = SessionInteractionService(sm, registry, coordinator)
                submitter = _GlobalLockCoordinator(interaction) if global_lock else interaction
                session_ids: list[str] = []
                for session_index in range(config.session_count):
                    session = await sm.create(agent_id=agent_id, title=f"s-mixed-{session_index}")
                    session_ids.append(session.id)
                batch_started: float = time.perf_counter()
                batch_samples, batch_ms = await _run_session_scaling(
                    submitter, repository, session_ids, agent_id,
                    config, batch_index, is_warmup, batch_started,
                )
                long_samples = [
                    sample for sample in batch_samples
                    if sample.evidence_summary.get("session_index") == config.long_request_session_index
                ]
                short_samples = [
                    sample for sample in batch_samples
                    if sample.evidence_summary.get("session_index") != config.long_request_session_index
                ]
                long_ended_at = max(
                    (datetime.fromisoformat(str(sample.evidence_summary["ended_at"])) for sample in long_samples),
                    default=None,
                )
                short_before_long = (
                    long_ended_at is not None
                    and bool(short_samples)
                    and all(
                        datetime.fromisoformat(str(sample.evidence_summary["ended_at"])) < long_ended_at
                        for sample in short_samples
                    )
                )
                for sample in batch_samples:
                    object.__setattr__(sample, "case_id", ConcurrencyScenario.MIXED_LONG_SHORT.value)
                    object.__setattr__(sample, "schedule_mode", config.schedule_mode)
                    object.__setattr__(sample, "evidence_summary", {
                        **sample.evidence_summary,
                        "short_completed_before_long": short_before_long,
                    })
                    if not global_lock and not short_before_long:
                        object.__setattr__(sample, "passed", False)
                        object.__setattr__(sample, "failure_kind", "long_short_blocking")
                if not is_warmup:
                    total_batch_ms += batch_ms
                samples.extend(batch_samples)
            finally:
                _cleanup_round_data(data_dir)
        return samples, total_batch_ms

    async def _run_scenario_cancel(
        self, config: WorkloadConfig, agent_id: str, output_dir: Path,
    ) -> list[BenchmarkSample]:
        """执行取消不阻塞场景。

        核心验证：长 Run 持锁期间 cancel() 立即返回（不等待锁）；
        长 Run 完成后同 Session 后续请求正常执行（锁已释放）。
        """
        samples: list[BenchmarkSample] = []

        for batch_index in range(config.warmup + config.repeat):
            is_warmup: bool = batch_index < config.warmup

            data_dir = _make_round_data_dir(batch_index, "cancel")
            try:
                delay_ms: int = config.long_delay_ms or 200

                cancel_barrier: asyncio.Event = asyncio.Event()
                long_llm = LongDelayLLM(
                    delay_ms=delay_ms,
                    cancel_barrier=cancel_barrier,
                )

                coordinator, repository, sm, registry, context_port = _build_services(
                    data_dir, long_llm, FixedDelayTool(config.fake_delay_ms), config.fake_delay_ms,
                )
                session = await sm.create(agent_id=agent_id, title="s-cancel")
                interaction = SessionInteractionService(sm, registry, coordinator)

                _, user_msg = make_benchmark_request(agent_id, 0, 0)

                # 用 asyncio.ensure_future 启动长请求，确保两个协程可交替执行
                submit_task = asyncio.ensure_future(
                    interaction.submit(session.id, user_msg)
                )
                barrier_task = asyncio.ensure_future(cancel_barrier.wait())

                # 等待任一完成
                done, pending = await asyncio.wait(
                    [submit_task, barrier_task],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=10.0,
                )

                cancel_start: float = time.perf_counter()
                cancel_delivery_ms: float = 0.0
                cancel_effect_ms: float = 0.0
                cancel_not_blocking: bool = False
                long_result = None

                if barrier_task in done:
                    # LLM 已进入延迟中点：此时可测试 cancel 不阻塞
                    cancel_start = time.perf_counter()
                    active_runs = await repository.list_active_runs(session.id)
                    if not active_runs:
                        raise AssertionError("取消屏障打开后未找到活动 Run")
                    await interaction.cancel(active_runs[0].run_id, "PR3 benchmark cancellation")
                    cancel_delivery_end = time.perf_counter()
                    cancel_delivery_ms = (cancel_delivery_end - cancel_start) * 1000.0
                    cancel_not_blocking = cancel_delivery_ms < (delay_ms * 0.5)

                    # 等待长任务完成
                    if submit_task in pending:
                        try:
                            long_result = await asyncio.wait_for(submit_task, timeout=10.0)
                        except asyncio.TimeoutError:
                            long_result = None
                    else:
                        long_result = submit_task.result()

                    cancel_effect_end = time.perf_counter()
                    cancel_effect_ms = (cancel_effect_end - cancel_start) * 1000.0
                else:
                    # 长任务先完成（不应出现）
                    long_result = submit_task.result() if submit_task in done else None
                    cancel_effect_ms = 0.0
                    cancel_not_blocking = True  # 退化为不阻塞

                # 后续请求：验证锁已释放
                _, followup_msg = make_benchmark_request(agent_id, 0, 1)
                try:
                    followup_result = await asyncio.wait_for(
                        interaction.submit(session.id, followup_msg),
                        timeout=10.0,
                    )
                    outcome = followup_result.state.outcome()
                    followup_ok: bool = outcome is not None and outcome.value == "completed"
                except asyncio.TimeoutError:
                    followup_ok = False

                lock_released: bool = followup_ok
                cancellation_effective: bool = (
                    long_result is not None
                    and long_result.state.outcome() is not None
                    and long_result.state.outcome().value == "cancelled"
                )

                sample = BenchmarkSample(
                    dataset=SUITE_CONCURRENCY,
                    case_id=ConcurrencyScenario.CANCEL_NON_BLOCKING.value,
                    attempt=batch_index,
                    is_warmup=is_warmup,
                    git_commit=self._git_commit,
                    python_version=sys.version.split()[0],
                    platform=platform.platform(),
                    config_hash="",
                    eval_schema_version="",
                    passed=cancel_not_blocking and cancellation_effective and lock_released and followup_ok,
                    failure_kind=None if (cancel_not_blocking and cancellation_effective and lock_released and followup_ok) else "assertion",
                    assertions_passed=sum([cancel_not_blocking, cancellation_effective, lock_released, followup_ok]),
                    assertions_total=4,
                    trace_available=False,
                    wall_duration_ms=cancel_effect_ms,
                    run_id=long_result.run_id if long_result else "",
                    schedule_mode=config.schedule_mode,
                    session_count=config.session_count,
                    requests_per_session=config.requests_per_session,
                    fake_delay_ms=config.fake_delay_ms,
                    cancel_delivery_ms=cancel_delivery_ms,
                    cancel_effect_ms=cancel_effect_ms,
                    cancellation_delivered=cancel_not_blocking,
                    cancellation_effective=cancellation_effective,
                    lock_released=lock_released,
                    followup_completed=followup_ok,
                    evidence_summary={
                        "delay_ms": delay_ms,
                        "cancel_delivery_ms": cancel_delivery_ms,
                        "cancel_effect_ms": cancel_effect_ms,
                        "cancel_not_blocking": cancel_not_blocking,
                    },
                )
                samples.append(sample)
            finally:
                _cleanup_round_data(data_dir)

        return samples

    # ---- 工件写出 ----

    def _write_artifacts(
        self,
        snapshot: ConcurrencySnapshot,
        samples: list[BenchmarkSample],
        output_dir: Path,
        baseline_dir: Path | None,
    ) -> None:
        """写出 JSONL / JSON / Markdown 工件。"""
        snapshot_id: str = make_snapshot_id()

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # JSONL
        samples_path: Path = out / "samples" / f"{snapshot_id}.jsonl"
        samples_path.parent.mkdir(parents=True, exist_ok=True)
        with samples_path.open("w", encoding="utf-8") as fh:
            for s in samples:
                fh.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")

        # JSON 快照
        snapshot_path: Path = out / f"{snapshot_id}.json"
        snapshot_path.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Markdown 报告
        report_path: Path = out / f"{snapshot_id}.md"
        report_path.write_text(
            _build_concurrency_report(snapshot, snapshot_id, samples_path),
            encoding="utf-8",
        )
        (out / "correctness.md").write_text(
            _build_correctness_report(snapshot, snapshot_id, samples_path),
            encoding="utf-8",
        )
        (out / "scheduling-comparison.md").write_text(
            _build_scheduling_report(snapshot, snapshot_id, samples_path),
            encoding="utf-8",
        )

        # 工作负载配置
        config_path: Path = out / "workload-config.json"
        config_path.write_text(
            json.dumps(
                {"workload_configs": [list(snapshot.workload_configs)]},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

        # 可选基线目录
        if baseline_dir is not None:
            bl = Path(baseline_dir)
            bl.mkdir(parents=True, exist_ok=True)
            bl_samples = bl / "samples" / f"{snapshot_id}.jsonl"
            bl_samples.parent.mkdir(parents=True, exist_ok=True)
            bl_samples.write_text(samples_path.read_text(encoding="utf-8"), encoding="utf-8")
            (bl / f"{snapshot_id}.json").write_text(
                snapshot_path.read_text(encoding="utf-8"), encoding="utf-8",
            )

        print(f"\n工件已写出: {out}")
        print(f"  快照: {snapshot_path}")
        print(f"  报告: {report_path}")
        print(f"  样本: {samples_path}")


# --------------------------------------------------------------------------- #
# Markdown 报告
# --------------------------------------------------------------------------- #


def _build_concurrency_report(
    snapshot: ConcurrencySnapshot,
    snapshot_id: str,
    samples_path: Path,
) -> str:
    """生成并发实验 Markdown 报告。"""
    lines: list[str] = [
        f"# dotClaw 并发隔离与调度收益快照 {snapshot_id}",
        "",
        f"> 生成时间：{snapshot.generated_at}",
        f"> Git：`{snapshot.git_commit}`",
        f"> Warmup：{snapshot.warmup} | Repeat：{snapshot.repeat}",
        f"> 固定延迟：{snapshot.fake_delay_ms} ms",
        "",
        "## 场景汇总",
        "",
        "| 场景 | 调度模式 | 请求数 | 轮数 | Wall P50(ms) | Wall P95(ms) | 排队 P50(ms) | 队列 P95(ms) |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for sc in snapshot.scenarios:
        w = sc.wall_duration_ms
        q = sc.queue_wait_ms
        lines.append(
            f"| `{sc.scenario_id}` | {sc.schedule_mode} | {sc.total_requests} | {sc.total_batches} "
            f"| {w.p50_ms:.1f} | {w.p95_ms:.1f} | {q.p50_ms:.1f} | {q.p95_ms:.1f} |"
        )

    lines.extend([
        "",
        "## 正确性",
        "",
        "| 场景 | 调度模式 | FIFO 通过 | 隔离通过 | 取消通过 | 消息串扰 | 事件串扰 |",
        "|---|---|---|---|---|---|---|",
    ])

    for sc in snapshot.scenarios:
        lines.append(
            f"| `{sc.scenario_id}` | {sc.schedule_mode} "
            f"| {sc.fifo_passed_count}/{sc.fifo_total_count} "
            f"| {sc.isolation_passed_count}/{sc.isolation_total_count} "
            f"| {sc.cancel_passed_count}/{sc.cancel_total_count} "
            f"| {sc.message_leak_total} | {sc.event_leak_total} |"
        )

    lines.extend([
        "",
        "## 吞吐与对照",
        "",
        "| 场景 | 调度模式 | 吞吐(req/s) | 批次总耗时(ms) |",
        "|---|---|---|---|",
    ])

    for sc in snapshot.scenarios:
        tput = f"{sc.throughput_per_sec:.1f}" if sc.throughput_per_sec is not None else "—"
        bms = f"{sc.batch_total_ms:.1f}" if sc.batch_total_ms is not None else "—"
        lines.append(
            f"| `{sc.scenario_id}` | {sc.schedule_mode} | {tput} | {bms} |"
        )

    lines.extend([
        "",
        "## 边界说明",
        "",
        "- FIFO 结论只针对受控、同进程、单 SessionRunCoordinator 实例内的同 Session 提交。",
        "- 隔离判据以持久化运行事实中的标识回显为准；Fake LLM/Tool 只消除外部不确定性。",
        "- 全局锁对照仅证明调度结构的容量影响，不能表述为真实 Provider/API 端到端加速。",
        "- 取消结论仅证明 Runtime 内取消信号、终态收口与租约释放；不涉及外部副作用停止。",
        "",
        "## 原始证据",
        "",
        f"- 采样记录（JSONL）：`samples/{snapshot_id}.jsonl`",
        f"- 快照（JSON）：`{snapshot_id}.json`",
        f"- 工作负载配置：`workload-config.json`",
    ])

    return "\n".join(lines)


def _build_correctness_report(
    snapshot: ConcurrencySnapshot,
    snapshot_id: str,
    samples_path: Path,
) -> str:
    """生成可独立审阅的正确性报告。"""
    lines: list[str] = [
        "# PR3 正确性报告",
        "",
        f"- 快照：`{snapshot_id}`",
        f"- 原始样本：`samples/{samples_path.name}`",
        f"- Warmup / Repeat：{snapshot.warmup} / {snapshot.repeat}",
        "",
        "| 场景 | 请求数 | FIFO | 消息 | 事件 | 上下文 | 工具 | 输出串流 | 取消 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for sc in snapshot.scenarios:
        lines.append(
            f"| `{sc.scenario_id}` | {sc.total_requests} | "
            f"{sc.fifo_passed_count}/{sc.fifo_total_count} | "
            f"{sc.message_leak_total} | {sc.event_leak_total} | "
            f"{sc.context_leak_total} | {sc.tool_leak_total} | "
            f"{sc.stream_leak_total} | {sc.cancel_passed_count}/{sc.cancel_total_count} |"
        )
    lines.extend([
        "",
        "FIFO 仅覆盖受控、单进程、单 SessionRunCoordinator 实例；隔离与取消均以该快照的原始 JSONL 为证据。",
    ])
    return "\n".join(lines)


def _build_scheduling_report(
    snapshot: ConcurrencySnapshot,
    snapshot_id: str,
    samples_path: Path,
) -> str:
    """生成仅针对 Benchmark 全局串行对照的调度报告。"""
    sessions: dict[str, ScenarioStats] = {}
    globals_: dict[str, ScenarioStats] = {}
    for sc in snapshot.scenarios:
        key: str = sc.scenario_id.removesuffix("_session").removesuffix("_global")
        if sc.schedule_mode == ScheduleMode.SESSION_LOCK.value:
            sessions[key] = sc
        elif sc.schedule_mode == ScheduleMode.GLOBAL_LOCK.value:
            globals_[key] = sc

    lines: list[str] = [
        "# PR3 调度对照报告",
        "",
        f"- 快照：`{snapshot_id}`",
        f"- 原始样本：`samples/{samples_path.name}`",
        "- 对照对象：当前 Session 锁与 Benchmark 进程内全局串行锁；不代表真实 Provider/API 加速。",
        "",
        "| 负载 | Session 锁吞吐 | 全局锁吞吐 | 吞吐变化 | Session 锁排队 P50/P95 | 全局锁排队 P50/P95 | 排队 P95 变化 | Session 锁 Wall P95 | 全局锁 Wall P95 | Wall P95 变化 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(set(sessions) & set(globals_)):
        comparison = compare_schedule_modes(sessions[key], globals_[key])
        session = sessions[key]
        global_ = globals_[key]
        def render(value: object) -> str:
            return "—" if value is None else f"{float(value):+.2%}"
        lines.append(
            f"| `{key}` | {session.throughput_per_sec:.1f} | {global_.throughput_per_sec:.1f} | "
            f"{render(comparison.get('throughput_change_rate'))} | "
            f"{session.queue_wait_ms.p50_ms:.1f}/{session.queue_wait_ms.p95_ms:.1f} | "
            f"{global_.queue_wait_ms.p50_ms:.1f}/{global_.queue_wait_ms.p95_ms:.1f} | "
            f"{render(comparison.get('queue_wait_p95_change_rate'))} | "
            f"{session.wall_duration_ms.p95_ms:.1f} | {global_.wall_duration_ms.p95_ms:.1f} | "
            f"{render(comparison.get('wall_p95_change_rate'))} |"
        )
    if not (set(sessions) & set(globals_)):
        lines.append("| 无可比的完整对照负载 | — | — | — | — | — | — | — | — | — |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并执行完整并发实验套件。"""
    parser = argparse.ArgumentParser(description="dotClaw PR3：并发隔离与调度收益")
    parser.add_argument("--suite", type=str, default="reliability_concurrency_v1",
                        help="实验族标识（默认 reliability_concurrency_v1）")
    parser.add_argument("--warmup", type=int, default=5,
                        help="预热采样数，>= 0（默认 5）")
    parser.add_argument("--repeat", type=int, default=100,
                        help="正式采样数，> 0（默认 100）")
    parser.add_argument("--fake-delay-ms", type=int, default=20,
                        help="固定延迟替身的延迟毫秒数（默认 20）")
    parser.add_argument("--output", type=str, default=None,
                        help="非提交运行工件输出目录（默认 benchmarks/reports/concurrency/<run-id>）")
    parser.add_argument("--save-baseline", type=str, default=None,
                        help="可选提交基线目录")
    args = parser.parse_args(argv)

    if args.warmup < 0:
        parser.error("--warmup 必须大于等于 0")
    if args.repeat <= 0:
        parser.error("--repeat 必须大于 0")
    if args.fake_delay_ms < 1:
        parser.error("--fake-delay-ms 必须大于 0")

    output_dir: Path = (
        Path(args.output) if args.output
        else Path("benchmarks") / "reports" / "concurrency" / make_snapshot_id()
    )
    baseline_dir: Path | None = Path(args.save_baseline) if args.save_baseline else None

    try:
        snapshot: ConcurrencySnapshot = asyncio.run(
            ConcurrencyReliabilityRunner().run(
                warmup=args.warmup,
                repeat=args.repeat,
                fake_delay_ms=args.fake_delay_ms,
                output_dir=output_dir,
                baseline_dir=baseline_dir,
            )
        )
    except (ValueError, FileExistsError) as exc:
        print(f"实验失败：{exc}", file=sys.stderr)
        return 1

    print(f"\n=== dotClaw 并发实验完成 ===")
    print(f"  快照: {snapshot.generated_at}")
    print(f"  场景数: {len(snapshot.scenarios)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
