# Multi-Agent 子系统 PRD

> **版本**: v1.0 | **状态**: Draft | **日期**: 2026-06-29
> **关联**: [设计文档](./DESIGN.md) | [架构路线图](../architecture-and-roadmap.md)

---

## 1. 背景与动机

### 1.1 业务驱动力

dotclaw 当前是**单 Agent 单体架构**：一个 `Agent` 实例持有一个 `AgentLoop`，单线程 ReAct 循环处理用户请求。这种架构在以下场景存在明确瓶颈：

| 场景 | 单 Agent 瓶颈 | Multi-Agent 解法 |
|---|---|---|
| 复杂多步骤任务 | 单 Agent 上下文过长导致注意力衰减 | 拆解为子 Agent 并行执行 |
| 多领域交叉任务 | 单一 system prompt 无法同时专精多个领域 | 多个专精 Agent 协作 |
| 超长程任务 | max_loop_steps 硬限制，无任务分片 | 子 Agent 分片执行 + 结果汇聚 |
| 需要不同权限级别 | 单 Agent 工具权限一把抓 | 子 Agent 独立权限白名单 |
| 评测对比 | 无 A/B 测试能力 | 同一任务分发多个 Agent 配置并行跑 |

### 1.2 JD 对齐

本 PRD 直接对齐 DeepSeek Harness 团队 JD 的核心方向：

- ✅ **Subagent 与 Multi-Agent**：子 Agent 孵化、多智能体拓扑、Agent 间通信
- ✅ **上下文管理**：子 Agent 上下文继承与裁剪
- ✅ **长期记忆**：跨 Agent 记忆共享与合并
- ✅ **自进化 Agent**：基于多 Agent 协作结果的自改进
- ✅ **超长程任务**：任务分片 + 断点续跑
- ✅ **基准测试与评测**：Multi-Agent 评测维度

### 1.3 项目现状

dotclaw 已完成 Phase 1-9，具备以下可用基础设施：

- ✅ ReAct 循环 + 工具系统 + 审批机制
- ✅ 三级记忆（L0 工作 / L1 日记忆 / L2 蒸馏）
- ✅ ContextSlot 四级分层装配
- ✅ 多模型路由 + 流式输出
- ✅ Journal 16 类事件 + trace/report/snapshot 三路输出
- ✅ 6 维评测系统 + 基线对比
- ✅ Skill 按需注入
- ✅ MCP 协议双传输

**缺失**：Agent 间协作、子 Agent 生命周期、多 Agent 拓扑、Agent 间通信协议。

---

## 2. 目标与非目标

### 2.1 目标

| 序号 | 目标 | 衡量标准 |
|---|---|---|
| G1 | Agent 可从工具列表中 spawn 子 Agent 执行独立任务 | LLM 自主决策 spawn，端到端跑通 |
| G2 | 子 Agent 继承/裁剪父 Agent 上下文（记忆/工具/Skill） | 子 Agent 可用父 Agent 的记忆但工具受限 |
| G3 | 支持树形委托拓扑（父→子→结果汇聚） | 1 父 + N 子并行执行，父汇总结果 |
| G4 | 子 Agent 生命周期完整可观测（Journal 扩展） | spawn/run/complete 事件全量记录 |
| G5 | 评测系统支持 Multi-Agent 维度 | 新增 subagent_dispatch / multi_agent_collab 评测 case |

### 2.2 非目标（本期不做）

- ❌ 网络层 Agent 间通信（本期仅限同进程）
- ❌ 持久化 Agent 拓扑（子 Agent 随父 Agent 生命周期）
- ❌ 对等协商共识算法（如 Raft/Paxos）
- ❌ Agent 市场/发现服务
- ❌ 基于 RL 的 Agent 路由策略

---

## 3. 用户故事

### US-1: 开发者 spawn 子 Agent 做代码审查

