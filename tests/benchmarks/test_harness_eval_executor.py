"""PR8 Agent Harness 隔离 Runtime 执行器测试。"""

import json
from pathlib import Path

import pytest

from benchmarks.harness_business_dataset import ExecutionCondition, load_harness_dataset
from benchmarks.harness_eval_executor import EvalHarnessTaskExecutor, _build_case
from dotclaw.eval.environment import EvalDependencies
from dotclaw.runtime.application.dto import ContextBundle
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import LLMOutputPort
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall


class _PlanFollowingLLM:
    """按冻结 available_operations 顺序调用工具，最后返回当前上下文摘要。"""

    def __init__(self) -> None:
        self._step_by_run: dict[str, int] = {}
        self.seen_roles: list[tuple[MessageRole, ...]] = []

    async def complete(
        self,
        context: ContextBundle,
        execution: RunExecutionView,
        output_port: LLMOutputPort | None = None,
    ) -> RunMessage:
        """从 system 数据读取动作，不接触外部依赖。"""
        del output_port
        self.seen_roles.append(tuple(message.role for message in context.messages))
        payload = json.loads(context.messages[0].content)
        step = self._step_by_run.get(execution.run_id, 0)
        actions = payload["available_operations"]
        self._step_by_run[execution.run_id] = step + 1
        if step < len(actions):
            action = actions[step]
            return RunMessage(
                f"call-{execution.run_id}-{step}",
                1,
                RunMessageKind.LLM_RESPONSE,
                MessageRole.ASSISTANT,
                "",
                tool_calls=(ToolCall(f"tool-{step}", action["name"], action["arguments"]),),
            )
        observed = tuple(message.content for message in context.messages if message.content and message is not context.messages[0])
        delegated = tuple(item["content"] for item in payload["delegation_results"])
        content = "；".join((*payload["facts"], *delegated, *observed, *payload["constraints"]))
        return RunMessage(f"final-{execution.run_id}", 1, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, content)

    async def cancel(self, run_id: str) -> None:
        """开发替身不持有远程资源。"""


@pytest.mark.asyncio
async def test_evidence_full_executes_all_source_tools() -> None:
    """研究 Full 条件必须真实经过 Runtime 工具循环并消费全部来源。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = next(item for item in dataset.instances if item.instance_id == "evidence-01-vendor-decision")
    llm = _PlanFollowingLLM()
    executor = EvalHarnessTaskExecutor(EvalDependencies(llm_port=llm))

    result = await executor.execute(instance, ExecutionCondition.FULL, attempt=0, preflight=False)

    assert result.trace_available
    assert result.deterministic_passed
    assert result.tool_call_count == 3
    assert "research_sources_consumed" in result.observed_checks
    assert result.candidate
    assert any(MessageRole.TOOL in roles for roles in llm.seen_roles[1:])


def test_candidate_prompt_does_not_expose_expected_delivery_or_tool_outputs() -> None:
    """候选 Prompt 不得泄露理想答案或尚未执行的来源结果。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = next(item for item in dataset.instances if item.instance_id == "evidence-01-vendor-decision")

    case = _build_case(instance, ExecutionCondition.FULL, "model")
    payload = json.loads(case.context_fixtures[0].messages[0].content)

    assert "expected_delivery" not in payload
    assert "planned_actions" not in payload
    assert payload["facts"] == []
    assert len(payload["available_operations"]) == len(instance.allowed_facts)
    assert "不得重复调用" in payload["operation_protocol"]
    baseline = _build_case(instance, ExecutionCondition.SINGLE_PASS_RESEARCH, "model")
    assert case.policy_fixture.max_iterations == baseline.policy_fixture.max_iterations


