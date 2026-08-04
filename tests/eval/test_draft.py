"""PR5 验收 1 / 2：终态 Trace 的稳定 Draft、部分 Trace 拒绝与各类事实提取。"""

from __future__ import annotations

import pytest

from dotclaw.runtime.application.dto import ToolResultStatus
from dotclaw.runtime.domain.facts import RunStatistics
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.eval.draft import (
    DRAFT_SCHEMA_VERSION,
    EvalCaseDraft,
    trace_to_eval_case_draft,
)
from dotclaw.eval.models import EvalCaseValidationError

from .helpers import build_case, make_terminal_trace


# ---------------------------------------------------------------------------
# 验收 1：终态 Trace 稳定转换 / 部分 Trace 拒绝
# ---------------------------------------------------------------------------


def test_terminal_trace_converts_to_draft() -> None:
    """终态 Trace 可转换，且草案记录来源标识、哈希与 Trace schema 版本。"""
    trace = make_terminal_trace("run-x")
    draft = trace_to_eval_case_draft(trace)

    assert draft.draft_id == "draft-run-x"
    assert draft.source_run_id == "run-x"
    assert draft.source_record_hash == trace.source.record_hash
    assert draft.source_trace_schema_version == trace.schema_version
    assert draft.schema_version == DRAFT_SCHEMA_VERSION
    assert draft.requires_review is False
    assert draft.confirmed_case_id is None
    assert draft.case.case_id == "case-run-x"
    assert draft.case.agent_id == trace.run.agent_id


def test_conversion_is_stable_for_same_trace() -> None:
    """同一 Trace 重复转换必须产出逐字节一致的草案（不含时刻类不稳定字段）。"""
    trace = make_terminal_trace("run-stable")
    first = trace_to_eval_case_draft(trace)
    second = trace_to_eval_case_draft(trace)
    assert first.to_dict() == second.to_dict()

    rebuilt = make_terminal_trace("run-stable")
    third = trace_to_eval_case_draft(rebuilt)
    assert first.to_dict() == third.to_dict()


def test_partial_trace_is_rejected() -> None:
    """部分（语义不完整）Trace 一律拒绝转换。"""
    trace = make_terminal_trace("run-partial", ended=False)
    assert trace.is_partial is True
    with pytest.raises(EvalCaseValidationError):
        trace_to_eval_case_draft(trace)


def test_custom_case_id_and_name_are_honored() -> None:
    """显式指定 case_id / name 时以调用方为准。"""
    draft = trace_to_eval_case_draft(make_terminal_trace(), case_id="case-custom", name="人工命名")
    assert draft.case.case_id == "case-custom"
    assert draft.case.name == "人工命名"


# ---------------------------------------------------------------------------
# 验收 2：Tool / Approval / Delegation / Context / Policy / Token 基线提取
# ---------------------------------------------------------------------------


def test_input_message_is_extracted() -> None:
    """入口用户输入被提取为 Case 的 input。"""
    draft = trace_to_eval_case_draft(make_terminal_trace())
    assert draft.case.input.message_id == "msg-input"
    assert draft.case.input.content == "do it"


def test_tool_fixtures_are_extracted() -> None:
    """工具 Span 按顺序提取名称、关键参数、状态与输出。"""
    draft = trace_to_eval_case_draft(make_terminal_trace())
    fixtures = draft.case.tool_fixtures
    assert [item.tool_name for item in fixtures] == ["t", "danger"]
    assert fixtures[0].key_arguments == {"x": 1}
    assert fixtures[0].status is ToolResultStatus.COMPLETED
    assert fixtures[0].output == "ok"
    assert fixtures[1].output == "allowed"
    assert fixtures[0].fixture_id == "tool-c1"
    assert fixtures[1].fixture_id == "tool-c2"


def test_approval_fixtures_are_extracted() -> None:
    """审批 Span 的决议被冻结为 ApprovalFixture。"""
    draft = trace_to_eval_case_draft(make_terminal_trace())
    approvals = draft.case.approval_fixtures
    assert len(approvals) == 1
    assert approvals[0].approval_id == "a1"
    assert approvals[0].approved is True
    assert approvals[0].fixture_id == "approval-a1"


def test_delegation_fixtures_are_extracted() -> None:
    """委派 Span 的受理信息与结果被冻结为 DelegationFixture。"""
    draft = trace_to_eval_case_draft(make_terminal_trace())
    delegations = draft.case.delegation_fixtures
    assert len(delegations) == 1
    item = delegations[0]
    assert item.child_run_id == "child-1"
    assert item.target_agent_id == "agent-2"
    assert item.task_id == "task-1"
    assert item.outcome is RunOutcome.COMPLETED
    assert item.output == "delegated done"


def test_llm_fixture_preserves_response_order() -> None:
    """LLM 响应按 Span 顺序脚本化，含工具调用。"""
    draft = trace_to_eval_case_draft(make_terminal_trace())
    responses = draft.case.llm_fixture.responses
    assert [item.message_id for item in responses] == [
        "msg-llm1",
        "msg-llm2",
        "msg-llm3",
        "msg-llm4",
    ]
    assert responses[0].tool_calls[0].name == "t"
    assert responses[-1].content == "final answer"


