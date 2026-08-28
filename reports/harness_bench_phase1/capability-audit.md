# Harness-Bench Phase 1 Capability Audit

## 固定范围

- Harness-Bench: `Qihoo360/harness-bench@1025086a446653702b80cfb48babbeec35db6b2c`
- dotClaw: `aandbcct/dotClaw@1a27ceef6e2739339f89660be23af21c771c9b28`
- 类别严格取自官方 `task.yaml`：Workspace, Tool Use & Multimodal Operations（15 题）与 Long-running Autonomy & State Adaptation（11 题）。
- “公平执行”表示在相同模型、endpoint、参数、timeout、预算、fixture、hook、oracle 和机器条件下，任务信息对两边一致；不表示预判能够得分。
- NanoBot 已固定为 `nanobot-ai==0.3.0`，并使用与 dotClaw 相同的 qwen 模型、endpoint、temperature 0.7、max tokens 4096 和 15 轮工具预算。表中的能力判断来自上游协议审计；真实得分以正式运行结果为准。

## 逐题审计

| task_id | category | 主要要求 | 需要的 Tool / Runtime 能力 | dotClaw 当前能力 | NanoBot 静态能力 | 可公平执行 | 缺失能力或限制 |
|---|---|---|---|---|---|---|---|
| 001-file | Workspace/Tool | 读取文本并写单整数 | workspace read/write | 具备 | 具备 | 是 | 无 |
| 002-exec | Workspace/Tool | shell 算术、basename、pipeline | shell + workspace write | 具备 | 具备 | 是 | Windows shell 语义需在 smoke 中验证 |
| 003-browser | Workspace/Tool | 读取本地 HTTP 页面 marker | local HTTP 或 curl | 可用 shell 访问本地服务 | 具备 | 是 | dotClaw 受控 web 工具不接受任意 localhost；应走已允许的 shell，不放宽 network policy |
| 006-access-bilibili | Workspace/Tool | 本地 mock 页面提取标题和 URL | local HTTP + text extraction | 具备 | 具备 | 是 | 同 003 |
| 008-image-recognize | Workspace/Tool | 识别两张本地图像 | vision input | 不具备可把本地图片注入模型的正式工具链 | 取决于所选模型与 NanoBot 多模态路径 | 是，失败保留 | dotClaw 缺少正式本地图像视觉输入能力 |
| 013-image-edit | Workspace/Tool | 风格迁移、换背景并产出 PNG | image edit/generation | 不具备 | 具备 image generation 工具；编辑质量仍取决于配置 | 是，失败保留 | dotClaw 缺少图像生成/编辑工具；不得为 benchmark 临时补题解 |
| 020-archive-checksum | Workspace/Tool | 解压、SHA-256、manifest、mismatch | shell/archive/hash + JSON | 具备 | 具备 | 是 | 无 |
| 021-batch-rename-transform | Workspace/Tool | 递归转换 TXT/CSV/JSON 与冲突命名 | shell/script + file IO | 具备 | 具备 | 是 | 复杂产物受 15 轮循环上限影响 |
| 022-local-rest-api-summary | Workspace/Tool | 分页、429/503 重试、join | local HTTP + retry + JSON | 具备 shell 路径 | 具备 | 是 | 无 |
| 023-web-form-extraction | Workspace/Tool | CSRF/hidden field、提交与确认 | HTTP cookies/forms + file IO | 具备 shell/script 路径 | 具备 | 是 | 无 |
| 077-archive-manifest-defense | Workspace/Tool | 嵌套归档、安全解压、拒绝清单 | safe archive inspection + hashing | 具备通用 shell/script；Policy 仍禁止 workspace 逃逸 | 具备 | 是 | 不允许关闭路径逃逸防护 |
| 078-local-api-cursor-retry-ledger | Workspace/Tool | cursor、重试、cursor expiry、checkpoint | local HTTP + retry/state | 具备 shell/script 路径 | 具备 | 是 | tunnel 若非 localhost，仍只能按 task URL 使用 shell；不得开放通用 web policy |
| 079-smallfile-batch-reject-ledger | Workspace/Tool | 多格式小文件批处理、去重、reject | recursive IO + scripting | 具备 | 具备 | 是 | 轮次/上下文预算可能成为得分限制 |
| 080-schema-roundtrip-conversion | Workspace/Tool | MD/CSV/JSON/YAML schema roundtrip | parsers + deterministic serialization | 具备 shell/Python 路径 | 具备 | 是 | 无 |
| 081-local-html-dom-form-extract | Workspace/Tool | DOM/hidden fields/form flow | local HTTP + DOM parsing | 具备 shell/script 路径 | 具备 | 是 | 无 |
| 007-session-memory | Long-running | 两轮同 Session 记住 secret，round1 不落盘 | multi-round conversation persistence | 具备；适配入口以同一 Harness session 映射到持久化 Session | 具备 | 是 | 必须用两个独立进程 smoke 证明，不以单进程测试替代 |
| 014-task-decomposition | Long-running | 拆分、顺序执行、进度与最终报告 | planning + workspace writes | 具备 | 具备 | 是 | 15 轮循环上限为原生 harness 差异 |
| 057-interruption-resume | Long-running | 保留 round1 state，round2 patch/resume | multi-round + durable workspace/session | 具备 | 具备 | 是 | 无 |
| 058-multiday-project-state | Long-running | 三轮累计状态、决策反转审计 | three-round persistence + JSON/CSV/MD | 具备 | 具备 | 是 | prompt 内“一次工具调用写完”属于官方任务信息，两边一致 |
| 059-event-update-replan | Long-running | 两轮原计划/修订/diff | multi-round state + replanning | 具备 | 具备 | 是 | 无 |
| 060-task-cancellation-cleanup | Long-running | round2 权威取消并清理 tmp | multi-round + delete within workspace | 具备；workspace 内删除可经 shell | 具备 | 是 | 仅允许 workspace 内清理，Policy 逃逸仍 DENY |
| 061-periodic-status-rollup | Long-running | 至少轮询 25 秒、异步注入、去重 | long-running shell/poll + async fixture hook | 具备；单次 process 工具上限 60 秒 | 具备 | 是 | 必须验证 hook 注入期间 subprocess 未提前退出 |
| 103-policy-update-replan-diff | Long-running | 两轮 policy 变更、稳定 decision id、diff | multi-round state + planning | 具备 | 具备 | 是 | 无 |
| 104-async-ops-window-rollup | Long-running | 至少轮询 30 秒、异步注入、拓扑归因 | long-running poll + async hook | 具备；60 秒 process 上限覆盖最低轮询 | 具备 | 是 | 必须验证 hook 与 Windows 文件可见性 |
| 105-partial-batch-resume-ledger | Long-running | 部分失败后幂等恢复、不重处理 | multi-round durable state + audit | 具备 | 具备 | 是 | 无 |
| 106-release-approval-gate-plan | Long-running | 识别 blockers，只产出待审批计划 | evidence synthesis + safety | 具备 | 具备 | 是 | 任务本身不要求执行生产副作用 |

