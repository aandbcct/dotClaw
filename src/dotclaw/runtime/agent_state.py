"""AgentState —— Agent 运行时状态机。

AgentState 是单次 AgentRun 的执行状态机，驱动 ReAct 循环。
采用事件驱动模式：Runtime 喂入事件 → 状态机转换 phase → 返回下一步动作（AgentAction）。

v4 变更（TransitionTable 重构）：
- 路由方式从 procedural handler (isinstance 链式分发) 改为声明式 TRANSITION_TABLE
- 所有 (phase, event) → (next_phase, action) 转换集中在一处定义
- 瞬态 Phase 的自动推进由内部 _auto_chain() 处理，外部行为不变
- 新增/修改状态转换只需在 TRANSITION_TABLE 中加一行

架构层级：
    AgentState（任务级状态机，单次 AgentRun 生命周期）
      └── tasks: list[Task]（Agent 内部问题拆解的子任务清单）

AgentState 不持有 LLM/Tool 等执行能力——它只决策"下一步该做什么"，
实际执行由 Runtime 根据返回的 AgentAction 完成。

TRANSITION_TABLE 结构：
    每条规则 = (current_phase, event_type, guard, side_effect, next_phase, action, priority)
    - guard: (state, event) → bool，返回 False 跳过此规则
    - side_effect: (state, event) → None，修改 state 的副作用
    - priority: 越小越优先匹配（默认 0）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..agent.agent import LLMResponse
from ..llm.base import Message
from .task import Task


# ============================================================================
# 枚举定义
# ============================================================================

class AgentPhase(Enum):
    """AgentState 的执行阶段。

    状态机沿此枚举进行转换：
        IDLE → THINKING → ACTING → THINKING → ... → RESPONDING → DONE
                         ↘ RESPONDING → DONE
                         ↘ TRUNCATED → RESPONDING → DONE（v1）/ THINKING（v2 续跑）
                         ↘ HANDOFF → DONE
                         ↘ FAILED
                         ↘ WAITING_APPROVAL → THINKING / RESPONDING（审批完成）
                         ↘ RETRYING → THINKING（自动重试）/ RESPONDING（重试耗尽）
    [预留] PLANNING / REFLECTION / AGGREGATING
    """

    IDLE = "idle"
    """初始状态，等待启动事件"""

    THINKING = "thinking"
    """等待 LLM 调用。Runtime 应执行 INVOKE_LLM 动作"""

    ACTING = "acting"
    """等待工具执行。Runtime 应执行 EXECUTE_TOOLS 动作"""

    RESPONDING = "responding"
    """生成最终回复。Runtime 应执行 FINALIZE 动作"""

    TRUNCATED = "truncated"
    """LLM 输出被截断（finish_reason="length"）。"""

    HANDOFF = "handoff"
    """任务流转给其他 Agent。Runtime 应执行 HANDOFF 动作"""

    DONE = "done"
    """终止状态：正常完成"""

    FAILED = "failed"
    """终止状态：执行异常"""

    WAITING_APPROVAL = "waiting_approval"
    """等待人工审批。Runtime 应持久化状态并返回 WAIT_SENTINEL"""

    RETRYING = "retrying"
    """工具失败重试中。Runtime 自动推送 ContinueEvent"""

    # ── 预留 ──
    # PLANNING = "planning"          # [预留] 复杂任务拆解规划
    # REFLECTION = "reflection"      # [预留] 回复前自检
    # AGGREGATING = "aggregating"    # [预留] 多子任务结果聚合


class AgentAction(Enum):
    """AgentState 返回给 Runtime 的执行指令。

    每个 action 对应 Runtime 需要执行的具体操作。
    """

    INVOKE_LLM = "invoke_llm"
    """调用 LLM 获取下一轮响应"""

    EXECUTE_TOOLS = "execute_tools"
    """执行 LLM 返回的工具调用"""

    FINALIZE = "finalize"
    """结束当前 AgentRun，构建结果返回"""

    HANDOFF_TARGET = "handoff_target"
    """执行 handoff：停止当前 AgentRun 并将控制权转交目标 Agent"""

    WAIT = "wait"
    """等待状态，无操作（用于跨状态自动转换的中间步骤）"""


class AgentStatus(Enum):
    """AgentRun 的最终结束状态。

    最终写入 AgentRun.end_status。
    """

    RUNNING = "running"
    """执行中"""

    COMPLETED = "completed"
    """正常完成，LLM 返回了最终回复"""

    HANDOFF = "handoff"
    """任务流转给其他 Agent"""

    FAILED = "failed"
    """执行失败"""


# 终止状态的集合
_TERMINAL_PHASES: frozenset[AgentPhase] = frozenset({
    AgentPhase.DONE,
    AgentPhase.FAILED,
})


# ============================================================================
# 事件定义
# ============================================================================

@dataclass
class AgentStartEvent:
    """Agent 启动事件。

    Runtime 在创建 AgentState 后发送此事件以启动状态机。
    """

    user_message: str
    """用户输入文本"""


@dataclass
class LLMResponseEvent:
    """LLM 返回响应事件。

    Runtime 调用 LLM 后发送此事件，携带 LLM 返回的完整结果。
    """

    response: LLMResponse
    """LLM 的完整返回（content + tool_calls + finish_reason + tokens）"""


@dataclass
class ToolsDoneEvent:
    """工具执行完成事件。

    Runtime 执行完所有工具调用后发送此事件。

    字段：
        results: 工具执行结果消息列表（role="tool"）
        stop_signal: 工具输出中包含停止信号（如 task_complete tool）
        handoff_signal: 工具输出中包含 handoff 信号
        tool_error: 工具执行过程中发生错误
        retryable: 错误是否可重试（Phase 4）
        needs_approval: 是否需要人工审批（Phase 4）
    """

    results: list[Message]
    """工具执行结果列表。每个都是 role="tool" 的 Message"""

    stop_signal: bool = False
    """工具结果中是否包含停止信号"""

    handoff_signal: bool = False
    """工具结果中是否包含 handoff 信号"""

    tool_error: bool = False
    """工具执行过程中是否发生错误"""

    retryable: bool = False
    """工具错误是否可重试（Phase 4: RETRYING 流）"""

    needs_approval: bool = False
    """工具执行是否需要人工审批（Phase 4: WAITING_APPROVAL 流）"""


@dataclass
class ApprovalDoneEvent:
    """人工审批完成事件（Phase 4）。

    Runtime 收到审批结果后通过 resume_state 调用 run()，状态机
    接收此事件以决定下一步（批准 → 继续 / 拒绝 → 结束）。
    """

    approved: bool
    """审批是否通过"""

    feedback: str = ""
    """审批反馈信息（拒绝时可含拒绝原因）"""


@dataclass
class ContinueEvent:
    """内部自动推进事件。

    由 Runtime._step() 或 AgentState._auto_chain() 自动推送，
    用于驱动瞬态 Phase（TRUNCATED / RESPONDING / HANDOFF）显式转换到 DONE。
    """


# 事件联合类型
AgentEvent = AgentStartEvent | LLMResponseEvent | ToolsDoneEvent | ContinueEvent | ApprovalDoneEvent


# ============================================================================
# TransitionTable 数据结构
# ============================================================================

# 类型别名
GuardFn = Callable[["AgentState", object], bool]
SideEffectFn = Callable[["AgentState", object], None]


@dataclass
class TransitionRule:
    """一条状态转换规则。

    current_phase + event_type 联合匹配，guard 返回 True 时触发。
    将修改/新增状态转换集中到 TRANSITION_TABLE 一处。
    """

    current: AgentPhase
    """当前 Phase"""

    event_type: type
    """触发事件类型"""

    guard: GuardFn | None
    """护卫条件 (state, event) → bool。返回 True 表示此规则匹配"""

    side_effect: SideEffectFn | None
    """副作用 (state, event) → None。如保存 LLM 响应、更新计数器"""

    next_phase: AgentPhase
    """目标 Phase"""

    next_action: AgentAction
    """返回给 Runtime 的动作"""

    priority: int = 0
    """优先级（越小越先匹配），默认 0"""


# ============================================================================
# Guard 函数（护卫条件）
# ============================================================================

def _guard_llm_error(state: AgentState, event: object) -> bool:
    """LLM 返回 finish_reason="error"。"""
    evt = event
    return (
        isinstance(evt, LLMResponseEvent)
        and evt.response.finish_reason == "error"
    )


def _guard_truncated(state: AgentState, event: object) -> bool:
    """LLM 返回 finish_reason="length"（输出被截断）。"""
    evt = event
    return (
        isinstance(evt, LLMResponseEvent)
        and evt.response.finish_reason == "length"
    )


def _guard_has_tool_calls(state: AgentState, event: object) -> bool:
    """LLM 返回了工具调用。"""
    evt = event
    return (
        isinstance(evt, LLMResponseEvent)
        and bool(evt.response.tool_calls)
    )


def _guard_tool_error(state: AgentState, event: object) -> bool:
    """工具执行出错。"""
    evt = event
    return isinstance(evt, ToolsDoneEvent) and evt.tool_error


def _guard_handoff(state: AgentState, event: object) -> bool:
    """工具返回 handoff 信号。"""
    evt = event
    return isinstance(evt, ToolsDoneEvent) and evt.handoff_signal


def _guard_stop(state: AgentState, event: object) -> bool:
    """工具返回 stop 信号。"""
    evt = event
    return isinstance(evt, ToolsDoneEvent) and evt.stop_signal


def _guard_truncated_continue(state: AgentState, event: object) -> bool:
    """TRUNCATED 续跑：未达上限时可续跑（v2 行为）。

    Runtime 可在注入 continue 提示时设置 _truncated_continue_allowed 标记，
    如果 Runtime 未允许续跑则回退到 v1 直接结束（兼容旧调用方）。
    """
    allow: bool = getattr(state, "_truncated_continue_allowed", False)
    if not allow:
        return False
    return state.truncated_count < state._MAX_TRUNCATED_CONTINUE


def _guard_safety(state: AgentState, event: object) -> bool:
    """安全阀触发：达到 max_iterations 或检测到工具死循环。

    注：工具调用历史已在 _se_llm_response 中记录，此处只做检测。
    """
    if state.iteration >= state.max_iterations:
        state.error_message = f"超出最大迭代次数 ({state.max_iterations})"
        return True
    if state._detect_tool_loop():
        state.error_message = "检测到工具死循环"
        return True
    return False


def _guard_retryable_error(state: AgentState, event: object) -> bool:
    """工具执行出错且可重试。"""
    evt = event
    return (
        isinstance(evt, ToolsDoneEvent)
        and evt.tool_error
        and evt.retryable
    )


def _guard_needs_approval(state: AgentState, event: object) -> bool:
    """工具执行需要人工审批。"""
    evt = event
    return isinstance(evt, ToolsDoneEvent) and evt.needs_approval


def _guard_can_retry(state: AgentState, event: object) -> bool:
    """RETRYING：重试次数未达上限。"""
    retry_count: int = getattr(state, "retry_count", 0)
    max_retries: int = getattr(state, "max_tool_retries", 2)
    return retry_count < max_retries


def _guard_approved(state: AgentState, event: object) -> bool:
    """审批通过。"""
    evt = event
    return isinstance(evt, ApprovalDoneEvent) and evt.approved


# ============================================================================
# Side Effect 函数（副作用）
# ============================================================================

def _se_start(state: AgentState, event: object) -> None:
    """启动：设置 iteration=1。"""
    state.iteration = 1


def _se_llm_error(state: AgentState, event: object) -> None:
    """LLM 错误：标记 FAILED。"""
    state.end_status = AgentStatus.FAILED
    state.error_message = "LLM 调用返回 error"


def _se_llm_response(state: AgentState, event: object) -> None:
    """保存 LLM 响应 + 记录工具调用历史。"""
    evt = event
    if isinstance(evt, LLMResponseEvent):
        state.current_llm_response = evt.response
        state._record_tool_calls()


def _se_truncated(state: AgentState, event: object) -> None:
    """TRUNCATED：保存 LLM 响应（v2: 不设置 end_status，由续跑逻辑处理）。"""
    _se_llm_response(state, event)


def _se_truncated_continue(state: AgentState, event: object) -> None:
    """TRUNCATED 续跑：递增加计数器。"""
    state.truncated_count += 1


def _se_llm_final(state: AgentState, event: object) -> None:
    """LLM 最终回复（无工具调用）：保存响应 + 标记完成。"""
    _se_llm_response(state, event)
    state.end_status = AgentStatus.COMPLETED


def _se_tool_error(state: AgentState, event: object) -> None:
    """工具错误：更新结果 + 标记 FAILED。"""
    _se_save_tool_results(state, event)
    state.end_status = AgentStatus.FAILED
    state.error_message = "工具执行错误"


def _se_handoff(state: AgentState, event: object) -> None:
    """Handoff：更新结果 + 标记 + 提取信息。"""
    _se_save_tool_results(state, event)
    state.end_status = AgentStatus.HANDOFF
    _se_extract_handoff(state, event)


def _se_tools_final(state: AgentState, event: object) -> None:
    """工具完成（stop 信号或安全阀）：保存结果 + 标记完成。"""
    _se_save_tool_results(state, event)
    state.end_status = AgentStatus.COMPLETED


def _se_next_iteration(state: AgentState, event: object) -> None:
    """正常继续下一轮：保存结果 + iteration++。"""
    _se_save_tool_results(state, event)
    state.iteration += 1


def _se_save_tool_results(state: AgentState, event: object) -> None:
    """保存工具结果（不记录工具调用—已在 _se_llm_response 中记录）。"""
    evt = event
    if isinstance(evt, ToolsDoneEvent):
        state.current_tool_results = list(evt.results)


def _se_extract_handoff(state: AgentState, event: object) -> None:
    """从工具结果中提取 handoff 信息。"""
    evt = event
    if isinstance(evt, ToolsDoneEvent):
        for msg in evt.results:
            if msg.content:
                state.handoff_context = msg.content
                break


def _se_prepare_retry(state: AgentState, event: object) -> None:
    """RETRYING：保存工具结果 + 递增重试计数。"""
    _se_save_tool_results(state, event)
    # Initialize retry_count if not present
    if not hasattr(state, "retry_count"):
        object.__setattr__(state, "retry_count", 0)
    state.retry_count += 1  # type: ignore[attr-defined]


def _se_do_retry(state: AgentState, event: object) -> None:
    """执行重试：无需额外处理（RETRYING→THINKING）。"""


def _se_retry_exhausted(state: AgentState, event: object) -> None:
    """重试耗尽：标记 COMPLETED 结束。"""
    state.end_status = AgentStatus.COMPLETED


def _se_request_approval(state: AgentState, event: object) -> None:
    """请求审批：保存工具结果。"""
    _se_save_tool_results(state, event)


def _se_approval_restore(state: AgentState, event: object) -> None:
    """审批通过恢复：无额外处理（WAITING_APPROVAL→THINKING）。"""


def _se_approval_rejected(state: AgentState, event: object) -> None:
    """审批拒绝：标记 COMPLETED 结束。"""
    state.end_status = AgentStatus.COMPLETED
    evt = event
    if isinstance(evt, ApprovalDoneEvent) and evt.feedback:
        state.error_message = f"审批被拒绝: {evt.feedback}"


# ============================================================================
# TRANSITION_TABLE —— 所有状态转换的集中定义
# ============================================================================
# 格式: (current, event_type, guard, side_effect, next_phase, next_action, priority)
# 新增/修改状态转换只需在此表中添加/修改一行。
# 瞬态 Phase (TRUNCATED, RESPONDING, HANDOFF) 通过 ContinueEvent 自动推进到 DONE，
# 由 handle_event() 内部的 _auto_chain() 在一次调用中完成链式转换。

_TRANSITION_TABLE: list[TransitionRule] = [
    # ===== IDLE =====
    TransitionRule(
        AgentPhase.IDLE, AgentStartEvent,
        guard=None, side_effect=_se_start,
        next_phase=AgentPhase.THINKING, next_action=AgentAction.INVOKE_LLM,
    ),

    # ===== THINKING =====
    TransitionRule(
        AgentPhase.THINKING, LLMResponseEvent,
        guard=_guard_llm_error, side_effect=_se_llm_error,
        next_phase=AgentPhase.FAILED, next_action=AgentAction.FINALIZE,
        priority=10,
    ),
    TransitionRule(
        AgentPhase.THINKING, LLMResponseEvent,
        guard=_guard_truncated, side_effect=_se_truncated,
        next_phase=AgentPhase.TRUNCATED, next_action=AgentAction.WAIT,
        priority=20,
    ),
    TransitionRule(
        AgentPhase.THINKING, LLMResponseEvent,
        guard=_guard_has_tool_calls, side_effect=_se_llm_response,
        next_phase=AgentPhase.ACTING, next_action=AgentAction.EXECUTE_TOOLS,
        priority=30,
    ),
    TransitionRule(
        AgentPhase.THINKING, LLMResponseEvent,
        guard=None, side_effect=_se_llm_final,
        next_phase=AgentPhase.RESPONDING, next_action=AgentAction.FINALIZE,
        priority=40,
    ),

    # ===== ACTING =====
    # (priority 5) RETRYING: 工具失败但可重试
    TransitionRule(
        AgentPhase.ACTING, ToolsDoneEvent,
        guard=_guard_retryable_error, side_effect=_se_prepare_retry,
        next_phase=AgentPhase.RETRYING, next_action=AgentAction.WAIT,
        priority=5,
    ),
    # (priority 10) 工具错误 → FAILED
    TransitionRule(
        AgentPhase.ACTING, ToolsDoneEvent,
        guard=_guard_tool_error, side_effect=_se_tool_error,
        next_phase=AgentPhase.FAILED, next_action=AgentAction.FINALIZE,
        priority=10,
    ),
    # (priority 15) WAITING_APPROVAL: 工具需要人工审批
    TransitionRule(
        AgentPhase.ACTING, ToolsDoneEvent,
        guard=_guard_needs_approval, side_effect=_se_request_approval,
        next_phase=AgentPhase.WAITING_APPROVAL, next_action=AgentAction.WAIT,
        priority=15,
    ),
    TransitionRule(
        AgentPhase.ACTING, ToolsDoneEvent,
        guard=_guard_handoff, side_effect=_se_handoff,
        next_phase=AgentPhase.HANDOFF, next_action=AgentAction.HANDOFF_TARGET,
        priority=20,
    ),
    TransitionRule(
        AgentPhase.ACTING, ToolsDoneEvent,
        guard=_guard_stop, side_effect=_se_tools_final,
        next_phase=AgentPhase.RESPONDING, next_action=AgentAction.FINALIZE,
        priority=30,
    ),
    TransitionRule(
        AgentPhase.ACTING, ToolsDoneEvent,
        guard=_guard_safety, side_effect=_se_tools_final,
        next_phase=AgentPhase.RESPONDING, next_action=AgentAction.FINALIZE,
        priority=40,
    ),
    TransitionRule(
        AgentPhase.ACTING, ToolsDoneEvent,
        guard=None, side_effect=_se_next_iteration,
        next_phase=AgentPhase.THINKING, next_action=AgentAction.INVOKE_LLM,
        priority=50,
    ),

    # ===== 瞬态 Phase → DONE / 续跑（ContinueEvent 自动推进） =====
    # RETRYING: ContinueEvent 自动推送
    TransitionRule(
        AgentPhase.RETRYING, ContinueEvent,
        guard=_guard_can_retry, side_effect=_se_do_retry,
        next_phase=AgentPhase.THINKING, next_action=AgentAction.INVOKE_LLM,
        priority=10,
    ),
    TransitionRule(
        AgentPhase.RETRYING, ContinueEvent,
        guard=None, side_effect=_se_retry_exhausted,
        next_phase=AgentPhase.RESPONDING, next_action=AgentAction.FINALIZE,
        priority=20,
    ),
    # [v2] TRUNCATED → THINKING / RESPONDING（根据 truncated_continue 配置）
    TransitionRule(
        AgentPhase.TRUNCATED, ContinueEvent,
        guard=_guard_truncated_continue, side_effect=_se_truncated_continue,
        next_phase=AgentPhase.THINKING, next_action=AgentAction.INVOKE_LLM,
        priority=10,
    ),
    TransitionRule(
        AgentPhase.TRUNCATED, ContinueEvent,
        guard=None, side_effect=None,
        next_phase=AgentPhase.RESPONDING, next_action=AgentAction.FINALIZE,
        priority=20,
    ),
    TransitionRule(
        AgentPhase.RESPONDING, ContinueEvent,
        guard=None, side_effect=None,
        next_phase=AgentPhase.DONE, next_action=AgentAction.FINALIZE,
    ),
    TransitionRule(
        AgentPhase.HANDOFF, ContinueEvent,
        guard=None, side_effect=None,
        next_phase=AgentPhase.DONE, next_action=AgentAction.HANDOFF_TARGET,
    ),

    # WAITING_APPROVAL: ContinueEvent 自动恢复（run() 自动检测）
    TransitionRule(
        AgentPhase.WAITING_APPROVAL, ContinueEvent,
        guard=None, side_effect=None,
        next_phase=AgentPhase.THINKING, next_action=AgentAction.INVOKE_LLM,
    ),

    # WAITING_APPROVAL: ApprovalDoneEvent 显式恢复（外部审批系统）
    TransitionRule(
        AgentPhase.WAITING_APPROVAL, ApprovalDoneEvent,
        guard=_guard_approved, side_effect=_se_approval_restore,
        next_phase=AgentPhase.THINKING, next_action=AgentAction.INVOKE_LLM,
        priority=10,
    ),
    TransitionRule(
        AgentPhase.WAITING_APPROVAL, ApprovalDoneEvent,
        guard=None, side_effect=_se_approval_rejected,
        next_phase=AgentPhase.RESPONDING, next_action=AgentAction.FINALIZE,
        priority=20,
    ),

    # ── [预留] Phase 5：安全阀 ──
    # TransitionRule(THINKING, LLMResponseEvent,
    #     guard=_guard_death_spiral, side_effect=_se_death_spiral,
    #     next_phase=RESPONDING, next_action=FINALIZE, priority=1)
]


# ============================================================================
# AgentState — 状态机主体
# ============================================================================

@dataclass
class AgentState:
    """Agent 运行时状态机 —— 驱动单次 AgentRun 的 ReAct 循环。

    AgentState 是事件驱动的状态机：
    1. Runtime 调用 handle_event(event) 喂入事件
    2. 状态机通过 TRANSITION_TABLE 查找匹配规则 → 转换 phase → 返回 AgentAction
    3. 瞬态 Phase 由内部 _auto_chain() 自动推进（ContinueEvent 链）
    4. Runtime 根据 action 执行对应操作
    5. 操作结果作为新事件再次喂入

    AgentState **不持有** LLM/Tool 执行能力 —— 它只决策"下一步该做什么"。

    安全机制：
    - max_iterations：硬性迭代上限，超出后强制结束
    - tool_loop_detection：检测连续相同工具调用，防止死循环

    Args:
        task_id: AgentRun 唯一标识（对应 AgentRun.run_id）
        thread_id: 所属 Session ID
        agent_id: 执行此 AgentRun 的 Agent ID
        max_iterations: 最大 ReAct 迭代次数（来自 AgentIdentity.max_loop_steps）
    """

    # ── 标识 ──

    task_id: str
    """AgentRun 唯一标识（8 位 hex）"""

    thread_id: str
    """所属 Session ID"""

    agent_id: str
    """执行此 AgentRun 的 Agent ID"""

    # ── 状态机核心 ──

    phase: AgentPhase = AgentPhase.IDLE
    """当前执行阶段"""

    iteration: int = 0
    """当前迭代次数（0 = 尚未开始）"""

    max_iterations: int = 10
    """最大 ReAct 迭代次数。超出后强制进入 RESPONDING"""

    # ── 当前上下文 ──

    current_llm_response: LLMResponse | None = None
    """最近一次 LLM 响应。用于 FINALIZE 时提取最终内容"""

    current_tool_results: list[Message] = field(default_factory=list)
    """最近一次工具执行的结果列表"""

    # ── 安全检测 ──

    tool_call_history: list[tuple[str, str]] = field(default_factory=list)
    """工具调用历史。每条 = (tool_name, args_hash)。
    用于检测连续相同调用，阈值：_TOOL_LOOP_THRESHOLD 次。
    """

    _TOOL_LOOP_THRESHOLD: int = field(default=3, repr=False)
    """工具死循环检测阈值：连续 N 次相同调用视为死循环"""

    _TOOL_LOOP_WINDOW: int = field(default=5, repr=False)
    """工具循环检测窗口大小"""

    # ── 结束状态 ──

    end_status: AgentStatus = AgentStatus.RUNNING
    """AgentRun 结束状态。只在进入 DONE/FAILED 时写入"""

    error_message: str | None = None
    """异常信息。仅在 end_status=FAILED 或安全阀触发时非空"""

    handoff_target: str | None = None
    """Handoff 目标 Agent ID。仅在 end_status=HANDOFF 时非空"""

    handoff_context: str | None = None
    """Handoff 上下文信息。附加到目标 Agent 的 user_message"""

    # ── TRUNCATED 续跑 ──

    truncated_count: int = 0
    """TRUNCATED 续跑次数。防止无限续跑（上限 _MAX_TRUNCATED_CONTINUE 次）。"""

    _MAX_TRUNCATED_CONTINUE: int = field(default=3, repr=False)
    """TRUNCATED 续跑次数上限"""

    # ── RETRYING ──

    retry_count: int = 0
    """当前重试次数（RETRYING 流中使用）"""

    max_tool_retries: int = 2
    """工具执行最大重试次数"""

    # ── 内部任务拆解 ──

    tasks: list[Task] = field(default_factory=list)
    """Agent 内部问题拆解的子任务清单。由 Agent 通过计划工具（如 todo_write）创建，
    每个 Task 拥有独立的 TaskProgress 状态。在 AgentRun 结束时可随 AgentState 持久化。"""

    # ── 属性 ──

    @property
    def is_terminal(self) -> bool:
        """是否已进入终止状态。"""
        return self.phase in _TERMINAL_PHASES

    @property
    def tool_calls_total(self) -> int:
        """累计工具调用次数（从 tool_call_history 推断）。"""
        return len(self.tool_call_history)

    # ======================== 公开接口：handle_event() ========================

    def handle_event(self, event: AgentEvent) -> AgentAction:
        """事件分发入口 —— TransitionTable 声明式路由。

        1. 从 TRANSITION_TABLE 中筛选 current_phase == self.phase 的规则
        2. 按 priority 排序（越小越优先）
        3. 找到第一个 event_type 匹配且 guard 通过的规则
        4. 执行 side_effect → 设置 phase → 返回 action
        5. 内部 _auto_chain() 自动推进 ContinueEvent 链（瞬态→DONE）

        Args:
            event: 事件对象

        Returns:
            下一步 Runtime 应执行的操作

        Raises:
            ValueError: 无匹配规则（非法状态转换）
        """
        # 筛选当前 Phase 的候选规则
        candidates: list[TransitionRule] = [
            t for t in _TRANSITION_TABLE
            if t.current == self.phase and isinstance(event, t.event_type)
        ]

        if not candidates:
            raise ValueError(
                f"无法从 {self.phase.value} 响应 {_event_name(type(event))} 事件"
            )

        # 按优先级排序
        candidates.sort(key=lambda t: t.priority)

        # 找到第一个匹配的规则
        matched: TransitionRule | None = None
        for t in candidates:
            if t.guard is not None and not t.guard(self, event):
                continue
            matched = t
            break

        if matched is None:
            raise ValueError(
                f"无法从 {self.phase.value} 响应 {_event_name(type(event))} 事件"
            )

        # 执行副作用
        if matched.side_effect is not None:
            matched.side_effect(self, event)

        # 设置新 Phase + 动作
        self.phase = matched.next_phase
        action: AgentAction = matched.next_action

        # 自动推进 ContinueEvent 链（瞬态 Phase → DONE）
        action = self._auto_chain(action)

        return action

    # ======================== 内部：ContinueEvent 自动链式推进 ========================

    def _auto_chain(self, action: AgentAction) -> AgentAction:
        """自动推进 ContinueEvent 链。

        如果当前 phase 存在 ContinueEvent 转换规则，则自动应用。
        用于在一次 handle_event() 调用中完成：
        RESPONDING → DONE / HANDOFF → DONE

        注意：当 action 为 WAIT 时停止链式推进 —— 让 Runtime 有机会
        在推送 ContinueEvent 之前执行额外逻辑（如注入 "continue" 提示）。

        Args:
            action: 当前 action（来自上一步转换）

        Returns:
            最终 action（链式推进后的结果）
        """
        while action != AgentAction.WAIT:
            # 查找当前 phase 的 ContinueEvent 规则
            continue_rule: TransitionRule | None = None
            for t in _TRANSITION_TABLE:
                if t.current == self.phase and t.event_type == ContinueEvent:
                    if t.guard is not None and not t.guard(self, None):
                        continue
                    continue_rule = t
                    break

            if continue_rule is None:
                break  # 无 ContinueEvent 规则，停止链式推进

            # 应用 ContinueEvent 规则
            if continue_rule.side_effect is not None:
                continue_rule.side_effect(self, None)
            self.phase = continue_rule.next_phase
            action = continue_rule.next_action

        return action

    # ======================== 原子操作：工具历史 ========================

    def _record_tool_calls(self) -> None:
        """从 current_llm_response.tool_calls 提取并记录到 tool_call_history。

        原子操作：
        1. 读取 current_llm_response.tool_calls
        2. 对每个 tool_call 计算 (name, args_hash) 并追加到历史
        """
        resp: LLMResponse | None = self.current_llm_response
        if resp is None or not resp.tool_calls:
            return

        for tc in resp.tool_calls:
            name: str = getattr(tc, "name", "")
            args_str: str = getattr(tc, "arguments", "")
            args_hash: str = _hash_args(args_str)
            self.tool_call_history.append((name, args_hash))

    def add_tool_calls_from_response(self) -> None:
        """记录当前 LLM 响应中的工具调用到历史。

        供 Runtime 在 LLM 响应后调用，用于精确跟踪 token 和调用次数。
        与 _record_tool_calls 不同的是，此方法公开且可在任意时机调用。
        """
        self._record_tool_calls()

    # ======================== 原子操作：死循环检测 ========================

    def _detect_tool_loop(self) -> bool:
        """检测工具死循环：最近 N 次调用中同一 (tool_name, args_hash)
        连续出现达到阈值。

        原子操作：
        1. 取 tool_call_history 最近 _TOOL_LOOP_WINDOW 条记录
        2. 统计连续相同调用的最大长度
        3. 与 _TOOL_LOOP_THRESHOLD 比较

        Returns:
            True 表示检测到死循环
        """
        history: list[tuple[str, str]] = self.tool_call_history
        if len(history) < self._TOOL_LOOP_THRESHOLD:
            return False

        window: list[tuple[str, str]] = history[-self._TOOL_LOOP_WINDOW:]

        last_call: tuple[str, str] | None = None
        consecutive: int = 0

        for call in reversed(window):
            if last_call is None:
                last_call = call
                consecutive = 1
            elif self._same_tool_call(call, last_call):
                consecutive += 1
            else:
                break

        return consecutive >= self._TOOL_LOOP_THRESHOLD

    @staticmethod
    def _same_tool_call(a: tuple[str, str], b: tuple[str, str]) -> bool:
        """判断两次工具调用是否相同。

        Args:
            a: (tool_name, args_hash)
            b: (tool_name, args_hash)

        Returns:
            True 表示相同工具+相同参数
        """
        return a[0] == b[0] and a[1] == b[1]

    # ======================== 原子操作：快照/恢复 ========================

    def snapshot(self) -> dict:
        """生成当前 AgentState 的可序列化快照。

        原子操作：
        1. 收集所有需要持久化的字段
        2. 序列化 tasks 为 dict 列表
        3. 返回 JSON 兼容的 dict

        Returns:
            可序列化的状态快照字典
        """
        tasks_data: list[dict] = []
        for t in self.tasks:
            tasks_data.append({
                "task_id": t.task_id,
                "description": t.description,
                "progress": t.progress.value,
                "parent_task_id": t.parent_task_id,
                "agent_id": t.agent_id,
                "agent_run_ids": list(t.agent_run_ids),
                "result": t.result,
                "error": t.error,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            })

        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "agent_id": self.agent_id,
            "phase": self.phase.value,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "end_status": self.end_status.value,
            "error_message": self.error_message,
            "handoff_target": self.handoff_target,
            "handoff_context": self.handoff_context,
            "tool_calls_total": self.tool_calls_total,
            "tasks": tasks_data,
        }

    @classmethod
    def restore(
        cls,
        snapshot_data: dict,
    ) -> AgentState:
        """从持久化快照恢复 AgentState。

        原子操作：
        1. 从 dict 提取基础字段创建 AgentState 实例
        2. 恢复 phase、iteration、end_status 等运行时状态
        3. 不恢复 tasks（tasks 由独立 Task 管理）

        Args:
            snapshot_data: snapshot() 产出的字典

        Returns:
            恢复的 AgentState 实例
        """
        state = cls(
            task_id=snapshot_data.get("task_id", ""),
            thread_id=snapshot_data.get("thread_id", ""),
            agent_id=snapshot_data.get("agent_id", ""),
            max_iterations=snapshot_data.get("max_iterations", 10),
        )

        # 恢复运行时状态
        phase_str: str = snapshot_data.get("phase", AgentPhase.IDLE.value)
        try:
            state.phase = AgentPhase(phase_str)
        except ValueError:
            state.phase = AgentPhase.IDLE

        state.iteration = snapshot_data.get("iteration", 0)

        end_status_str: str = snapshot_data.get("end_status", AgentStatus.RUNNING.value)
        try:
            state.end_status = AgentStatus(end_status_str)
        except ValueError:
            state.end_status = AgentStatus.RUNNING

        state.error_message = snapshot_data.get("error_message")
        state.handoff_target = snapshot_data.get("handoff_target")
        state.handoff_context = snapshot_data.get("handoff_context")

        return state


# ============================================================================
# 辅助函数
# ============================================================================

def _hash_args(args_str: str) -> str:
    """对工具参数做确定性哈希。

    用于工具死循环检测中比较两次调用的参数是否相同。

    Args:
        args_str: JSON 格式的工具参数字符串

    Returns:
        MD5 哈希的前 8 位（hex）
    """
    return hashlib.md5(args_str.encode()).hexdigest()[:8]


def _event_name(event_type: type) -> str:
    """将事件类型映射为简短名称（用于错误消息）。"""
    mapping: dict[type, str] = {
        AgentStartEvent: "start",
        LLMResponseEvent: "llm_response",
        ToolsDoneEvent: "tools_done",
        ContinueEvent: "continue",
    }
    return mapping.get(event_type, event_type.__name__)
