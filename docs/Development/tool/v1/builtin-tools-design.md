# dotClaw 通用内置工具集设计（Tool v1 扩展）

> 日期：2026-07-22
> 状态：设计已与用户逐条确认（范围 / 出口架构 / 求值方案 / 存储 / 数据源）
> 关联：阶段五安全审计已闭环（4 轮 P0/P1 修复，全量 325 passed / 0 skipped）

## 1. 目标与范围

为 agent harness 补充一组通用 builtin 工具，补齐"计算 / 任务 / 文本 / 时间 / 网络"能力。
本轮落地 **7 类**：

- 本地 / 安全：`calculator`、`todo`、`text`(parse/extract)、`datetime`
- 网络（经 EgressGateway）：`web.search`、`web.fetch`、`weather`

设计约束（沿用阶段五已确立的安全模型）：

- 所有工具经 `@tool` 声明，由 `ToolDiscovery` 自动发现，无需改动注册表。
- 每条工具走固定安全链路：`Broker 校验 → Policy → (ask 审批) → Handler → Journal`。
- 网络工具必须受 `network.http` 出口裁决（host 白/黑名单 + fail-closed），密钥不进 Journal。
- 文件类工具（todo / text 落盘）复用已修的 workspace 路径解析（Broker 校验路径 = handler 实际落点）。

## 2. 网络出口层（新基础设施）

新增 `src/dotclaw/tools/egress.py`：

- `EgressGateway`：封装 `httpx.AsyncClient` 单例，提供 `get(url, **kw)` / `post_json(url, json, **kw)`。
  按 service 名注入 API key（从 env 解析，缺失时抛 `MissingSecretError`，**不写入 Journal/trace**）。
- `PolicyScope` 扩展：新增 `allowed_hosts: list[str]` / `denied_hosts: list[str]`（默认空）。
- `PolicyEngine._evaluate_one` 新增 `network.http` 分支：
  - `request.host` 命中 `denied_hosts` → `DENY`；
  - 若配置了 `allowed_hosts` 且 `request.host` 不在其中 → `DENY`；
  - profile 决策 `DENY`/`ASK` → 相应；否则 `ALLOW`。**fail-closed**（无明确 ALLOW 即拒绝）。
- 注入方式：`ToolExecutor` 持有 `egress_gateway`（类比 `policy_engine`）；`ToolContext` 新增 `egress` 字段，由 `_build_context` 透传。
- 网络工具 `policy=ToolPolicy.NETWORK`，且声明 host 承载参数（复用 `ToolDefinition.path_param` 思路，新增 `host_param`，默认 `"url"`；`web.search`/`weather` 声明其 provider endpoint 参数），使 Broker 能抽取 host 做裁决。

配置：`config.tools.network.secrets = {search: "SEARCH_API_KEY"}`（weather 用 Open-Meteo 免密钥）。
密钥值运行时从 `os.environ[ENV_VAR]` 解析。

## 3. 各工具实现要点

### 3.1 calculator（policy=None）
- 模块 `builtin/calc_tool.py`，工具名 `builtin.calculator.evaluate`。
- 求值用 **simpleeval**（新增依赖）：构造 `SimpleEval()` 时 `names=None`、禁用 `__import__`，
  仅保留白名单数学函数（`sin/cos/tan/sqrt/abs/log/exp/pow/round/floor/ceil` 等）。
- 输入：表达式字符串；输出：数值或格式化结果。超长/非法表达式返回结构化错误（非异常崩溃）。
- 安全：绝不 `eval`/`exec`；simpleeval 本身禁止属性/下标/名称访问。

### 3.2 todo（policy=WORKSPACE_WRITE / WORKSPACE_READ）
- 模块 `builtin/todo_tool.py`，工具：`builtin.todo.add` / `.list` / `.done` / `.clear`。
- 存储：`<workspace_root>/.dotclaw/todo.json`（复用 capability 的 workspace 解析，落点与校验一致）。
- 数据结构：`[{"id": int, "text": str, "done": bool, "created_at": iso}]`。
- `add` 追加；`list` 列出（含完成态）；`done` 按 id 标记；`clear` 清空已完成或全部（参数控制）。
- 文件读写经 Broker 校验，逃逸被 DENY。

### 3.3 text（policy=None）
- 模块 `builtin/text_tool.py`：
  - `builtin.text.parse`：输入格式(JSON/YAML/CSV) + 文本 → 结构化（用 `pyyaml`/`csv` 标准库；JSON 用内置）。
  - `builtin.text.extract`：正则模式 + 文本 → 匹配列表（限制回溯/长度，防 ReDoS）。

### 3.4 datetime（policy=None）
- 模块 `builtin/datetime_tool.py`：`builtin.datetime.convert`：时区换算（依赖 `zoneinfo` 标准库）、
  相对时间解析（"3小时前" → 时间戳）、自然语言日期格式化。输入含目标时区/格式，输出 ISO 或指定格式。

