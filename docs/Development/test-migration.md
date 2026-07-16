# 测试迁移清单

## 默认测试集

`pytest` 默认只收集已迁移到当前 `Runtime + AgentState` 架构的测试：

- `tests/agent/`
- `tests/channel/`
- `tests/context/`
- `tests/journal/`
- `tests/orchestration/`
- `tests/runtime/`
- `tests/session/`

这套测试是当前开发的阻断门槛，必须保持全绿。

## 历史测试

下列文件暂不进入默认收集，因为它们仍依赖已删除的 `AgentLoop`、`AgentContext`、`AgentResult`、`agent.prompt` 或 `metrics` 命名空间。它们保留在仓库中，仅作为行为迁移参考；禁止通过恢复旧生产模块来让它们通过。

| 历史测试 | 旧依赖 | 迁移目标 |
| --- | --- | --- |
| `tests/test_phase1_acceptance.py` | `AgentLoop`、旧 Session store | 已迁移至 `tests/runtime/test_run_contract.py` |
| `tests/test_phase2_acceptance.py` | 裸 `async def` 和真实限流等待 | `tests/llm/` 中可控时钟的模型路由契约测试 |
| `tests/test_phase3_acceptance.py` | `AgentResult`、`AgentContext`、`PromptBuilder` | `AgentIdentity`、`SlotContext`、Runtime 返回契约测试 |
| `tests/test_phase4_acceptance.py` | `AgentContext`、`MemoryProvider` | `MemoryManager` 与 `MemorySlot` 契约测试 |
| `tests/test_phase7_acceptance.py` | `SkillsProvider`、`AgentContext` | `SkillRegistry` 与 `SkillsSlot` 契约测试 |
| `tests/metrics/` | 已删除的 `dotclaw.metrics` | `dotclaw.journal` 的事件、快照与存储测试 |

Phase 1 已完成迁移：纯文本、流式输出、工具消息回填、运行记录持久化和审批挂起都由 `Runtime.run()` 契约覆盖。旧测试的同步审批与自动多轮历史注入不属于当前 Runtime 契约，后续应作为独立的审批恢复和会话上下文功能验收。

迁移一个历史文件时，应先在当前模块目录新增等价测试并验证通过，再删除该历史文件。每次迁移后更新本表。
