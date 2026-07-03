"""TaskState —— 任务级状态机。

TaskState 是单次 AgentRun 的执行状态机，驱动 ReAct 循环。
采用事件驱动模式：Runtime 喂入事件 → 状态机转换 phase → 返回下一步动作。

架构层级：
    State（会话级持久状态，与 thread_id 绑定）
      └── TaskState（任务级状态机，单次 AgentRun 生命周期）

TaskState 不持有 LLM/Tool 等执行能力——它只决策"下一步该做什么"，
实际执行由 Runtime 根据返回的 TaskAction 完成。

内部逻辑（原子操作封装为方法）：
1. handle_event() — 事件分发入口
2. _handle_start() — 启动 → THINKING
3. _handle_llm_response() — LLM 响应 → ACTING / RESPONDING / TRUNCATED / FAILED
4. _handle_tools_done() — 工具完成 → THINKING / RESPONDING / HANDOFF / FAILED
5. _check_guards() — 安全阀检查（最大迭代、工具死循环）
6. _detect_tool_loop() — 检测连续相同工具调用
7. _transition() — 执行状态转换 + 返回动作
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..agent.agent import LLMResponse
from ..llm.base import Message


# ============================================================================
# 枚举定义
# ============================================================================

class TaskPhase(Enum):
    """TaskState 的执行阶段。

    状态机沿此枚举进行转换：
        IDLE → THINKING → ACTING → THINKING → ... → RESPONDING → DONE
                         ↘ RESPONDING → DONE
                         ↘ TRUNCATED → RESPONDING → DONE
                         ↘ HANDOFF → DONE
                         ↘ FAILED
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
    """LLM 输出被截断（finish_reason="length"）。
    v1: 直接转入 RESPONDING（预留 v2 Continue 注入扩展点）"""

    HANDOFF = "handoff"
    """任务流转给其他 Agent。Runtime 应执行 HANDOFF 动作"""

    DONE = "done"
    """终止状态：正常完成"""

    FAILED = "failed"
    """终止状态：执行异常"""


class TaskAction(Enum):
    """TaskState 返回给 Runtime 的执行指令。

    每个 action 对应 Runtime 需要执行的具体操作。
    """

    INVOKE_LLM = "invoke_llm"
    """调用 LLM 获取下一轮响应"""

    EXECUTE_TOOLS = "execute_tools"
    """执行 LLM 返回的工具调用"""

    FINALIZE = "finalize"
    """结束当前 Task，构建 AgentRun 返回"""

    HANDOFF_TARGET = "handoff_target"
    """执行 handoff：停止当前 Task 并将控制权转交目标 Agent"""

    WAIT = "wait"
    """等待状态，无操作（用于跨状态自动转换的中间步骤）"""


