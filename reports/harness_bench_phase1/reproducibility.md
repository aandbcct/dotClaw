# Harness-Bench Phase 1 可复现说明

## 固定条件

- 被测模型：`gpt-5.6-luna`
- Provider：jojocode
- API 基址标识：`https://max2.jojocode.com/v1`
- temperature：0.7
- max tokens：4096
- dotClaw 最大工具迭代：15
- 裁判模型：`gpt-5.6-sol`，temperature 0.2，流式输出
- 题目：官方 Phase 1 的 26 项，每个 Harness 每题保留一份最终有效记录
- NanoBot：0.3.0
- Harness-Bench 基线提交：`1025086a446653702b80cfb48babbeec35db6b2c`

## 结果选择口径

首次运行中，NanoBot 的部分结果因上游未返回 `response_model` 被落入 `unknown-api` 目录；这些是同一模型的原始结果，不是不同模型。已按 task_id 合并到 `gpt-5.6-luna` 正式目录，并优先选择裁判完整的最新记录。补评分只读取已保存的 sandbox、代理轨迹和 Oracle 结果，不重新运行被测代理。

## 重跑与异常

- dotClaw 只重跑了上游 `llm_failure`/连接故障任务；工具路径拒绝、请求确认、未产出文件等行为失败保留为有效能力结果。
- NanoBot 只重跑了 008、013、020、021 的断点误判项；随后停止重复批次，采用已落盘的 26 个不同 task 结果并补齐裁判评分。
- 代理结果中的 `adapter_result.ok=false` 不自动等于基础设施失败；最终报告同时保留执行成功数、完成数、Oracle 检查失败项和裁判备注。

## 安全与密钥

- API key 只从环境变量读取，未写入仓库报告或正式 JSONL。
- usage proxy 已对 Authorization、Proxy-Authorization、X-API-Key、Api-Key 做落盘脱敏。
- 运行结束后扫描 sandbox JSON，未发现 bearer/header 密钥残留。

## 哈希

- dotClaw 工作区 diff SHA-256：`7e1c8c063dcc0cea1be155eb457b888ffa96047fc790d5ae37554a2b4a0959bc`
- Harness-Bench 补丁 SHA-256：`9571195d558ff5c0ba382521212b3663cd0427c00d1e4939796e99866eecb8de`