def test_context_and_policy_are_frozen() -> None:
    """上下文版本按 LLM Span 数冻结，Policy 直接取自 Run 快照，工具定义从 Span 提取。"""
    trace = make_terminal_trace()
    draft = trace_to_eval_case_draft(trace)
    assert [item.fixture_id for item in draft.case.context_fixtures] == [
        "ctx-1", "ctx-2", "ctx-3", "ctx-4",
    ]
    # 工具定义从 Trace 的工具 Span 中按名称去重派生
    tool_names = [tool.name for tool in draft.case.context_fixtures[0].tools]
    assert tool_names == ["t", "danger"]
    assert draft.case.policy_fixture == trace.run.policy
    assert draft.case.policy_fixture.max_iterations == 10
    assert draft.case.conversation_fixture.session_id == trace.run.session_id


def test_token_and_call_count_baseline_is_extracted() -> None:
    """Token 与调用次数基线作为独立 Expectation，对齐 scorer 契约。"""
    statistics = RunStatistics(
        duration_ms=1234, llm_call_count=4, tool_call_count=2, tokens_in=111, tokens_out=222
    )
    draft = trace_to_eval_case_draft(make_terminal_trace(statistics=statistics))
    by_kind_target = {
        (item.kind, item.target): item.expected
        for item in draft.case.expectations
    }
    assert by_kind_target[("token_budget", "tokens_in")] == 111
    assert by_kind_target[("token_budget", "tokens_out")] == 222
    assert by_kind_target[("iteration_budget", "llm_calls")] == 4
    assert by_kind_target[("iteration_budget", "tool_calls")] == 2


def test_base_expectations_cover_status_iteration_and_tool_sequence() -> None:
    """基础断言包含运行状态、独立迭代预算与工具序列，对齐 scorer 契约。"""
    draft = trace_to_eval_case_draft(make_terminal_trace())
    by_kind_target = {(item.kind, item.target): item.expected for item in draft.case.expectations}
    assert by_kind_target[("run_status", "run")] == RunOutcome.COMPLETED.value
    assert by_kind_target[("iteration_budget", "llm_calls")] == 0
    assert by_kind_target[("iteration_budget", "tool_calls")] == 0
    assert by_kind_target[("token_budget", "tokens_in")] == 0
    assert by_kind_target[("token_budget", "tokens_out")] == 0
    assert by_kind_target[("tool_sequence", "run")] == ["t", "danger"]


def test_source_trace_is_recorded_without_content() -> None:
    """草案保留结构化来源视图，便于人工审阅时回溯。"""
    draft = trace_to_eval_case_draft(make_terminal_trace("run-src"))
    assert draft.case.source_trace is not None
    assert "run-src" in draft.case.source_trace


# ---------------------------------------------------------------------------
# Draft 模型自身：序列化对称与严格校验
# ---------------------------------------------------------------------------


def test_draft_round_trip_is_symmetric() -> None:
    """to_dict / from_dict 双向对称。"""
    draft = trace_to_eval_case_draft(make_terminal_trace())
    restored = EvalCaseDraft.from_dict(draft.to_dict())
    assert restored.to_dict() == draft.to_dict()


def test_draft_round_trip_keeps_review_and_confirm_fields() -> None:
    """审阅标记与确认结果参与序列化。"""
    draft = EvalCaseDraft(
        draft_id="draft-1",
        source_run_id="run-1",
        source_record_hash="hash-1",
        source_trace_schema_version="1.0",
        case=build_case(),
        requires_review=True,
        confirmed_case_id="case-9",
    )
    restored = EvalCaseDraft.from_dict(draft.to_dict())
    assert restored.requires_review is True
    assert restored.confirmed_case_id == "case-9"


def test_draft_rejects_unsupported_schema_version() -> None:
    """读取到未知 Draft schema 版本必须明确失败。"""
    payload = trace_to_eval_case_draft(make_terminal_trace()).to_dict()
    payload["schema_version"] = "9.9"
    with pytest.raises(EvalCaseValidationError):
        EvalCaseDraft.from_dict(payload)


@pytest.mark.parametrize(
    "field_name",
    ["draft_id", "source_run_id", "source_record_hash", "source_trace_schema_version"],
)
def test_draft_rejects_empty_identifiers(field_name: str) -> None:
    """来源标识与草案标识均不可为空。"""
    values = {
        "draft_id": "draft-1",
        "source_run_id": "run-1",
        "source_record_hash": "hash-1",
        "source_trace_schema_version": "1.0",
    }
    values[field_name] = ""
    with pytest.raises(EvalCaseValidationError):
        EvalCaseDraft(case=build_case(), **values)


def test_draft_rejects_non_bool_requires_review() -> None:
    """from_dict 拒绝非布尔 requires_review，防止字符串 "false" 被静默判 True。"""
    payload = trace_to_eval_case_draft(make_terminal_trace()).to_dict()
    payload["requires_review"] = "false"
    with pytest.raises(EvalCaseValidationError, match="requires_review.*必须是布尔值"):
        EvalCaseDraft.from_dict(payload)

    payload["requires_review"] = 1
    with pytest.raises(EvalCaseValidationError, match="requires_review.*必须是布尔值"):
        EvalCaseDraft.from_dict(payload)
