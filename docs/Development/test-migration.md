# 测试迁移清单

## 默认测试集

`pytest` 默认只收集已迁移到当前 `RuntimeEngine + AgentState` 架构的测试：

- `tests/agent/`
- `tests/channel/`
- `tests/context/`
- `tests/journal/`
- `tests/llm/`
- `tests/orchestration/`
- `tests/runtime/`
- `tests/session/`

这套测试是当前开发的阻断门槛，必须保持全绿。

推荐在项目虚拟环境中执行默认测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

默认命令会排除带有 `legacy` 标记的历史测试。需要核对迁移前的旧行为时，显式指定历史路径并执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -m legacy tests/metrics tests/test_phase4_acceptance.py tests/test_phase7_acceptance.py
```

## 历史测试

下列文件暂不进入默认收集，因为它们仍依赖已删除的 `AgentLoop`、`AgentContext`、`AgentResult`、`agent.prompt` 或 `metrics` 命名空间。它们保留在仓库中，仅作为行为迁移参考；禁止通过恢复旧生产模块来让它们通过。

| 历史测试 | 旧依赖 | 迁移目标 |
| --- | --- | --- |
| `tests/test_phase1_acceptance.py` | `AgentLoop`、旧 Session store | 已迁移至 `tests/runtime_v2/test_runtime_engine.py` |
| `tests/test_phase2_acceptance.py` | 裸 `async def` 和真实 API 依赖 | 已迁移至 `tests/llm/test_model_router_contract.py` |
| `tests/test_phase3_acceptance.py` | `AgentResult`、`AgentContext`、`PromptBuilder` | 已迁移至 `tests/agent/` 与 `tests/context/` |
| `tests/test_phase4_acceptance.py` | `AgentContext`、`MemoryProvider` | `MemoryManager` 与 `MemorySlot` 契约测试 |
| `tests/test_phase7_acceptance.py` | `SkillsProvider`、`AgentContext` | `SkillRegistry` 与 `SkillsSlot` 契约测试 |
| `tests/metrics/` | 已删除的 `dotclaw.metrics` | `dotclaw.journal` 的事件、快照与存储测试 |

Phase 1–Phase 6 已完成迁移：普通运行、工具调用、审批恢复、取消、并发隔离、委派、运行记录持久化和数据迁移均由 `tests/runtime_v2/` 覆盖。

Phase 2 已完成迁移：模型路由、OpenAI 兼容流、限流、熔断与 Proxy 降级均在本地替身下验证；已移除真实 API 与网络依赖。

Phase 3 已完成迁移：身份声明、不可变上下文、Slot 失败隔离和消息校验/清理均由当前模块测试覆盖。旧 ContextAssembler 已删除，不保留兼容 API。

迁移一个历史文件时，应先在当前模块目录新增等价测试并验证通过，再删除该历史文件。每次迁移后更新本表。

Runtime 旧生产 API 的调用方、替代方向与删除条件见
[Runtime 重构迁移清单](runtime/runtime重构迁移清单.md)。
