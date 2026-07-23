# 各模块todo列表

## 软件架构

- [ ] 顶层组合根还是mian->agent->runtime的形式，agent和runtime应该都是一等公民，需要理清楚层级关系

  ```
  Q：
  agent/factory.py 实际负责整个应用装配，却放在 agent/ 内。
  runtime_factory.py 只装配 Runtime 内核及其 Adapter，但其名字和文档又暗示它是唯一组合根。
  main.py 虽然拿到了 runtime_services，但正常路径实际只用 agent 和 session_mgr；这也说明返回三元组是装配边界泄漏。
  Agent 已经是轻量门面，运行过程交给 SessionRunCoordinator；它不应再承担“创建所有基础设施”的语义。
  ```

- [ ] 跨进程/多节点 Session，Runtime 从“单进程任务执行器”升级为“分布式任务调度系统”

## agentState状态机

- [ ] **状态分层**：将运行状态拆成生命周期、执行阶段、等待原因和终态结果，避免一个枚举承担所有语义。
- [ ] **等待原因独立**：用 `WaitReason` 表示审批、委派、补充信息、恢复等外部等待，而不是继续堆叠 `WAITING_*` 状态。
- [ ] **执行阶段内化**：将 `WAITING_LLM`、`WAITING_TOOLS` 改为运行中的 `stage`，它们不是“暂停”。
- [ ] **统一状态迁移**：所有状态改变只能通过 `transition(event)` 完成，业务代码不再直接修改状态字段。
- [ ] **状态机产出决策**：一次迁移同时返回新状态、下一步动作、RunStatus 投影、是否需要 checkpoint 和应记录的事件。
- [ ] **Engine 只执行副作用**：Runtime 主循环根据状态机给出的 action 调 LLM、工具、审批或恢复逻辑，不自行推断状态含义。
- [ ] **RunStatus 单向投影**：`AgentRun.status` 由状态机状态推导，不再与 `AgentState` 分别维护生命周期。
- [ ] **修正中断语义**：可恢复的 `INTERRUPTED` 应属于“挂起并等待恢复”，真正放弃才进入终态 `ABANDONED`。
- [ ] **Checkpoint 记录恢复必要状态**：持久化分层状态、待审批/待恢复信息和安全恢复点，不把整个运行对象当快照保存。
- [ ] **兼容迁移优先**：先让 checkpoint 能读取旧 `phase` 格式，再逐步移除旧枚举和 Engine 中对具体 phase 的分支。

## multi-agent

- [ ] 重启后恢复未完成任务；
- [ ] 多个子任务并行或嵌套委托；
- [ ] 远程 Agent、跨进程执行或外部消息队列；
- [ ] 后台长期任务和主动通知；
- [ ] 复杂任务树、自动结果聚合与持久化任务历史。

## 上下文槽

- [ ] 明确上下文构建形式，中间有上下文需要调整怎么办

- [x] 上下文有哪些槽位，不在factor里创立，应跟随各个层级的生命周期，通过注入的方式决定有哪些上下文

- [ ] ~~使用消息队列，当文件/源地址发生变化时，推送消息通知slot更新缓存~~更新时设slot失效标志位

- [ ] 加入暂存区概念，将agentrun中更新的slot内容存入暂存区，作为事实源前的暂存

  ```
  Q：槽位缓存更新的时机问题，因为存在agentrun取消/失败，所以本次agentrun更新的一些槽位的缓存，可能因为失败/取消不更新了，在下一次需要改回来，那怎么保证这个幂等性
  A:应该有一个事实源和一个暂存区，当slot更新时只做失效标记，让slot去暂存区和事实源重建，run完暂存区->事实源，下次agentrun前先对照缓存版本号和事实源版本号，高于事实源版本号时refresh重建
  ```

- [ ] 

## 上下文压缩

- [x] 考虑在单次agentrun中需要触发压缩的情况，每次llm_request前判断压缩

## checkpoint/resume

- [ ] 完善checkpoint保存节点

  ```
  **外部副作用前**：保存“准备执行什么”的意图与恢复状态。
  **重要节点完成后**：保存“已经发生什么”的事实与下一步状态。
  ```

- [ ] 

## llm proxy

- [ ] llm proxy接口多用于路由适配，目前写死了chat，应该可以从config里读功能节点，得到多功能列表
- [ ] 取消只做了应用层的取消，没有llm proxy的取消
- [ ] 支持多模态
- [ ] llm支持think、response等llm标签，能够在前端输出思考链路
- [ ] Provider Client 存在严重的并发安全问题

  ```
  Provider Client 存在严重的并发安全问题
  ```

  

## tool



```
Q：系统需要对tool的能力需求进行审核，不能让tool调用什么情况下都放行
A：需要tool能力声明，需要CapabilityBroker和PolicyEngine，做能力路由和通行审批，但这个能力审批具体设计好麻烦
```

- [x] 工件注册改成装饰器注册

  ```
  Q：现在tool注册太麻烦了，写完工具，要写注册接口get，要在init里静态注册
  A：改成装饰器注册，自动发现
  ```

```
Q：tool可能会得到一些敏感参数，这要怎么做过滤？
```

```
Q：后续可能有大量工具，全量注入不现实，要怎么做工具路由？
```



- [ ] 多加一些内建工具，连接mcp服务器
- [ ] 首版只注册builtin包内的工具，后续需要通过显式 allowlist 配置再开放自定义本地工具包

## journal

- [ ] 经过两次runtime重建，journal模块快废了，记录功能名存实亡，转移了很多职能给runtime，看现在还差那些缺口

## client

- [ ] 加一个前端，做个桌面应用怎么样

- [ ] 多channel时应用层需要加一个连接层，承载面向用户会话的操作，

  ```
  ·ChatService：面向用户会话的操作。接收消息、提交 Run、处理审批、取消、重试、放弃，并返回适合 Channel 展示的结果。当前 Agent.process()、resolve_approval() 等方法已经基本在做这件事，所以现阶段不需要重复创建。
  ·RuntimeOperations：面向运维/控制面的操作。查询可用工具、MCP 连接状态、技能目录，或触发记忆蒸馏等。这些不属于一次聊天 Run 的核心执行，也不应让 Channel 直接耦合 ToolExecutor、MCPToolProvider 等实现。
  ```

- [ ] 目前channel输出只输出最终回复，中间操作也要作为输出内容，让用户感知