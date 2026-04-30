"""配置模块：YAML 配置加载"""

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
    """递归替换 ${ENV_VAR} 为环境变量值"""
    if isinstance(value, str):
        pattern = re.compile(r'\$\{([^}]+)\}')

        def replacer(m: re.Match) -> str:
            var_name = m.group(1)
            return os.environ.get(var_name, m.group(0))

        return pattern.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


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
        # 返回默认配置
        return Config()

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # 展开环境变量
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
