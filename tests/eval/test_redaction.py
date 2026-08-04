"""PR5 验收 4：敏感字段名、凭证模式、普通 LLM 回复与 requires_review 门槛。"""

from __future__ import annotations

import pytest

from dotclaw.eval.draft import EvalCaseDraft
from dotclaw.eval.redaction import REDACTED_MARKER, redact_draft

from .helpers import build_case, llm_response, make_llm_fixture, tool_fixture

PRIVATE_KEY_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n"
    "-----END RSA PRIVATE KEY-----"
)


def wrap(case, *, requires_review: bool = False) -> EvalCaseDraft:
    """把候选 Case 包成草案，便于直接调用脱敏。"""
    return EvalCaseDraft(
        draft_id="draft-1",
        source_run_id="run-1",
        source_record_hash="hash-1",
        source_trace_schema_version="1.0",
        case=case,
        requires_review=requires_review,
    )


def draft_with_tool_args(arguments: dict) -> EvalCaseDraft:
    """构造带工具关键参数的草案。"""
    return wrap(build_case(tool_fixtures=(tool_fixture("tool-1", "t", arguments),)))


def draft_with_llm_content(content: str) -> EvalCaseDraft:
    """构造带 LLM 回复正文的草案。"""
    return wrap(
        build_case(llm_fixture=make_llm_fixture("llm-1", (llm_response("m1", content=content),)))
    )


# ---------------------------------------------------------------------------
# 敏感字段名：确定性替换，不触发人工复核
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name", ["token", "api_key", "password", "authorization", "cookie", "secret"]
)
def test_sensitive_field_names_are_replaced_without_review(field_name: str) -> None:
    """字段名命中即整体替换为固定标记；这是安全处理，不需要人工复核。"""
    draft = draft_with_tool_args({field_name: "super-sensitive-value"})
    result = redact_draft(draft)
    assert result.case.tool_fixtures[0].key_arguments[field_name] == REDACTED_MARKER
    assert result.requires_review is False


def test_sensitive_field_name_match_is_case_insensitive() -> None:
    """字段名大小写不影响命中。"""
    result = redact_draft(draft_with_tool_args({"API_KEY": "abc", "Authorization": "xyz"}))
    arguments = result.case.tool_fixtures[0].key_arguments
    assert arguments["API_KEY"] == REDACTED_MARKER
    assert arguments["Authorization"] == REDACTED_MARKER
    assert result.requires_review is False


def test_sensitive_field_replaces_whole_nested_value() -> None:
    """敏感字段下的整棵子树被整体替换，不做部分保留。"""
    result = redact_draft(
        draft_with_tool_args({"secret": {"inner": ["a", "b"], "deep": {"k": "v"}}})
    )
    assert result.case.tool_fixtures[0].key_arguments["secret"] == REDACTED_MARKER


def test_nested_non_sensitive_values_are_preserved() -> None:
    """非敏感字段的嵌套结构原样保留。"""
    payload = {"nested": {"list": [1, 2, {"api_key": "leak"}], "plain": "keep"}}
    result = redact_draft(draft_with_tool_args(payload))
    arguments = result.case.tool_fixtures[0].key_arguments
    assert arguments["nested"]["plain"] == "keep"
    assert arguments["nested"]["list"][0] == 1
    assert arguments["nested"]["list"][2]["api_key"] == REDACTED_MARKER


# ---------------------------------------------------------------------------
# 已知凭证模式：启发式替换 + 置需人工复核
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "请使用 Authorization: Bearer abc.def-123456 访问",
        PRIVATE_KEY_BLOCK,
        "key = sk-" + "a" * 32,
        "aws id AKIAIOSFODNN7EXAMPLE here",
        "google AIza" + "b" * 35,
        "slack xoxb-1234567890-abcdefghij",
        "github ghp_" + "c" * 36,
    ],
)
def test_known_credential_patterns_are_redacted_and_flagged(payload: str) -> None:
    """自由文本中的已知凭证被替换，并因启发式不保证全覆盖而置需人工复核。"""
    result = redact_draft(draft_with_llm_content(payload))
    content = result.case.llm_fixture.responses[0].content
    assert REDACTED_MARKER in content
    assert result.requires_review is True


def test_credential_in_tool_output_is_redacted_and_flagged() -> None:
    """工具输出同样经过同一脱敏器。"""
    draft = wrap(
        build_case(
            tool_fixtures=(tool_fixture("tool-1", "t", {}, output="凭证：Bearer zzz.yyy-999"),)
        )
    )
    result = redact_draft(draft)
    assert REDACTED_MARKER in result.case.tool_fixtures[0].output
    assert result.requires_review is True


# ---------------------------------------------------------------------------
# 普通 LLM 回复与门槛边界
# ---------------------------------------------------------------------------


def test_plain_llm_reply_is_neither_redacted_nor_flagged() -> None:
    """普通 LLM 回复可直接作为 Playback Fixture 保存，不触发复核。"""
    draft = draft_with_llm_content("这是一段普通回复，说明了处理步骤。")
    result = redact_draft(draft)
    assert result.case.llm_fixture.responses[0].content == "这是一段普通回复，说明了处理步骤。"
    assert result.requires_review is False
    assert result.case.to_dict() == draft.case.to_dict()


def test_existing_review_flag_is_preserved() -> None:
    """既有的需复核标记不会被脱敏过程清除。"""
    draft = wrap(build_case(), requires_review=True)
    assert redact_draft(draft).requires_review is True


def test_redaction_preserves_draft_identity_fields() -> None:
    """脱敏只改载荷，不改来源标识与确认状态。"""
    draft = draft_with_llm_content("Bearer abc.def-123456")
    result = redact_draft(draft)
    assert result.draft_id == draft.draft_id
    assert result.source_run_id == draft.source_run_id
    assert result.source_record_hash == draft.source_record_hash
    assert result.source_trace_schema_version == draft.source_trace_schema_version
    assert result.confirmed_case_id is None


def test_redaction_is_deterministic() -> None:
    """同一输入重复脱敏结果一致（标记固定，不引入随机值）。"""
    draft = draft_with_tool_args({"api_key": "abc", "note": "Bearer abc.def-123456"})
    first = redact_draft(draft)
    second = redact_draft(draft)
    assert first.to_dict() == second.to_dict()
    assert redact_draft(first).to_dict() == first.to_dict()
