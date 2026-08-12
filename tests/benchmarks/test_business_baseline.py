"""PR8 业务基线编排与工件测试。"""

import json
import platform
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmarks.business_baseline as business_baseline
from benchmarks.business_baseline import BusinessDatasetError, load_business_documents, load_judge_spec, main, run_ext_dataset, run_ext_diagnostic, run_fixture_dataset
from dotclaw.eval.dataset import load_case
from dotclaw.eval.environment import EvalDependencies
from dotclaw.eval.reexecution import ReexecutionRunner
from dotclaw.llm.base import ChatChunk, ChatTextDelta, TextDeltaKind
from dotclaw.runtime.application.dto import ContextBundle
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import LLMOutputPort
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall


class _ExtLLM:
    """覆盖六个冻结 EXT 任务的 LLM 替身，工具调用仍由 Fixture 执行。"""

    def __init__(self, fail_evidence_brief: bool = False) -> None:
        self.calls: list[str] = []
        self._fail_evidence_brief = fail_evidence_brief

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
            content = "不符合冻结资料的输出。" if self._fail_evidence_brief else "事实简报：sunny；未知项不作推断。"
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


def test_dataset_rejects_case_with_mismatched_business_identity(tmp_path: Path) -> None:
    """数据集加载必须拒绝业务任务 ID 与可执行 Case ID 不一致的损坏文件。"""
    source = Path("benchmarks/datasets/runtime_core_v2")
    target = tmp_path / "runtime_core_v2"
    shutil.copytree(source, target)
    path = target / "cases" / "evidence_brief.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["task_id"] = "wrong-id"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BusinessDatasetError, match="task_id 必须与可执行 case_id 一致"):
        load_business_documents(tmp_path, "runtime_core_v2")


def test_evidence_brief_judge_spec_matches_frozen_case_facts() -> None:
    """事实简报的 Judge 规范必须与 Case 的冻结资料和交付标记同口径。"""
    root = Path("benchmarks/datasets")
    case = json.loads((root / "runtime_core_v2/cases/evidence_brief.json").read_text(encoding="utf-8"))
    spec = load_judge_spec(root, "runtime_core_v2", "evidence_brief")
    context = case["context_fixtures"][0]["messages"][0]["content"]
    allowed = "\n".join(spec.allowed_facts)
    constraints = "\n".join(spec.required_constraints)
    assert spec.version == "2"
    assert all(token in context for token in ("资料 A", "sunny", "未知", "不得推断"))
    assert all(token in allowed for token in ("资料 A", "sunny", "未知", "不得推断"))
    assert all(marker in spec.expected_delivery or marker in constraints for marker in case["delivery_markers"])
    assert "未知且不作推断" in spec.criteria["boundary"]
    assert "不引入其他事实" in spec.criteria["facts"]


def test_ext_cli_requires_provider_and_judge_conditions(tmp_path: Path) -> None:
    """EXT CLI 缺少真实模型或 Judge 条件时必须以参数错误退出。"""
    with pytest.raises(SystemExit) as error:
        main(["--mode", "ext", "--output", str(tmp_path)])
    assert error.value.code == 2


def test_ext_diagnostic_cli_rejects_formal_artifact_options(tmp_path: Path) -> None:
    """单案例诊断不得接受正式采样或基线参数，避免排障工件混入证据。"""
    with pytest.raises(SystemExit) as error:
        main([
            "--mode", "ext-diagnostic", "--output", str(tmp_path),
            "--provider", "provider", "--model", "model",
            "--judge-provider", "provider", "--judge-model", "judge",
            "--diagnostic-case", "evidence_brief", "--formal-sampling",
        ])
    assert error.value.code == 2