class TaskStatus(Enum):
    """Task 的最终结束状态。

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
_TERMINAL_PHASES: frozenset[TaskPhase] = frozenset({
    TaskPhase.DONE,
    TaskPhase.FAILED,
})


# ============================================================================
# 事件定义
# ============================================================================

@dataclass
class TaskStartEvent:
    """Task 启动事件。

    Runtime 在创建 TaskState 后发送此事件以启动状态机。
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
        stop_signal: 工具输出中包含停止信号（如 TaskComplete tool）
        handoff_signal: 工具输出中包含 handoff 信号
        tool_error: 工具执行过程中发生错误
    """

    results: list[Message]
    """工具执行结果列表。每个都是 role="tool" 的 Message"""

    stop_signal: bool = False
    """工具结果中是否包含停止信号"""

    handoff_signal: bool = False
    """工具结果中是否包含 handoff 信号"""

    tool_error: bool = False
    """工具执行过程中是否发生错误"""


# 事件联合类型
TaskEvent = TaskStartEvent | LLMResponseEvent | ToolsDoneEvent


# ============================================================================
# TaskState — 状态机主体
# ============================================================================

@dataclass
class TaskState:
    """任务级状态机 —— 驱动单次 AgentRun 的 ReAct 循环。

    TaskState 是事件驱动的状态机：
    1. Runtime 调用 handle_event(event) 喂入事件
    2. 状态机内部转换 phase 并返回 TaskAction
    3. Runtime 根据 action 执行对应操作
    4. 操作结果作为新事件再次喂入

    TaskState **不持有** LLM/Tool 执行能力 —— 它只决策"下一步该做什么"。

    安全机制：
    - max_iterations：硬性迭代上限，超出后强制结束
    - tool_loop_detection：检测连续相同工具调用，防止死循环

    Args:
        task_id: Task 唯一标识
        thread_id: 所属 Session ID
        agent_id: 执行此 Task 的 Agent ID
        max_iterations: 最大 ReAct 迭代次数（来自 AgentIdentity.max_loop_steps）
    """

    # ── 标识 ──

    task_id: str
    """Task 唯一标识（8 位 hex）"""

    thread_id: str
    """所属 Session ID，用于 State 存取"""

    agent_id: str
    """执行此 Task 的 Agent ID"""

    # ── 状态机核心 ──

    phase: TaskPhase = TaskPhase.IDLE
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

    end_status: TaskStatus = TaskStatus.RUNNING
    """Task 结束状态。只在进入 DONE/FAILED 时写入"""

    error_message: str | None = None
    """异常信息。仅在 end_status=FAILED 时非空"""

    handoff_target: str | None = None
    """Handoff 目标 Agent ID。仅在 end_status=HANDOFF 时非空"""

    handoff_context: str | None = None
    """Handoff 上下文信息。附加到目标 Agent 的 user_message"""

    # ── 属性 ──

    @property
    def is_terminal(self) -> bool:
        """是否已进入终止状态。"""
        return self.phase in _TERMINAL_PHASES

    @property
    def tool_calls_total(self) -> int:
        """累计工具调用次数（从 tool_call_history 推断）。"""
        return len(self.tool_call_history)

    # ======================== 公开接口 ========================

    def handle_event(self, event: TaskEvent) -> TaskAction:
        """事件分发入口。

        根据事件类型分发到对应的内部原子处理方法。

        Args:
            event: 事件对象（TaskStartEvent / LLMResponseEvent / ToolsDoneEvent）

        Returns:
            下一步 Runtime 应执行的操作

        Raises:
            ValueError: 未知事件类型或非法状态转换
        """
        if isinstance(event, TaskStartEvent):
            return self._handle_start()
        elif isinstance(event, LLMResponseEvent):
            return self._handle_llm_response(event)
        elif isinstance(event, ToolsDoneEvent):
            return self._handle_tools_done(event)
        else:
            raise ValueError(f"未知事件类型: {type(event).__name__}")

    # ======================== 原子操作：事件处理 ========================

    def _handle_start(self) -> TaskAction:
        """处理启动事件：IDLE → THINKING。

        原子操作：
        1. 校验当前 phase 必须为 IDLE
        2. 进入 THINKING 阶段

        Returns:
            TaskAction.INVOKE_LLM —— 要求 Runtime 调用 LLM
        """
        if self.phase != TaskPhase.IDLE:
            raise ValueError(f"无法从 {self.phase} 响应 start 事件")
        self.iteration = 1
        return self._transition(TaskPhase.THINKING, TaskAction.INVOKE_LLM)

    def _handle_llm_response(self, event: LLMResponseEvent) -> TaskAction:
        """处理 LLM 响应事件：决定下一阶段。

        原子操作：
        1. 保存 LLM 响应到 current_llm_response
        2. 根据 finish_reason 和 tool_calls 判断下一阶段
        3. 执行状态转换

        决策逻辑：
        - finish_reason="error" → FAILED
        - finish_reason="length" → TRUNCATED（v1: → RESPONDING）
        - has tool_calls → ACTING
        - no tool_calls → RESPONDING

        Args:
            event: LLM 响应事件

        Returns:
            TaskAction —— INVOKE_LLM / EXECUTE_TOOLS / FINALIZE / WAIT
        """
        if self.phase != TaskPhase.THINKING:
            raise ValueError(f"无法从 {self.phase} 响应 llm_response 事件")

        self.current_llm_response = event.response

        # 不可恢复错误
        if event.response.finish_reason == "error":
            self.end_status = TaskStatus.FAILED
            self.error_message = "LLM 调用返回 error"
            return self._transition(TaskPhase.FAILED, TaskAction.FINALIZE)

        # 输出被截断
        if event.response.finish_reason == "length":
            # v1: 直接转入 RESPONDING（预留 v2 Continue 注入扩展点）
            return self._transition(TaskPhase.TRUNCATED, TaskAction.WAIT)

        # 有工具调用 → 进入执行阶段
        if event.response.tool_calls:
            return self._transition(TaskPhase.ACTING, TaskAction.EXECUTE_TOOLS)

        # 无工具调用 → 正常回复
        self.end_status = TaskStatus.COMPLETED
        return self._transition(TaskPhase.RESPONDING, TaskAction.FINALIZE)

    def _handle_tools_done(self, event: ToolsDoneEvent) -> TaskAction:
        """处理工具执行完成事件：决定下一阶段。

        原子操作：
        1. 保存工具结果
        2. 更新工具调用历史
        3. 检查停止信号 / handoff 信号 / 工具错误
        4. 检查安全阀（最大迭代、工具死循环）
        5. 执行状态转换

        决策优先级：
        1. 工具错误 → FAILED
        2. handoff 信号 → HANDOFF
        3. stop 信号 → RESPONDING
        4. 安全阀触发 → RESPONDING
        5. 正常 → THINKING（下一轮循环）

        Args:
            event: 工具执行完成事件

        Returns:
            TaskAction —— INVOKE_LLM / FINALIZE / HANDOFF_TARGET
        """
        if self.phase != TaskPhase.ACTING:
            raise ValueError(f"无法从 {self.phase} 响应 tools_done 事件")

        self.current_tool_results = list(event.results)

        # 更新工具调用历史（用于死循环检测）
        self._record_tool_calls()

        # 优先级 1：工具执行错误
        if event.tool_error:
            self.end_status = TaskStatus.FAILED
            self.error_message = "工具执行错误"
            return self._transition(TaskPhase.FAILED, TaskAction.FINALIZE)

        # 优先级 2：handoff 信号
        if event.handoff_signal:
            self.end_status = TaskStatus.HANDOFF
            self._extract_handoff_info(event)
            return self._transition(TaskPhase.HANDOFF, TaskAction.HANDOFF_TARGET)

        # 优先级 3：stop 信号
        if event.stop_signal:
            self.end_status = TaskStatus.COMPLETED
            return self._transition(TaskPhase.RESPONDING, TaskAction.FINALIZE)

        # 优先级 4：安全阀检查
        guard_result: tuple[bool, str] = self._check_guards()
        if guard_result[0]:
            self.end_status = TaskStatus.COMPLETED
            return self._transition(TaskPhase.RESPONDING, TaskAction.FINALIZE)

        # 优先级 5：正常继续下一轮
        self.iteration += 1
        return self._transition(TaskPhase.THINKING, TaskAction.INVOKE_LLM)

    # ======================== 原子操作：安全阀 ========================

    def _check_guards(self) -> tuple[bool, str]:
        """检查所有安全阀。

        原子操作：
        1. 检查最大迭代次数
        2. 检查工具死循环

        Returns:
            (should_stop, reason) —— should_stop=True 表示需要终止循环

        注意：此方法不修改 phase，由调用方决定如何处理。
        """
        # 检查最大迭代次数
        if self.iteration >= self.max_iterations:
            self.error_message = f"超出最大迭代次数 ({self.max_iterations})"
            return (True, "max_iterations")

        # 检查工具死循环
        if self._detect_tool_loop():
            self.error_message = "检测到工具死循环"
            return (True, "tool_loop")

        return (False, "")

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

        # 只检查最近窗口内的记录
        window: list[tuple[str, str]] = history[-self._TOOL_LOOP_WINDOW:]

        # 统计最后连续相同调用的次数
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

    # ======================== 原子操作：辅助方法 ========================

    def _transition(self, new_phase: TaskPhase, action: TaskAction) -> TaskAction:
        """执行状态转换。

        原子操作：
        1. 设置新 phase
        2. 处理跨状态自动转换：
           - TRUNCATED → RESPONDING → DONE
           - RESPONDING → DONE（自动终态化）
           - HANDOFF → DONE（自动终态化）

        Args:
            new_phase: 目标阶段
            action: 关联的动作

        Returns:
            最终需要 Runtime 执行的动作
        """
        self.phase = new_phase

        # TRUNCATED → RESPONDING → DONE（v1 直接结束）
        if new_phase == TaskPhase.TRUNCATED:
            self.end_status = TaskStatus.COMPLETED
            self.phase = TaskPhase.DONE
            return TaskAction.FINALIZE

        # RESPONDING → DONE（自动终态化）
        if new_phase == TaskPhase.RESPONDING:
            self.phase = TaskPhase.DONE
            return action

        # HANDOFF → DONE（自动终态化）
        if new_phase == TaskPhase.HANDOFF:
            self.phase = TaskPhase.DONE
            return action

        return action

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

    def _extract_handoff_info(self, event: ToolsDoneEvent) -> None:
        """从工具结果中提取 handoff 信息。

        原子操作：
        1. 遍历 event.results 查找 handoff 相关字段
        2. 提取 target_agent_id 和 context

        目前 handoff 信息约定从工具结果的 metadata 中读取。
        v1 简单实现：取第一个非空 tool result 的 content 作为 context，
        handoff_target 由 Runtime 层决定（不在 TaskState 内解析）。
        """
        for msg in event.results:
            if msg.content:
                self.handoff_context = msg.content
                break

    def add_tool_calls_from_response(self) -> None:
        """记录当前 LLM 响应中的工具调用到历史。

        供 Runtime 在 LLM 响应后调用，用于精确跟踪 token 和调用次数。
        与 _record_tool_calls 不同的是，此方法公开且可在任意时机调用。
        """
        self._record_tool_calls()


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
