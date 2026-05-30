"""配置模块：YAML 配置加载 + Phase 2 路由配置"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录（包含 config.yaml）"""
    import dotclaw
    module_path = Path(dotclaw.__file__).parent  # src/dotclaw/
    return module_path.parent.parent  # 项目根目录


def _expand_env(value: Any) -> Any:
    """递归替换 ${ENV_VAR} 为环境变量值（委托 common.utils）"""
    from dotclaw.common.utils import expand_env_vars
    return expand_env_vars(value)


# ============================================================
# P1 遗留 dataclass（保持后向兼容）
# ============================================================

@dataclass
class LLMClientConfig:
    provider: str = "qwen"
    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen-plus"


@dataclass
class LLMConfig:
    default_model: str = "qwen-plus"
    clients: dict[str, LLMClientConfig] = field(default_factory=dict)
    fallbacks: list[str] = field(default_factory=list)
    retry_max_retries: int = 3
    retry_base_delay: float = 1.0
    stream: bool = True


@dataclass
class AgentConfig:
    system_prompt: str = "你是一个有用、诚实且友好的 AI 助手。"
    max_context_tokens: int = 8000
    keep_recent_messages: int = 10
    rules: str = ""   # P3 新增：额外行为规则，追加到 system prompt


@dataclass
class ToolsConfig:
    exec_enabled: bool = True
    exec_needs_approval: bool = True
    python_enabled: bool = True
    python_needs_approval: bool = True
    python_timeout: int = 30
    web_search_enabled: bool = False


@dataclass
class SkillsConfig:
    directory: str = "./skills"


@dataclass
class MemoryConfig:
    long_term_file: str = "./data/memory/MEMORY.md"


@dataclass
class SessionConfig:
    directory: str = "./data/sessions"


@dataclass
class SchedulerConfig:
    enabled: bool = True


@dataclass
class DebugConfig:
    level: str = "INFO"
    log_file: str = "./data/dotclaw.log"


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)


# ============================================================
# P2 新增 — 路由配置 dataclass
# ============================================================

@dataclass
class ProviderRetryConfig:
    max_attempts: int = 3
    backoff_factor: float = 2.0


@dataclass
class ProviderConfig:
    """供应商配置"""
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    rate_limit: dict = field(default_factory=dict)
    retry: ProviderRetryConfig = field(default_factory=ProviderRetryConfig)


@dataclass
class ModelConfig:
    """模型配置"""
    provider: str = "qwen"
    model_id: str = "qwen-plus"
    context_window: int = 32000
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    status: str = "active"


@dataclass
class PurposePriority:
    """用途路由优先级 — priority 越小越优先"""
    model: str = ""
    priority: int = 1


@dataclass
class PurposeConfig:
    """用途配置 — 降级链从 priority 列表自动生成"""
    description: str = ""
    priority: list[PurposePriority] = field(default_factory=list)


@dataclass
class DefaultsConfig:
    """全局默认"""
    provider: str = "qwen"
    model: str = "qwen-plus"
    parameters: dict = field(default_factory=dict)
    fallback_enabled: bool = True


@dataclass
class RouterConfig:
    """模型路由配置（聚合）"""
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    purposes: dict[str, PurposeConfig] = field(default_factory=dict)


# ============================================================
# P2 新增 — 路由配置加载
# ============================================================

def load_router_config(path: str | Path | None = None) -> RouterConfig:
    """
    加载 model_router_config.yaml。

    支持 ${ENV_VAR} 环境变量展开。
    """
    if path is None:
        path = _find_project_root() / "model_router_config.yaml"
    elif not Path(path).is_absolute():
        path = _find_project_root() / path

    from dotclaw.common.utils import safe_load_yaml, expand_env_vars
    raw = safe_load_yaml(Path(path))
    if not raw:
        return RouterConfig()

    raw = expand_env_vars(raw)

    # defaults
    defaults_raw = raw.get("defaults", {})
    defaults = DefaultsConfig(
        provider=defaults_raw.get("provider", "qwen"),
        model=defaults_raw.get("model", "qwen-plus"),
        parameters=defaults_raw.get("parameters", {}),
        fallback_enabled=defaults_raw.get("fallback_enabled", True),
    )

    # providers
    providers = {}
    for name, cfg in raw.get("providers", {}).items():
        retry_raw = cfg.get("retry", {})
        providers[name] = ProviderConfig(
            api_key=cfg.get("api_key", ""),
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            rate_limit=cfg.get("rate_limit", {}),
            retry=ProviderRetryConfig(
                max_attempts=retry_raw.get("max_attempts", 3),
                backoff_factor=retry_raw.get("backoff_factor", 2.0),
            ),
        )

    # models
    models = {}
    for name, cfg in raw.get("models", {}).items():
        models[name] = ModelConfig(
            provider=cfg.get("provider", "qwen"),
            model_id=cfg.get("model_id", name),
            context_window=cfg.get("context_window", 32000),
            capabilities=cfg.get("capabilities", ["chat"]),
            status=cfg.get("status", "active"),
        )

    # purposes
    purposes = {}
    for name, cfg in raw.get("purposes", {}).items():
        priorities = [
            PurposePriority(model=p.get("model", ""), priority=p.get("priority", p.get("weight", 1)))
            for p in cfg.get("priority", [])
        ]
        purposes[name] = PurposeConfig(
            description=cfg.get("description", ""),
            priority=priorities,
        )

    return RouterConfig(
        defaults=defaults,
        providers=providers,
        models=models,
        purposes=purposes,
    )


