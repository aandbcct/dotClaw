# 阶段 0 基线记录（ApplicationHost 收口）

> 本文件冻结 `tests/runtime_v2` 在 ApplicationHost 收口迁移**开始前**的有效行为基线，
> 供后续阶段（阶段 1–5）衡量回归与契约进展。依据《runtime-application-host收口开发计划.md》§3。

## 环境

- 解释器：`python3.13`（受管虚拟环境 `binaries/python/envs/default`）
- 依赖：`pip install -e ".[dev]"`（含 `pytest`、`pytest-asyncio`、`aiofiles`、`pyyaml`、
  `openai`、`rich`、`numpy`、`pydantic`、`mcp`、`tiktoken` 等）
- pytest 配置：根 `pyproject.toml` 中 `asyncio_mode = "auto"`、`pythonpath = ["src"]`、
  `addopts = "-m 'not legacy'"`
- 命令：`pytest tests/runtime_v2 -q -p no:cacheprovider`

## 基线结果（执行日期 2026-07-22）

```
75 passed in ~7s
```

- **通过：75**
- **失败：0**
- **xfail（阶段0契约，待迁移）：见 `test_phase0_contracts.py`**

## 历史失败隔离说明

- 本次记录前曾出现 57 个失败，根因为测试环境缺失 `pytest-asyncio` 插件（全部 `async def`
  测试报 "async def functions are not natively supported"），并非代码回归。补齐 dev 依赖后
  基线为 **75 passed / 0 failed**，与本次收口迁移无关，已隔离。
- 阶段 0 不修改任何生产代码，仅新增契约测试；既有 Runtime 核心测试在迁移全程须维持通过。

## 阶段 0 契约测试清单（当前均为 xfail）

| 契约 | 测试 | 目标阶段 | 旧实现现状 |
| --- | --- | --- | --- |
| 新 Session 必须写入非空 `agent_id` | `test_session_creation_requires_non_empty_agent_id` | 1 | `create(agent_id="")` 不抛错 |
| 未知 Identity 不得提交 | `test_unknown_identity_submission_is_rejected` | 1 | `SessionInteractionService` 未实现 |
| 并发提交使用不同输出收集器不串流 | `test_concurrent_submissions_do_not_cross_stream` | 3 | Run 级输出端口未实现 |
| 重启后已批准 Run 恢复不重复请求审批 | `test_approved_run_recovery_does_not_rerequest_approval_after_restart` | 4 | 审批权威依赖进程内 `_waiting_calls` |
| 多 Identity 的 `context_slot_ids` 均生效 | `test_multi_identity_context_slots_both_effective` | 4 | 仅单 Identity 配置生效 |
| 活动 Session 删除被拒绝 | `test_active_session_deletion_is_rejected` | 5 | 删除协调器未实现 |
| 终态 Session 删除清理完整目录与审批记录 | `test_terminal_session_deletion_removes_session_directory_and_approvals` | 5 | `delete()` 仅删 `session.json`；`ApprovalRepository` 暂无以 Session 清理的最小方法 |

## 完成门槛核对

- [x] 新契约在旧实现上明确失败（xfail）或标注待迁移。
- [x] 既有 Runtime 核心测试（75 个）维持通过。
- [x] 基线已记录并与历史环境失败隔离。
