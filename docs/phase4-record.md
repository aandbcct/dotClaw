# dotClaw Phase 4 记忆系统 — 开发日志

> 本文件记录 P4 记忆系统的开发进度、已知问题、修复记录和后续规划。
> 架构文档见 `docs/arch/memory-architecture.md`。

## 变更日志

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1.0 | 2026-06-01 | 初始版本，基于 P4 设计文档 + 现有代码梳理 |
| v1.1 | 2026-06-01 | 修复 6 个 bug：sync 实现、时间衰减实现、EmbeddingCache 注入、MemoryProvider 注册、flush_threshold 统一、FTS5 trigram 短查询修复 |

---

## v1.0 — 2026-06-01

### 变更内容

- 基于 P4 设计文档 + 现有代码梳理，产出架构文档 `docs/arch/memory-architecture.md`

### 发现的问题

| # | 问题 | 模块 | 类型 | 说明 |
|---|------|------|------|------|
| 1 | `sync()` 未实现（pass） | `manager.py` | 功能缺失 | 文件变更检测 → 分块 → embedding → 索引 全链路未实现 |
| 2 | 时间衰减 `_apply_temporal_decay()` 空壳 | `manager.py` | 功能缺失 | `pass`，日记忆和长期记忆无衰减区分 |
| 3 | `EmbeddingCache` 未创建/传入 | `main.py` | 初始化缺陷 | `main.py` 中未创建 `EmbeddingCache` 实例，未传给 `MemoryManager` |
| 4 | `MemoryProvider` 未注册到 PromptBuilder | `main.py` | 初始化缺陷 | `MemoryProvider()` 被注释，记忆检索结果不注入 system prompt |
| 5 | `flush_threshold` 配置不一致 | `settings.py` | 配置冲突 | `MemoryConfig` 默认 `5`，设计文档 `20`，`_raw_to_config()` 硬编码 `20` |
| 6 | `_raw_to_config()` 缺少 P4 字段映射 | `config/settings.py` | 配置断裂 | `vector_weight`、`keyword_weight`、`sync_on_search`、`temporal_decay_half_life_days`、`dream_schedule` 等字段不会被加载到 `MemoryConfig`，YAML 配置写了也白写 |
| 7 | FTS5 trigram 短中文查询会抛异常 | `storage.py` | 健壮性 | trigram tokenizer 需要至少 3 个字符，短中文查询直接 `MATCH ?` 会报错 |

---

## v1.1 — 2026-06-01

### 变更内容

修复 4 个 bug：sync 实现、时间衰减实现、EmbeddingCache 注入、MemoryProvider 注册；移除 tiktoken 相关引用

### 已修复（来自 v1.0 问题）

| # | 原问题 | 修复内容 | 涉及文件 |
|---|--------|----------|----------|
| ✅ v1.0-1 | `sync()` 未实现 | 实现 hash 变更检测 → 分块 → batch embedding → UPSERT 写入完整链路 | `manager.py` |
| ✅ v1.0-2 | 时间衰减空壳 | 实现指数衰减公式：`score *= exp(-age_days * ln(2) / half_life)` | `manager.py` |
| ✅ v1.0-3 | EmbeddingCache 未传入 | `main.py` 中创建 `EmbeddingCache()` 并传入 `MemoryManager` | `main.py` |
| ✅ v1.0-4 | MemoryProvider 未注册 | `main.py` 中 `MemoryProvider()` 加入 PromptBuilder 列表 | `main.py` |
| ✅ v1.0-5 | flush_threshold 配置不一致 | `MemoryConfig` dataclass 默认值统一为 `20` | `settings.py` |
| ✅ v1.0-7 | FTS5 trigram 短中文查询异常 | 加 `len(query) >= 3` 长度检查，短查询走 unicode61 或 LIKE 降级 | `storage.py` |

### 遗留问题（来自 v1.0 未修复）

