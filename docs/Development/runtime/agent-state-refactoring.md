# AgentState 状态机重构方案

> 状态：设计文档 | 日期：2026-07-06 | 目标受众：开发人员
>
> 关联文档：[session&conversation&task&agentrun关系.md](./session&conversation&task&agentrun关系.md)、[runtime&state&agent关系.md](./runtime&state&agent关系.md)

---

## 一、当前架构分析

### 1.1 状态机在系统中的位置

```
Runtime（能力提供者，退化为原子方法集）
  └── TurnLoop（事件循环，持有 asyncio.Queue）
        └── AgentState（状态机中枢，驱动 ReAct 循环）
              └── tasks: list[Task]（Agent 内部拆解的子任务清单）
```

AgentState 是整个系统的中枢：它接收事件 → 计算下一状态 → 返回 AgentAction → TurnLoop 执行动作 → 执行结果作为新事件再次喂入。

### 1.2 当前状态集

```
AgentPhase: IDLE → THINKING → ACTING → THINKING → ... → RESPONDING → DONE
                        ↘ RESPONDING → DONE
                        ↘ TRUNCATED → DONE（v1: 直接结束，预留 v2 Continue）
                        ↘ HANDOFF → DONE
                        ↘ FAILED

AgentAction: INVOKE_LLM | EXECUTE_TOOLS | FINALIZE | HANDOFF_TARGET | WAIT
```

### 1.3 当前路由方式：procedural handler

```
handle_event(event)
  └─ isinstance(event, AgentStartEvent)   → _handle_start()
  └─ isinstance(event, LLMResponseEvent)  → _handle_llm_response()   [内部 if-else 分叉]
  └─ isinstance(event, ToolsDoneEvent)    → _handle_tools_done()     [内部 if-else 分叉]
       └─ 所有 handler 最终走 _transition() — 隐式自动终态化
```

**核心特征：这是一个 ReAct 循环的状态机，不是通用 Agent 状态机。** 所有状态和转换都固化了 "Think → Act → Think → ... → Respond" 这一个模式。

---

## 二、三个根本性问题

### 问题一：路由逻辑分散在 handler 的 if-else 中，没有集中式路由表

**现状：**

- `_handle_llm_response()` 用 4 个 if 分支决定下一状态（error/length/tool_calls/no_tool_calls）
- `_handle_tools_done()` 用 5 级优先级 if 链决定下一状态（tool_error/handoff/stop_signal/guard/continue）
- `_transition()` 额外做隐式转换（TRUNCATED→DONE、RESPONDING→DONE、HANDOFF→DONE）

**后果：**

- 想了解"状态机整体长什么样"——需要读 3 个方法的全部代码（约 200 行）
- 想加一个新 phase——需要修改 2-3 个 handler 方法 + event 分发 + transition 方法
- 想验证"某个转换是否合法"——没有单一来源可查
- Handler 之间有意外的耦合——`_transition()` 可能在开发者不知情的情况下自动终态化新 phase

**反例——加一个 `WAITING_APPROVAL` 状态需要改哪些地方：**

```
1. AgentPhase enum 加 WAITING_APPROVAL        → agent_state.py:38
2. _handle_tools_done() 加 if 分支             → agent_state.py:403 附近
3. handle_event() isinstance 链加 ApprovalDoneEvent → agent_state.py:283
4. 新增 _handle_approval_done() 方法            → agent_state.py 新方法
5. _transition() 确认不会自动终态化此 phase     → agent_state.py:497
6. TurnLoop.run_forever() 加 APPROVAL_DONE trigger 处理 → turn_loop.py:96
7. 新增 TurnLoop._step_approval() 方法          → turn_loop.py 新方法
```

**7 处改动，横跨 2 个文件。**

### 问题二：事件类型决定 handler，而非 (当前状态, 事件类型) 联合路由

**现状：**

`handle_event()` 按 `isinstance(event, XxxEvent)` 分发，handler 内部虽然用 `self.phase` 做防御性校验，但这不是路由逻辑。

**后果：**

同一事件在不同 phase 下需要不同处理时，handler 内部会产生更深的 if-else：

