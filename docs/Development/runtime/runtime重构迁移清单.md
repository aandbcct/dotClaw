# Runtime 重构迁移清单

> 状态：Phase 1–Phase 6 已完成并通过最终验收。
> 对应设计：[Runtime 重构设计](runtime重构设计.md)。

## 当前唯一运行架构

```text
Agent
  → SessionRunCoordinator
  → RuntimeEngine
  → Context / LLM / Tool / Repository / Delegation Ports
  → Conversation、AgentRun、RunEvent、RunMessage、Checkpoint
```

- `RuntimeEngine` 只依赖 Port，不导入 Journal、Session、旧上下文或 Dispatcher。
- `orchestration.RuntimeDelegationAdapter` 实现 `DelegationPort`，负责子 Session、子 Run、Task/Broker 回调和取消传播。
- `FileRunRepository`、`FileCheckpointRepository` 分别保存运行事实和恢复安全点；Journal 不再写入或恢复运行状态。

## 已删除模块与替代关系

| 已删除内容 | 唯一替代者 | 删除前验证 | 当前状态 |
| --- | --- | --- | --- |
| `runtime/runtime.py`、旧 Runtime facade | `RuntimeEngine` + `SessionRunCoordinator` | main、Agent、orchestration 已切换；v2 运行与委派测试通过 | 已删除 |
| `runtime/state_store.py`、Session 级 `state.json` | `CheckpointRepository` | 审批恢复、取消与 checkpoint 边界测试通过 | 已删除 |
| `runtime/agent_state.py`、`runtime/task.py` | `runtime/domain/state.py`、`DelegationPort` | 领域状态机和 delegation adapter 测试通过 | 已删除 |
| `session/agent_run.py`、旧 `messages/state_snapshot/trace_ids` | `runtime/domain/models.py::AgentRun`、RunMessage、RunEvent、Checkpoint | 旧样例迁移和摘要边界测试通过 | 已删除 |
| `agent/slotContext.py`、`slotContextImp.py` | `context/slot_context.py`、`slots.py`、`SlotContextProvider` | scoped cache、预算与降级测试通过 | 已删除 |
| `agent/resume.py`、旧 trace 恢复 | `ApprovalService` + `CheckpointRepository` | 同 run_id 审批恢复测试通过 | 已删除 |
| `orchestration/runners/local.py`、旧 Task 工具 | `RuntimeDelegationAdapter` | 真实 Adapter → Dispatcher → Coordinator 回调及取消测试通过 | 已删除 |
| `journal/sinks/state_sink.py` | `CheckpointRepository` | 无生产调用方；Journal 禁用时 Engine 测试通过 | 已删除 |

## 数据迁移

旧单文件 AgentRun 通过 `scripts/migrate_agent_run_v1_to_v2.py` 只读迁移到以下布局：

```text
data/sessions/{session_id}/agent_runs/{run_id}/
├── run.json
├── events.jsonl
├── messages.json
├── checkpoint.json
└── success_commit.json # 仅在成功事务未完成时存在，恢复后自动删除
```

迁移脚本保留旧源文件；成功迁移、checkpoint 脱敏和缺失源文件的可行动错误均由 `tests/runtime_v2/test_file_repositories.py` 验证。

## 最终审计命令

```powershell
# 默认回归
.\.venv\Scripts\python.exe -m pytest

# Runtime v2 架构和物理删除护栏
.\.venv\Scripts\python.exe -m pytest tests/runtime_v2/test_architecture_contract.py tests/runtime_v2/test_phase6_finalization.py

# 禁止旧生产模块残留
rg "runtime\.runtime|runtime\.state_store|runtime\.agent_state|runtime\.task|session\.agent_run|StateSink|ContextAssembler" src/dotclaw
```