| # | 原问题 | 说明 |
|---|--------|------|
| v1.0-6 | `_raw_to_config()` 缺少 P4 字段映射 | 仍未修复，见下方 v1.1 已知问题 #1 |

### v1.1 新发现的已知问题

| # | 问题 | 优先级 | 模块 | 类型 | 说明 |
|---|------|--------|------|------|------|
| 1 | `_raw_to_config()` 缺少 P4 字段映射（继承自 v1.0-6） | **高** | `config/settings.py` | 配置断裂 | YAML 中的 `vector_weight`、`keyword_weight`、`sync_on_search`、`temporal_decay_half_life_days`、`dream_schedule` 等字段不会被加载到 `MemoryConfig`，配置写了也白写 |
| 2 | `_rebuild_fts()` 只重建一个索引 | **高** | `storage.py` | 逻辑 bug | `chunks_fts` rebuild 失败才重建 `chunks_fts_trigram`，正确逻辑是两个索引各自 try/except 独立重建 |
| 3 | `EmbeddingProvider` 同步调用阻塞事件循环 | **高** | `embedding.py` | 性能/正确性 | `embed_batch()` 内部 `OpenAI.embeddings.create()` 是同步 HTTP，在 `async def sync()` 中被调用会阻塞 asyncio 事件循环。应改用 `AsyncOpenAI` 或 `asyncio.to_thread()` 包装 |
| 4 | numpy import 无降级路径 | **高** | `storage.py` | 健壮性 | `import numpy as np` 在模块顶层，numpy 未安装时 `storage.py` 整体无法导入（设计文档要求降级为纯 Python 余弦相似度） |
| 5 | `save_chunks_batch()` 每次全量重建 FTS5 | **中** | `storage.py` | 性能 | 每次批量写入都触发 `INSERT INTO chunks_fts(...) VALUES('rebuild')`，即使只更新了一个文件的几个 chunk 也会全表重建。应改为增量写入 FTS5 |
| 6 | `DeepDream` 覆盖写 MEMORY.md 无备份 | **中** | `dream.py` | 数据安全 | `write_text(distilled)` 完全覆盖，LLM 蒸馏输出丢失已有记忆时无法恢复。需要写入前备份旧版本 |
| 7 | `DeepDream` 实例未持久化 | **中** | `main.py` | 设计缺陷 | `dream = DeepDream(...)` 是局部变量，`/dream` 命令每次 `new DeepDream()` 重新实例化。虽然功能上无影响（state 从文件读取），但不符合统一初始化模式 |
| 8 | CJK 检测范围不足 | **低** | `storage.py` | 精确性 | 仅检测 `\u4e00-\u9fff` 基本区，遗漏日文假名、CJK 扩展区。短中文+英文混合查询可能走错 FTS5 分支 |
| 9 | `sync_on_search` 无递归防护 | **低** | `manager.py` | 防御性 | 当前 `sync()` 内部不触发 `search()` 不会递归，但缺少显式防护（如 `_syncing` 标志），后续改动可能引入递归风险 |

### 后续规划（不在 v1.1 范围）

| # | 限制 | 影响 | 规划 |
|---|------|------|------|
| 1 | 向量检索全表扫描 | >1万 chunk 时延迟明显 | 引入 HNSW/ANN 索引 |
| 2 | 记忆注入无 token 预算 | 长记忆挤占对话上下文 | 增加 `memory_budget_tokens` 配置 |
| 3 | 蒸馏一次性喂所有未蒸馏日记 | 多日累积后 token 超限 | 分片蒸馏 |
| 4 | FTS5 trigram 短中文查询走 LIKE 降级（<3 字符） | 单/双字中文检索精度下降 | 设计权衡，trigram tokenizer 限制 |
| 5 | tiktoken 未接入 TextChunker | 分块 token 估算精度低 | 迁移到 `cl100k_base` |

---

*本文件由 dotClaw 开发工程师维护。每次架构变更、bug 修复、新问题发现后请同步更新。*
