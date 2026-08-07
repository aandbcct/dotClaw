"""PR2 旧 Agent v1 适配器：场景执行映射、替身日志校验与失败分类。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.historical_audit import AuditError, AuditGate
from benchmarks.historical_legacy_agent_v1 import (
    LegacyAgentV1Adapter,
    _build_launch_script,
)


class FakeProc:
    """伪造 subprocess 返回。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """绑定返回码与输出。"""
        self.returncode: int = returncode
        self.stdout: str = stdout
        self.stderr: str = stderr


def _adapter(tmp_path: Path) -> LegacyAgentV1Adapter:
    """构造绑定临时脚本目录的适配器。"""
    return LegacyAgentV1Adapter(
        dataset="runtime_core_v1",
        case_id="tool_success",
        script_dir=tmp_path / "scripts",
        subprocess_timeout=30.0,
    )


def _valid_output() -> str:
    """构造一段合法的历史场景输出。"""
    return json.dumps({
        "end_status": "completed",
        "tool_calls": 1,
        "iterations": 2,
        "final_output": "The weather is sunny today",
        "duration_ms": 3,
        "tokens_in": 0,
        "tokens_out": 0,
        "wall_duration_ms": 5.0,
        "error": None,
        "evidence_refs": [],
    })


def _write_tool_log(evidence_dir: Path, *, tool: str = "search", q: str = "weather") -> None:
    """写出记录型替身日志。"""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "tool_log.jsonl").write_text(
        json.dumps({"tool": tool, "arguments": {"q": q}}) + "\n", encoding="utf-8"
    )


def _run_scenario(tmp_path: Path, adapter: LegacyAgentV1Adapter, *, output: str, evidence_dir: Path, monkeypatch) -> object:
    """以伪造子进程边界执行一次场景。"""
    import benchmarks.historical_legacy_agent_v1 as module

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: FakeProc(stdout=output))
    import asyncio

    return asyncio.run(adapter.run_scenario(
        worktree=tmp_path / "worktree",
        python=tmp_path / "python.exe",
        state_dir=tmp_path / "state",
        evidence_dir=evidence_dir,
        attempt=0,
        is_warmup=False,
    ))


# --------------------------------------------------------------------------- #
# 正常路径：映射与校验
# --------------------------------------------------------------------------- #


def test_scenario_maps_to_passing_sample(tmp_path: Path, monkeypatch) -> None:
    """固定历史输出 + 替身日志可映射为通过的历史样本。"""
    evidence_dir = tmp_path / "evidence"
    _write_tool_log(evidence_dir)
    sample = _run_scenario(tmp_path, _adapter(tmp_path), output=_valid_output(), evidence_dir=evidence_dir, monkeypatch=monkeypatch)

    assert sample.passed is True
    assert sample.failure_kind is None
    assert sample.end_status == "completed"
    assert sample.tool_call_count == 1
    assert sample.loop_iterations == 2
    assert sample.tool_name_ok is True
    assert sample.tool_arguments_ok is True
    assert sample.final_output_ok is True
    assert sample.wall_duration_ms == 5.0


