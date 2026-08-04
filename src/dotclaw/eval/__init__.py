"""EvalCase、隔离 Fixture Environment 与确定性评测。

本包定义版本化 ``EvalCase``、默认拒绝真实依赖的 Fixture 端口、把二者装配为隔离
``RuntimeEngine`` 的 ``EvalEnvironment``，以及执行用例并用九个确定性 Scorer 产出
可追溯 ``EvalResult`` 的 ``EvalRunner``（PR4）。
"""

from .environment import (
    EvalDependencies,
    EvalEnvironment,
    EvalRunOutcome,
    InMemoryCheckpointRepository,
)
from .fixtures import (
    ApprovalFixture,
    ContextFixture,
    DelegationFixture,
    FixtureApprovalRepository,
    FixtureConfigurationError,
    FixtureContextPort,
    FixtureDelegationPort,
    FixtureRunPolicyPort,
    FixtureToolPort,
    LLMFixture,
    LLMResponseFixture,
    ScriptedLLMPort,
    ToolFixture,
)
from .draft import DRAFT_SCHEMA_VERSION, EvalCaseDraft, trace_to_eval_case_draft
from .draft_service import EvalCaseDraftService
from .gate import RegressionGate
from .playback import PlaybackRunner
from .redaction import REDACTED_MARKER, redact_draft
from .reexecution import ReexecutionRunner
from .regression import PlaybackBatch, RegressionCaseResult, RegressionReport
from .models import (
    EVAL_SCHEMA_VERSION,
    EvalCase,
    EvalCaseValidationError,
    ExecutionMode,
    Expectation,
    FixtureMatchMode,
)
from .results import (
    AssertionResult,
    EvalResult,
    EvaluationFailureKind,
)
from .runner import EvalRunner
from .scorers import (
    ALL_SCORERS,
    ExpectationKind,
    SCORERS,
    Scorer,
)

__all__ = [
    "EvalCaseDraft",
    "DRAFT_SCHEMA_VERSION",
    "trace_to_eval_case_draft",
    "redact_draft",
    "REDACTED_MARKER",
    "EvalCaseDraftService",
    "EvalCase",
    "EvalCaseValidationError",
    "ExecutionMode",
    "FixtureMatchMode",
    "Expectation",
    "LLMResponseFixture",
    "LLMFixture",
    "ToolFixture",
    "ContextFixture",
    "ApprovalFixture",
    "DelegationFixture",
    "ScriptedLLMPort",
    "FixtureToolPort",
    "FixtureDelegationPort",
    "FixtureRunPolicyPort",
    "FixtureContextPort",
    "FixtureApprovalRepository",
    "FixtureConfigurationError",
    "EvalDependencies",
    "EvalEnvironment",
    "EvalRunOutcome",
    "InMemoryCheckpointRepository",
    "AssertionResult",
    "EvalResult",
    "EvaluationFailureKind",
    "EvalRunner",
    "ExpectationKind",
    "Scorer",
    "SCORERS",
    "ALL_SCORERS",
    "EVAL_SCHEMA_VERSION",
    "PlaybackRunner",
    "PlaybackBatch",
    "ReexecutionRunner",
    "RegressionGate",
    "RegressionReport",
    "RegressionCaseResult",
]