### 3.5 web.search（policy=NETWORK）
- 模块 `builtin/web_tool.py`，工具 `builtin.web.search`。
- 通过 `ToolContext.egress` 调用可配置 search provider（默认 `SEARCH_API_KEY` 对应的适配器；
  适配器模式便于换源：Serper/Brave/Tavily 等）。
- 入参 `query` + 固定 `endpoint` 参数（provider base URL，供 Broker 抽 host 裁决）；输出前 N 条结果摘要。

### 3.6 web.fetch（policy=NETWORK）
- 同 `web_tool.py`，`builtin.web.fetch`：`url` 直接入参（host 受白/黑名单裁决），
  抓取后返回正文（截断到合理长度，避免巨型响应）。

### 3.7 weather（policy=NETWORK）
- 同 `web_tool.py`，`builtin.weather.get`：调用 **Open-Meteo**（免密钥，立即可测）。
- 入参 `location`（城市名或 lat/lon）+ 天数；先经地理解析（Open-Meteo geocoding，同城免密钥）再取预报。
- `endpoint` 参数声明 provider host，供 Broker 裁决。

## 4. 数据流与安全链路

```
LLM → ToolCall(name, args)
  → ToolExecutor._run_chain
      → CapabilityBroker.resolve(def, args, workspace_root)
          · 文件类：normalize_workspace_path（已修）
          · 网络类：_network_request → host（新增 host 白/黑名单裁决）
      → PolicyEngine.evaluate(requests, _effective_scope(agent_id))
          · network.http 分支 fail-closed
      → (ASK → 审批)
      → Handler(args, ToolContext(egress=...))   # 网络工具拿 EgressGateway
      → Executor 回填绝对路径（文件类，已修）
      → Journal（密钥不记录）
```

- Agent 隔离：网络策略同样走 `_effective_scope(agent_id)`（per-run 冻结），子 Agent 不继承主 Agent 规则。

## 5. 错误处理

- calculator：非法表达式 → `ToolResult.from_error(code=INVALID_ARGUMENT)`，不抛裸异常。
- todo：workspace 逃逸 → `POLICY_DENIED`；JSON 损坏 → 重建空列表并告警。
- 网络：host 被拒 → `POLICY_DENIED`；密钥缺失 → `MissingSecretError` → `ToolResult` 结构化错误；
  超时/HTTP 非 2xx → 结构化错误（含状态码，不含密钥）。
- ReDoS 防护：正则编译限长、结果数上限。

## 6. 测试策略

- calculator：合法/非法表达式、白名单函数、禁用属性/导入（尝试注入 `os.system` 应被拒）、超长输入。
- todo：增/列/完成/清空；workspace 外路径被 DENY；落点与校验一致（复用审计2 思路）。
- text/datetime：各格式解析、正则提取、时区换算、相对时间。
- 网络（关键，需真实出口）：
  - EgressGateway 单测（mock httpx）：host 白/黑名单裁决、密钥注入不进日志、缺失密钥报错。
  - `weather` 对 Open-Meteo 的真实集成测试（免密钥，可离线 mock 或联网 smoke）。
  - `web.search`/`web.fetch`：用 mock provider + host 裁决测试；真实 key 走 env，CI 缺 key 时 skip。
- 全部在 `.venv`（pytest-asyncio / aiofiles / openai 已装）跑，确保异步测试真实执行。

## 7. 文件改动清单（预计）

新增：
- `src/dotclaw/tools/egress.py`（EgressGateway + 密钥）
- `src/dotclaw/tools/builtin/calc_tool.py`
- `src/dotclaw/tools/builtin/todo_tool.py`
- `src/dotclaw/tools/builtin/text_tool.py`
- `src/dotclaw/tools/builtin/datetime_tool.py`
- `src/dotclaw/tools/builtin/web_tool.py`（search/fetch/weather）
- 测试：`tests/tools/test_builtin_*.py`

修改：
- `src/dotclaw/tools/policy.py`：`PolicyScope` 增 `allowed_hosts/denied_hosts` + `network.http` 裁决分支
- `src/dotclaw/tools/base.py`：`ToolContext` 增 `egress`；`ToolDefinition` 增 `host_param`（复用 path_param 模式）
- `src/dotclaw/tools/capability.py`：`resolve` 网络分支抽取 host 并应用白/黑名单
- `src/dotclaw/tools/executor.py`：注入 `egress_gateway`，`_build_context` 透传 `egress`
- `src/dotclaw/agent/factory.py`：构建 `EgressGateway` 并注入 executor；`config` 增 `tools.network.secrets`
- `src/dotclaw/config/settings.py`：解析 `tools.network.secrets` / `tools.network.allowed_hosts`

依赖：新增 `simpleeval`（及可选 `pyyaml` 增强 YAML 支持，CSV/JSON 用标准库）。
