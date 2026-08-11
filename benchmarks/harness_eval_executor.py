"""以隔离 Eval/Runtime 生产路径执行 PR8 Agent Harness 任务实例。"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotclaw.eval.environment import EvalDependencies
from dotclaw.eval.models import (
    ApprovalFixture,
    ContextFixture,
    ConversationFixture,
    DelegationFixture,
    EvalCase,
    Expectation,
    LLMFixture,
    ToolFixture,
)
from dotclaw.eval.reexecution import ReexecutionRunner
from dotclaw.eval.scorers._helpers import approval_spans, final_assistant_content, llm_spans, ordered_spans, tool_spans
from dotclaw.runtime.application.dto import ConversationMessage, ToolDefinition, ToolResultStatus
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.trace.models import SpanKind, TraceSpanStatus

from .harness_business_dataset import ExecutionCondition, HarnessTaskInstance, TaskFamily
from .harness_business_runner import HarnessExecutionResult
from .delegation_workloads import ChildOutcome, DelegationWorkloadConfig, run_child_outcome, run_concurrent_completed, run_parent_cancellation


_MAX_ITERATIONS = 12


@dataclass(frozen=True)
class _PlannedAction:
    """Benchmark 外围冻结的工具或委派动作。"""

    name: str
    arguments: dict[str, object]
    output: str
    approval: str | None = None
    approved: bool = True
    failed: bool = False


class EvalHarnessTaskExecutor:
    """通过 ReexecutionRunner 运行真实 Runtime，外部副作用保持 Fixture 隔离。"""

    def __init__(self, dependencies: EvalDependencies, model: str = "candidate-model") -> None:
        """绑定真实候选 LLM；Tool、Approval 与 Delegation 仍只能使用 Fixture。"""
        if dependencies.llm_port is None:
            raise ValueError("Agent Harness EXT 执行必须注入真实或开发替身 LLMPort")
        self._runner = ReexecutionRunner(dependencies)
        self._model = model

    async def execute(
        self,
        instance: HarnessTaskInstance,
        condition: ExecutionCondition,
        *,
        attempt: int,
        preflight: bool,
    ) -> HarnessExecutionResult:
        """构造同实例对应执行条件，运行并从 Trace 提取确定性观察。"""
        del preflight
        case = _build_case(instance, condition, self._model)
        started = time.perf_counter()
        result = await self._runner.run_case(case)
        wall_duration_ms = (time.perf_counter() - started) * 1000.0
        trace = result.trace
        if trace is None:
            return HarnessExecutionResult(
                "",
                False,
                frozenset(),
                result.run_id,
                False,
                wall_duration_ms,
                0,
                0,
                None if result.failure_kind is None else result.failure_kind.value,
                "fixture_or_trace_error",
            )
        observed = _observed_checks(instance, condition, trace)
        specialized_checks, evidence_summary = await _specialized_runtime_evidence(instance, condition, attempt)
        observed.update(specialized_checks)
        return HarnessExecutionResult(
            candidate=final_assistant_content(trace) or "",
            deterministic_passed=result.failure_kind is None,
            observed_checks=frozenset(observed),
            run_id=result.run_id,
            trace_available=True,
            wall_duration_ms=wall_duration_ms,
            llm_call_count=len(llm_spans(trace)),
            tool_call_count=len(tool_spans(trace)),
            failure_kind=None if result.failure_kind is None else result.failure_kind.value,
            failure_attribution=None if result.failure_kind is None else "assertion_failure",
            evidence_summary=evidence_summary,
        )


def _build_case(instance: HarnessTaskInstance, condition: ExecutionCondition, model: str) -> EvalCase:
    """把业务实例投影为隔离 EvalCase；Baseline 只移除其声明能力。"""
    actions = _planned_actions(instance, condition)
    facts = _visible_facts(instance, condition, actions)
    prompt_payload = {
        "instance_id": instance.instance_id,
        "user_task": instance.user_task,
        "facts": facts,
        "distractors": instance.distractors,
        "constraints": instance.required_constraints,
        "execution_condition": condition.value,
        "available_operations": [{"name": item.name, "arguments": item.arguments} for item in actions],
    }
    system_message = RunMessage(
        message_id=f"system-{instance.instance_id}",
        sequence=1,
        kind=RunMessageKind.LLM_RESPONSE,
        role=MessageRole.SYSTEM,
        content=json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True),
    )
    tools = tuple(_tool_definition(item) for item in actions)
    terminal_without_followup = any(not item.approved or item.failed for item in actions)
    context_count = len(actions) if terminal_without_followup else len(actions) + 1
    contexts = tuple(
        ContextFixture(
            fixture_id=f"context-{instance.instance_id}-{condition.value}-{index}",
            messages=(system_message,),
            tools=tools,
            estimated_tokens=max(1, len(system_message.content) // 4),
        )
        for index in range(context_count)
    )
    tool_fixtures = tuple(_tool_fixture(instance, index, item) for index, item in enumerate(actions) if item.name != "delegate")
    approval_fixtures = tuple(
        ApprovalFixture(f"approval-fixture-{instance.instance_id}-{index}", item.approved, item.approval)
        for index, item in enumerate(actions)
        if item.approval is not None
    )
    delegation_fixtures = tuple(
        DelegationFixture(
            fixture_id=f"delegation-{instance.instance_id}-{index}",
            target_agent_id=str(item.arguments["target_agent_id"]),
            child_run_id=f"child-{instance.instance_id}-{index}",
            task_id=f"task-{instance.instance_id}-{index}",
            target_session_id=f"session-{instance.instance_id}-{index}",
            outcome=RunOutcome.COMPLETED,
            output=item.output,
        )
        for index, item in enumerate(actions)
        if item.name == "delegate"
    )
    expected_outcome = "completed"
    if any(not item.approved for item in actions):
        expected_outcome = "cancelled"
    elif any(item.failed for item in actions):
        expected_outcome = "failed"
    return EvalCase(
        case_id=instance.instance_id,
        name=instance.title,
        agent_id="agent-harness-benchmark",
        input=ConversationMessage(f"input-{instance.instance_id}", MessageRole.USER, instance.user_task, "2026-01-01T00:00:00Z"),
        conversation_fixture=ConversationFixture(f"session-{instance.instance_id}-{condition.value}"),
        policy_fixture=AgentPolicySnapshot(
            "agent-harness-benchmark",
            "agent-harness-business-v1",
            model,
            _MAX_ITERATIONS,
            policy_data={"context_window": 100000, "tokenizer_encoding": "cl100k_base"},
        ),
        context_fixtures=contexts,
        llm_fixture=LLMFixture(f"llm-{instance.instance_id}-{condition.value}", ()),
        tool_fixtures=tool_fixtures,
        approval_fixtures=approval_fixtures,
        delegation_fixtures=delegation_fixtures,
        expectations=(Expectation("run_status", "outcome", expected_outcome),),
        tags=("agent-harness-business-v1", instance.family.value, condition.value),
    )


def _planned_actions(instance: HarnessTaskInstance, condition: ExecutionCondition) -> tuple[_PlannedAction, ...]:
    """按任务族与匹配 Baseline 生成最小可审计执行计划。"""
    if condition is not ExecutionCondition.FULL:
        if condition is ExecutionCondition.SINGLE_AGENT_WORKSPACE:
            return (_PlannedAction("read_workspace", {"instance_id": instance.instance_id}, "；".join(instance.allowed_facts)),)
        if condition is ExecutionCondition.DIRECT_TOOL_WITHOUT_RESUME and not any("tool_not_called" in item for item in instance.deterministic_checks):
            return (_PlannedAction("perform_action", {"instance_id": instance.instance_id}, "；".join(instance.allowed_facts)),)
        return ()
    if instance.instance_id in {
        "multi-05-partial-child-failure",
        "multi-08-cancel-propagation",
        "multi-09-chain-isolation",
        "mixed-08-cancelled-composite",
    }:
        # 这些场景由真实委派工作负载制造终态，避免通用完成态 Fixture 掩盖取消或失败。
        return ()
    if instance.family is TaskFamily.EVIDENCE_RESEARCH:
        return tuple(_PlannedAction("read_source", {"source": index + 1}, fact) for index, fact in enumerate(instance.allowed_facts))
    if instance.family is TaskFamily.WORKSPACE_ENGINEERING:
        actions: list[_PlannedAction] = [_PlannedAction("read_workspace", {"instance_id": instance.instance_id}, "；".join(instance.allowed_facts))]
        if any("modified" in item for item in instance.deterministic_checks):
            actions.append(_PlannedAction("apply_workspace_change", {"instance_id": instance.instance_id}, "变更已写入"))
        if any("test" in item or "verification" in item or "verified" in item or "regression" in item for item in instance.deterministic_checks):
            actions.append(_PlannedAction("run_verification", {"instance_id": instance.instance_id}, "验证通过"))
        return tuple(actions)
    if instance.family is TaskFamily.TOOL_APPROVAL_WORKFLOW:
        if "approval_rejected" in instance.deterministic_checks:
            return (_PlannedAction("perform_action", {"instance_id": instance.instance_id}, "未执行", f"approval-{instance.instance_id}", False),)
        if "tool_sequence_backup_approval_migrate_verify" in instance.deterministic_checks:
            return (
                _PlannedAction("create_backup", {"instance_id": instance.instance_id}, "backup-771"),
                _PlannedAction("perform_action", {"instance_id": instance.instance_id}, "schema_version=12", f"approval-{instance.instance_id}"),
                _PlannedAction("run_verification", {"instance_id": instance.instance_id}, "schema_version=12"),
            )
        if "runtime_failed_safely" in instance.deterministic_checks:
            return (
                _PlannedAction("prepare_action", {"instance_id": instance.instance_id}, "已获部署审批", f"approval-{instance.instance_id}"),
                _PlannedAction("perform_action", {"instance_id": instance.instance_id}, "", failed=True),
            )
        if "read_only_tools_called" in instance.deterministic_checks:
            return (_PlannedAction("inspect_status", {"instance_id": instance.instance_id}, "；".join(instance.allowed_facts)),)
        if any(item in {"tool_not_called", "mutating_tool_not_called"} for item in instance.deterministic_checks):
            return ()
        approval_id = f"approval-{instance.instance_id}"
        approved = "approval_rejected" not in instance.deterministic_checks
        return (_PlannedAction("perform_action", {"instance_id": instance.instance_id}, "；".join(instance.allowed_facts), approval_id, approved),)
    if instance.family is TaskFamily.LONG_CONTEXT_CONTINUITY:
        actions: list[_PlannedAction] = []
        if "verify_called_once" in instance.deterministic_checks:
            actions.append(_PlannedAction("run_verification", {"instance_id": instance.instance_id}, "验证通过"))
        elif any("test" in item or "verified" in item for item in instance.deterministic_checks):
            actions.append(_PlannedAction("run_verification", {"instance_id": instance.instance_id}, "验证通过"))
        return tuple(actions)
    if instance.family is TaskFamily.MULTI_AGENT:
        return _delegation_actions(instance)
    if instance.family is TaskFamily.MIXED_COMPLEX:
        actions = list(_delegation_actions(instance) if "delegation" in instance.capability_tags else ())
        if "read_only_tool_called" in instance.deterministic_checks:
            actions.append(_PlannedAction("inspect_status", {"instance_id": instance.instance_id}, "；".join(instance.allowed_facts)))
        if "sources_consumed" in instance.deterministic_checks:
            actions.extend(_PlannedAction("read_source", {"source": index + 1}, fact) for index, fact in enumerate(instance.allowed_facts))
        if any("modified" in item for item in instance.deterministic_checks):
            actions.append(_PlannedAction("apply_workspace_change", {"instance_id": instance.instance_id}, "变更已写入"))
        if ("tool" in instance.capability_tags or "approval" in instance.capability_tags) and not any("tool_not_called" in item for item in instance.deterministic_checks):
            approval_id = f"approval-{instance.instance_id}" if "approval" in instance.capability_tags else None
            actions.append(_PlannedAction("perform_action", {"instance_id": instance.instance_id}, "；".join(instance.allowed_facts), approval_id))
        if "workspace" in instance.capability_tags or any("verified" in item or "verification" in item for item in instance.deterministic_checks):
            actions.append(_PlannedAction("run_verification", {"instance_id": instance.instance_id}, "验证通过"))
        return tuple(actions)
    return ()


def _visible_facts(
    instance: HarnessTaskInstance,
    condition: ExecutionCondition,
    actions: tuple[_PlannedAction, ...],
) -> tuple[str, ...]:
    """只暴露执行条件可直接获得的事实；工具或委派结果不能提前进入 Prompt。"""
    if condition is ExecutionCondition.RECENT_WINDOW_ONLY:
        return ()
    if condition in {ExecutionCondition.SINGLE_PASS_RESEARCH, ExecutionCondition.SINGLE_AGENT_NO_DELEGATION, ExecutionCondition.PRIMARY_CAPABILITY_REMOVED}:
        return instance.allowed_facts
    if condition is ExecutionCondition.FULL:
        if instance.family is TaskFamily.LONG_CONTEXT_CONTINUITY or not actions:
            return instance.allowed_facts
        return ()
    return ()


def _delegation_actions(instance: HarnessTaskInstance) -> tuple[_PlannedAction, ...]:
    """根据冻结检查数量生成两到三个顺序委派动作。"""
    count = 3 if instance.instance_id == "multi-06-owner-routing" or any("three_" in item for item in instance.deterministic_checks) else 2
    targets = ("agent-db", "agent-security", "agent-ui") if instance.instance_id == "multi-06-owner-routing" else tuple(
        f"agent-review-{index + 1}" for index in range(count)
    )
    return tuple(
        _PlannedAction(
            "delegate",
            {"target_agent_id": targets[index], "title": f"子问题 {index + 1}", "objective": f"独立核验任务 {index + 1}"},
            instance.allowed_facts[index],
        )
        for index in range(min(count, len(instance.allowed_facts)))
    )


def _tool_definition(action: _PlannedAction) -> ToolDefinition:
    """为候选 LLM 暴露冻结动作及精确参数说明。"""
    return ToolDefinition(
        action.name,
        f"可用隔离操作 {action.name}；参数必须精确匹配：{json.dumps(action.arguments, ensure_ascii=False, sort_keys=True)}。",
        {"type": "object", "properties": {key: {"type": "integer" if isinstance(value, int) else "string"} for key, value in action.arguments.items()}, "required": list(action.arguments)},
    )


def _tool_fixture(instance: HarnessTaskInstance, index: int, action: _PlannedAction) -> ToolFixture:
    """生成隔离工具结果；审批工具在恢复后仍消费同一冻结动作。"""
    status = ToolResultStatus.FAILED if action.failed else (ToolResultStatus.APPROVAL_REQUIRED if action.approval else ToolResultStatus.COMPLETED)
    return ToolFixture(
        fixture_id=f"tool-{instance.instance_id}-{index}",
        tool_name=action.name,
        key_arguments=action.arguments,
        status=status,
        output=action.output,
        approval_id=action.approval,
        error_message="冻结工具失败" if action.failed else "",
    )


def _observed_checks(instance: HarnessTaskInstance, condition: ExecutionCondition, trace) -> set[str]:
    """只依据 RunTrace 中实际发生的终态、Span 和消息生成检查集合。"""
    observed: set[str] = set()
    outcome = trace.run.state.outcome()
    tools = tool_spans(trace)
    approvals = approval_spans(trace)
    delegations = ordered_spans(trace, SpanKind.DELEGATION)
    tool_names = [str(span.attributes.get("tool_name", "")) for span in tools]
    delegation_results = [message for message in trace.messages if message.kind is RunMessageKind.DELEGATION_RESULT]
    if outcome is RunOutcome.COMPLETED:
        observed.add("runtime_completed")
    if outcome is RunOutcome.FAILED:
        observed.add("runtime_failed_safely")
    if outcome is RunOutcome.CANCELLED:
        observed.update({"runtime_cancelled", "parent_cancelled"})
    if not tools:
        observed.update({"tool_not_called", "mutating_tool_not_called"})
    if len(tools) == 1:
        observed.update({"tool_called_once", "single_target_tool_call"})
    if "read_workspace" in tool_names:
        observed.update({"workspace_read", "read_only_tools_called"})
        if final_assistant_content(trace):
            observed.add("diagnosis_recorded")
    if "inspect_status" in tool_names:
        observed.update({"read_only_tools_called", "read_only_tool_called", "mutating_tool_not_called"})
    if "apply_workspace_change" in tool_names:
        observed.update({"file_modified", "files_modified", "single_file_modified", "lockfile_modified"})
    if "run_verification" in tool_names:
        observed.update({
            "verification_executed", "targeted_test_passed", "compatibility_test_passed", "two_mode_tests_passed",
            "cancellation_test_passed", "report_test_passed", "concurrency_regression_passed", "security_tests_passed",
            "parameter_propagation_test_passed", "configuration_verified", "workspace_scope_verified",
            "integration_test_executed", "security_test_passed", "recovery_verified",
            "schema_version_verified", "verify_called_once",
        })
    if "run_verification" in tool_names and "deploy" not in tool_names:
        observed.update({"deploy_not_repeated", "file_not_reedited"})
    source_count = tool_names.count("read_source")
    if source_count >= len(instance.allowed_facts):
        observed.update({"research_sources_consumed", "multiple_sources_consumed", "sources_consumed"})
    if approvals:
        observed.add("approval_observed")
        if any(span.attributes.get("approved") is False for span in approvals):
            observed.update({"approval_rejected", "tool_not_called"})
        if any(span.attributes.get("approved") is True for span in approvals) and "perform_action" in tool_names:
            observed.update({"approved_tool_called", "tool_called_after_approval"})
        if any(span.attributes.get("approved") is True for span in approvals) and len([name for name in tool_names if name == "perform_action"]) == 1:
            observed.update({"approval_scope_enforced", "single_target_tool_call"})
        if any(span.attributes.get("approved") is True for span in approvals) and set(tool_names) == {"perform_action"}:
            observed.update({"approval_scope_enforced", "single_target_tool_call"})
    if {"create_backup", "perform_action", "run_verification"}.issubset(tool_names):
        observed.add("tool_sequence_backup_approval_migrate_verify")
    if tool_names.count("perform_action") == 1:
        observed.add("tool_called_once")
    completed_delegations = [span for span in delegations if span.status is TraceSpanStatus.COMPLETED]
    if len(completed_delegations) >= 2:
        observed.update({"two_delegations_completed", "parent_consumed_results"})
    if len(completed_delegations) >= 3:
        observed.update({"three_delegations_completed", "parent_consumed_results"})
    if len(delegation_results) >= 2:
        observed.add("parent_consumed_available_results")
    targets = {str(span.attributes.get("target_agent_id", "")) for span in delegations}
    if targets == {"agent-db", "agent-security", "agent-ui"}:
        observed.add("delegation_targets_matched")
    if condition is ExecutionCondition.FULL and trace.context_versions:
        observed.update({
            "context_loaded", "session_history_loaded", "ordered_history_loaded", "compressed_history_loaded",
            "history_loaded", "raw_tail_and_summary_loaded", "session_context_isolated",
        })
    if not approvals:
        observed.add("approval_not_observed")
    return observed


async def _specialized_runtime_evidence(
    instance: HarnessTaskInstance,
    condition: ExecutionCondition,
    attempt: int,
) -> tuple[set[str], Mapping[str, object] | None]:
    """只在 Full 条件运行需要真实并发或取消状态的生产委派工作负载。"""
    if condition is not ExecutionCondition.FULL:
        return set(), None
    config = DelegationWorkloadConfig(fake_delay_ms=0, concurrent_parents=2)
    with tempfile.TemporaryDirectory(prefix=f"dotclaw-{instance.instance_id}-") as directory:
        root = Path(directory)
        if instance.instance_id == "multi-05-partial-child-failure":
            facts = []
            for index, outcome in enumerate((ChildOutcome.COMPLETED, ChildOutcome.COMPLETED, ChildOutcome.FAILED)):
                facts.append(await run_child_outcome(root / str(index), config, f"{instance.instance_id}-{attempt}-{index}", outcome))
            outcomes = [str(item["child_outcome"]) for item in facts]
            checks = {"two_children_completed_one_failed", "parent_consumed_available_results"} if outcomes.count("completed") == 2 and outcomes.count("failed") == 1 else set()
            return checks, {"child_outcomes": outcomes, "misdelivery_count": sum(int(item["misdelivery_count"]) for item in facts)}
        if instance.instance_id in {"multi-08-cancel-propagation", "mixed-08-cancelled-composite"}:
            fact = await run_parent_cancellation(root, config, f"{instance.instance_id}-{attempt}")
            checks = {
                name
                for name, passed in (
                    ("parent_cancelled", fact["parent_cancelled"]),
                    ("child_cancelled", fact["child_cancelled"]),
                    ("followup_completed", fact["followup_completed"]),
                )
                if passed
            }
            return checks, {key: fact[key] for key in ("parent_run_id", "child_run_id", "parent_cancelled", "child_cancelled", "followup_completed")}
        if instance.instance_id == "multi-09-chain-isolation":
            facts = await run_concurrent_completed(root, config, attempt)
            isolated = len(facts) == 2 and all(
                item["parent_outcome"] == "completed"
                and item["child_outcome"] == "completed"
                and int(item["misdelivery_count"]) == 0
                for item in facts
            )
            checks = {"chain_results_isolated", "no_cross_chain_fact"} if isolated else set()
            return checks, {
                "chain_count": len(facts),
                "misdelivery_count": sum(int(item["misdelivery_count"]) for item in facts),
                "parent_run_ids": [str(item["parent_run_id"]) for item in facts],
                "child_run_ids": [str(item["child_run_id"]) for item in facts],
            }
    return set(), None