```python
# 假设将来 ToolsDoneEvent 在 REFLECTION 阶段有不同语义
def _handle_tools_done(self, event: ToolsDoneEvent) -> AgentAction:
    if self.phase == AgentPhase.REFLECTION:
        return self._handle_reflection_tools_done(event)  # 嵌套分叉
    elif self.phase == AgentPhase.ACTING:
        # ... 现有逻辑
```

这种模式会随着 phase 增加而指数级恶化。

### 问题三：`_transition()` 有隐式魔法，与扩展不可调和

```python
def _transition(self, new_phase: AgentPhase, action: AgentAction) -> AgentAction:
    self.phase = new_phase

    # 魔法 1：TRUNCATED 被覆盖为 DONE
    if new_phase == AgentPhase.TRUNCATED:
        self.phase = AgentPhase.DONE
        return AgentAction.FINALIZE       # 魔法 2：吞掉原 action，换成 FINALIZE

    # 魔法 3：RESPONDING → DONE（自动终态化）
    if new_phase == AgentPhase.RESPONDING:
        self.phase = AgentPhase.DONE
        return action

    # 魔法 4：HANDOFF → DONE（自动终态化）
    if new_phase == AgentPhase.HANDOFF:
        self.phase = AgentPhase.DONE
        return action

    return action
```

**问题：** 新 phase（如 `WAITING_APPROVAL`、`RETRYING`）既不需要自动终态化也不需要吞 action，但它们会经过这个方法。当前代码走 default `return action` 分支——行为正确但不安全，因为未来修改 `_transition()` 时可能意外影响新 phase。

---

## 三、长链路 Agent 需要的能力

长链路 Agent 的典型流程：

```
用户输入
  → 规划(PLANNING)：拆解复杂任务
  → 执行子任务 A：多次 Think-Act 循环
  → 等待审批(WAITING_APPROVAL)：人工介入
  → 审批通过
  → 执行子任务 B（使用不同策略）
  → 自检(REFLECTION)：验证结果质量
  → 发现不足 → 重试子任务 B(RETRYING)
  → 聚合结果(AGGREGATING)
  → 回复
```

当前缺少的 Phase：

| Phase | 触发场景 | 当前能否表达 |
|-------|---------|------------|
| **PLANNING** | LLM 判断需要拆解复杂任务 | ❌ 并入 THINKING，无法从状态机层面区分"在规划还是在推理" |
| **WAITING_APPROVAL** | 工具执行后需要人工审批 | ❌ 缺（APPROVAL_DONE trigger 已定义但无状态） |
| **RETRYING** | 工具失败但可重试（不同策略） | ❌ 当前 FAILED 直接终止，无重试路径 |
| **REFLECTION** | LLM 完成回复后自检 | ❌ 缺（RESPONDING 直接 → DONE） |
| **AGGREGATING** | 多个子任务结果需要聚合 | ❌ 缺 |
| **INTERRUPTED** | 用户中途打断 | ❌ TurnLoop.stop() 直接杀循环，状态机不感知 |

**这些 phase 的缺失不是因为"不需要"，而是因为当前 ReAct-only 状态集装不下它们。**

---

## 四、重构方案

### 4.1 核心思路：从 procedural handler 到声明式 TransitionTable

**目标：** "加一个新状态转换" 从改 7 处代码变成加 1 行声明，且不修改任何现有方法。

### 4.2 TransitionTable 数据结构

```python
from dataclasses import dataclass, field
from typing import Callable, Any

@dataclass
class Transition:
    """一条状态转换规则。

    当 AgentState 处于 `current` phase 且收到类型为 `event_type` 的事件，
    且 `guard` 条件满足时，状态转换到 `next_phase`，返回 `action`。
    可选的 `side_effect` 在转换前执行（如设置 end_status、记录日志等）。
    """
    current: "AgentPhase"
    event_type: type                    # 事件类型，如 LLMResponseEvent
    guard: Callable[["AgentState", Any], bool] | None  # 守卫条件
    next_phase: "AgentPhase"
    action: "AgentAction"
    side_effect: Callable[["AgentState", Any], None] | None = None
    priority: int = 0                   # 优先级，数值越小越优先匹配
```

