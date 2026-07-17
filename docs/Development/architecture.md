# 开发架构状态

> 当前 Runtime 设计以 [Runtime 重构设计](runtime/runtime重构设计.md) 为唯一实施依据；本页用于区分现行设计和历史资料。

## 现行 Runtime 目标

`RuntimeEngine` 是业务无状态的共享执行协调器。每个 `AgentRun` 创建独立的 `RunExecution`，纯 `AgentState` 决定下一步动作，具体的上下文、LLM、工具与持久化能力均通过 Port 接入。

```mermaid
flowchart LR
    Client[CLI / Web / Scheduler] --> Coordinator[SessionRunCoordinator]
    Coordinator --> Engine[RuntimeEngine]
    Engine --> Execution[RunExecution]
    Execution --> State[AgentState]
    Engine --> Ports[Context / LLM / Tool / Repository Ports]
```

运行历史的迁移结果、已删除模块和数据迁移方式见
[Runtime 重构迁移清单](runtime/runtime重构迁移清单.md)。
