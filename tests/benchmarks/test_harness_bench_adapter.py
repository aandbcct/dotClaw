"""Harness-Bench Generic CLI 适配入口的隔离与持久化契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotclaw.benchmark_runner import (
    _load_or_create_session,
    _require_inside,
    _write_isolated_profile,
)
from dotclaw.bootstrap.application_host import ApplicationHost


def test_require_inside_rejects_workspace_escape(tmp_path: Path) -> None:
    """适配入口不得接受 benchmark sandbox 外部路径。"""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    with pytest.raises(ValueError, match="必须位于 benchmark sandbox 内"):
        _require_inside(tmp_path / "outside", sandbox, "workspace")


def test_isolated_profile_uses_proxy_and_never_persists_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """临时路由只保存环境变量引用，并把真实上游注册到 usage proxy。"""
    sandbox = tmp_path / "sandbox"
    workspace = sandbox / "workspace"
    proxy_dir = sandbox / "usage-proxy"
    workspace.mkdir(parents=True)
    proxy_dir.mkdir()
    routes_file = proxy_dir / "routes.json"
    routes_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HARNESSBENCH_LLM_PROXY_URL", "http://127.0.0.1:43210")
    monkeypatch.setenv("HARNESSBENCH_LLM_PROXY_ROUTES", str(routes_file))

    profile_root, config = _write_isolated_profile(sandbox, workspace, "qwen3.7-plus")

    router_text = (profile_root / "model_router_config.yaml").read_text(encoding="utf-8")
    routes = json.loads(routes_file.read_text(encoding="utf-8"))
    assert "${DOTCLAW_BENCHMARK_API_KEY}" in router_text
    assert "api_key:" in router_text
    assert routes["/dotclaw/qwen"]["framework"] == "dotclaw"
    assert config.session.directory.startswith(str(sandbox))
    assert config.memory.workspace.startswith(str(sandbox))
    assert config.tools.policy.workspace_root == str(workspace)
    assert config.tools.policy.rules["workspace.write"] == "allow"
    assert config.tools.policy.rules["process.exec"] == "allow"
    assert config.tools.policy.rules["network.http"] == "deny"
    assert config.tools.unattended_allow_profiles == ["workspace.write", "process.exec"]
    assert config.tools.mcp_enabled is False


async def test_session_mapping_survives_application_host_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两轮独立 Host 生命周期必须重新加载同一个持久化 Session。"""
    sandbox = tmp_path / "sandbox"
    workspace = sandbox / "workspace"
    proxy_dir = sandbox / "usage-proxy"
    workspace.mkdir(parents=True)
    proxy_dir.mkdir()
    routes_file = proxy_dir / "routes.json"
    routes_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HARNESSBENCH_LLM_PROXY_URL", "http://127.0.0.1:43210")
    monkeypatch.setenv("HARNESSBENCH_LLM_PROXY_ROUTES", str(routes_file))
    profile_root, config = _write_isolated_profile(sandbox, workspace, "qwen3.7-plus")

    first_host = ApplicationHost(config, profile_root)
    await first_host.initialize()
    assert first_host.tool_executor is not None
    assert first_host.tool_executor.requires_approval("builtin.files.write_text") is False
    assert first_host.tool_executor.requires_approval("builtin.process.execute") is False
    first = await _load_or_create_session(first_host, profile_root, "harness-session-1")
    await first_host.shutdown()

    second_host = ApplicationHost(config, profile_root)
    await second_host.initialize()
    second = await _load_or_create_session(second_host, profile_root, "harness-session-1")
    await second_host.shutdown()

    assert second.id == first.id
    assert second.model == first.model == "qwen3.7-plus"
