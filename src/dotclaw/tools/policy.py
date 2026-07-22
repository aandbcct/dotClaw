"""策略引擎（Tool v1 阶段三新增）。

Policy Engine 对 Capability Broker 翻译出的资源请求计算 allow / ask / deny 决策，
并携带简单约束（workspace 根目录、denied_paths、允许的 MCP server）。它不做任何
外部副作用，也不接触用户交互（交互由 Approval Port 负责）。

核心不变量（总体设计 §4.3 / §10.1）：
- 默认拒绝：决策必须由规则显式给出，无规则命中时取保守的 ask。
- 全局策略是安全上限，Agent 级策略只能收窄（取更严格者），不得放宽。
- 审计摘要不得包含密钥、认证头或原始敏感值。

依赖方向：只依赖 capability（CapabilityRequest / ResourceKind）。所有新增注释使用中文。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .capability import CapabilityRequest, ResourceKind


class PolicyDecision(str, Enum):
    """策略决策：允许 / 需审批 / 拒绝。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

    @property
    def severity(self) -> int:
        """严格程度：ALLOW(0) < ASK(1) < DENY(2)。数值越大越严格。"""
        return {PolicyDecision.ALLOW: 0, PolicyDecision.ASK: 1, PolicyDecision.DENY: 2}[self]


@dataclass
class PolicyScope:
    """一次策略评估的作用域：全局上限 + Agent 收窄 + 资源约束。

    Agent 级规则只能收窄全局允许范围（取更严格决策），不得放宽。
    """

    global_rules: dict[str, PolicyDecision]
    agent_rules: dict[str, PolicyDecision] = field(default_factory=dict)
    workspace_root: str = "."
    denied_paths: list[str] = field(default_factory=list)
    allowed_mcp_servers: list[str] = field(default_factory=list)


@dataclass
class PolicyOutcome:
    """单次评估的最终决策与匹配原因。

    matched_rule 仅记录档案名/约束名，reason 为安全描述，均不含用户输入值。
    """

    decision: PolicyDecision
    matched_rule: str
    reason: str = ""


# 默认全局规则（开发计划阶段三·配置与默认值，与总体设计 §7.1 一致）。
_DEFAULT_RULES: dict[str, PolicyDecision] = {
    "workspace.read": PolicyDecision.ALLOW,
    "workspace.write": PolicyDecision.ASK,
    "process.exec": PolicyDecision.ASK,
    "network.http": PolicyDecision.DENY,
    "mcp.connect": PolicyDecision.ASK,
    "mcp.call": PolicyDecision.ASK,
}


def default_policy_scope(workspace_root: str = ".") -> PolicyScope:
    """构造默认策略作用域（设计确认的初始规则与安全约束）。"""
    return PolicyScope(
        global_rules=dict(_DEFAULT_RULES),
        agent_rules={},
        workspace_root=workspace_root,
        denied_paths=[".env", ".git/**", "**/*.key"],
        allowed_mcp_servers=["github"],
    )


class PolicyEngine:
    """对一组资源请求计算统一的 allow / ask / deny 决策。"""

    def __init__(self, scope: PolicyScope | None = None) -> None:
        # 作用域可在构造时绑定；evaluate 也可传入临时 scope 覆盖。
        self.scope = scope

    def evaluate(
        self,
        requests: list[CapabilityRequest],
        scope: PolicyScope | None = None,
    ) -> PolicyOutcome:
        """评估资源请求列表，返回合并后的决策。

        合并规则（总体设计 §4.3 / §10.1）：
        - 任一请求 DENY → 整体 DENY（首个 deny 即返回）。
        - 否则任一请求 ASK → 整体 ASK。
        - 否则整体 ALLOW。

        无请求（passthrough 工具）视为 ALLOW。
        """
        scope = scope or self.scope
        if scope is None:
            scope = default_policy_scope()
        if not requests:
            return PolicyOutcome(PolicyDecision.ALLOW, "none", "")

        for request in requests:
            outcome = self._evaluate_one(request, scope)
            if outcome.decision is PolicyDecision.DENY:
                return outcome

        outcomes = [self._evaluate_one(r, scope) for r in requests]
        for outcome in outcomes:
            if outcome.decision is PolicyDecision.ASK:
                return outcome
        return PolicyOutcome(PolicyDecision.ALLOW, "all-allow", "")

    def _evaluate_one(self, request: CapabilityRequest, scope: PolicyScope) -> PolicyOutcome:
        """评估单个资源请求，依次应用档案规则与资源约束。"""
        global_decision = scope.global_rules.get(request.profile, PolicyDecision.ASK)
        agent_decision = scope.agent_rules.get(request.profile, global_decision)
        # Agent 只能收窄：取更严格者（severity 大者为最终决策）。
        effective = (
            global_decision
            if global_decision.severity >= agent_decision.severity
            else agent_decision
        )

        if effective is PolicyDecision.DENY:
            return PolicyOutcome(PolicyDecision.DENY, request.profile, "全局或 Agent 策略拒绝")

        # 资源约束（优先级高于 ask/allow 的档案决策）。
        if request.kind in (ResourceKind.FILE_READ, ResourceKind.FILE_WRITE):
            if request.escaped:
                return PolicyOutcome(PolicyDecision.DENY, request.profile, "路径逃逸 workspace 根目录")
            if request.normalized_path and _match_denied_paths(
                scope.denied_paths, request.normalized_path
            ):
                return PolicyOutcome(PolicyDecision.DENY, request.profile, "命中拒绝路径")

        if request.kind is ResourceKind.MCP_CALL:
            # 默认拒绝：server 不在允许列表即拒绝（空列表亦视为 deny-all，fail-closed）。
            if request.server not in scope.allowed_mcp_servers:
                return PolicyOutcome(PolicyDecision.DENY, request.profile, "MCP server 不在允许列表")

        if request.kind is ResourceKind.MCP_CONNECT:
            # server 连接授权：同样受允许列表约束（开发计划阶段四）。
            # 不在允许列表的 server 直接拒绝连接（Provider 据此降级，不阻塞 Agent）。
            if request.server not in scope.allowed_mcp_servers:
                return PolicyOutcome(PolicyDecision.DENY, request.profile, "MCP server 不在允许列表")

        return PolicyOutcome(effective, request.profile, "")


def _match_denied_paths(patterns: list[str], path: str) -> bool:
    """判断规范化相对路径是否命中 denied_paths 中的任意 glob 模式。

    支持 `**` 递归（转为 fnmatch 的 `*`）与按文件名兜底匹配，覆盖 `**/*.key`
    这类任意层级的拒绝规则。路径统一为 `/` 分隔。
    """
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    for pattern in patterns:
        pat = pattern.replace("\\", "/")
        # 直接匹配（** 当作跨段通配）。
        if fnmatch.fnmatch(norm, pat.replace("**/", "*").replace("**", "*")):
            return True
        # 含 ** 时，额外按文件名匹配（覆盖根级 key 文件）。
        if "**" in pat:
            tail = pat.split("**/", 1)[-1]
            if fnmatch.fnmatch(base, tail):
                return True
    return False