## Benchmark permission policy

- `workspace.read`: ALLOW，仅解析到当前 Harness-Bench workspace root。
- `workspace.write`: ALLOW，仅解析到当前 Harness-Bench workspace root；路径逃逸、符号链接/联接点逃逸与 denied path 仍由正式 Capability/Policy 链拒绝。
- `process.exec`: ALLOW，使无人值守任务可使用正式 process tool；不绕过 Tool Executor、Capability、Policy、Journal 或 Runtime。
- `network.http`: DENY。任务提供的 mock HTTP 服务通过 shell 访问，避免把任意公网 web tool 全局放开。
- `mcp.connect` / `mcp.call`: DENY；临时 profile 不加载用户私人 MCP。
- 不存在“benchmark 一律 approve”。本 profile 只是把 workspace 内文件操作与 process 从 ASK 收敛为显式 ALLOW；任何 Policy DENY 仍然有效。

## 当前结论

26 题均保留在正式范围内。静态审计判断 24 题具备协议级可执行条件，008 与 013 对 dotClaw 是已知缺失能力，但仍进入正式运行并保留结果。File、Exec、Multi-round 三类 smoke 已贯通；Multi-round smoke 由两个独立 dotClaw 进程复用同一持久化 Session。NanoBot 的 File smoke 也已贯通。正式 26×2 运行使用固定的同模型配置，不把 smoke 结果并入正式样本。
