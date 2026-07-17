"""RuntimeEngine 与 Journal 的依赖隔离测试。"""

from __future__ import annotations

from pathlib import Path


def test_runtime_engine_has_no_journal_or_legacy_orchestration_import() -> None:
    """Engine 必须只经 Ports 调用 delegation，不能回退到 Journal 或 Dispatcher。"""
    source: str = (Path(__file__).resolve().parents[2] / "src/dotclaw/runtime/application/engine.py").read_text(encoding="utf-8")

    assert "journal" not in source.lower()
    assert "orchestration.dispatcher" not in source
    assert "LegacyRuntimeFacade" not in source
