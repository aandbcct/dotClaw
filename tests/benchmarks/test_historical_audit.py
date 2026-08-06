"""PR2 历史审计：四道审计门顺序执行、失败分类与审计报告落盘。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.historical_audit import (
    AuditError,
    AuditGate,
    EnvironmentBoundary,
    GitBoundary,
    HistoricalAuditor,
    ScenarioSample,
)


# --------------------------------------------------------------------------- #
# 伪造边界：测试只验证门顺序与失败分类，不调用真实历史提交
# --------------------------------------------------------------------------- #


class FakeGit(GitBoundary):
    """可配置失败的 Git 边界。"""

    def __init__(self, *, resolvable: bool = True, existing_worktree: bool = False, fail_create: bool = False) -> None:
        """绑定失败开关并记录创建过的 worktree。"""
        super().__init__(Path("."))
        self.resolvable: bool = resolvable
        self.existing_worktree: bool = existing_worktree
        self.fail_create: bool = fail_create
        self.created: list[Path] = []

    def resolve_full_commit(self, candidate: str) -> str:
        """不可解析时抛出门 1 审计失败；HEAD 始终可解析。"""
        if not self.resolvable and candidate != "HEAD":
            raise AuditError(AuditGate.COMMIT_RESOLUTION, candidate, "无法解析提交 'nope'：unknown revision")
        return "f" * 40

    def short_commit(self, full_commit: str) -> str:
        """返回固定短提交号。"""
        return full_commit[:7]

    def create_worktree(self, path: Path, full_commit: str) -> None:
        """模拟 worktree 创建；已存在或失败均抛门 2 审计失败。"""
        if self.existing_worktree:
            raise AuditError(AuditGate.WORKTREE_CREATION, full_commit, f"worktree 路径已存在：{path}")
        if self.fail_create:
            raise AuditError(AuditGate.WORKTREE_CREATION, full_commit, "git worktree add 失败")
        path.mkdir(parents=True)
        (path / "src" / "dotclaw").mkdir(parents=True)
        (path / "src" / "dotclaw" / "__init__.py").write_text("", encoding="utf-8")
        self.created.append(path)


class FakeEnvironment(EnvironmentBoundary):
    """可配置失败与源码探针结果的环境边界。"""

    def __init__(self, *, fail_create: bool = False, source_file: str | None = None) -> None:
        """绑定失败开关与探针返回的源码文件路径。"""
        self.fail_create: bool = fail_create
        self.source_file: str | None = source_file

    def create(self, env_path: Path) -> None:
        """venv 创建失败时抛门 3 审计失败。"""
        if self.fail_create:
            raise AuditError(AuditGate.ENVIRONMENT, "?", "venv 创建失败：模拟错误")
        env_path.mkdir(parents=True)

    def python_binary(self, env_path: Path) -> Path:
        """返回 venv 内解释器路径。"""
        return env_path / "python.exe"

    def probe_python(self, python: Path) -> str:
        """返回固定解释器版本。"""
        return "3.13.5"

    def probe_dependencies(self, python: Path) -> str:
        """返回固定依赖摘要。"""
        return "openai==1.30.0"

    def probe_source(self, python: Path, worktree: Path) -> dict[str, object]:
        """返回探针结果；默认指向 worktree 内源码。"""
        source = self.source_file or str((worktree / "src").resolve() / "dotclaw" / "__init__.py")
        return {"dotclaw_file": source}


class FakeAdapter:
    """可配置失败的场景适配器。"""

    def __init__(self, *, scenario_id: str = "tool_success", ok: bool = True, fail_scenario: bool = False) -> None:
        """绑定场景标识、校验开关与场景执行失败开关。"""
        self._scenario_id: str = scenario_id
        self.ok: bool = ok
        self.fail_scenario: bool = fail_scenario
        self.calls: list[tuple[str, int, bool]] = []

    @property
    def scenario_id(self) -> str:
        """返回适配器场景标识。"""
        return self._scenario_id

    def verify_expected(self) -> None:
        """适配器与场景不匹配时抛门 5 审计失败。"""
        if not self.ok:
            raise AuditError(AuditGate.SCENARIO, "?", "适配器与目标场景不匹配")

    async def run_scenario(
        self,
        *,
        worktree: Path,
        python: Path,
        state_dir: Path,
        evidence_dir: Path,
        attempt: int,
        is_warmup: bool,
    ) -> ScenarioSample:
        """记录状态目录并返回固定场景事实；fail_scenario 时返回失败场景。"""
        self.calls.append((str(state_dir), attempt, is_warmup))
        if self.fail_scenario:
            return ScenarioSample(
                end_status="failed",
                passed=False,
                failure_kind="assertion",
                wall_duration_ms=5.0,
                loop_iterations=1,
                tool_call_count=0,
                tool_name_ok=False,
                tool_arguments_ok=False,
                final_output_ok=False,
                final_output=None,
                evidence_refs=[str(evidence_dir)],
            )
        return ScenarioSample(
            end_status="completed",
            passed=True,
            failure_kind=None,
            wall_duration_ms=5.0,
            loop_iterations=2,
            tool_call_count=1,
            tool_name_ok=True,
            tool_arguments_ok=True,
            final_output_ok=True,
            final_output="sunny",
            evidence_refs=[str(evidence_dir)],
        )


def _auditor(tmp_path: Path, *, git: FakeGit | None = None, env: FakeEnvironment | None = None, adapter: FakeAdapter | None = None) -> HistoricalAuditor:
    """构造绑定伪造边界的审计器。"""
    return HistoricalAuditor(
        repo_root=Path("."),
        output_root=tmp_path / "audits",
        dataset="runtime_core_v1",
        case_id="tool_success",
        git=git or FakeGit(),
        environment=env or FakeEnvironment(),
        adapter=adapter or FakeAdapter(),
    )


def _audit_json(tmp_path: Path) -> dict:
    """读取审计输出目录下的 audit.json。"""
    files = list((tmp_path / "audits").glob("*/audit.json"))
    assert files, "未找到 audit.json"
    return json.loads(files[0].read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_audit_all_gates_pass(tmp_path: Path) -> None:
    """四道审计门按顺序全部通过，写出完整审计报告与开发期采样。"""
    git = FakeGit()
    auditor = _auditor(tmp_path, git=git)
    report = await auditor.audit("4e4cdd3", warmup=1, repeat=2)

    assert report.passed is True
    assert report.full_commit == "f" * 40
    assert [gate.gate for gate in report.gates] == [gate for gate in AuditGate]
    assert all(gate.passed for gate in report.gates)
    assert report.worktree_path is not None
    assert report.environment_path is not None
    assert git.created  # worktree 已创建

    # audit.json 与证据落盘
    payload = _audit_json(tmp_path)
    assert payload["passed"] is True
    assert payload["candidate_order"] == ["4e4cdd3"]
    env_dir = Path(report.environment_path)
    assert (env_dir / "python_version.txt").exists()
    assert (env_dir / "dependencies.txt").exists()
    assert (env_dir / "source_path.txt").exists()

    # 开发期采样：warmup + repeat 条，全部可反序列化
    samples_path = Path(report.samples_path)
    lines = samples_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["execution_source"] == "historical_adapter"
    assert first["source_commit"] == "f" * 40
    assert first["scenario_id"] == "tool_success"
    assert first["trace_available"] is False
    assert first["run_statistics"]["tokens_in"] is None


@pytest.mark.asyncio
async def test_audit_samples_use_isolated_state_dirs(tmp_path: Path) -> None:
    """连续采样每次使用独立临时状态目录，不读取前一次状态。"""
    adapter = FakeAdapter()
    await _auditor(tmp_path, adapter=adapter).audit("4e4cdd3", warmup=1, repeat=2)

    state_dirs = [call[0] for call in adapter.calls]
    assert len(state_dirs) == 4  # 门 5 一次 + 门 6 三次
    assert len(set(state_dirs)) == len(state_dirs)  # 全部互不相同


# --------------------------------------------------------------------------- #
# 边界路径：逐门失败分类
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_audit_unresolvable_commit_fails(tmp_path: Path) -> None:
    """不可解析提交：门 1 失败，审计报告记录失败证据且不生成基线。"""
    auditor = _auditor(tmp_path, git=FakeGit(resolvable=False))
    with pytest.raises(AuditError) as exc:
        await auditor.audit("nope", warmup=0, repeat=1)
    assert exc.value.gate is AuditGate.COMMIT_RESOLUTION

    payload = _audit_json(tmp_path)
    assert payload["passed"] is False
    assert payload["selection_note"].startswith("审计失败：门 commit_resolution")
    assert payload["full_commit"] == ""


@pytest.mark.asyncio
async def test_audit_existing_worktree_fails(tmp_path: Path) -> None:
    """已有同名 worktree：门 2 明确失败，不静默回退。"""
    auditor = _auditor(tmp_path, git=FakeGit(existing_worktree=True))
    with pytest.raises(AuditError) as exc:
        await auditor.audit("4e4cdd3", warmup=0, repeat=1)
    assert exc.value.gate is AuditGate.WORKTREE_CREATION
    assert "已存在" in exc.value.summary


@pytest.mark.asyncio
async def test_audit_environment_failure(tmp_path: Path) -> None:
    """环境创建失败：门 3 明确失败。"""
    auditor = _auditor(tmp_path, env=FakeEnvironment(fail_create=True))
    with pytest.raises(AuditError) as exc:
        await auditor.audit("4e4cdd3", warmup=0, repeat=1)
    assert exc.value.gate is AuditGate.ENVIRONMENT


@pytest.mark.asyncio
async def test_audit_source_import_outside_worktree_fails(tmp_path: Path) -> None:
    """源码路径不属于该 worktree：门 4 明确失败。"""
    auditor = _auditor(
        tmp_path,
        env=FakeEnvironment(source_file="D:/dev/dotClaw/src/dotclaw/__init__.py"),
    )
    with pytest.raises(AuditError) as exc:
        await auditor.audit("4e4cdd3", warmup=0, repeat=1)
    assert exc.value.gate is AuditGate.SOURCE_IMPORT
    assert "不在 worktree src" in exc.value.summary


@pytest.mark.asyncio
async def test_audit_scenario_mismatch_fails(tmp_path: Path) -> None:
    """适配器与场景不匹配：门 5 明确失败，不生成采样。"""
    auditor = _auditor(tmp_path, adapter=FakeAdapter(ok=False))
    with pytest.raises(AuditError) as exc:
        await auditor.audit("4e4cdd3", warmup=0, repeat=1)
    assert exc.value.gate is AuditGate.SCENARIO
    payload = _audit_json(tmp_path)
    assert payload["passed"] is False


@pytest.mark.asyncio
async def test_audit_scenario_validation_failure_fails(tmp_path: Path) -> None:
    """场景执行但语义校验不通过：门 5 明确失败，不产出基线。"""
    auditor = _auditor(tmp_path, adapter=FakeAdapter(fail_scenario=True))
    with pytest.raises(AuditError) as exc:
        await auditor.audit("4e4cdd3", warmup=0, repeat=1)
    assert exc.value.gate is AuditGate.SCENARIO
    assert "场景校验失败" in exc.value.summary
    payload = _audit_json(tmp_path)
    assert payload["passed"] is False


@pytest.mark.asyncio
async def test_audit_invalid_warmup_repeat_rejected(tmp_path: Path) -> None:
    """warmup 负数与 repeat=0 被明确拒绝。"""
    auditor = _auditor(tmp_path)
    with pytest.raises(ValueError):
        await auditor.audit("4e4cdd3", warmup=-1, repeat=1)
    with pytest.raises(ValueError):
        await auditor.audit("4e4cdd3", warmup=0, repeat=0)


# --------------------------------------------------------------------------- #
# 场景语义事实校验
# --------------------------------------------------------------------------- #


def test_scenario_sample_requires_required_fields() -> None:
    """最低语义事实缺一不可：终态为空或负值时明确失败。"""
    with pytest.raises(AuditError):
        ScenarioSample(
            end_status="",
            passed=True,
            failure_kind=None,
            wall_duration_ms=5.0,
            loop_iterations=2,
            tool_call_count=1,
            tool_name_ok=True,
            tool_arguments_ok=True,
            final_output_ok=True,
            final_output="x",
            evidence_refs=(),
        )
    with pytest.raises(AuditError):
        ScenarioSample(
            end_status="completed",
            passed=True,
            failure_kind=None,
            wall_duration_ms=-1.0,
            loop_iterations=2,
            tool_call_count=1,
            tool_name_ok=True,
            tool_arguments_ok=True,
            final_output_ok=True,
            final_output="x",
            evidence_refs=(),
        )


def test_audit_id_format() -> None:
    """审计标识：<short-commit>-<YYYYMMDDTHHMMSSZ>。"""
    from datetime import datetime, timezone

    from benchmarks.historical_audit import make_audit_id

    audit_id = make_audit_id("4e4cdd3", datetime(2026, 8, 6, 9, 15, 30, tzinfo=timezone.utc))
    assert audit_id == "4e4cdd3-20260806T091530Z"


# --------------------------------------------------------------------------- #
# run 命令候选校验：只接受通过审计的完整提交
# --------------------------------------------------------------------------- #


def test_require_audited_candidate_accepts_full_commit() -> None:
    """run 只接受与审计报告一致的完整提交号。"""
    from benchmarks.historical_baseline import _require_audited_candidate

    report = {"passed": True, "full_commit": "f" * 40, "audit_id": "audit-1"}
    assert _require_audited_candidate(report, "f" * 40) == "f" * 40


def test_require_audited_candidate_rejects_short_hash() -> None:
    """短哈希不能代替完整提交号运行正式基线。"""
    from benchmarks.historical_baseline import _require_audited_candidate

    report = {"passed": True, "full_commit": "f" * 40, "audit_id": "audit-1"}
    with pytest.raises(ValueError, match="完整提交"):
        _require_audited_candidate(report, "f" * 12)


def test_require_audited_candidate_rejects_unpassed() -> None:
    """未通过审计的候选不能运行正式基线。"""
    from benchmarks.historical_baseline import _require_audited_candidate

    report = {"passed": False, "full_commit": "f" * 40, "audit_id": "audit-1"}
    with pytest.raises(ValueError, match="未通过审计"):
        _require_audited_candidate(report, "f" * 40)