### 4.3 将现有逻辑改写为 TransitionTable

```python
TRANSITION_TABLE: list[Transition] = _build_transition_table()


def _build_transition_table() -> list[Transition]:
    """构建完整的状态转换表。

    匹配规则：current phase == Transition.current 且 isinstance(event, Transition.event_type)，
    按 priority 升序匹配，第一个 guard 通过的 Transition 被采用。
    没有 guard 的 Transition 总是匹配（兜底），应放在同组最后。
    """
    t = []

    # ═══════════════════════════════════════
    # 启动：IDLE → 首次 LLM 调用
    # ═══════════════════════════════════════
    t.append(Transition(
        current=AgentPhase.IDLE,
        event_type=AgentStartEvent,
        guard=None,
        next_phase=AgentPhase.THINKING,
        action=AgentAction.INVOKE_LLM,
        side_effect=_side_set_iteration_1,
    ))

    # ═══════════════════════════════════════
    # THINKING → 收到 LLM 响应后的分叉
    # ═══════════════════════════════════════
    t.append(Transition(
        current=AgentPhase.THINKING,
        event_type=LLMResponseEvent,
        guard=_guard_llm_error,
        next_phase=AgentPhase.FAILED,
        action=AgentAction.FINALIZE,
        side_effect=_side_set_error,
        priority=1,
    ))
    t.append(Transition(
        current=AgentPhase.THINKING,
        event_type=LLMResponseEvent,
        guard=_guard_llm_truncated,
        next_phase=AgentPhase.TRUNCATED,
        action=AgentAction.WAIT,
        priority=2,
    ))
    t.append(Transition(
        current=AgentPhase.THINKING,
        event_type=LLMResponseEvent,
        guard=_guard_has_tool_calls,
        next_phase=AgentPhase.ACTING,
        action=AgentAction.EXECUTE_TOOLS,
        side_effect=_side_store_llm_response,
        priority=3,
    ))
    t.append(Transition(
        current=AgentPhase.THINKING,
        event_type=LLMResponseEvent,
        guard=None,                                   # 兜底：纯文本回复
        next_phase=AgentPhase.RESPONDING,
        action=AgentAction.FINALIZE,
        side_effect=_side_set_completed,
    ))

    # ═══════════════════════════════════════
    # ACTING → 工具执行完成后分叉
    # ═══════════════════════════════════════
    t.append(Transition(
        current=AgentPhase.ACTING,
        event_type=ToolsDoneEvent,
        guard=_guard_tool_error,
        next_phase=AgentPhase.FAILED,
        action=AgentAction.FINALIZE,
        side_effect=_side_set_tool_error,
        priority=1,
    ))
    t.append(Transition(
        current=AgentPhase.ACTING,
        event_type=ToolsDoneEvent,
        guard=_guard_handoff_signal,
        next_phase=AgentPhase.HANDOFF,
        action=AgentAction.HANDOFF_TARGET,
        side_effect=_side_set_handoff,
        priority=2,
    ))
    t.append(Transition(
        current=AgentPhase.ACTING,
        event_type=ToolsDoneEvent,
        guard=_guard_stop_signal,
        next_phase=AgentPhase.RESPONDING,
        action=AgentAction.FINALIZE,
        side_effect=_side_set_completed,
        priority=3,
    ))
    t.append(Transition(
        current=AgentPhase.ACTING,
        event_type=ToolsDoneEvent,
        guard=_guard_should_stop,
        next_phase=AgentPhase.RESPONDING,
        action=AgentAction.FINALIZE,
        side_effect=_side_set_completed,
        priority=4,
    ))
    t.append(Transition(
        current=AgentPhase.ACTING,
        event_type=ToolsDoneEvent,
        guard=None,                                   # 兜底：继续下一轮
        next_phase=AgentPhase.THINKING,
        action=AgentAction.INVOKE_LLM,
        side_effect=_side_increment_iteration,
    ))

    # ═══════════════════════════════════════
    # 新增：审批等待
    # ═══════════════════════════════════════
    t.append(Transition(
        current=AgentPhase.ACTING,
        event_type=ToolsDoneEvent,
        guard=_guard_approval_required,
        next_phase=AgentPhase.WAITING_APPROVAL,
        action=AgentAction.WAIT,
        priority=0,   # 审批优先于所有其他处理
    ))
    t.append(Transition(
        current=AgentPhase.WAITING_APPROVAL,
        event_type=ApprovalDoneEvent,
        guard=None,
        next_phase=AgentPhase.THINKING,
        action=AgentAction.INVOKE_LLM,
        side_effect=_side_resume_from_approval,
    ))

    return t
```