```
作为开发者，我让 dotclaw review 我的代码仓库。
Agent 自动 spawn 3 个子 Agent：
  - Agent-A 审查安全性
  - Agent-B 审查性能
  - Agent-C 审查代码风格
3 个子 Agent 并行执行，完成后各自返回审查报告。
父 Agent 汇总 3 份报告，输出统合的 review 结论。
```

### US-2: 超长程任务自动分片

```
我让 dotclaw 处理 100 个文件的迁移任务。
Agent 将任务拆分为 10 个子任务，spawn 10 个子 Agent 并行处理。
某个子 Agent 中途失败，父 Agent 收到失败通知，重新 spawn 该分片。
全部完成后，父 Agent 汇总结果。
```

### US-3: 评测 A/B 对比

```
我在 Eval 系统中配置同一 task 分发给 default/v4-pro/v4-flash 三个 model 子 Agent。
评测系统自动并行执行，收集结果，生成对比报告。
```

### US-4: 子 Agent 中断恢复

```
一个子 Agent 正在执行 50 步 ReAct 循环时 dotclaw 崩溃。
重启后，父 Agent 检测到未完成的子 Agent，从 checkpoint 恢复继续执行。
```

---

## 4. 功能需求

### FR-1: SubagentSpawner — 子 Agent 孵化器

| 字段 | 说明 |
|---|---|
| **触发方式** | LLM 通过 `spawn_agent` tool call 触发；也支持代码直接调用 |
| **spawn 参数** | `task`(必填)、`agent_config_override`(可选)、`mode`(one-shot/persistent) |
| **上下文继承** | 默认继承 MemoryManager / SkillRegistry；ToolExecutor 默认继承但有 scope 裁剪 |
| **返回值** | `SubagentHandle`（含 agent_id、status、result awaitable） |
| **并发控制** | 可配置最大并发子 Agent 数（默认 10） |

### FR-2: SubagentHandle — 子 Agent 句柄

| 方法 | 说明 |
|---|---|
| `await result()` | 等待子 Agent 完成，返回 `AgentResult` |
| `send(message)` | 向运行中的子 Agent 发送 steering 消息 |
| `kill()` | 终止子 Agent |
| `status` | 返回 `running` / `completed` / `failed` / `killed` |

### FR-3: Agent 原语拆分

| 类 | 职责 | 变化 |
|---|---|---|
| `AgentRuntime`（新增） | 纯运行时：持有 LLM + ToolExecutor + Channel | 从 Agent 中抽离 |
| `AgentIdentity`（新增） | 身份配置：id/name/system_prompt/workspace/allowed_tools | 可序列化，可在 Agent 间传递 |
| `Agent`（改造） | 持有 AgentRuntime + AgentIdentity + SessionMgr + ... | 减负，专注生命周期 |
| `AgentLoop`（改造） | 不再直接引用 Agent，改为引用 AgentRuntime | 解耦，子 Agent 复用同一个 Loop |

### FR-4: Agent 间通信协议

| 消息类型 | 方向 | 说明 |
|---|---|---|
| `TASK_ASSIGN` | 父→子 | 分配任务，携带上下文 |
| `RESULT` | 子→父 | 返回执行结果 |
| `STEER` | 父→子 | 运行时干预（改方向、补充信息） |
| `HEARTBEAT` | 子→父 | 长时间运行时定期汇报进度 |
| `ERROR` | 子→父 | 执行异常通知 |

### FR-5: 内置工具扩展

新增 3 个 builtin tool：

| 工具名 | 参数 | 说明 |
|---|---|---|
| `spawn_agent` | `task`, `agent_config_override?`, `mode?` | 孵化子 Agent |
| `list_agents` | 无 | 列出当前所有子 Agent 及其状态 |
| `kill_agent` | `agent_id` | 终止指定子 Agent |

### FR-6: Journal 扩展

新增 5 个事件：

