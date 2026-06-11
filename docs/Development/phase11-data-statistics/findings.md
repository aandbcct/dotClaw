# 数据统计模块 — 调研发现

> 创建日期：2026-06-07
> 关联：task_plan.md, docs/phase11-data-statistics/

---

## F1：现有指标基础

- dotClaw 当前**没有专门的统计模块**
- 仅有 `AgentLogger`/`TraceRecord` 记录单次请求的 `duration_ms` 和工具调用次数
- README 明确标注"数据统计模块"在后续计划中待开发
- 架构：基于 ReAct，核心循环在 `src/agent/loop.py`，LLM 代理在 `src/llm/proxy.py`，工具执行在 `src/agent/tools/executor.py`

## F2：依赖注入路径

- 核心上下文对象是 `AgentContext`（位于 `src/agent/context.py`）
- 通过 `AgentContext` 传递 `metrics_collector` 是最自然的依赖注入方式
- 已有类似模式：AgentContext 中已有 memory、config 等字段通过 context 传递

## F3：配置体系

- 两层 YAML：`config.yaml` + `model_router_config.yaml`
- 配置指纹可通过 SHA256(config.yaml + model_router_config.yaml) 生成
- ~~成本计算需要模型单价~~ → **已确认：配置中无价格信息，cost_usd 暂不计算，返回 0.0，cost_by_model 返回空 dict**

## F4：现有文档体系

- 项目使用 Phase 编号管理开发节奏，Phase 11 为数据统计模块
- 文档类型：roadmap（计划）、design（设计）、record（变更日志）、codeReview（审查报告）
- 本次开发需要创建：data-statistics-roadmap.md（本 task_plan.md 的正式版本）、record.md（变更日志）

## F5：已确认的删除

- 从 v1.0 设计中的 ~60 个指标字段精简到 ~40 个
- 删除了 3 个辅助 dataclass：ToolSelectionError、FalseTriggerRecord、MissedTriggerRecord
- 删除原因：可推导、标注依赖、重复/模糊/噪声

## F6：待确认点（全部已确认）

1. ~~LLM 代理层是否已返回 TTFT/TPS？~~ → **已确认：LLM 返回值中无法获取，由客户端侧用 `perf_counter()` 自行计时。在 `llm.request_end` 事件中携带 `ttft_ms` 和 `tps` 字段**
2. ~~配置中是否有模型定价信息？~~ → **已确认：无价格信息。`cost_usd` 暂返回 0.0，`cost_by_model` 返回空 dict，字段保留为未来预留**
3. ~~`hit` 判定标准？~~ → **已确认：返回非空结果即算命中**
4. ~~`index_size` / `index_size_mb` 来源？~~ → **已确认：记忆系统重构前暂不采集，当前返回 0 / 0.0。字段保留，重构时再接入**
