"""工具执行调度器（Tool v1 阶段三重构 — 固定安全链路）。

固定执行顺序（总体设计 §4.5 / §8.1）：
    原始参数 → 入参验证 → Capability Broker → Policy Engine → 审批 → Handler → Journal

职责边界（总体设计 §6）：
- 只编排链路、超时与统一结果；不解析具体资源，也不直接做交互。
- 参数校验失败绝不进入 Broker / Policy / Handler。
- Policy 返回 deny 直接拒绝；返回 ask 时经 Approval Port 询问用户，无 Channel 即拒绝。
- 所有审计事件只写入脱敏后的策略/审批摘要，不含密钥、认证头或原始敏感值。

两个公开入口：
- execute()：走完整链路（含 channel 审批），供带交互通道的调用方使用。
- execute_approved()：走完整链路，但 ask 视为已批准（供 Runtime v2 适配器两阶段
  审批后复用，避免在已批准时重复询问）。

所有新增注释使用中文。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Callable

from .base import (
    ToolErrorCode,
    ToolErrorType,
    ToolExecutionContext,
    ToolResult,
)
from .capability import CapabilityBroker, CapabilityRequest, ResourceKind
from .decorator import ToolPolicy
from .handler import ToolHandler
from .policy import PolicyDecision, PolicyEngine, PolicyScope, default_policy_scope
from .registry import ToolRegistry
from .schema import ToolValidationError, validate_args, validate_json_schema
from .approval import ApprovalManager
from dotclaw.journal import Journal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import SkillParser

logger = logging.getLogger("dotclaw.tools.executor")


class ToolExecutor:
    """工具执行调度器 — 固定安全链路编排。"""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_manager: ApprovalManager | None = None,
        policy_engine: PolicyEngine | None = None,
        capability_broker: CapabilityBroker | None = None,
        skill_parser: "SkillParser | None" = None,
        approval_commands: set[str] | None = None,
        agent_policy_resolver: "Callable[[str], dict[str, str] | None] | None" = None,
    ):
        self._registry = registry
        self._approval = approval_manager or ApprovalManager()
        self._policy_scope = (
            policy_engine.scope
            if policy_engine is not None and policy_engine.scope is not None
            else default_policy_scope()
        )
        self._policy_engine = policy_engine or PolicyEngine(self._policy_scope)
        self._broker = capability_broker or CapabilityBroker()
        self._skill_parser = skill_parser
        # 配置级审批命令列表（新规范名）。与工具声明式 needs_approval 合并参与决策，
        # 解决"approval_commands 死配置"问题（开发计划阶段五审计）。
        self._approval_commands = set(approval_commands or [])
        # Agent 级策略解析器：按 agent_id 解析其 policy_rules，供每次调用冻结
        # 独立的策略作用域（P1 修复：Agent 级策略不再保存在全局 Executor，避免
        # delegation 子 Agent 继承主 Agent 规则或主 Agent 规则污染所有 Agent）。
        self._agent_policy_resolver = agent_policy_resolver

    @property
    def registry(self) -> ToolRegistry:
        """工具注册表（供工厂/MCP 等需要直接操作注册表的场景）。"""
        return self._registry

    @property
    def policy_engine(self):
        """策略引擎（供工厂装配 MCP Provider 的连接网关复用）。"""
        return self._policy_engine

    @property
    def capability_broker(self):
        """能力 Broker（供工厂装配 MCP Provider 复用）。"""
        return self._broker

    def get_definitions(self) -> list:
        """返回所有工具定义（转发给 Registry）。"""
        return self._registry.get_definitions()

    def snapshot_definitions(self) -> tuple:
        """返回当前可用工具定义的不可变快照（Run 级隔离）。

        Registry.snapshot() 已对每个定义做深拷贝，因此本次返回的元组不受后续
        注册表增删影响（总体设计 §9 / 开发计划阶段四）。一次 Run 在创建时调用本
        方法捕获固定工具集，Run 内不再读取动态 Registry。
        """
        return self._registry.snapshot()

    def get_handler(self, name: str) -> ToolHandler | None:
        """按名称获取 Handler（转发给 Registry）。"""
        return self._registry.get(name)

    def requires_approval(
        self, name: str, execution_context: ToolExecutionContext | None = None
    ) -> bool:
        """查询工具是否可能触发交互审批（不访问 Channel、不执行工具）。

        由声明式 needs_approval 或配置 approval_commands，或档案有效决策为 ask 推导；
        供适配器做粗粒度预判。Agent 级收窄通过 execution_context.agent_id 生效，与
        _run_chain 共用 _effective_scope，避免 Adapter 预检遗漏受限 Agent 收窄后的 ask
        策略（修复：预检只看全局规则导致收窄 Agent 的 ask 被直接 execute_approved 绕过）。
        """
        handler = self._registry.get(name)
        if handler is None:
            return False
        definition = handler.definition()
        if definition.needs_approval or definition.name in self._approval_commands:
            return True
        profile = definition.policy_profile
        if profile is not None:
            try:
                ToolPolicy(profile)
            except ValueError:
                return False
            scope = self._effective_scope(execution_context)
            global_decision = scope.global_rules.get(profile, PolicyDecision.ASK)
            agent_decision = scope.agent_rules.get(profile, global_decision)
            # Agent 只能收窄：取更严格者（severity 大者为最终决策）。
            effective = (
                global_decision
                if global_decision.severity >= agent_decision.severity
                else agent_decision
            )
            if effective is PolicyDecision.ASK:
                return True
        return False

    async def execute_approved(
        self,
        name: str,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext | None = None,
        journal: Journal | None = None,
    ) -> ToolResult:
        """执行已获结构化审批的调用：走完整链路，但 ask 视为已批准。"""
        return await self._run_chain(
            name, arguments, execution_context, journal, channel=None, pre_approved=True
        )

    def _check_skill(self, tool_name: str, args: dict, journal: Any, status: str) -> None:
        """工具执行后检查是否命中 skill，命中则发射对应 journal 事件。"""
        if not self._skill_parser:
            return
        result = self._skill_parser.parse(tool_name, args)
        if result is None:
            return
        skill_name, part, osname = result
        if part == "body":
            journal.skill_body_loaded(skill_name, status=status)
        elif part == "reference":
            journal.skill_reference_load(skill_name, osname, status=status)
        elif part == "script":
            journal.skill_script_exec(skill_name, osname, status=status)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        channel: Any | None = None,
        journal: Journal | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """执行工具：完整安全链路（含 channel 审批）。"""
        return await self._run_chain(
            name, arguments, execution_context, journal, channel=channel, pre_approved=False
        )

    async def _run_chain(
        self,
        name: str,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext | None,
        journal: Journal | None,
        channel: Any | None,
        pre_approved: bool,
    ) -> ToolResult:
        """固定安全链路实现。

        顺序：tool_start → 校验 → Broker → Policy → (ask 审批) → Handler → tool_end。
        """
        if journal:
            # 不写入原始参数（设计 §7.2）；脱敏摘要经 tool_policy_resolved 记录。
            journal.tool_start(name)

        handler = self._registry.get(name)
        if handler is None:
            return self._finish(self._missing_tool_result(name), name, journal, handler=None)

        definition = handler.definition()

        # ① 参数校验（失败不进入 Broker / Policy / Handler）。
        # 本地工具走 Pydantic（args_model）；MCP 等外部工具走 JSON Schema（input_schema）。
        # 校验失败在调用 tools/call 之前返回 INVALID_ARGUMENTS（开发计划阶段四）。
        model = handler.args_model
        schema = handler.input_schema
        if model is not None and not isinstance(arguments, model):
            try:
                validated = validate_args(model, arguments)
            except ToolValidationError as exc:
                result = ToolResult.from_error(
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                    message=str(exc),
                    error_type=ToolErrorType.VALIDATION,
                )
                return self._finish(result, name, journal, handler)
        elif schema:
            try:
                validated = validate_json_schema(arguments, schema)
            except ToolValidationError as exc:
                result = ToolResult.from_error(
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                    message=str(exc),
                    error_type=ToolErrorType.VALIDATION,
                )
                return self._finish(result, name, journal, handler)
        else:
            validated = arguments

        # ② Capability Broker：翻译资源请求。
        requests = self._broker.resolve(
            definition, validated, self._policy_scope.workspace_root
        )
        summary = _summarize_requests(requests)

        # ③ Policy Engine：计算决策（按当前 Run 的 Agent 冻结作用域，P1 修复）。
        scope = self._effective_scope(execution_context)
        outcome = self._policy_engine.evaluate(requests, scope)
        if outcome.decision is PolicyDecision.DENY:
            result = ToolResult.from_error(
                code=ToolErrorCode.POLICY_DENIED,
                message=f"策略拒绝：{outcome.reason}",
                error_type=ToolErrorType.POLICY,
            )
            if journal:
                journal.tool_policy_resolved(name, "deny", outcome.matched_rule, summary)
            return self._finish(result, name, journal, handler)

        # ④ 审批：ask 或声明式 needs_approval（且未预先批准）时，经 Approval Port 询问。
        # needs_approval 用于没有资源档案、仍需显式审批的工具（如配置 approval_commands
        # 指定的工具）；无交互通道时一律拒绝（设计不变量 §10.1.3）。
        needs_explicit_approval = (
            definition.needs_approval or definition.name in self._approval_commands
        ) and not pre_approved
        if (outcome.decision is PolicyDecision.ASK or needs_explicit_approval) and not pre_approved:
            approved = await self._approval.request(summary, channel)
            if journal:
                journal.tool_approval_outcome(
                    name, "approved" if approved else "denied", summary
                )
            if not approved:
                result = ToolResult.from_error(
                    code=ToolErrorCode.APPROVAL_DENIED,
                    message="需要审批但未获批准或无交互通道",
                    error_type=ToolErrorType.APPROVAL,
                )
                return self._finish(result, name, journal, handler)

        # ⑤ Handler 执行（超时控制）。
        # ⑤½ 路径回填：将 Broker 校验过的绝对路径写回参数，确保 handler 实际操作目标
        # 与策略检查目标完全一致（P0 修复：自定义 workspace_root 时，若 handler 自行用
        # CWD 解析相对路径，安全边界会在 Broker 批准但实际落到错误位置时失效）。
        validated = _apply_resolved_paths(validated, requests)
        try:
            result = await self._execute_handler(name, validated, handler, execution_context)
        except Exception:  # 超时/调度异常已在 _execute_handler 内归一化
            result = ToolResult.from_error(
                code=ToolErrorCode.EXECUTOR_ERROR,
                message="工具调度异常",
                error_type=ToolErrorType.EXECUTOR,
            )
        if journal and not result.is_error:
            journal.tool_policy_resolved(name, "allow", outcome.matched_rule, summary)
        return self._finish(result, name, journal, handler)

    async def _execute_handler(
        self,
        name: str,
        validated: Any,
        handler: ToolHandler,
        execution_context: ToolExecutionContext | None,
    ) -> ToolResult:
        """执行已通过策略/审批的工具，含超时控制与异常归一化。"""
        definition = handler.definition()
        timeout: float = definition.timeout
        ctx = self._build_context(timeout, execution_context)

        try:
            result = await asyncio.wait_for(handler.execute(validated, ctx), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning("工具 %s 执行超时（%ss）", name, timeout)
            return ToolResult.from_error(
                code=ToolErrorCode.TIMEOUT,
                message=f"错误：工具执行超时（{int(timeout)}秒）",
                error_type=ToolErrorType.TIMEOUT,
            )
        except Exception as exc:  # 业务异常统一转为 EXECUTION_ERROR
            logger.exception("工具 %s 调度出错", name)
            return ToolResult.from_error(
                code=ToolErrorCode.EXECUTION_ERROR,
                message=f"错误：工具调度异常 - {exc}",
                error_type=ToolErrorType.EXECUTOR,
            )

    def _effective_scope(
        self, execution_context: ToolExecutionContext | None
    ) -> "PolicyScope":
        """按当前 Run 的 Agent 冻结策略作用域（P1 修复）。

        Agent 级策略不再保存在全局 Executor，而是每次调用依据
        execution_context.agent_id 解析该 Agent 的 policy_rules 并构造独立作用域，
        避免 delegation 子 Agent 继承主 Agent 规则、或主 Agent 规则污染所有 Agent。
        无解析器或 agent_id 时回退全局作用域（兼容测试/直接调用）。
        """
        if self._agent_policy_resolver and execution_context is not None and execution_context.agent_id:
            try:
                rules = self._agent_policy_resolver(execution_context.agent_id)
            except Exception:  # 解析失败不阻断执行，回退全局作用域
                logger.warning("解析 Agent 策略失败: %s", execution_context.agent_id)
                rules = None
            agent_rules: dict[str, PolicyDecision] = {}
            if rules:
                for profile, decision in rules.items():
                    try:
                        agent_rules[profile] = PolicyDecision(decision)
                    except ValueError:
                        logger.warning("忽略非法 Agent 策略规则: %s=%s", profile, decision)
            # 无论该 Agent 是否有规则，都返回独立 scope：有规则用其收窄规则，
            # 无规则则 agent_rules 为空（仅全局上限生效），绝不回退已被污染的共享
            # scope，避免 delegation 子 Agent 继承主 Agent 的收窄规则（四次审计修复）。
            return dataclasses.replace(self._policy_scope, agent_rules=agent_rules)
        return self._policy_scope

    def _build_context(
        self,
        timeout: float,
        execution_context: ToolExecutionContext | None,
    ) -> ToolExecutionContext:
        """合并 Runtime 注入的上下文与工具定义超时。"""
        if execution_context is not None:
            return ToolExecutionContext(
                timeout=timeout,
                agentrun_id=execution_context.agentrun_id,
                agent_id=execution_context.agent_id,
            )
        return ToolExecutionContext(timeout=timeout)

    def _finish(
        self,
        result: ToolResult,
        name: str,
        journal: Journal | None,
        handler: ToolHandler | None,
    ) -> ToolResult:
        """统一收尾：发射 tool_end 与 skill 命中事件。"""
        if journal:
            status = "error" if result.is_error else "success"
            journal.tool_end(
                name,
                result_len=len(result.output),
                status=status,
                error_type=result.error_type if result.is_error else "",
            )
            if not result.is_error and handler is not None:
                self._check_skill(name, {}, journal, status)
        return result

    def _missing_tool_result(self, name: str) -> ToolResult:
        """构造未注册工具的统一错误结果。"""
        return ToolResult.from_error(
            code=ToolErrorCode.TOOL_NOT_FOUND,
            message=f"错误：未找到工具 '{name}'",
            error_type=ToolErrorType.NOT_FOUND,
        )


def _apply_resolved_paths(validated: Any, requests: list[CapabilityRequest]) -> Any:
    """将 Broker 校验过的绝对路径回填到对应参数，使 handler 实际操作目标与策略检查一致。

    P0 修复核心：Broker 检查的是相对 workspace_root 解析后的路径，而文件/memory handler
    原本用 CWD 解析相对路径；回填绝对路径后，handler 落点与策略检查目标严格一致。
    仅对 FILE_READ / FILE_WRITE 请求、且携带已解析 absolute_path 与 param_field 时生效。
    """
    for req in requests:
        if (
            req.kind in (ResourceKind.FILE_READ, ResourceKind.FILE_WRITE)
            and req.param_field
            and req.absolute_path
        ):
            field = req.param_field
            if hasattr(validated, "model_copy"):
                validated = validated.model_copy(update={field: req.absolute_path})
            elif isinstance(validated, dict):
                validated[field] = req.absolute_path
    return validated


def _summarize_requests(requests: list[CapabilityRequest]) -> str:
    """汇总资源请求为脱敏摘要（供审计与审批提示）。"""
    if not requests:
        return ""
    return "; ".join(req.describe() for req in requests)