def test_scenario_sample_maps_to_pr1_schema_json(tmp_path: Path, monkeypatch) -> None:
    """历史样本可映射为与 PR1 相同 schema 的记录：缺失 Trace/token 为 null。"""
    from benchmarks.eval_baseline_models import BenchmarkSample

    evidence_dir = tmp_path / "evidence"
    _write_tool_log(evidence_dir)
    output = json.loads(_valid_output())
    output["tokens_in"] = None
    output["tokens_out"] = None
    sample = _run_scenario(tmp_path, _adapter(tmp_path), output=json.dumps(output), evidence_dir=evidence_dir, monkeypatch=monkeypatch)

    # 直接经 PR1 模型反序列化，验证 schema 兼容
    record = {
        "schema_version": "1.0",
        "suite": "runtime_core",
        "dataset": "runtime_core_v1",
        "case_id": "tool_success",
        "attempt": 0,
        "is_warmup": False,
        "git_commit": "4e4cdd3",
        "python_version": "historical",
        "platform": "historical",
        "config_hash": "historical-adapter",
        "eval_schema_version": "1.0",
        "execution_source": "historical_adapter",
        "source_commit": "4e4cdd3a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
        "scenario_id": "tool_success",
        "evidence_kind": "final_result",
        "passed": sample.passed,
        "failure_kind": sample.failure_kind,
        "assertions_passed": 1 if sample.passed else 0,
        "assertions_total": 1,
        "trace_available": False,
        "wall_duration_ms": sample.wall_duration_ms,
        "run_id": None,
        "trace_metrics": {"llm_duration_ms": None, "tool_duration_ms": None, "approval_wait_ms": None, "critical_path_ms": None},
        "run_statistics": {"duration_ms": None, "llm_call_count": sample.loop_iterations, "tool_call_count": sample.tool_call_count, "tokens_in": None, "tokens_out": None},
        "trace_source": None,
    }
    restored = BenchmarkSample.from_dict(record)
    assert restored.execution_source.value == "historical_adapter"
    assert restored.evidence_kind.value == "final_result"
    assert restored.trace_available is False
    assert restored.run_statistics["tokens_in"] is None
    assert restored.run_statistics["llm_call_count"] == 2


# --------------------------------------------------------------------------- #
# 边界：场景校验失败分类
# --------------------------------------------------------------------------- #


def test_rejects_tool_name_mismatch(tmp_path: Path, monkeypatch) -> None:
    """工具名不匹配时不得映射为通过。"""
    evidence_dir = tmp_path / "evidence"
    _write_tool_log(evidence_dir, tool="translate")
    sample = _run_scenario(tmp_path, _adapter(tmp_path), output=_valid_output(), evidence_dir=evidence_dir, monkeypatch=monkeypatch)
    assert sample.passed is False
    assert sample.failure_kind == "assertion"
    assert sample.tool_name_ok is False


def test_rejects_tool_arguments_mismatch(tmp_path: Path, monkeypatch) -> None:
    """关键参数不匹配时不得映射为通过。"""
    evidence_dir = tmp_path / "evidence"
    _write_tool_log(evidence_dir, q="location")
    sample = _run_scenario(tmp_path, _adapter(tmp_path), output=_valid_output(), evidence_dir=evidence_dir, monkeypatch=monkeypatch)
    assert sample.passed is False
    assert sample.tool_arguments_ok is False


def test_rejects_final_output_mismatch(tmp_path: Path, monkeypatch) -> None:
    """最终回答不包含期望标记时不得映射为通过。"""
    evidence_dir = tmp_path / "evidence"
    _write_tool_log(evidence_dir)
    output = json.loads(_valid_output())
    output["final_output"] = "I cannot answer"
    sample = _run_scenario(tmp_path, _adapter(tmp_path), output=json.dumps(output), evidence_dir=evidence_dir, monkeypatch=monkeypatch)
    assert sample.passed is False
    assert sample.final_output_ok is False


def test_rejects_claimed_success_without_tool_log(tmp_path: Path, monkeypatch) -> None:
    """声称成功但工具日志缺失：不信任终态，不映射为通过。"""
    evidence_dir = tmp_path / "evidence"  # 不写日志
    sample = _run_scenario(tmp_path, _adapter(tmp_path), output=_valid_output(), evidence_dir=evidence_dir, monkeypatch=monkeypatch)
    assert sample.passed is False
    assert sample.tool_name_ok is False
    assert sample.tool_arguments_ok is False


