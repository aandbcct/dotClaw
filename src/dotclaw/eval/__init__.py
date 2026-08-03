"""EvalCase 与隔离 Fixture Environment。

本包定义版本化 ``EvalCase``、默认拒绝真实依赖的 Fixture 端口，以及把二者
装配为隔离 ``RuntimeEngine`` 的 ``EvalEnvironment``。PR3 不评分。
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
from .models import (
    EvalCase,
    EvalCaseValidationError,
    ExecutionMode,
    Expectation,
    FixtureMatchMode,
)

__all__ = [
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
]