| 事件 | 关键字段 | 说明 |
|---|---|---|
| `agent_spawn` | parent_id, child_id, task, mode, config_override | 子 Agent 孵化 |
| `agent_message` | from_id, to_id, msg_type, payload | Agent 间通信 |
| `agent_complete` | agent_id, result, duration_ms, iterations | 子 Agent 完成 |
| `agent_error` | agent_id, error_type, error_msg | 子 Agent 异常 |
| `agent_kill` | agent_id, reason | 子 Agent 被终止 |

### FR-7: Eval 扩展

新增 Multi-Agent 评测维度：

| 评测 case | 说明 | 核心指标 |
|---|---|---|
| `subagent_dispatch` | 子 Agent spawn 纯开销 | spawn 延迟 P50/P95，并发吞吐 |
| `multi_agent_collab` | N 子 Agent 并行执行同一 task | 结果汇聚准确率、端到端延迟 |
| `context_inherit` | 子 Agent 上下文继承正确性 | 记忆召回命中率、工具裁剪正确性 |

---

## 5. 优先级与里程碑

### M1: Agent 原语重构（1-2 周）

- [ ] 拆分 `AgentRuntime` / `AgentIdentity`
- [ ] `AgentLoop` 解耦为依赖 `AgentRuntime`
- [ ] 现有所有测试继续通过（回归）
- [ ] 工厂 `build_agent()` 适配新结构

### M2: Subagent 核心链路（2-3 周）

- [ ] `SubagentSpawner` 实现
- [ ] `SubagentHandle` 实现
- [ ] `spawn_agent` / `list_agents` / `kill_agent` 内置工具
- [ ] AgentLoop 集成 spawn 路径
- [ ] 子 Agent 上下文继承（Memory / Tools / Skills）
- [ ] 端到端：用户消息 → LLM 决策 spawn → 子 Agent 执行 → 返回结果

### M3: Multi-Agent 拓扑与评测（1-2 周）

- [ ] AgentMessage 通信协议
- [ ] 树形委托拓扑（父 spawn N 子 → gather 结果）
- [ ] Journal 扩展 5 个新事件
- [ ] Eval 扩展 3 个新 case
- [ ] ResumeManager 适配（子 Agent 中断恢复）

### M4: 打磨与文档（1 周）

- [ ] 压力测试（100 并发子 Agent）
- [ ] 内存泄漏检测
- [ ] 设计文档完善
- [ ] 使用示例 + README 更新

---

## 6. 成功指标

| 指标 | 目标值 | 测量方式 |
|---|---|---|
| 单子 Agent spawn 延迟 | P95 < 500ms | Eval `subagent_dispatch` |
| 10 并发子 Agent 吞吐 | 全部完成 < 30s（假设每个 2s 任务） | Eval `multi_agent_collab` |
| 上下文继承记忆召回率 | > 90%（子 Agent 能搜到父 Agent 近期的记忆） | Eval `context_inherit` |
| 工具裁剪正确率 | 100%（子 Agent 不可调用白名单外的工具） | 单元测试 |
| 回归测试通过率 | 100% | pytest 全量 |
| Journal 事件完整性 | 每个子 Agent 生命周期至少 3 个事件（spawn/complete/error） | trace 校验 |

---

## 7. 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| Agent 拆分解耦导致大量破坏性变更 | 中 | 高 | 先加新类，保留旧 Agent 接口兼容，渐进迁移 |
| 子 Agent 并发导致 LLM rate limit | 高 | 中 | RateLimiter 已有全局限流，子 Agent 共享同一 limiter |
| 子 Agent 内存爆炸 | 中 | 高 | 默认最大并发数限制 + 子 Agent 完成即回收 |
| 父 Agent 等待子 Agent 时超时 | 中 | 中 | AgentLoop 增加子 Agent 等待超时 + 超时降级策略 |
| 子 Agent 上下文继承导致 token 膨胀 | 中 | 中 | 子 Agent 独立 ContextSlot 装配，不灌入全部父上下文 |