def _build_router_config_from_legacy(llm_config: LLMConfig) -> RouterConfig:
    """
    从旧 config.yaml 的 llm.clients 格式自动构建 RouterConfig。

    规则：
    - defaults.provider 从第一个 client 的 provider 推断
    - defaults.model = llm_config.default_model
    - providers: 每个 client 的 provider 生成一个 ProviderConfig
    - models: 每个 client 映射为一个 ModelConfig
    - purposes.chat.priority: 按 clients 顺序排列，权重平均分配
    - purposes.chat.fallback_chain = llm_config.fallbacks
    """
    clients = llm_config.clients
    if not clients:
        return RouterConfig()

    # 推断 provider
    first_client = next(iter(clients.values()))
    inferred_provider = first_client.provider if first_client.provider else "qwen"

    defaults = DefaultsConfig(
        provider=inferred_provider,
        model=llm_config.default_model,
        parameters={"temperature": 0.7, "max_tokens": 4096},
        fallback_enabled=True,
    )

    # 构建 providers（去重：同一 provider 只取第一个 client 的配置）
    providers = {}
    seen_providers = set()
    for name, cfg in clients.items():
        provider_name = cfg.provider or "qwen"
        if provider_name not in seen_providers:
            seen_providers.add(provider_name)
            providers[provider_name] = ProviderConfig(
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                rate_limit={"requests_per_minute": 0},
                retry=ProviderRetryConfig(
                    max_attempts=llm_config.retry_max_retries,
                    backoff_factor=llm_config.retry_base_delay,
                ),
            )

    # 构建 models
    models = {}
    for name, cfg in clients.items():
        models[name] = ModelConfig(
            provider=cfg.provider or "qwen",
            model_id=cfg.model or name,
            context_window=32000,
            capabilities=["chat", "function_calling"],
            status="active",
        )

    # 构建 purposes
    n = len(clients)
    priorities = []
    for i, name in enumerate(clients):
        priorities.append(PurposePriority(model=name, priority=i + 1))

    purposes = {
        "chat": PurposeConfig(
            description="日常对话（从 config.yaml 自动生成）",
            priority=priorities,
        ),
    }

    return RouterConfig(
        defaults=defaults,
        providers=providers,
        models=models,
        purposes=purposes,
    )


# ============================================================
# P1 遗留 — load_config()
# ============================================================

def _raw_to_config(raw: dict[str, Any]) -> Config:
    """将字典转换为 Config dataclass"""
    llm_raw = raw.get("llm", {})
    clients = {}
    for name, cfg in llm_raw.get("clients", {}).items():
        clients[name] = LLMClientConfig(**cfg)

    llm = LLMConfig(
        default_model=llm_raw.get("default_model", "qwen-plus"),
        clients=clients,
        fallbacks=llm_raw.get("fallbacks", []),
        retry_max_retries=llm_raw.get("retry", {}).get("max_retries", 3),
        retry_base_delay=llm_raw.get("retry", {}).get("base_delay", 1.0),
        stream=llm_raw.get("stream", True),
    )

    agent_raw = raw.get("agent", {})
    agent = AgentConfig(
        system_prompt=agent_raw.get("system_prompt", ""),
        max_context_tokens=agent_raw.get("max_context_tokens", 8000),
        keep_recent_messages=agent_raw.get("keep_recent_messages", 10),
        rules=agent_raw.get("rules", ""),
    )

    tools_raw = raw.get("tools", {})
    tools = ToolsConfig(
        exec_enabled=tools_raw.get("exec", {}).get("enabled", True),
        exec_needs_approval=tools_raw.get("exec", {}).get("needs_approval", True),
        python_enabled=tools_raw.get("python", {}).get("enabled", True),
        python_needs_approval=tools_raw.get("python", {}).get("needs_approval", True),
        python_timeout=tools_raw.get("python", {}).get("timeout", 30),
        web_search_enabled=tools_raw.get("web_search", {}).get("enabled", False),
    )

    skills = SkillsConfig(
        directory=raw.get("skills", {}).get("directory", "./skills"),
    )
    memory = MemoryConfig(
        long_term_file=raw.get("memory", {}).get("long_term_file", "./data/memory/MEMORY.md"),
    )
    session = SessionConfig(
        directory=raw.get("session", {}).get("directory", "./data/sessions"),
    )
    scheduler = SchedulerConfig(
        enabled=raw.get("scheduler", {}).get("enabled", True),
    )
    debug_raw = raw.get("debug", {})
    debug = DebugConfig(
        level=debug_raw.get("level", "INFO"),
        log_file=debug_raw.get("log_file", "./data/dotclaw.log"),
    )

    return Config(
        llm=llm,
        agent=agent,
        tools=tools,
        skills=skills,
        memory=memory,
        session=session,
        scheduler=scheduler,
        debug=debug,
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    """
    加载 YAML 配置文件。

    支持 ${ENV_VAR} 环境变量展开。
    默认从项目根目录（config.yaml 所在目录）加载。
    """
    if Path(path).is_absolute():
        config_path = Path(path)
    else:
        config_path = _find_project_root() / path
    if not config_path.exists():
        return Config()

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _expand_env(raw)
    return _raw_to_config(raw)


# ---------------------------------------------------------------------------
# 全局配置单例（懒加载）
# ---------------------------------------------------------------------------
_config: Config | None = None


def get_config() -> Config:
    """获取全局配置单例"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