### 4.4 守卫函数的职责分离

守卫函数（guard）只做条件判断，副作用（如设置 end_status）交给独立的 `side_effect`：

```python
# ── 守卫函数（纯判断，不修改状态）──

def _guard_llm_error(state: AgentState, event: LLMResponseEvent) -> bool:
    return event.response.finish_reason == "error"

def _guard_llm_truncated(state: AgentState, event: LLMResponseEvent) -> bool:
    return event.response.finish_reason == "length"

def _guard_has_tool_calls(state: AgentState, event: LLMResponseEvent) -> bool:
    return bool(event.response.tool_calls)

def _guard_tool_error(state: AgentState, event: ToolsDoneEvent) -> bool:
    return event.tool_error

def _guard_handoff_signal(state: AgentState, event: ToolsDoneEvent) -> bool:
    return event.handoff_signal

def _guard_stop_signal(state: AgentState, event: ToolsDoneEvent) -> bool:
    return event.stop_signal

def _guard_should_stop(state: AgentState, event: ToolsDoneEvent) -> bool:
    """安全检查：max_iterations 或 tool_loop"""
    return state._check_guards()[0]

# ── 副作用函数（修改 state，不决定路由）──

def _side_set_iteration_1(state: AgentState, event: Any) -> None:
    state.iteration = 1

def _side_set_error(state: AgentState, event: Any) -> None:
    state.end_status = AgentStatus.FAILED
    state.error_message = "LLM 调用返回 error"

def _side_store_llm_response(state: AgentState, event: LLMResponseEvent) -> None:
    state.current_llm_response = event.response

def _side_set_completed(state: AgentState, event: Any) -> None:
    state.end_status = AgentStatus.COMPLETED

def _side_set_tool_error(state: AgentState, event: Any) -> None:
    state.end_status = AgentStatus.FAILED
    state.error_message = "工具执行错误"

def _side_set_handoff(state: AgentState, event: ToolsDoneEvent) -> None:
    state.end_status = AgentStatus.HANDOFF
    state._extract_handoff_info(event)

def _side_increment_iteration(state: AgentState, event: Any) -> None:
    state.iteration += 1

def _side_resume_from_approval(state: AgentState, event: Any) -> None:
    state.approval_result = getattr(event, "result", None)
```

### 4.5 统一 handle_event 入口

```python
def handle_event(self, event: "AgentEvent") -> "AgentAction":
    """事件分发入口——基于 TransitionTable 的声明式路由。

    遍历 TRANSITION_TABLE，找到第一个匹配的 Transition：
    1. current phase 匹配
    2. event 类型匹配（isinstance）
    3. guard 条件满足（或无 guard）

    Returns:
        AgentAction — 指示 Runtime 下一步应执行的操作

    Raises:
        ValueError: 未找到任何匹配的转换（非法状态转换）
    """
    # 按 priority 排序（低 → 高），同 priority 按表顺序
    candidates = [t for t in TRANSITION_TABLE if t.current == self.phase]
    candidates.sort(key=lambda t: t.priority)

    for t in candidates:
        if not isinstance(event, t.event_type):
            continue
        if t.guard is not None and not t.guard(self, event):
            continue

        # 命中！
        if t.side_effect is not None:
            t.side_effect(self, event)
        self.phase = t.next_phase
        return t.action

    raise ValueError(
        f"No transition found for ({self.phase.value}, {type(event).__name__})"
    )
```

### 4.6 TRUNCATED 自动续跑（替换 _transition 隐式魔法）

将 TRUNCATED 处理从 `_transition()` 魔法中移出，变成一个显式转换 + TurnLoop 配合：

