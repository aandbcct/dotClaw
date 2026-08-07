"""PR4 工具副作用前 checkpoint（检查点）的代表性子进程强制退出验证。"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .recovery_assertions import checkpoint_summary, collect_recovery_facts
from .recovery_faults import EffectLog, interrupt_initial_run, make_engine
from dotclaw.runtime.adapters import RunRepositoryAdapter

_EXPECTED_EXIT_CODE = 97


@dataclass(frozen=True)
class SubprocessRecoveryResult:
    """一个真实进程边界恢复样本的最小事实。"""

    exit_code: int
    run_id: str
    control_pass: bool
    internal_pass: bool
    tool_effect_count: int
    recovery_ms: float
    evidence_summary: Mapping[str, object]


async def _child_prepare(root: Path) -> None:
    """在子进程将工具前 checkpoint 写盘后以不可捕获退出码终止。"""
    await interrupt_initial_run(root, "tool_before_effect", "subprocess")


def _child_main(root: Path) -> int:
    """运行子进程故障端；故意不用 finally，模拟进程突然结束。"""
    asyncio.run(_child_prepare(root))
    os._exit(_EXPECTED_EXIT_CODE)


async def run_subprocess_recovery(root: Path) -> SubprocessRecoveryResult:
    """启动子进程、校验退出码，再由父进程新装配服务恢复。"""
    command = [sys.executable, "-m", "benchmarks.recovery_subprocess", "--child", str(root)]
    process = subprocess.run(command, cwd=Path.cwd(), check=False)
    if process.returncode != _EXPECTED_EXIT_CODE:
        raise RuntimeError(f"子进程退出码错误：预期 {_EXPECTED_EXIT_CODE}，实际 {process.returncode}")
    repository = RunRepositoryAdapter(root)
    active = await repository.list_active_runs(next(path.name for path in root.iterdir() if path.is_dir() and path.name != "evidence"))
    if len(active) != 1:
        raise RuntimeError("子进程退出后未找到唯一活动 Run")
    run = active[0]
    before_action, _ = await checkpoint_summary(root, run.session_id, run.run_id)
    before_context_count = len(await repository.load_context_versions(run.session_id, run.run_id))
    started = time.perf_counter()
    result = await make_engine(root, "tool_before_effect", resume=True).resume_run(run.run_id)
    recovery_ms = (time.perf_counter() - started) * 1000.0
    facts = await collect_recovery_facts(root, run.session_id, run.run_id, before_action=before_action, before_context_count=before_context_count)
    persisted_files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
    evidence = {
        "subprocess_command": command,
        "subprocess_exit_code": process.returncode,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "source_commit": _source_commit(),
        "persisted_files": persisted_files,
        "checkpoint_action_before": before_action,
    }
    return SubprocessRecoveryResult(process.returncode, run.run_id, facts.control_recovery_pass and before_action == "execute_tools" and result.run_id == run.run_id, facts.internal_facts_pass and facts.tool_result_count == 1, EffectLog(root).count("tool_effect"), recovery_ms, evidence)


def _source_commit() -> str:
    """记录验证时的源码提交；不可用时明确写 unknown（未知）。"""
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path.cwd(), capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main(argv: list[str] | None = None) -> int:
    """提供内部子进程入口；父进程调用 API（应用接口）而非解析 stdout。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    args = parser.parse_args(argv)
    if args.child is None:
        parser.error("该模块仅由恢复套件作为子进程调用")
    return _child_main(args.child)


if __name__ == "__main__":
    raise SystemExit(main())
