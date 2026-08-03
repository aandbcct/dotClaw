# dotClaw PR5 开发计划：TraceToEvalCaseDraft 与 Dataset

## 1. 定位与边界

### 唯一目标

将终态完整 RunTrace 生成可人工审阅的 Draft，并把确认后的 Case 保存为版本化 JSON Dataset。

```text
RunTrace → TraceToEvalCaseDraft → drafts/<draft_id>.draft.json
         → Channel 审阅 → confirm_draft() → cases/<case_id>.json
```

包含 Draft 转换、脱敏、目录 Dataset、Channel 用 `EvalCaseDraftService`。不执行 Eval、Playback、Gate、真实 LLM 或自动审核。

## 2. 文件与接口

新增：

```text
src/dotclaw/eval/
├── draft.py
├── dataset.py
├── draft_service.py
└── redaction.py

tests/eval/
├── test_draft.py
├── test_dataset.py
├── test_draft_service.py
└── test_redaction.py
```

修改：

```text
src/dotclaw/bootstrap/application_host.py  # 以实际 ApplicationHost 组合根文件为准
src/dotclaw/channel/...                   # 仅接入 Draft Service，不直接访问文件
```

公开接口：

```python
def trace_to_eval_case_draft(trace: RunTrace) -> EvalCaseDraft: ...

class EvalCaseDraftService:
    async def load_draft(self, dataset_name: str, draft_id: str) -> EvalCaseDraft: ...
    async def save_reviewed_draft(
        self, dataset_name: str, draft_id: str, draft: EvalCaseDraft
    ) -> EvalCaseDraft: ...
    async def confirm_draft(
        self, dataset_name: str, draft_id: str, case_id: str
    ) -> EvalCase: ...
```

- 服务是 Channel 的窄应用入口，不属于 Runtime 主流程，不新增 Runtime Port 或通用审核状态机。
- `dataset_name`、`draft_id`、`case_id` 必须经与 Runtime 文件仓储一致的单一路径片段校验；服务只能操作配置的 Dataset 根目录。
- 读取不存在 Draft 抛 `FileNotFoundError`；确认已存在 Case 抛 `FileExistsError`；待审核 Draft 确认抛明确 `ValueError`。

## 3. 数据与存储规则

目录即 Dataset：

```text
datasets/<dataset_name>/
├── drafts/<draft_id>.draft.json
└── cases/<case_id>.json
```

- Case / Draft 都有独立 schema version，JSON 读写严格校验；Case 加载按文件名稳定排序。
- `EvalCaseDraft` 记录 draft_id、source_run_id、source_record_hash、source_trace_schema_version、候选 EvalCase 载荷、`requires_review`、`confirmed_case_id?`。
- 一个终态 `is_partial=false` Trace 才可转换；Draft 从 RunTrace 提取 input、冻结 Agent Policy / Context、Conversation、LLM / Tool / Approval / Delegation Fixture，以及基础 Expectation、Token / 调用次数基线。
- 生成 Draft 不等于生成 Case。Runner 只读取 `cases/`，永不读取 `drafts/`；确认成功后保留 Draft 并写入 confirmed_case_id，重复确认失败。
- 本 PR 不建 Manifest、数据库、Registry、Dataset Repository 或后台审核队列。

## 4. 脱敏与人工审核

`redaction.py` 递归处理 Draft 可序列化载荷：

- 字段名命中 `token`、`api_key`、`password`、`authorization`、`cookie`、`secret` 时替换值；
- 识别 Bearer Token、私钥块和当前项目约定的常见 API Key 格式；
- LLM 回复可作为 Playback Fixture 保存，但必须经过同一脱敏器；普通 LLM 回复本身不设置 `requires_review`；
- 无法安全处理的载荷设置 `requires_review=true`。Channel 通过 `save_reviewed_draft()` 保存审阅后的内容并显式清除该标记；`confirm_draft()` 再次校验标记，不信任客户端仅传确认参数；
- 自动脱敏是已知模式保护，不宣称识别任意自然语言敏感信息，也不猜测如何生成替代 Fixture。

## 5. 原子性与 Channel 接入

- Draft、Case 均使用与现有文件仓储相同的临时文件 + 原子替换方式写入。
- `confirm_draft()` 先加载并验证 Draft、检查 Case 目标不存在、原子写 Case，最后原子回写 Draft 的 confirmed_case_id；若最后一步失败，Case 已存在时后续确认检测到该 Case 并报告需人工处理，不覆盖已有 Case。
- ApplicationHost 构造 Dataset 根路径和 `EvalCaseDraftService` 后注入 Channel；Channel 只处理命令 / 展示与服务结果，不直接读写 JSON。

## 6. 测试与验收

必须覆盖：

1. 终态 Trace 的稳定 Draft、部分 Trace 被拒绝；
2. Tool、Approval、Delegation、Context / Policy / Token 基线均被提取；
3. 多个 Draft 共存、稳定加载排序、非法路径片段、schema 不兼容；
4. 敏感字段名、凭证模式、普通 LLM 回复、无法安全处理载荷与 requires_review 门槛；
5. 审阅保存、Case 原子创建、重复 case_id、重复确认和确认中断后的可见状态；
6. Channel 经服务加载 / 审阅 / 确认，且没有直接文件访问；
7. `pytest -q tests/eval tests/trace tests/runtime_v2`、`compileall src`、`git diff --check` 通过。

## 7. 推荐提交顺序与完成门槛

1. **Draft / Case JSON 模型与目录加载**；
2. **Trace 转 Draft 与基础 Expectation**；
3. **脱敏与人工审核门槛**；
4. **Draft Service、原子确认与 Channel 注入**；
5. **端到端 Dataset 回归**。

完成后人工可以将历史完整 Trace 变为可执行 EvalCase；尚不批量执行、比较或阻断 CI。