```python
# TransitionTable 中的条目
t.append(Transition(
    current=AgentPhase.TRUNCATED,
    event_type=ContinueEvent,          # TurnLoop 在 TRUNCATED 后自动发送
    guard=None,
    next_phase=AgentPhase.THINKING,
    action=AgentAction.INVOKE_LLM,
    side_effect=_side_prepare_continue,  # 注入 "Continue" 提示 + 裁历史
))

# v1 行为（不启用自动续跑时）：
t.append(Transition(
    current=AgentPhase.TRUNCATED,
    event_type=ContinueEvent,
    guard=_guard_v1_no_continue,       # config 控制是否启用续跑
    next_phase=AgentPhase.DONE,
    action=AgentAction.FINALIZE,
    side_effect=_side_set_completed,
))
```

这样 `_transition()` 方法可以简化为：

```python
def _transition(self, new_phase: AgentPhase, action: AgentAction) -> AgentAction:
    """仅设置 phase，不再有任何隐式行为。
    
    TransitionTable 接管所有转换逻辑后，此方法退化为纯状态赋值。
    保留此方法作为 TransitionTable 的 side_effect 便捷调用入口。
    """
    self.phase = new_phase
    return action
```

---

## 五、TurnLoop 合并方案

### 5.1 当前问题

`_step_user_input()` 和 `_step_tool_result()` 各有约 80 行几乎重复的代码，唯一差异是：

- 初始事件类型不同（AgentStartEvent vs ToolsDoneEvent）
- 触发源标记不同（TriggerType.USER_INPUT vs TriggerType.TOOL_RESULT）
- user message 是从初始输入还是已有 context 构建

### 5.2 合并为通用 _step()

```python
async def _step(
    self,
    trigger_type: TriggerType,
    user_input: str,
    state: AgentState | None,
) -> str:
    """通用执行步。

    由 TransitionTable 的 AgentAction 驱动执行：
    INVOKE_LLM → 调用 LLM → 返回的事件决定下一步
    EXECUTE_TOOLS → 执行工具 → 保存状态 → 推 TOOL_RESULT 事件
    FINALIZE → 结束 AgentRun → 返回最终文本
    HANDOFF_TARGET → 移交目标 Agent
    WAIT → 持久化状态 → 等待下一次 trigger
    """
    agentrun_id = uuid.uuid4().hex[:8]
    self._run_ids.append(agentrun_id)

    run_messages: list[Message] = []
    self._journal.agentrun_start(agentrun_id, trigger_type.value)

    started_at = datetime.now(timezone.utc).isoformat()
    start_time = time.time()

    # 创建或恢复 AgentState
    if state is None:
        state = self._create_agent_state(agentrun_id)

    # 构建初始事件
    event: AgentEvent
    if trigger_type == TriggerType.USER_INPUT:
        event = ASStartEvent(user_message=user_input)
        # 记录 user message 到 trace + context
        user_msg = Message(role="user", content=user_input)
        self._journal.record_message(user_msg)
        self._context_messages.append(user_msg)
        run_messages.append(user_msg)
    elif trigger_type == TriggerType.TOOL_RESULT:
        # tool_results 已在 _step_caller 中记录到 context
        event = _build_tools_done_event(self._pending_tool_results)
        run_messages.extend(self._pending_tool_results)
    else:
        event = ASStartEvent(user_message=user_input)

    # 构建 system prompt
    system_prompt = await self._build_system_prompt(user_input)
    run_messages.append(Message(role="system", content=system_prompt))

    # ── 核心循环：状态机驱动 ──
    tokens_in_total, tokens_out_total = 0, 0
    action = state.handle_event(event)

    while True:
        if action == AgentAction.WAIT:
            # 等待外部事件（审批、异步工具等）
            self._pending_state = state
            snapshot = state.snapshot()
            await self._save_state_snapshot(snapshot)
            self._journal.agentrun_end(RunEndStatus.TOOL_WAIT.value)
            await self._save_agent_run(
                agentrun_id=agentrun_id,
                end_status=RunEndStatus.TOOL_WAIT.value,
                trigger=trigger_type.value,
                state_snapshot=snapshot,
                messages=run_messages,
                started_at=started_at,
            )
            return ""

        elif action == AgentAction.INVOKE_LLM:
            context_msgs = await self._build_context_msgs(
                user_message=user_input, system_prompt=system_prompt,
            )
            resp = await self._invoke_llm(agentrun_id, context_msgs)
            tokens_in_total += resp.input_tokens
            tokens_out_total += resp.output_tokens

            asst_msg = Message(
                role="assistant", content=resp.content or "",
                tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
            )
            self._journal.record_message(asst_msg)
            self._context_messages.append(asst_msg)
            run_messages.append(asst_msg)

            action = state.handle_event(ASLLMResponseEvent(response=resp))

        elif action == AgentAction.EXECUTE_TOOLS:
            tool_msgs = await self._execute_current_tools(state)
            for tm in tool_msgs:
                self._journal.record_message(tm)
                self._context_messages.append(tm)
                run_messages.append(tm)

            action = state.handle_event(ASToolsDoneEvent(results=tool_msgs))

        elif action == AgentAction.FINALIZE:
            duration_ms = int((time.time() - start_time) * 1000)
            ended_at = datetime.now(timezone.utc).isoformat()
            self._pending_state = None
            self._journal.agentrun_end(RunEndStatus.COMPLETED.value)
            await self._save_agent_run(
                agentrun_id=agentrun_id,
                end_status=RunEndStatus.COMPLETED.value,
                trigger=trigger_type.value,
                tokens_in=tokens_in_total, tokens_out=tokens_out_total,
                duration_ms=duration_ms,
                state_snapshot=state.snapshot(),
                messages=run_messages,
                started_at=started_at, ended_at=ended_at,
            )
            return (state.current_llm_response and state.current_llm_response.content) or ""

        elif action == AgentAction.HANDOFF_TARGET:
            handoff_target = state.handoff_target or ""
            await self._runtime._handle_handoff(
                thread_id=self._session_id,
                target_agent_id=handoff_target,
                context=state.handoff_context or "",
                parent_run_id=agentrun_id,
            )
            # 继续走 FINALIZE 路径
            action = AgentAction.FINALIZE
```

