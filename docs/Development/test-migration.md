# 测试迁移清单

## 默认测试集

`pytest` 默认只收集已迁移到当前 `Runtime + AgentState` 架构的测试：

- `tests/agent/`
- `tests/channel/`
- `tests/context/`
- `tests/journal/`
- `tests/llm/`
- `tests/orchestration/`
- `tests/runtime/`
- `tests/session/`

这套测试是当前开发的阻断门槛，必须保持全绿。

## 历史测试

下列文件暂不进入默认收集，因为它们仍依赖已删除的 `AgentLoop`、`AgentContext`、`AgentResult`、`agent.prompt` 或 `metrics` 命名空间。它们保留在仓库中，仅作为行为迁移参考；禁止通过恢复旧生产模块来让它们通过。

| 历史测试 | 旧依赖 | 迁移目标 |
| --- | --- | --- |
| `tests/test_phase1_acceptance.py` | `AgentLoop`、旧 Session store | 已迁移至 `tests/runtime/test_run_contract.py` |
| `tests/test_phase2_acceptance.py` | 裸 `async def` 和真实 API 依赖 | 已迁移至 `tests/llm/test_model_router_contract.py` |
| `tests/test_phase3_acceptance.py` | `AgentResult`、`AgentContext`、`PromptBuilder` | 已迁移至 `tests/agent/` 与 `tests/context/` |
| `tests/test_phase4_acceptance.py` | `AgentContext`、`MemoryProvider` | `MemoryManager` 与 `MemorySlot` 契约测试 |
| `tests/test_phase7_acceptance.py` | `SkillsProvider`、`AgentContext` | `SkillRegistry` 与 `SkillsSlot` 契约测试 |
| `tests/metrics/` | 已删除的 `dotclaw.metrics` | `dotclaw.journal` 的事件、快照与存储测试 |

Phase 1 已完成迁移：纯文本、流式输出、工具消息回填、运行记录持久化和审批挂起都由 `Runtime.run()` 契约覆盖。旧测试的同步审批与自动多轮历史注入不属于当前 Runtime 契约，后续应作为独立的审批恢复和会话上下文功能验收。

Phase 2 已完成迁移：模型路由、OpenAI 兼容流、限流、熔断与 Proxy 降级均在本地替身下验证；已移除真实 API 与网络依赖。

Phase 3 已完成迁移：身份声明、不可变上下文、Slot 失败隔离和消息校验/清理均由当前模块测试覆盖。`AgentResult` 与 `PromptBuilder` 已被当前 Runtime 和 ContextAssembler 取代，不保留兼容 API。

迁移一个历史文件时，应先在当前模块目录新增等价测试并验证通过，再删除该历史文件。每次迁移后更新本表。