@pytest.mark.asyncio
async def test_multi_agent_full_resumes_two_fixture_delegations() -> None:
    """多 Agent Full 条件必须由 Harness 完成两次生产委派并回灌候选综合。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = next(item for item in dataset.instances if item.instance_id == "multi-01-two-source-review")
    executor = EvalHarnessTaskExecutor(EvalDependencies(llm_port=_PlanFollowingLLM()))

    result = await executor.execute(instance, ExecutionCondition.FULL, attempt=0, preflight=False)

    assert result.trace_available
    assert result.deterministic_passed
    assert "two_delegations_completed" in result.observed_checks
    assert "parent_consumed_results" in result.observed_checks
    assert result.llm_call_count == 1
    assert result.evidence_summary
    assert result.evidence_summary["delegation_submit_count"] == 2
    assert result.evidence_summary["result_backfill_count"] == 2
    assert result.candidate


@pytest.mark.asyncio
async def test_multi_agent_owner_routing_uses_frozen_targets() -> None:
    """专长路由必须由 Harness 按冻结目标执行，而不是依赖模型选择目标。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = next(item for item in dataset.instances if item.instance_id == "multi-06-owner-routing")
    executor = EvalHarnessTaskExecutor(EvalDependencies(llm_port=_PlanFollowingLLM()))

    result = await executor.execute(instance, ExecutionCondition.FULL, attempt=0, preflight=False)

    assert "three_delegations_completed" in result.observed_checks
    assert "delegation_targets_matched" in result.observed_checks
    assert result.evidence_summary
    assert set(result.evidence_summary["target_agent_ids"]) == {"agent-db", "agent-security", "agent-ui"}


@pytest.mark.asyncio
async def test_all_regular_delegation_cases_are_harness_orchestrated() -> None:
    """所有普通完成态委派实例都必须完成冻结子任务并把结果交给候选综合。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    specialized = {
        "multi-05-partial-child-failure",
        "multi-08-cancel-propagation",
        "multi-09-chain-isolation",
        "mixed-08-cancelled-composite",
    }
    instances = tuple(
        instance
        for instance in dataset.instances
        if "delegation" in instance.capability_tags and instance.instance_id not in specialized
    )
    executor = EvalHarnessTaskExecutor(EvalDependencies(llm_port=_PlanFollowingLLM()))

    for instance in instances:
        result = await executor.execute(instance, ExecutionCondition.FULL, attempt=0, preflight=False)
        expected_count = 3 if "three_delegations_completed" in instance.deterministic_checks or instance.instance_id == "multi-06-owner-routing" else 2

        assert result.evidence_summary, instance.instance_id
        assert result.evidence_summary["delegation_submit_count"] == expected_count, instance.instance_id
        assert result.evidence_summary["result_backfill_count"] == expected_count, instance.instance_id
        assert result.evidence_summary["parent_consumed_results"] is True, instance.instance_id
        assert result.candidate, instance.instance_id


@pytest.mark.asyncio
async def test_long_context_baseline_does_not_claim_full_history_loaded() -> None:
    """最近窗口 Baseline 不得伪造长期历史或压缩上下文已加载。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = next(item for item in dataset.instances if item.instance_id == "context-01-language-preference")
    executor = EvalHarnessTaskExecutor(EvalDependencies(llm_port=_PlanFollowingLLM()))

    result = await executor.execute(instance, ExecutionCondition.RECENT_WINDOW_ONLY, attempt=0, preflight=False)

    assert result.trace_available
    assert "session_history_loaded" not in result.observed_checks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instance_id", "expected_checks"),
    (
        ("multi-05-partial-child-failure", {"two_children_completed_one_failed", "parent_consumed_available_results"}),
        ("multi-08-cancel-propagation", {"parent_cancelled", "child_cancelled", "followup_completed"}),
        ("multi-09-chain-isolation", {"chain_results_isolated", "no_cross_chain_fact"}),
        ("mixed-08-cancelled-composite", {"context_loaded", "parent_cancelled", "child_cancelled", "followup_completed"}),
    ),
)
async def test_specialized_cases_use_persisted_delegation_evidence(instance_id: str, expected_checks: set[str]) -> None:
    """取消、失败与并行隔离 Case 必须由真实委派工作负载提供持久化证据。"""
    dataset = load_harness_dataset(Path("benchmarks/datasets"))
    instance = next(item for item in dataset.instances if item.instance_id == instance_id)
    executor = EvalHarnessTaskExecutor(EvalDependencies(llm_port=_PlanFollowingLLM()))

    result = await executor.execute(instance, ExecutionCondition.FULL, attempt=0, preflight=False)

    assert result.trace_available
    assert expected_checks <= result.observed_checks
    assert result.evidence_summary