### 5.3 run_forever 的简化

```python
async def run_forever(self, initial_message: str) -> str:
    self._active = True
    final_response = ""

    await self._queue.put(TriggerEvent(
        trigger_type=TriggerType.USER_INPUT,
        data=initial_message,
    ))

    try:
        while self._active:
            trigger = await self._queue.get()

            if trigger.trigger_type == TriggerType.TOOL_RESULT:
                # 恢复或创建 state
                state = self._pending_state
                self._pending_state = None
                self._pending_tool_results = _ensure_list(trigger.data)
                user_input = ""
            else:
                state = None
                user_input = str(trigger.data) if trigger.data else ""

            final_response = await self._step(
                trigger_type=trigger.trigger_type,
                user_input=user_input,
                state=state,
            )

            if final_response:
                self._active = False
    except Exception:
        self._active = False
        raise
    finally:
        self._journal.finalize()

    return final_response
```

---

## 六、实施计划

### Phase 1：TransitionTable 替代 handler（不改状态集）

| 任务 | 预估代码量 | 风险 |
|------|-----------|------|
| 定义 Transition dataclass + 守卫函数 + 副作用函数 | ~120 行 | 低 |
| 构建 TRANSITION_TABLE（覆盖现有全部转换） | ~80 行 | 低 |
| 重写 handle_event() 为 TransitionTable 驱动 | ~20 行 | 低 |
| 保留旧 handler 方法但不调用（标 deprecated） | — | 无 |
| 简化 _transition() 为纯状态赋值 | ~5 行 | 低 |

**验收标准：** 所有现有测试通过，行为与重构前完全一致。

### Phase 2：TurnLoop _step 合并

| 任务 | 预估代码量 | 风险 |
|------|-----------|------|
| 提取通用 `_step()` 方法 | ~100 行 | 中 |
| 删除 `_step_user_input()` 和 `_step_tool_result()` | — | 中 |
| 简化 `run_forever()` | ~30 行 | 中 |

