"""PR8 业务基线编排与工件测试。"""

import json

import pytest

from benchmarks.business_baseline import BusinessDatasetError, load_business_documents, run_ext_dataset, run_fixture_dataset
from dotclaw.eval.dataset import load_case
from dotclaw.eval.environment import EvalDependencies
from dotclaw.eval.reexecution import ReexecutionRunner
from dotclaw.runtime.application.dto import ContextBundle
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import LLMOutputPort
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall


class _ExtLLM:
    """覆盖六个冻结 EXT 任务的 LLM 替身，工具调用仍由 Fixture 执行。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(
        self,
        context: ContextBundle,
        execution: RunExecutionView,
        output_port: LLMOutputPort | None = None,
    ) -> RunMessage:
        """按实际用户输入和可用工具返回冻结的 EXT 候选交付。"""
        text = "\n".join(message.content for message in context.messages)
        tools = {tool.name for tool in context.tools}
        self.calls.append(execution.policy.model_id)
        if "update_report" in tools:
            return RunMessage("ext-change-call", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("change-1", "update_report", {"path": "report.md", "content": "已更新"}),))
        if "我的偏好" in text:
            return RunMessage("ext-preference-first", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "已记录偏好：简洁并验证。")
        if "请按已记录偏好" in text:
            return RunMessage("ext-preference-followup", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "简洁方案：先实施，再验证。")
        if "仅支持结论 sunny" in text:
            content = "事实简报：sunny；未知项不作推断。"
        elif "版本为 v1" in text:
            content = "存在冲突；未知当前版本；建议核验权威来源。"
        elif "缺少测试报告" in text:
            content = "问题：缺少测试报告。行动项：运行并归档测试。"
        elif "冻结子任务结果" in text:
            content = "子任务返回 sunny；整合结论明确证据边界。"
        else:
            content = "批准后报告已更新；验证：冻结校验通过。"
        return RunMessage(f"ext-final-{len(self.calls)}", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, content)

    async def cancel(self, run_id: str) -> None:
        """开发替身不持有可取消的外部资源。"""


class _PassingJudge:
    """按传入规范逐项通过的 Judge 替身，并记录一次调用边界。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def judge(self, prompt: str) -> str:
        """从固定提示词正文读取判据标识，返回严格 JSON。"""
        self.calls += 1
        payload = json.loads(prompt.split("\n", 1)[1])
        return json.dumps({"verdict": "pass", "criteria": {key: "pass" for key in payload["criteria"]}, "reason": "开发替身通过"}, ensure_ascii=False)


def test_runtime_core_v2_has_frozen_task_shape() -> None:
    """任务集必须固定为八个 Case、两个工作流。"""
    cases, workflows = load_business_documents(__import__("pathlib").Path("benchmarks/datasets"), "runtime_core_v2")
    assert len(cases) == 8 and len(workflows) == 2


@pytest.mark.asyncio
async def test_all_standard_tasks_are_independent_executable_v2_cases() -> None:
    """八项标准任务必须直接消费各自的 v2 Case，而非映射回 v1 旧 Case。"""
    root = __import__("pathlib").Path("benchmarks/datasets")
    cases, _ = load_business_documents(root, "runtime_core_v2")
    runner = ReexecutionRunner()

    for task_id, document in cases.items():
        case = load_case(root, "runtime_core_v2", task_id)
        result = await runner.run_case(case)
        assert document["task_id"] == case.case_id
        assert result.passed, f"{task_id}: {result.failure_detail}"


@pytest.mark.asyncio
async def test_fixture_run_writes_traceable_artifacts(tmp_path) -> None:
    """Fixture 开发运行也必须写出 JSONL、快照、配置和任务报告。"""
    snapshot = await run_fixture_dataset(__import__("pathlib").Path("benchmarks/datasets"), "runtime_core_v2", warmup=0, repeat=1, output=tmp_path, baseline=tmp_path / "baseline")
    assert snapshot.global_summary.sample_count == 10
    assert (tmp_path / snapshot.samples_path).is_file()
    assert (tmp_path / f"{snapshot.snapshot_id}.json").is_file()
    assert (tmp_path / "business-config.json").is_file()
    assert len((tmp_path / snapshot.samples_path).read_text(encoding="utf-8").splitlines()) == 10
    assert json.loads((tmp_path / f"{snapshot.snapshot_id}.json").read_text(encoding="utf-8"))["dataset"] == "runtime_core_v2"


@pytest.mark.asyncio
async def test_ext_run_uses_injected_llm_judges_once_and_writes_artifacts(tmp_path) -> None:
    """EXT 开发运行必须覆盖冻结六任务、固定模型并写出 JSONL/快照/Judge 结果。"""
    llm, judge = _ExtLLM(), _PassingJudge()
    snapshot = await run_ext_dataset(
        __import__("pathlib").Path("benchmarks/datasets"),
        "runtime_core_v2",
        warmup=0,
        repeat=1,
        output=tmp_path,
        baseline=tmp_path / "baseline",
        dependencies=EvalDependencies(llm_port=llm),
        judge=judge,
        provider="fake-provider",
        model="fake-model",
        temperature=0.0,
        judge_provider="fake-provider",
        judge_model="fake-judge",
        judge_temperature=0.0,
        timeout_seconds=5.0,
        retry_count=0,
    )
    samples = [json.loads(line) for line in (tmp_path / snapshot.samples_path).read_text(encoding="utf-8").splitlines()]
    assert snapshot.global_summary.sample_count == 6
    assert judge.calls == 6
    assert {item["case_id"] for item in samples} == {"evidence_brief", "evidence_conflict_resolution", "workspace_diagnosis", "workspace_change_verified", "delegated_research_synthesis", "preference_aware_followup"}
    assert all(item["judge_verdict"] == "pass" for item in samples)
    assert "fake-model" in llm.calls
    assert (tmp_path / "ext-quality.md").is_file()
    assert (tmp_path / "business-config.json").is_file()


@pytest.mark.asyncio
async def test_ext_run_rejects_subset_of_frozen_tasks(tmp_path) -> None:
    """CLI/API 不得静默缩减 EXT 六任务，避免不完整样本冒充正式口径。"""
    with pytest.raises(BusinessDatasetError, match="精确覆盖"):
        await run_ext_dataset(
            __import__("pathlib").Path("benchmarks/datasets"), "runtime_core_v2", warmup=0, repeat=1,
            output=tmp_path, baseline=None, dependencies=EvalDependencies(llm_port=_ExtLLM()), judge=_PassingJudge(),
            provider="fake-provider", model="fake-model", temperature=0.0, judge_provider="fake-provider", judge_model="fake-judge",
            ext_cases=("evidence_brief",),
        )