def test_ext_cli_transmits_timeout_and_retry_to_runtime_adapter_and_judge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """EXT CLI 必须把同一调用条件传到候选模型与 Judge 的 Proxy 边界。"""
    class CapturingProxy:
        """记录 PR8 两类真实调用参数的最小代理替身。"""

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def chat(
            self,
            messages: list[object],
            tools: list[object] | None = None,
            model: str | None = None,
            purpose: str = "chat",
            stream: bool = True,
            timeout_seconds: float | None = None,
            retry_count: int | None = None,
        ):
            """记录调用条件并返回一段最终响应。"""
            del messages, tools, stream
            self.calls.append({"model": model, "purpose": purpose, "timeout_seconds": timeout_seconds, "retry_count": retry_count})
            yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "{}"),))

    proxy = CapturingProxy()

    async def fake_run_ext_dataset(*_args: object, dependencies: EvalDependencies, judge: object, **_kwargs: object) -> object:
        """只走 CLI 装配后的 Adapter 与 Judge，不启动真实采样。"""
        await dependencies.llm_port.complete(
            SimpleNamespace(messages=(), tools=()),
            SimpleNamespace(run_id="candidate-run", session_id="candidate-session", policy=SimpleNamespace(model_id="candidate-model")),
        )
        await judge.judge("judge prompt")
        return SimpleNamespace()

    monkeypatch.setattr(business_baseline, "_build_llm", lambda _config, _root: proxy)
    monkeypatch.setattr(business_baseline, "run_ext_dataset", fake_run_ext_dataset)
    assert main([
        "--mode", "ext", "--output", str(tmp_path),
        "--provider", "candidate-provider", "--model", "candidate-model",
        "--judge-provider", "judge-provider", "--judge-model", "judge-model",
        "--timeout-seconds", "17", "--retry-count", "2",
    ]) == 0
    assert proxy.calls == [
        {"model": "candidate-model", "purpose": "chat", "timeout_seconds": 17.0, "retry_count": 2},
        {"model": "judge-model", "purpose": "chat", "timeout_seconds": 17.0, "retry_count": 2},
    ]


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
    assert (tmp_path / "fixture-summary.md").is_file()
    assert (tmp_path / "failure-attribution.md").is_file()
    assert (tmp_path / "baseline" / snapshot.samples_path).is_file()
    assert json.loads((tmp_path / "business-config.json").read_text(encoding="utf-8"))["formal_sampling"] == "false"
    assert len((tmp_path / snapshot.samples_path).read_text(encoding="utf-8").splitlines()) == 10
    assert json.loads((tmp_path / f"{snapshot.snapshot_id}.json").read_text(encoding="utf-8"))["dataset"] == "runtime_core_v2"
    workflow_samples = [item for item in (json.loads(line) for line in (tmp_path / snapshot.samples_path).read_text(encoding="utf-8").splitlines()) if item["task_kind"] == "session_workflow"]
    assert len(workflow_samples) == 2
    assert all(item["fixture_fingerprint"] for item in workflow_samples)
    assert all(item["python_version"] == sys.version.split()[0] and item["platform"] == platform.platform() for item in workflow_samples)
    assert len(snapshot.fixture_fingerprints) == 10 and all(snapshot.fixture_fingerprints.values())


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
    assert json.loads((tmp_path / "business-config.json").read_text(encoding="utf-8"))["formal_sampling"] == "false"
    assert (tmp_path / "baseline" / snapshot.samples_path).is_file()
    workflow_sample = next(item for item in samples if item["task_kind"] == "session_workflow")
    assert workflow_sample["fixture_fingerprint"]
    assert len(snapshot.fixture_fingerprints) == 6 and all(snapshot.fixture_fingerprints.values())
    progress = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(progress) == 30
    assert {item["phase"] for item in progress} == {"sample_started", "deterministic_finished", "judge_started", "judge_finished", "sample_finished"}
    assert all(item["timeout_seconds"] == 5.0 and item["retry_count"] == 0 for item in progress)


@pytest.mark.asyncio
async def test_ext_diagnostic_runs_one_case_and_writes_nonbaseline_artifacts(tmp_path: Path) -> None:
    """单案例诊断固定一次真实链路编排，并与 EXT 正式工件明确隔离。"""
    judge = _PassingJudge()
    snapshot = await run_ext_diagnostic(
        Path("benchmarks/datasets"),
        "runtime_core_v2",
        task_id="evidence_brief",
        output=tmp_path,
        dependencies=EvalDependencies(llm_port=_ExtLLM()),
        judge=judge,
        provider="fake-provider",
        model="fake-model",
        temperature=0.0,
        judge_provider="fake-provider",
        judge_model="fake-judge",
        timeout_seconds=5.0,
        retry_count=0,
    )
    assert snapshot.warmup == 0 and snapshot.repeat == 1
    assert snapshot.global_summary.sample_count == 1
    assert snapshot.environment["diagnostic"] == "true" and snapshot.environment["formal_sampling"] == "false"
    assert snapshot.samples_path.startswith("samples/ext-diagnostic-")
    assert judge.calls == 1
    assert (tmp_path / snapshot.samples_path).is_file()
    assert (tmp_path / "progress.jsonl").is_file()
    assert "不属于正式采样" in (tmp_path / "diagnostic-summary.md").read_text(encoding="utf-8")
    config = json.loads((tmp_path / "business-config.json").read_text(encoding="utf-8"))
    assert config["mode"] == "ext-diagnostic" and config["diagnostic_case"] == "evidence_brief"
    assert not list(tmp_path.glob("ext-quality.md"))


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


@pytest.mark.asyncio
async def test_ext_deterministic_failure_skips_judge_and_is_attributed(tmp_path) -> None:
    """Fixture 或业务断言失败时不得调用 Judge，且必须留下唯一失败归因。"""
    judge = _PassingJudge()
    snapshot = await run_ext_dataset(
        __import__("pathlib").Path("benchmarks/datasets"), "runtime_core_v2", warmup=0, repeat=1,
        output=tmp_path, baseline=None, dependencies=EvalDependencies(llm_port=_ExtLLM(fail_evidence_brief=True)), judge=judge,
        provider="fake-provider", model="fake-model", temperature=0.0, judge_provider="fake-provider", judge_model="fake-judge",
    )
    samples = [json.loads(line) for line in (tmp_path / snapshot.samples_path).read_text(encoding="utf-8").splitlines()]
    failed = next(item for item in samples if item["case_id"] == "evidence_brief")
    assert judge.calls == 5
    assert failed["deterministic_passed"] is False and failed["judge_verdict"] is None
    assert failed["failure_attribution"] == "assertion_failure"
    progress = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text(encoding="utf-8").splitlines() if json.loads(line)["task_id"] == "evidence_brief"]
    assert [item["phase"] for item in progress] == ["sample_started", "deterministic_finished", "sample_finished"]
    assert progress[1]["deterministic_passed"] is False and progress[1]["failure_attribution"] == "assertion_failure"


@pytest.mark.asyncio
async def test_formal_sampling_requires_frozen_5x30_parameters(tmp_path) -> None:
    """开发烟测不能借 formal 标记伪装为正式采样。"""
    with pytest.raises(BusinessDatasetError, match="warmup=5、repeat=30"):
        await run_fixture_dataset(
            __import__("pathlib").Path("benchmarks/datasets"), "runtime_core_v2", warmup=0, repeat=1,
            output=tmp_path, formal_sampling=True,
        )
