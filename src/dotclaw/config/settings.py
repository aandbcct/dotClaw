"""配置模块：YAML 配置加载 + Phase 2 路由配置（Phase 5 升级）"""

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


def _resolve_memory_path(path_str: str, project_root: Path) -> Path:
    """将相对路径基于 project_root 解析为绝对路径"""
    p = Path(path_str)
    if p.is_absolute():
        return p
    return project_root / p


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
    # Phase 5 新增：source 级启停
    builtin_enabled: bool = True
    mcp_enabled: bool = True       # Phase 5 预留，暂不消费
    skill_enabled: bool = True      # Phase 5 预留，暂不消费

    # 危险命令审批列表
    approval_commands: list[str] = field(default_factory=lambda: ["exec", "python"])

    # 单工具禁用列表（向后兼容旧 config.exec.enabled=false）
    disabled_tools: list[str] = field(default_factory=list)

    # exec 工具配置
    exec_timeout: float = 60.0

    # web_search 配置
    web_search_enabled: bool = False

    # Phase 6 新增：MCP 配置
    mcp_global: McpGlobalConfig = field(default_factory=lambda: McpGlobalConfig())
    mcp_servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class McpGlobalConfig:
    """MCP 全局配置（默认值）"""
    startup_timeout: float = 4.0        # 握手超时（秒）
    tool_timeout: float = 60.0          # 工具调用超时（秒）
    restart_on_crash: bool = True       # 崩溃后是否自动重连
    max_restart_attempts: int = 3       # 最大重连次数


@dataclass
class McpServerConfig:
    """单个 MCP server 配置"""
    name: str = ""                      # server 名称（唯一标识）
    transport: str = "stdio"            # "stdio" | "streamable_http"

    # stdio 传输字段
    command: str = ""                   # 可执行命令
    args: list[str] = field(default_factory=list)

    # streamable_http 传输字段
    url: str = ""                       # HTTP endpoint
    headers: dict = field(default_factory=dict)  # 认证 headers

    # 覆盖全局配置（None 时使用全局默认）
    startup_timeout: float | None = None
    tool_timeout: float | None = None
    restart_on_crash: bool | None = None
    max_restart_attempts: int | None = None

    def get_startup_timeout(self, global_cfg: McpGlobalConfig) -> float:
        return self.startup_timeout if self.startup_timeout is not None else global_cfg.startup_timeout

    def get_tool_timeout(self, global_cfg: McpGlobalConfig) -> float:
        return self.tool_timeout if self.tool_timeout is not None else global_cfg.tool_timeout

    def get_restart_on_crash(self, global_cfg: McpGlobalConfig) -> bool:
        return self.restart_on_crash if self.restart_on_crash is not None else global_cfg.restart_on_crash

    def get_max_restart_attempts(self, global_cfg: McpGlobalConfig) -> int:
        return self.max_restart_attempts if self.max_restart_attempts is not None else global_cfg.max_restart_attempts

    def __post_init__(self):
        """M3 修复：dataclass 层面的基础校验"""
        if not self.name:
            raise ValueError("McpServerConfig.name 不能为空")
        if self.transport not in ("stdio", "streamable_http"):
            raise ValueError(f"不支持的 transport={self.transport}")


@dataclass
class SkillsConfig:
    """Phase 7 扩展：directory 支持列表 + enabled + skip_prefix"""
    directory: str | list[str] = "./skills"
    enabled: bool = True
    skip_prefix: str = "_"


@dataclass
class MemoryConfig:
    # P1 已有
    long_term_file: str = "./data/memory/MEMORY.md"

    # P4 新增
    workspace: str = "./data"
    db_path: str = "./data/memory/memory.db"
    chunk_max_tokens: int = 500
    chunk_overlap_tokens: int = 50
    embedding_provider: str | None = None
    embedding_model: str = "text-embedding-v3"
    embedding_dimensions: int = 1024
    embedding_api_base: str = ""
    embedding_api_key: str = ""
    max_results: int = 5
    min_score: float = 0.1
    vector_weight: float = 0.7
    keyword_weight: float = 0.3
    sync_on_search: bool = True
    # [已废弃] flush 改为每轮触发，不再使用阈值和消息数限制
    flush_threshold: int = 20
    flush_max_messages: int = 10
    dream_enabled: bool = True
    dream_schedule: str = "55 23 * * *"
    temporal_decay_half_life_days: float = 30.0

    def get_db_path(self, project_root: Path) -> Path:
        return _resolve_memory_path(self.db_path, project_root)

    def get_memory_dir(self, project_root: Path) -> Path:
        return _resolve_memory_path(self.workspace, project_root) / "memory"

    def get_workspace(self, project_root: Path) -> Path:
        return _resolve_memory_path(self.workspace, project_root)


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
class JournalConfig:
    """从 config.yaml 加载的 Journal 配置（含默认值）。"""
    trace_dir: str = "./data/traces"
    snapshot_dir: str = "./data/snapshots"
    console: bool = True
    trace: bool = True
    snapshot: bool = True


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
    journal: JournalConfig = field(default_factory=JournalConfig)


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
# P1 遗留 — load_config()（Phase 5 升级）
# ============================================================