def test_rejects_non_completed_end_status(tmp_path: Path, monkeypatch) -> None:
    """终态不是 completed 时映射为失败。"""
    evidence_dir = tmp_path / "evidence"
    _write_tool_log(evidence_dir)
    output = json.loads(_valid_output())
    output["end_status"] = "failed"
    sample = _run_scenario(tmp_path, _adapter(tmp_path), output=json.dumps(output), evidence_dir=evidence_dir, monkeypatch=monkeypatch)
    assert sample.passed is False


# --------------------------------------------------------------------------- #
# 数据损坏：子进程失败与不可解析输出
# --------------------------------------------------------------------------- #


def test_subprocess_failure_is_audit_error(tmp_path: Path, monkeypatch) -> None:
    """历史场景子进程失败：审计失败并记录摘要。"""
    import benchmarks.historical_legacy_agent_v1 as module

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: FakeProc(returncode=1, stderr="boom"))
    import asyncio

    adapter = _adapter(tmp_path)
    with pytest.raises(AuditError) as exc:
        asyncio.run(adapter.run_scenario(
            worktree=tmp_path / "wt",
            python=tmp_path / "python.exe",
            state_dir=tmp_path / "s",
            evidence_dir=tmp_path / "e",
            attempt=0,
            is_warmup=False,
        ))
    assert exc.value.gate is AuditGate.SCENARIO
    assert "exit=1" in exc.value.summary


def test_unparsable_output_is_audit_error(tmp_path: Path, monkeypatch) -> None:
    """历史场景输出不可解析：审计失败。"""
    import benchmarks.historical_legacy_agent_v1 as module

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: FakeProc(stdout="not json"))
    import asyncio

    adapter = _adapter(tmp_path)
    with pytest.raises(AuditError):
        asyncio.run(adapter.run_scenario(
            worktree=tmp_path / "wt",
            python=tmp_path / "python.exe",
            state_dir=tmp_path / "s",
            evidence_dir=tmp_path / "e",
            attempt=0,
            is_warmup=False,
        ))


# --------------------------------------------------------------------------- #
# 脚本生成与场景约束
# --------------------------------------------------------------------------- #


def test_launch_script_contains_historical_assembly() -> None:
    """启动脚本显式从历史 src 导入并装配 AgentLoop / LLMProxy / 替身工具。"""
    script = _build_launch_script()
    assert "sys.path.insert(0, str(_WORKTREE / \"src\"))" in script
    assert "from dotclaw.agent.loop import AgentLoop" in script
    assert "from dotclaw.agent.runtime import AgentRuntime" in script
    assert "from dotclaw.llm.proxy import LLMProxy" in script
    assert "BuiltinToolHandler" in script
    assert "tool_log.jsonl" in script


def test_script_written_once_and_reused(tmp_path: Path, monkeypatch) -> None:
    """同一脚本文件复用多次采样，不重复生成。"""
    import benchmarks.historical_legacy_agent_v1 as module

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: FakeProc(stdout=_valid_output()))
    import asyncio

    adapter = _adapter(tmp_path)
    evidence_dir = tmp_path / "evidence"
    _write_tool_log(evidence_dir)
    kwargs = dict(
        worktree=tmp_path / "wt",
        python=tmp_path / "python.exe",
        state_dir=tmp_path / "state",
        evidence_dir=evidence_dir,
        attempt=0,
        is_warmup=False,
    )
    asyncio.run(adapter.run_scenario(**kwargs))
    script_path = tmp_path / "scripts" / "legacy_agent_v1_scenario.py"
    assert script_path.exists()
    first_content = script_path.read_text(encoding="utf-8")
    asyncio.run(adapter.run_scenario(**kwargs))
    assert script_path.read_text(encoding="utf-8") == first_content


def test_adapter_rejects_wrong_scenario(tmp_path: Path) -> None:
    """适配器只服务 tool_success 场景，其它场景构造即失败。"""
    with pytest.raises(AuditError):
        LegacyAgentV1Adapter(
            dataset="runtime_core_v1",
            case_id="other",
            scenario_id="other_scenario",
            script_dir=tmp_path / "scripts",
        )
