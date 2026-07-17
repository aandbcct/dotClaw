"""Runtime 重构 Phase 6 的物理删除与公开边界验收测试。"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


def test_obsolete_runtime_implementations_are_physically_removed() -> None:
    """旧执行、状态、上下文、任务和 StateSink 文件必须不再留在生产树。"""
    obsolete_paths: tuple[str, ...] = (
        "src/dotclaw/runtime/runtime.py",
        "src/dotclaw/runtime/state_store.py",
        "src/dotclaw/runtime/agent_state.py",
        "src/dotclaw/runtime/task.py",
        "src/dotclaw/session/agent_run.py",
        "src/dotclaw/agent/resume.py",
        "src/dotclaw/agent/slotContext.py",
        "src/dotclaw/agent/slotContextImp.py",
        "src/dotclaw/orchestration/runners/local.py",
        "src/dotclaw/tools/builtin/task_tool.py",
        "src/dotclaw/journal/sinks/state_sink.py",
    )
    remaining_paths: list[str] = [
        relative_path
        for relative_path in obsolete_paths
        if (PROJECT_ROOT / relative_path).exists()
    ]

    assert not remaining_paths, "\n".join(remaining_paths)


def test_production_source_has_no_obsolete_runtime_imports() -> None:
    """当前生产代码不得保留已删除模块的导入或符号引用。"""
    forbidden_tokens: tuple[str, ...] = (
        "runtime.runtime",
        "runtime.state_store",
        "runtime.agent_state",
        "runtime.task",
        "session.agent_run",
        "agent.slotContext",
        "slotContextImp",
        "agent.resume",
        "runners.local",
        "task_tool",
        "StateSink",
        "StateStore",
        "ContextAssembler",
    )
    source_root: Path = PROJECT_ROOT / "src" / "dotclaw"
    violations: list[str] = []
    source_path: Path
    for source_path in source_root.rglob("*.py"):
        source: str = source_path.read_text(encoding="utf-8")
        token: str
        for token in forbidden_tokens:
            if token in source:
                violations.append(f"{source_path.relative_to(PROJECT_ROOT)} -> {token}")

    assert not violations, "\n".join(violations)
