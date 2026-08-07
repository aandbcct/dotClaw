"""历史候选提交的可复跑性审计：worktree / 环境 / 入口 / 场景审计门。

审计顺序固定（对应开发计划 §4.1）：

1. ``COMMIT_RESOLUTION``：解析不可变完整提交号；
2. ``WORKTREE_CREATION``：创建 detached worktree；
3. ``ENVIRONMENT``：按该提交创建独立解释器环境，记录 Python 与依赖证据；
4. ``SOURCE_IMPORT``：子进程显式从历史 ``src`` 导入，拒绝导入当前 checkout；
5. ``SCENARIO``：启动固定业务场景并校验终态、工具名、参数、调用次数与最终回答；
6. ``MAPPING``：映射统一记录并连续执行开发期采样。

任一门失败时记录候选、失败门、异常摘要和证据路径并停止审计：不产生历史快照或
对照百分比。已有同名 worktree、不可解析提交、环境创建失败或源码路径不属于该
worktree 都是明确失败，不静默回退到当前环境。

Git / 环境 / 场景执行边界均可注入 fake，测试只验证门顺序与失败分类，不调用
真实历史提交。
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .eval_baseline_models import SCENARIO_TOOL_SUCCESS

_AUDIT_OUTPUT_ROOT = Path("benchmarks") / "reports" / "historical-audits"
"""审计输出根目录（保持 gitignore，仅写运行工件）。"""


class AuditGate(StrEnum):
    """审计门的固定顺序与标识。"""

    COMMIT_RESOLUTION = "commit_resolution"
    WORKTREE_CREATION = "worktree_creation"
    ENVIRONMENT = "environment"
    SOURCE_IMPORT = "source_import"
    SCENARIO = "scenario"
    MAPPING = "mapping"


class AuditError(RuntimeError):
    """审计失败：携带失败门、候选与异常摘要。"""

    def __init__(self, gate: AuditGate, candidate: str, summary: str) -> None:
        """记录失败门与候选，供审计报告直接落盘。"""
        super().__init__(f"审计门 {gate.value} 失败（候选 {candidate}）：{summary}")
        self.gate: AuditGate = gate
        self.candidate: str = candidate
        self.summary: str = summary


@dataclass(frozen=True)
class GateResult:
    """一道审计门的执行结果与证据。"""

    gate: AuditGate
    passed: bool
    evidence: str
    detail: Mapping[str, object] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "gate": self.gate.value,
            "passed": self.passed,
            "evidence": self.evidence,
            "detail": dict(self.detail),
            "error": self.error,
        }


@dataclass(frozen=True)
class AuditReport:
    """一次候选审计的完整记录；审计失败也落盘以便追溯。"""

    audit_id: str
    candidate: str
    full_commit: str
    passed: bool
    dataset: str
    case_id: str
    scenario_id: str
    gates: Sequence[GateResult] = ()
    worktree_path: str | None = None
    environment_path: str | None = None
    samples_path: str | None = None
    fixture_fingerprint: str = ""
    candidate_order: Sequence[str] = ()
    selection_note: str = ""
    created_at: str = ""
    git_commit: str = ""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "audit_id": self.audit_id,
            "candidate": self.candidate,
            "full_commit": self.full_commit,
            "passed": self.passed,
            "dataset": self.dataset,
            "case_id": self.case_id,
            "scenario_id": self.scenario_id,
            "gates": [gate.to_dict() for gate in self.gates],
            "worktree_path": self.worktree_path,
            "environment_path": self.environment_path,
            "samples_path": self.samples_path,
            "fixture_fingerprint": self.fixture_fingerprint,
            "candidate_order": list(self.candidate_order),
            "selection_note": self.selection_note,
            "created_at": self.created_at,
            "git_commit": self.git_commit,
        }


class ScenarioSample:
    """一次历史场景执行产出的最低语义事实（开发计划 §5 必填项）。

    终态、通过 / 失败、失败类别、外层耗时、循环轮数、工具调用数、工具名与参数
    校验结果、最终回答校验结果及证据引用缺一不可；任一无法取得则审计失败。
    """

    def __init__(
        self,
        *,
        end_status: str,
        passed: bool,
        failure_kind: str | None,
        wall_duration_ms: float,
        loop_iterations: int,
        tool_call_count: int,
        tool_name_ok: bool,
        tool_arguments_ok: bool,
        final_output_ok: bool,
        final_output: str | None,
        evidence_refs: Sequence[str],
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        """校验并保存一次场景采样的语义事实。"""
        if not end_status:
            raise AuditError(AuditGate.SCENARIO, "?", "终态为空，无法取得最低语义事实")
        if wall_duration_ms < 0:
            raise AuditError(AuditGate.SCENARIO, "?", "外层耗时不能为负")
        if loop_iterations < 0 or tool_call_count < 0:
            raise AuditError(AuditGate.SCENARIO, "?", "循环轮数或工具调用数不能为负")
        self.end_status: str = end_status
        self.passed: bool = passed
        self.failure_kind: str | None = failure_kind
        self.wall_duration_ms: float = wall_duration_ms
        self.loop_iterations: int = loop_iterations
        self.tool_call_count: int = tool_call_count
        self.tool_name_ok: bool = tool_name_ok
        self.tool_arguments_ok: bool = tool_arguments_ok
        self.final_output_ok: bool = final_output_ok
        self.final_output: str | None = final_output
        self.evidence_refs: tuple[str, ...] = tuple(evidence_refs)
        self.tokens_in: int | None = tokens_in
        self.tokens_out: int | None = tokens_out

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "end_status": self.end_status,
            "passed": self.passed,
            "failure_kind": self.failure_kind,
            "wall_duration_ms": self.wall_duration_ms,
            "loop_iterations": self.loop_iterations,
            "tool_call_count": self.tool_call_count,
            "tool_name_ok": self.tool_name_ok,
            "tool_arguments_ok": self.tool_arguments_ok,
            "final_output_ok": self.final_output_ok,
            "final_output": self.final_output,
            "evidence_refs": list(self.evidence_refs),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


class HistoricalScenarioAdapter(Protocol):
    """历史场景执行适配协议：审计门只依赖该端口，不依赖具体旧入口。"""

    @property
    def scenario_id(self) -> str:
        """本适配器覆盖的业务场景标识。"""
        ...

    def verify_expected(self) -> None:
        """校验适配器自身与目标场景匹配；不匹配时抛 AuditError。"""
        ...

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
        """在历史 worktree 环境执行一次固定场景并返回语义事实。

        ``state_dir`` 是本次采样独立的临时状态目录，禁止跨样本泄漏；
        ``evidence_dir`` 用于保存替身日志等证据引用文件。
        """
        ...


class GitBoundary:
    """Git 命令边界；测试可注入 fake，不直接调用真实历史提交。"""

    def __init__(self, repo_root: Path) -> None:
        """绑定仓库根目录。"""
        self._root: Path = repo_root

    def _run(self, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
        """在仓库根目录执行 git 命令。"""
        return subprocess.run(
            ["git", "-C", str(self._root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=check,
        )

    def resolve_full_commit(self, candidate: str) -> str:
        """把候选解析为不可变完整提交号；无法解析时抛 AuditError。"""
        proc = self._run(["rev-parse", f"{candidate}^{{commit}}"], check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AuditError(
                AuditGate.COMMIT_RESOLUTION,
                candidate,
                f"无法解析提交 {candidate!r}：{proc.stderr.strip() or '无输出'}",
            )
        return proc.stdout.strip()

    def short_commit(self, full_commit: str) -> str:
        """返回完整提交号的短形式。"""
        proc = self._run(["rev-parse", "--short", full_commit], check=False)
        if proc.returncode != 0:
            return full_commit[:12]
        return proc.stdout.strip() or full_commit[:12]

    def create_worktree(self, path: Path, full_commit: str) -> None:
        """创建 detached worktree；已存在或创建失败均明确失败。"""
        if path.exists():
            raise AuditError(
                AuditGate.WORKTREE_CREATION,
                full_commit,
                f"worktree 路径已存在：{path}",
            )
        proc = self._run(["worktree", "add", "--detach", str(path), full_commit], check=False)
        if proc.returncode != 0:
            raise AuditError(
                AuditGate.WORKTREE_CREATION,
                full_commit,
                f"git worktree add 失败：{proc.stderr.strip()}",
            )

    def source_path(self, worktree: Path) -> Path:
        """返回 worktree 内的历史源码目录。"""
        return worktree / "src"


class EnvironmentBoundary:
    """独立解释器环境创建边界；测试可注入 fake。"""

    def __init__(self, base_python: str | None = None) -> None:
        """绑定创建环境所用的基础解释器（默认当前解释器）。"""
        self._base_python: str = base_python or sys.executable

    def create(self, env_path: Path) -> None:
        """创建带系统 site-packages 的独立 venv；失败时抛 AuditError。"""
        env_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [self._base_python, "-m", "venv", "--system-site-packages", str(env_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if proc.returncode != 0:
            raise AuditError(
                AuditGate.ENVIRONMENT,
                "?",
                f"venv 创建失败：{proc.stderr.strip() or '未知错误'}",
            )

    def python_binary(self, env_path: Path) -> Path:
        """返回 venv 内的 python 可执行文件路径。"""
        if platform.system() == "Windows":
            return env_path / "Scripts" / "python.exe"
        return env_path / "bin" / "python"

    def probe_python(self, python: Path) -> str:
        """返回解释器版本文本（如 3.13.5）。"""
        proc = subprocess.run(
            [str(python), "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            return "unknown"
        return proc.stdout.strip() or "unknown"

    def probe_dependencies(self, python: Path) -> str:
        """返回已解析依赖摘要（pip freeze 关键行或 unknown）。"""
        proc = subprocess.run(
            [str(python), "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode != 0:
            return "unknown"
        return proc.stdout.strip()

    def probe_source(self, python: Path, worktree: Path) -> Mapping[str, object]:
        """子进程从历史 ``src`` 导入 dotclaw，返回模块路径探针结果。

        只把显式传入的 worktree ``src`` 加入 ``sys.path``，拒绝导入当前 checkout；
        探针失败或输出不可解析都视为门 4 失败。
        """
        probe_script: str = (
            "import json, sys\n"
            f"sys.path.insert(0, {str((worktree / 'src').resolve())!r})\n"
            "import dotclaw\n"
            "print(json.dumps({'dotclaw_file': dotclaw.__file__}))\n"
        )
        proc = subprocess.run(
            [str(python), "-c", probe_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode != 0:
            raise AuditError(
                AuditGate.SOURCE_IMPORT,
                "?",
                f"历史 src 导入失败：{proc.stderr.strip() or '未知错误'}",
            )
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            raise AuditError(
                AuditGate.SOURCE_IMPORT, "?", f"探针输出无法解析：{error}"
            ) from error


def make_audit_id(short_commit: str, now: datetime | None = None) -> str:
    """构造审计标识：<short-commit>-<YYYYMMDDTHHMMSSZ>（UTC）。"""
    moment: datetime = now or datetime.now(timezone.utc)
    return f"{short_commit}-{moment.strftime('%Y%m%dT%H%M%SZ')}"


def audit_output_dir(audit_id: str) -> Path:
    """计算审计输出目录。"""
    return _AUDIT_OUTPUT_ROOT / audit_id


def to_sample_record(
    scene: ScenarioSample,
    *,
    dataset: str,
    case_id: str,
    scenario_id: str,
    fixture_fingerprint: str,
    git_commit: str,
    full_commit: str,
    attempt: int,
    is_warmup: bool,
) -> Mapping[str, object]:
    """把场景语义事实映射为与 PR1 相同 schema 的 JSON 记录（映射门）。

    历史链路没有的 Trace / token / 内部阶段时延序列化为 ``null``，不补 0。
    ``source_commit`` 记录实际执行的完整提交号，展示短哈希只进 ``git_commit``；
    ``fixture_fingerprint`` 记录该业务场景的固定夹具指纹，供当前/历史对照。
    """
    return {
        "schema_version": "1.0",
        "suite": "runtime_core",
        "dataset": dataset,
        "case_id": case_id,
        "attempt": attempt,
        "is_warmup": is_warmup,
        "git_commit": git_commit,
        "python_version": "historical",
        "platform": "historical",
        "config_hash": "historical-adapter",
        "eval_schema_version": "1.0",
        "execution_source": "historical_adapter",
        "source_commit": full_commit,
        "scenario_id": scenario_id,
        "evidence_kind": "final_result",
        "fixture_fingerprint": fixture_fingerprint,
        "passed": scene.passed,
        "failure_kind": scene.failure_kind,
        "assertions_passed": 1 if scene.passed else 0,
        "assertions_total": 1,
        "trace_available": False,
        "wall_duration_ms": scene.wall_duration_ms,
        "run_id": None,
        "trace_metrics": {
            "llm_duration_ms": None,
            "tool_duration_ms": None,
            "approval_wait_ms": None,
            "critical_path_ms": None,
        },
        "run_statistics": {
            "duration_ms": None,
            "llm_call_count": scene.loop_iterations,
            "tool_call_count": scene.tool_call_count,
            "tokens_in": scene.tokens_in,
            "tokens_out": scene.tokens_out,
        },
        "trace_source": None,
    }


class HistoricalAuditor:
    """按固定顺序执行六道审计门并落盘审计报告。"""

    def __init__(
        self,
        *,
        repo_root: Path,
        output_root: Path,
        dataset: str,
        case_id: str,
        scenario_id: str = SCENARIO_TOOL_SUCCESS,
        fixture_fingerprint: str = "",
        git: GitBoundary | None = None,
        environment: EnvironmentBoundary | None = None,
        adapter: HistoricalScenarioAdapter,
        base_python: str | None = None,
    ) -> None:
        """绑定审计边界与场景适配器。

        ``fixture_fingerprint`` 记录该业务场景的固定夹具指纹；审计报告与
        开发期采样记录均携带它，供后续 ``run`` 固化到历史快照。
        """
        self._repo_root: Path = Path(repo_root)
        self._output_root: Path = Path(output_root)
        self._dataset: str = dataset
        self._case_id: str = case_id
        self._scenario_id: str = scenario_id
        self._fixture_fingerprint: str = fixture_fingerprint
        self._git: GitBoundary = git or GitBoundary(self._repo_root)
        self._environment: EnvironmentBoundary = environment or EnvironmentBoundary(base_python)
        self._adapter: HistoricalScenarioAdapter = adapter

    async def audit(
        self,
        candidate: str,
        *,
        warmup: int,
        repeat: int,
        candidate_order: Sequence[str] | None = None,
        selection_note: str = "",
    ) -> AuditReport:
        """对单个候选执行完整审计，返回审计报告。

        ``candidate_order`` 记录本次审计所处的候选序列（默认仅当前候选）；
        ``selection_note`` 记录选择该候选的理由，供“首个通过者”审计结论追溯。
        任一审计门失败时抛 ``AuditError`` 并在 ``audit.json`` 记录失败证据；
        不产生历史快照或对照百分比。审计通过时额外写出开发期采样 JSONL。
        """
        if warmup < 0:
            raise ValueError(f"warmup 必须大于等于 0，实际 {warmup}")
        if repeat <= 0:
            raise ValueError(f"repeat 必须大于 0，实际 {repeat}")

        order: tuple[str, ...] = tuple(candidate_order) if candidate_order else (candidate,)
        note: str = selection_note or (
            "候选序列中的首个通过者：该候选通过全部审计门后可冻结为历史基线"
        )

        gates: list[GateResult] = []
        full_commit: str = ""
        worktree_path: Path | None = None
        environment_path: Path | None = None
        samples_path: Path | None = None

        def record(gate: AuditGate, passed: bool, evidence: str, detail: Mapping[str, object] | None = None, error: str | None = None) -> None:
            """记录一道审计门的结果。"""
            gates.append(GateResult(gate=gate, passed=passed, evidence=evidence, detail=detail or {}, error=error))

        try:
            # ── 门 1：解析不可变完整提交号 ──
            full_commit = self._git.resolve_full_commit(candidate)
            record(
                AuditGate.COMMIT_RESOLUTION, True,
                f"候选 {candidate!r} 解析为完整提交 {full_commit}",
                {"candidate": candidate, "full_commit": full_commit},
            )

            # ── 门 2：创建 detached worktree ──
            short_commit: str = self._git.short_commit(full_commit)
            audit_id: str = make_audit_id(short_commit)
            out_dir: Path = self._output_root / audit_id
            out_dir.mkdir(parents=True, exist_ok=True)
            worktree_path = out_dir / "worktrees" / short_commit
            self._git.create_worktree(worktree_path, full_commit)
            record(
                AuditGate.WORKTREE_CREATION, True,
                f"detached worktree 创建于 {worktree_path}",
                {"worktree_path": str(worktree_path), "full_commit": full_commit},
            )

            # ── 门 3：独立解释器环境 ──
            environment_path = out_dir / "environment"
            env_dir: Path = environment_path / "venv"
            self._environment.create(env_dir)
            python: Path = self._environment.python_binary(env_dir)
            python_version: str = self._environment.probe_python(python)
            dependencies: str = self._environment.probe_dependencies(python)
            self._write_environment_evidence(environment_path, python_version, dependencies, worktree_path)
            record(
                AuditGate.ENVIRONMENT, True,
                f"独立解释器环境创建于 {env_dir}（Python {python_version}）",
                {
                    "environment_path": str(env_dir),
                    "python_version": python_version,
                    "python_binary": str(python),
                },
            )

            # ── 门 4：显式历史 src 导入 ──
            probe = self._environment.probe_source(python, worktree_path)
            source_file: str = str(probe.get("dotclaw_file", ""))
            expected_src: str = str((worktree_path / "src").resolve())
            import_ok: bool = source_file.startswith(expected_src)
            if not import_ok:
                raise AuditError(
                    AuditGate.SOURCE_IMPORT,
                    full_commit,
                    f"历史源码导入指向 {source_file!r}，不在 worktree src {expected_src!r} 下",
                )
            record(
                AuditGate.SOURCE_IMPORT, True,
                f"dotclaw 从历史 src 导入：{source_file}",
                {"source_file": source_file, "expected_src": expected_src},
            )

            # ── 门 5：固定业务场景执行与校验 ──
            self._adapter.verify_expected()
            scene_dir: Path = out_dir / "scenario"
            scene_dir.mkdir(parents=True, exist_ok=True)
            first = await self._adapter.run_scenario(
                worktree=worktree_path,
                python=python,
                state_dir=scene_dir / "state-0",
                evidence_dir=scene_dir / "evidence-0",
                attempt=0,
                is_warmup=True,
            )
            if not (first.passed and first.tool_name_ok and first.tool_arguments_ok and first.final_output_ok):
                raise AuditError(
                    AuditGate.SCENARIO,
                    full_commit,
                    f"场景校验失败：终态 {first.end_status}，工具调用 {first.tool_call_count} 次，"
                    f"工具名/参数/最终回答校验="
                    f"{first.tool_name_ok}/{first.tool_arguments_ok}/{first.final_output_ok}",
                )
            record(
                AuditGate.SCENARIO, True,
                f"场景 {self._scenario_id} 执行通过：终态 {first.end_status}，"
                f"工具调用 {first.tool_call_count} 次，工具名/参数/最终回答校验全部通过",
                first.to_dict(),
            )

            # ── 门 6：映射统一记录并连续执行开发期采样 ──
            samples: list[Mapping[str, object]] = []
            for round_index in range(warmup + repeat):
                is_warmup_sample: bool = round_index < warmup
                attempt: int = round_index if is_warmup_sample else round_index - warmup
                scene = await self._adapter.run_scenario(
                    worktree=worktree_path,
                    python=python,
                    state_dir=scene_dir / f"state-{round_index + 1}",
                    evidence_dir=scene_dir / f"evidence-{round_index + 1}",
                    attempt=attempt,
                    is_warmup=is_warmup_sample,
                )
                samples.append(
                    self._to_sample(scene, attempt=attempt, is_warmup=is_warmup_sample, full_commit=full_commit)
                )
            samples_path = out_dir / "samples" / f"{audit_id}.jsonl"
            self._write_samples(samples_path, samples)
            record(
                AuditGate.MAPPING, True,
                f"开发期采样写出 {len(samples)} 条（warmup={warmup}, repeat={repeat}）",
                {"samples_path": str(samples_path), "warmup": warmup, "repeat": repeat},
            )

            report = AuditReport(
                audit_id=audit_id,
                candidate=candidate,
                full_commit=full_commit,
                passed=True,
                dataset=self._dataset,
                case_id=self._case_id,
                scenario_id=self._scenario_id,
                gates=tuple(gates),
                worktree_path=str(worktree_path),
                environment_path=str(environment_path),
                samples_path=str(samples_path),
                fixture_fingerprint=self._fixture_fingerprint,
                candidate_order=order,
                selection_note=note,
                created_at=datetime.now(timezone.utc).isoformat(),
                git_commit=self._head_short(),
            )
            self._write_report(out_dir, report)
            return report

        except AuditError as error:
            failed_candidate: str = candidate if error.candidate == "?" else error.candidate
            resolved: str = self._git.resolve_full_commit(failed_candidate) if self._is_resolvable(failed_candidate) else ""
            report = AuditReport(
                audit_id=make_audit_id(failed_candidate[:12]),
                candidate=candidate,
                full_commit=full_commit or resolved,
                passed=False,
                dataset=self._dataset,
                case_id=self._case_id,
                scenario_id=self._scenario_id,
                gates=tuple(gates),
                worktree_path=str(worktree_path) if worktree_path is not None else None,
                environment_path=str(environment_path) if environment_path is not None else None,
                fixture_fingerprint=self._fixture_fingerprint,
                candidate_order=order,
                selection_note=f"审计失败：门 {error.gate.value} —— {error.summary}",
                created_at=datetime.now(timezone.utc).isoformat(),
                git_commit=self._head_short(),
            )
            out_dir = self._output_root / report.audit_id
            out_dir.mkdir(parents=True, exist_ok=True)
            self._write_report(out_dir, report)
            raise

    def _is_resolvable(self, candidate: str) -> bool:
        """候选是否可解析为提交（用于失败报告落盘）。"""
        try:
            self._git.resolve_full_commit(candidate)
            return True
        except AuditError:
            return False

    def _head_short(self) -> str:
        """当前 HEAD 的短提交号；获取失败时不阻断报告落盘。"""
        try:
            return self._git.short_commit(self._git.resolve_full_commit("HEAD"))
        except AuditError:
            return "unknown"

    def _write_environment_evidence(self, environment_path: Path, python_version: str, dependencies: str, worktree_path: Path) -> None:
        """写出环境证据：Python 版本、依赖解析与源码路径。"""
        environment_path.mkdir(parents=True, exist_ok=True)
        (environment_path / "python_version.txt").write_text(python_version + "\n", encoding="utf-8")
        (environment_path / "dependencies.txt").write_text(dependencies + "\n", encoding="utf-8")
        (environment_path / "source_path.txt").write_text(str((worktree_path / "src").resolve()) + "\n", encoding="utf-8")

    def _to_sample(
        self,
        scene: ScenarioSample,
        *,
        attempt: int,
        is_warmup: bool,
        full_commit: str,
    ) -> Mapping[str, object]:
        """把场景语义事实映射为统一记录（委托模块级函数）。"""
        return to_sample_record(
            scene,
            dataset=self._dataset,
            case_id=self._case_id,
            scenario_id=self._scenario_id,
            fixture_fingerprint=self._fixture_fingerprint,
            git_commit=self._git.short_commit(full_commit),
            full_commit=full_commit,
            attempt=attempt,
            is_warmup=is_warmup,
        )

    def _write_samples(self, path: Path, samples: Sequence[Mapping[str, object]]) -> None:
        """逐条写出开发期采样 JSONL。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for sample in samples:
                fh.write(json.dumps(sample, ensure_ascii=False) + "\n")

    def _write_report(self, out_dir: Path, report: AuditReport) -> None:
        """原子写出审计报告。"""
        (out_dir / "audit.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