**验收标准：** 所有现有测试通过，USER_INPUT 和 TOOL_RESULT 两条路径行为不变。

### Phase 3：TRUNCATED 自动续跑

| 任务 | 预估代码量 | 风险 |
|------|-----------|------|
| 新增 AgentPhase.TRUNCATED → ContinueEvent 转换 | ~15 行 Transition | 低 |
| TurnLoop 在 TRUNCATED 后自动推 ContinueEvent | ~20 行 | 低 |
| 用 config 控制 v1（直接结束）vs v2（自动续跑） | ~10 行 | 低 |

### Phase 4：补充长链路 Phase

| 新增 Phase | 新增 Transition | 新增 Event | TurnLoop 改动 |
|-----------|----------------|-----------|--------------|
| PLANNING | 2 条 | — | — |
| WAITING_APPROVAL | 2 条 | ApprovalDoneEvent | APPROVAL_DONE trigger 已存在 |
| RETRYING | 3 条 | — | — |
| REFLECTION | 2 条 | — | — |

### Phase 5：安全保障节点（P0）

| 节点 | 方式 |
|------|------|
| Death Spiral 检测 | AgentState 加 `consecutive_error_turns` 计数器，`_guard_death_spiral` |
| User Cancellation | AgentState 加 `is_cancelled` + `_check_guards` 检测 |
| Cost Budget | AgentState 加 `max_total_tokens` + `_guard_cost_budget` |

---

## 七、迁移兼容性

所有改动对外部是**零破坏**的：

1. `AgentPhase` / `AgentAction` / `AgentStatus` 枚举保持不变（仅增加新值）
2. `handle_event()` 签名不变，返回值类型不变
3. `AgentState.snapshot()` / `restore()` 语义不变
4. TurnLoop 的公开接口（`push_trigger` / `run_forever` / `stop`）不变
5. Journal 的所有事件发射不变

旧代码（handler 方法、`_transition()` 魔法）可以保留为 deprecated，在 Phase 1-2 完成后逐版本清理。

---

## 八、扩展示例

### 示例：加一个 "工具失败自动重试" 能力

**改动前**（当前架构）：需要改 `_handle_tools_done()` + `_transition()` + TurnLoop，约 4 处。

**改动后**（TransitionTable）：加 2 行声明：

```python
# 替换现有 "工具错误 → FAILED" 为 "工具错误 → RETRYING"
# （现有条目保持不动，新增更高 priority 的条目覆盖）

t.append(Transition(
    current=AgentPhase.ACTING,
    event_type=ToolsDoneEvent,
    guard=_guard_tool_error_and_retryable,     # tool_error + 可重试标记
    next_phase=AgentPhase.RETRYING,
    action=AgentAction.INVOKE_LLM,             # 回到 LLM 让它换个策略
    side_effect=_side_record_retry_attempt,
    priority=0,                                 # 比 FAILED 的 priority=1 更优先
))
t.append(Transition(
    current=AgentPhase.RETRYING,
    event_type=LLMResponseEvent,
    guard=_guard_max_retries_exceeded,
    next_phase=AgentPhase.FAILED,
    action=AgentAction.FINALIZE,
    side_effect=_side_set_error,
))
t.append(Transition(
    current=AgentPhase.RETRYING,
    event_type=LLMResponseEvent,
    guard=None,                                  # 兜底：LLM 决定重试
    next_phase=AgentPhase.ACTING,               # 回到工具执行
    action=AgentAction.EXECUTE_TOOLS,
))
```

**TurnLoop 零改动。** 状态机自己驱动整个重试循环。

---

## 九、关键设计原则总结

1. **状态决定阶段，事件驱动转换，guard 细化分支**——三者分离
2. **副作用与路由解耦**——guard 不修改 state，side_effect 不影响路由
3. **兜底原则**——同组最后一条 Transition 的 guard=None，确保总有合法路径
4. **priority 控制覆盖**——高 priority 的 Transition 优先匹配，无需修改现有条目即可扩展
5. **一张表看全局**——`TRANSITION_TABLE` 本身就是状态机的可视化文档