def _raw_to_config(raw: dict[str, Any]) -> Config:
    """将字典转换为 Config dataclass（Phase 5 升级）"""
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

    # Phase 5 升级：ToolsConfig 新格式 + 向后兼容
    tools_raw = raw.get("tools", {})

    # 向后兼容：合并新格式 + 旧格式（始终合并，防止混合格式丢数据）
    approval_commands = list(tools_raw.get("approval_commands", []))
    for tool_name in ("exec", "python"):
        if tools_raw.get(tool_name, {}).get("needs_approval", False):
            if tool_name not in approval_commands:
                approval_commands.append(tool_name)

    # 向后兼容：合并新格式 + 旧格式
    disabled_tools = list(tools_raw.get("disabled_tools", []))
    for tool_name in ("exec", "python"):
        if not tools_raw.get(tool_name, {}).get("enabled", True):
            if tool_name not in disabled_tools:
                disabled_tools.append(tool_name)

    tools = ToolsConfig(
        builtin_enabled=tools_raw.get("builtin_enabled", True),
        mcp_enabled=tools_raw.get("mcp_enabled", True),
        skill_enabled=tools_raw.get("skill_enabled", True),
        approval_commands=approval_commands,
        disabled_tools=disabled_tools,
        exec_timeout=tools_raw.get("exec_timeout") or
                      tools_raw.get("python", {}).get("timeout", 60.0),
        web_search_enabled=tools_raw.get("web_search", {}).get("enabled", False),
        # Phase 6: MCP 配置解析
        mcp_global=_parse_mcp_global(tools_raw.get("mcp_global", {})),
        mcp_servers=_parse_mcp_servers(tools_raw.get("mcp_servers", [])),
    )

    skills_raw = raw.get("skills", {})
    # Phase 7: directory 支持字符串或列表
    dir_raw = skills_raw.get("directory", "./skills")
    if isinstance(dir_raw, list):
        directory = dir_raw
    else:
        directory = str(dir_raw)

    skills = SkillsConfig(
        directory=directory,
        enabled=skills_raw.get("enabled", True),
        skip_prefix=skills_raw.get("skip_prefix", "_"),
    )
    memory_raw = raw.get("memory", {})
    memory = MemoryConfig(
        long_term_file=memory_raw.get("long_term_file", "./data/memory/MEMORY.md"),
        workspace=memory_raw.get("workspace", "./data"),
        db_path=memory_raw.get("db_path", "./data/memory/memory.db"),
        chunk_max_tokens=memory_raw.get("chunk_max_tokens", 500),
        chunk_overlap_tokens=memory_raw.get("chunk_overlap_tokens", 50),
        embedding_provider=memory_raw.get("embedding_provider"),
        embedding_model=memory_raw.get("embedding_model", "text-embedding-v3"),
        embedding_dimensions=memory_raw.get("embedding_dimensions", 1024),
        embedding_api_base=memory_raw.get("embedding_api_base", ""),
        embedding_api_key=memory_raw.get("embedding_api_key", ""),
        max_results=memory_raw.get("max_results", 5),
        min_score=memory_raw.get("min_score", 0.1),
        vector_weight=memory_raw.get("vector_weight", 0.7),
        keyword_weight=memory_raw.get("keyword_weight", 0.3),
        sync_on_search=memory_raw.get("sync_on_search", True),
        flush_threshold=memory_raw.get("flush_threshold", 20),
        flush_max_messages=memory_raw.get("flush_max_messages", 10),
        dream_enabled=memory_raw.get("dream_enabled", True),
        dream_schedule=memory_raw.get("dream_schedule", "55 23 * * *"),
        temporal_decay_half_life_days=memory_raw.get("temporal_decay_half_life_days", 30.0),
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

    journal_raw = raw.get("journal", {})
    journal = JournalConfig(
        trace_dir=journal_raw.get("trace_dir", "./data/traces"),
        snapshot_dir=journal_raw.get("snapshot_dir", "./data/snapshots"),
        console=journal_raw.get("console", True),
        trace=journal_raw.get("trace", True),
        snapshot=journal_raw.get("snapshot", True),
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
        journal=journal,
    )


def _parse_mcp_global(raw: dict) -> McpGlobalConfig:
    """解析 MCP 全局配置"""
    return McpGlobalConfig(
        startup_timeout=raw.get("startup_timeout", 4.0),
        tool_timeout=raw.get("tool_timeout", 60.0),
        restart_on_crash=raw.get("restart_on_crash", True),
        max_restart_attempts=raw.get("max_restart_attempts", 3),
    )


def _parse_mcp_servers(raw_servers: list[dict]) -> list[McpServerConfig]:
    """解析 MCP servers 列表，校验 transport/name/command/url"""
    servers = []
    seen_names: set[str] = set()
    for srv in raw_servers:
        name = srv.get("name", "")
        transport = srv.get("transport", "stdio")

        if not name:
            raise ValueError("MCP server 配置缺少 name 字段")
        if name in seen_names:
            raise ValueError(f"MCP server name 重复: {name}")
        seen_names.add(name)

        if transport not in ("stdio", "streamable_http"):
            raise ValueError(f"MCP server {name}: 不支持的 transport={transport}")

        if transport == "stdio" and not srv.get("command"):
            raise ValueError(f"MCP server {name}: stdio transport 缺少 command")
        if transport == "streamable_http" and not srv.get("url"):
            raise ValueError(f"MCP server {name}: streamable_http transport 缺少 url")

        servers.append(McpServerConfig(
            name=name,
            transport=transport,
            command=srv.get("command", ""),
            args=srv.get("args", []),
            url=srv.get("url", ""),
            headers=srv.get("headers", {}),
            startup_timeout=srv.get("startup_timeout"),
            tool_timeout=srv.get("tool_timeout"),
            restart_on_crash=srv.get("restart_on_crash"),
            max_restart_attempts=srv.get("max_restart_attempts"),
        ))
    return servers


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
