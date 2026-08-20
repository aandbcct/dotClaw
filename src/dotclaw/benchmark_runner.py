"""Harness-Bench 的非交互协议适配入口。

该模块只负责把官方 Generic CLI 协议转换为 dotClaw 正式应用主链调用，
不实现独立 Agent 循环，也不包含任何按 task_id 分支的解题逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Sequence

import yaml

from dotclaw.bootstrap.application_host import ApplicationHost
from dotclaw.config import _find_project_root, load_config, load_router_config
from dotclaw.config.settings import Config
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.session.session import Session


_BENCHMARK_API_KEY_ENV = "DOTCLAW_BENCHMARK_API_KEY"
_BENCHMARK_UPSTREAM_ENV = "DOTCLAW_BENCHMARK_UPSTREAM_BASE_URL"


def _parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """解析 Harness-Bench Generic CLI 传入的固定协议字段。"""
    parser = argparse.ArgumentParser(description="dotClaw Harness-Bench 非交互适配器")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--model", required=True)
    return parser.parse_args(args)


def _require_inside(path: Path, root: Path, label: str) -> Path:
    """校验路径位于 benchmark sandbox 内，防止适配层扩大文件权限。"""
    resolved = path.resolve()
    sandbox = root.resolve()
    if not resolved.is_relative_to(sandbox):
        raise ValueError(f"{label} 必须位于 benchmark sandbox 内: {resolved}")
    return resolved


def _register_proxy_route(routes_file: Path, prefix: str, provider: str, upstream: str) -> None:
    """向当前任务专属 usage proxy 注册 dotClaw 上游路由。"""
    routes: dict[str, dict[str, str]] = {}
    if routes_file.is_file():
        loaded = json.loads(routes_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            routes = {
                str(key): {str(item_key): str(item_value) for item_key, item_value in value.items()}
                for key, value in loaded.items()
                if isinstance(value, dict)
            }
    routes[prefix] = {
        "framework": "dotclaw",
        "provider": provider,
        "upstream": upstream.rstrip("/"),
    }
    routes_file.parent.mkdir(parents=True, exist_ok=True)
    routes_file.write_text(json.dumps(routes, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_isolated_profile(sandbox: Path, workspace: Path, model_name: str) -> tuple[Path, Config]:
    """生成仅含隔离路径、单模型和无人值守权限的临时 profile。"""
    source_root = _find_project_root()
    source_router = load_router_config(source_root / "model_router_config.yaml")
    model = source_router.models.get(model_name)
    if model is None or model.status != "active":
        raise ValueError(f"模型不存在或未启用: {model_name}")
    provider = source_router.providers.get(model.provider)
    if provider is None or not provider.base_url:
        raise ValueError(f"模型 {model_name} 缺少可用 provider 配置")

    proxy_url = os.environ.get("HARNESSBENCH_LLM_PROXY_URL", "").rstrip("/")
    routes_raw = os.environ.get("HARNESSBENCH_LLM_PROXY_ROUTES", "")
    if not proxy_url or not routes_raw:
        raise ValueError("缺少 Harness-Bench usage proxy 环境变量")
    routes_file = _require_inside(Path(routes_raw), sandbox, "usage proxy routes")
    route_prefix = f"/dotclaw/{model.provider}"
    upstream = os.environ.get(_BENCHMARK_UPSTREAM_ENV, "").strip() or provider.base_url
    _register_proxy_route(routes_file, route_prefix, model.provider, upstream)

    profile_root = sandbox / ".dotclaw-profile"
    data_root = sandbox / ".dotclaw-data"
    identity_dir = profile_root / ".dotclaw" / "agentConfig"
    identity_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / ".dotclaw" / "agentConfig" / "default.yaml", identity_dir / "default.yaml")

    os.environ[_BENCHMARK_API_KEY_ENV] = provider.api_key
    router_payload = {
        "defaults": {
            "parameters": dict(source_router.defaults.parameters),
            "fallback_enabled": False,
        },
        "providers": {
            model.provider: {
                "driver": provider.driver.value,
                "api_key": f"${{{_BENCHMARK_API_KEY_ENV}}}",
                "base_url": f"{proxy_url}{route_prefix}",
                "rate_limit": dict(provider.rate_limit),
                "circuit_breaker": dict(provider.circuit_breaker),
                "retry": {
                    "max_attempts": provider.retry.max_attempts,
                    "backoff_factor": provider.retry.backoff_factor,
                },
            }
        },
        "models": {
            model_name: {
                "provider": model.provider,
                "model_id": model.model_id,
                "context_window": model.context_window,
                "tokenizer_encoding": model.tokenizer_encoding,
                "capabilities": list(model.capabilities),
                "status": "active",
                "reasoning": {
                    "mode": model.reasoning.mode,
                    "reasoning_tags": {
                        "start": model.reasoning.reasoning_start,
                        "end": model.reasoning.reasoning_end,
                    },
                    "response_tags": {
                        "start": model.reasoning.response_start,
                        "end": model.reasoning.response_end,
                    },
                },
            }
        },
        "purposes": {
            "chat": {"priority": [{"model": model_name, "priority": 1}]},
            "context_compaction": {"priority": [{"model": model_name, "priority": 1}]},
        },
    }
    (profile_root / "model_router_config.yaml").write_text(
        yaml.safe_dump(router_payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    config = deepcopy(load_config(source_root / "config.yaml"))
    config.session.directory = str(data_root / "sessions")
    config.memory.workspace = str(data_root)
    config.memory.long_term_file = str(data_root / "memory" / "MEMORY.md")
    config.memory.db_path = str(data_root / "memory" / "memory.db")
    config.memory.dream_enabled = False
    config.eval.dataset_directory = str(data_root / "datasets")
    config.debug.log_file = str(data_root / "dotclaw.log")
    config.journal.trace_dir = str(data_root / "sessions")
    config.journal.snapshot_dir = str(data_root / "snapshots")
    config.tools.mcp_enabled = False
    config.tools.mcp_servers = []
    config.skills.enabled = False
    config.tools.policy.workspace_root = str(workspace)
    config.tools.policy.rules = {
        "workspace.read": "allow",
        "workspace.write": "allow",
        "process.exec": "allow",
        "network.http": "deny",
        "mcp.connect": "deny",
        "mcp.call": "deny",
    }
    config.tools.approval_commands = []
    config.tools.unattended_allow_profiles = [
        "workspace.write",
        "process.exec",
    ]
    return profile_root, config


def _mapping_path(profile_root: Path, harness_session_id: str) -> Path:
    """用不可逆摘要生成映射文件名，避免外部 session_id 形成路径注入。"""
    digest = hashlib.sha256(harness_session_id.encode("utf-8")).hexdigest()
    return profile_root / "session-map" / f"{digest}.json"


async def _load_or_create_session(
    host: ApplicationHost, profile_root: Path, harness_session_id: str
) -> Session:
    """首次创建 Session，后续进程按映射重新加载同一持久化 Session。"""
    mapping_path = _mapping_path(profile_root, harness_session_id)
    if mapping_path.is_file():
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        dotclaw_session_id = str(mapping.get("dotclaw_session_id", ""))
        session = await host.session_manager.load(dotclaw_session_id)
        if session is None:
            raise RuntimeError("Session 映射存在但持久化 Session 缺失")
        return session

    session = await host.session_interaction.create_session(title="Harness-Bench")
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(
        json.dumps(
            {
                "harness_session_id": harness_session_id,
                "dotclaw_session_id": session.id,
                "created_at": datetime.now().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return session


async def _run(args: argparse.Namespace) -> int:
    """通过正式 ApplicationHost 主链执行一轮 benchmark prompt。"""
    sandbox = Path(args.sandbox).resolve()
    workspace = _require_inside(Path(args.workspace), sandbox, "workspace")
    prompt_file = _require_inside(Path(args.prompt_file), sandbox, "prompt file")
    profile_root, config = _write_isolated_profile(sandbox, workspace, args.model)
    prompt = prompt_file.read_text(encoding="utf-8")

    host = ApplicationHost(config, profile_root)
    await host.initialize()
    try:
        session = await _load_or_create_session(host, profile_root, args.session_id)
        result = await host.session_interaction.submit(session, prompt)
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return 0 if result.state.outcome() is RunOutcome.COMPLETED else 1
    finally:
        await host.shutdown()


def main(args: Sequence[str] | None = None) -> None:
    """命令行入口。"""
    parsed = _parse_args(args)
    raise SystemExit(asyncio.run(_run(parsed)))


if __name__ == "__main__":
    main()
