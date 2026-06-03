# Phase 3 开发计划二次审计报告（v2）

> 审计人：开发架构师
> 审计日期：2026-05-30
> 审计对象：`docs/phase3-roadmap.md` v2 — 修正后版本
> 上一轮审计：`docs/phase3-roadmap-review.md`（11 项修正，已全部回执）

---

## 审计结论：✅ 通过，可启动开发

v2 版本已修正 v1 审计发现的所有 3 个阻塞级缺陷和 5 个设计缝隙。文档质量达到了可开发标准。存在 **2 个建议级残留问题**（不阻塞），开发人员在编码时注意即可。

---

## 一、v1 修正验证

| # | v1 审计要点 | v2 修正位置 | 验证结果 |
|---|-----------|-----------|---------|
| 1 | request_id 生命周期错误 | §3.3：`new_request()` 由 `_build_context()` 调用 | ✅ 已修正 |
| 2 | config.agent.rules 未定义 | §2.4（yaml + python 示例）、§3.4（AgentConfig 新字段） | ✅ 已修正 |
| 3 | trim() 配对保护算法空洞 | §3.2：1:N assistant→tool 完整伪代码 | ✅ 已修正 |
| 4 | 双日志系统技术债 | §2.2 ⚠️ 标记、§6.7 P5 合并目标 | ✅ 已修正 |
| 5 | message_utils 归属不一致 | §2.1 → `agent/message_utils.py`，附完整理由 | ✅ 已修正 |
| 6 | Token 估算对中文不友好 | §3.2：中英文差异化 `_estimate_tokens()` | ✅ 已修正 |
| 7 | init 参数膨胀 | §2.5 AgentServices 前瞻建议 | ✅ 已修正 |
| 8 | Rules/Role 边界模糊 | §2.4 P4 跨 section 去重提示 | ✅ 已修正 |
| — | AgentResult 无消费者说明 | §2.5 消费者说明 | ✅ 已修正 |
| — | 测试计划缺失 | §5.3 新增 8 场景 | ✅ 已修正 |
| — | 中文 token 安全边界 | §6.8 20% 安全边界建议 | ✅ 已修正 |

---

## 二、残留问题（🟢 建议级，不阻塞开发）

### 建议 1：`RulesProvider` 无法从 `AgentContext` 获取 `rules`

**位置**：§2.4 — RulesProvider 数据来源描述 vs AgentContext 字段列表

**问题描述**：

v2 已在 `AgentConfig` 中增加了 `rules: str = ""` 字段（§3.4），但在 `AgentContext` 的字段定义（§2.3）中**没有对应的 `rules` 字段**。

| 组件 | 状态 | 问题 |
|------|------|------|
| `AgentConfig.rules` | ✅ 已定义 | `rules: str = ""` |
| `config.yaml agent.rules` | ✅ 已定义 | `rules: ""` |
| `AgentContext.rules` | ❌ 缺失 | 字段列表中无 `rules` |

`DataProvider.provide(context: AgentContext)` 只接收 AgentContext 一个参数。如果 `rules` 不在 AgentContext 中，`RulesProvider.provide()` 无法获取到规则文本。

§2.4 表格中 RulesProvider 的"数据来源"列写的是"`context.system_prompt` 中的 `config.agent.rules` 字段"，这个描述本身就是矛盾的——`context.system_prompt` 是已经拼好的 prompt 字符串，不包含 `rules` 子字段。

**建议修改**：

在 AgentContext 的字段列表（§2.3）中增加：

```
| `rules` | `str` | `config.agent.rules`，行为规则文本（空字符串时 RulesProvider 跳过） |
```

并在 `_build_context()` 的伪代码中补充：`rules=self.config.agent.rules`。

---

### 建议 2：`_build_messages()` 伪代码中 `self._message_utils` 暗示注入，但 message_utils 是纯函数模块

**位置**：§2.5 — `_build_messages()` 伪代码

**问题描述**：

§2.5 的伪代码写为：

```python
messages = self._message_utils.trim(messages, ...)
messages = self._message_utils.clean(messages)
```

`self._message_utils` 的命名暗示它是注入到 AgentLoop 的实例属性。但 `message_utils` 被定义为纯函数模块（§2.1）：函数无内部状态、不依赖实例。用模块级 import + 直接调用更合理：

```python
from . import message_utils

messages = message_utils.trim(messages, ...)
messages = message_utils.clean(messages)
```

**影响**：不影响功能，但如果开发人员照伪代码写 `self._message_utils` 会导致 NameError。

**建议修改**：将 §2.5 伪代码中的 `self._message_utils.trim` 改为 `message_utils.trim`，或补充说明 `message_utils` 为模块级 import。

---

## 三、v2 设计亮点（新增）

以下设计点是从 v1 到 v2 的改进中体现出来的：

1. **§6.8 安全边界量化**：从"中文场景 8000 偏小"这个定性建议，变成了"预留 20% 安全边界"的可操作指导——可测试、可验证
2. **§3.2 trim() 极端情况处理**：增加"单条消息超过 max_tokens 时记录 warning 并保留该消息"——避免死循环裁剪
3. **§5.3 测试场景精确化**：8 个场景都有明确的验证内容（如"验证 FrozenInstanceError"、"验证估算值在合理范围"）——可直接转化为测试代码
4. **§7 文件清单扩充**：从"7 新增 + 2 修改"扩展到"7 新增 + 4 修改 + 1 新增测试"，且每个修改都标注了具体变更内容——可跟踪

---

## 四、长期发展性复查

| 维度 | v1 评分 | v2 评分 | 变化说明 |
|------|--------|--------|---------|
| P4 记忆注入兼容 | ✅ 好 | ✅ 好 | 无变化 |
| P5 工具动态注册兼容 | ✅ 好 | ✅ 好 | 无变化 |
| P7 Skill 注入兼容 | ✅ 好 | ✅ 好 | 无变化 |
| P10 多渠道兼容 | ⚠️ 中等 | ✅ 好 | v2 增加了 AgentResult 消费者说明（P10 Web Channel 将使用），消除了"没人用"的疑虑 |
| Scheduler 触发兼容 | ✅ 好 | ✅ 好 | 无变化 |
| 双日志系统合并路径 | ❌ 未提及 | ✅ 明确 | §6.7 P5 合并目标，消除了永久技术债的担忧 |

---

## 五、最终结论

**Phase 3 开发计划 v2 —— 审计通过。**

2 个残留建议均不阻塞开发，开发人员可在编码时注意并自行处理：

- 建议 1（AgentContext 补充 `rules` 字段）：在写 `_build_context()` 时自然会发现并补充
- 建议 2（伪代码调用方式）：Python import 的使用方式直觉上就会是模块级调用

> **可以启动 Phase 3 开发。**
