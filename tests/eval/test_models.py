"""EvalCase 与 Fixture 模型的序列化、schema、重复 ID 与非法值校验。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotclaw.runtime.application.dto import ToolResultStatus
from dotclaw.runtime.domain.state import RunOutcome

from dotclaw.eval.models import (
    EvalCase,
    EvalCaseValidationError,
    ExecutionMode,
    Expectation,
    LLMResponseFixture,
    ToolFixture,
)

from helpers import (
    approval_fixture,
    build_case,
    context_fixture,
    delegation_fixture,
    make_llm_fixture,
    tool_call,
    tool_fixture,
)


def test_round_trip_preserves_fields() -> None:
    """to_dict 后 from_dict 应还原等价 Case。"""
    case = build_case(
        case_id="round-trip",
        tags=("smoke", "llm"),
        expectations=(Expectation(kind="final_text", target="last_message", expected="done"),),
    )
    restored = EvalCase.from_dict(case.to_dict())
    assert restored.to_dict() == case.to_dict()
    assert restored.case_id == "round-trip"
    assert restored.tags == ("smoke", "llm")
    assert restored.execution_mode is ExecutionMode.PLAYBACK


def test_unknown_schema_version_rejected() -> None:
    """读取未知 schema 版本必须明确失败。"""
    payload = build_case(case_id="v").to_dict()
    payload["schema_version"] = "9.9"
    try:
        EvalCase.from_dict(payload)
        raise AssertionError("应当拒绝未知 schema 版本")
    except EvalCaseValidationError:
        pass


def test_duplicate_fixture_id_rejected() -> None:
    """全局 fixture 标识重复必须明确失败。"""
    payload = build_case(case_id="x").to_dict()
    # 注入与默认 llm fixture（llm-1）重复的 tool fixture 标识
    payload["tool_fixtures"] = [{
        "fixture_id": "llm-1",
        "tool_name": "search",
        "key_arguments": {},
        "status": "completed",
        "output": "",
        "approval_id": None,
        "error_message": "",
    }]
    try:
        EvalCase.from_dict(payload)
        raise AssertionError("应当拒绝重复 fixture 标识")
    except EvalCaseValidationError:
        pass


def test_empty_case_id_or_agent_id_rejected() -> None:
    """空 case_id / agent_id 必须明确失败。"""
    for field_name in ("case_id", "agent_id"):
        payload = build_case(case_id="x").to_dict()
        payload[field_name] = ""
        try:
            EvalCase.from_dict(payload)
            raise AssertionError(f"应当拒绝空 {field_name}")
        except EvalCaseValidationError:
            pass


def test_illegal_tool_status_rejected() -> None:
    """非法工具状态取值必须明确失败。"""
    payload = build_case(
        tool_fixtures=(tool_fixture("tool-1", "search", {}),),
    ).to_dict()
    payload["tool_fixtures"][0]["status"] = "not_a_status"
    try:
        EvalCase.from_dict(payload)
        raise AssertionError("应当拒绝非法工具状态")
    except EvalCaseValidationError:
        pass


def test_approval_required_approval_id() -> None:
    """需要审批的工具必须声明 approval_id。"""
    try:
        ToolFixture(
            fixture_id="t",
            tool_name="x",
            key_arguments={},
            status=ToolResultStatus.APPROVAL_REQUIRED,
            approval_id=None,
        )
        raise AssertionError("应当拒绝缺少 approval_id 的审批工具")
    except EvalCaseValidationError:
        pass


def test_duplicate_llm_response_id_rejected() -> None:
    """LLM 脚本内重复响应消息标识必须明确失败。"""
    try:
        LLMResponseFixture(message_id="same")
        LLMResponseFixture(message_id="same")
        make_llm_fixture("llm-1", (
            LLMResponseFixture(message_id="same", content="a"),
            LLMResponseFixture(message_id="same", content="b"),
        ))
        raise AssertionError("应当拒绝重复 LLM 响应标识")
    except EvalCaseValidationError:
        pass


def test_non_json_expectation_option_rejected() -> None:
    """Expectation 的非 JSON options 必须明确失败。"""
    try:
        Expectation(kind="k", target="t", options={"fn": lambda: 1})
        raise AssertionError("应当拒绝非 JSON 的 options")
    except EvalCaseValidationError:
        pass


def test_delegation_outcome_round_trip() -> None:
    """DelegationFixture 的 outcome 可正确序列化与还原。"""
    case = build_case(
        delegation_fixtures=(delegation_fixture("del-1", "agent-b", "child-1", outcome=RunOutcome.COMPLETED, output="ok"),),
    )
    restored = EvalCase.from_dict(case.to_dict())
    assert restored.delegation_fixtures[0].outcome is RunOutcome.COMPLETED
    assert restored.delegation_fixtures[0].output == "ok"
