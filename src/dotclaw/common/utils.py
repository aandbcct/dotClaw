"""通用工具函数

Phase 2 限定范围：
- expand_env_vars: 环境变量展开（从 config/settings.py 提取）
- safe_load_yaml: YAML 安全加载封装

零外部依赖，不 import dotClaw 其他模块。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def validate_path_segment(value: str, field_name: str) -> str:
    """校验外部标识只能表示单个非空路径片段。

    同时按 Windows 与 POSIX 规则解析，避免测试或部署平台不同导致另一种
    路径分隔符绕过仓储边界。
    """
    posix_candidate = PurePosixPath(value)
    windows_candidate = PureWindowsPath(value)
    if (
        not value
        or value in {".", ".."}
        or posix_candidate.name != value
        or windows_candidate.name != value
    ):
        raise ValueError(f"{field_name} 必须是单个非空路径片段")
    return value


def expand_env_vars(value: Any) -> Any:
    """递归替换 ${ENV_VAR} 为环境变量值。未解析的变量写入 warning 日志。"""
    if isinstance(value, str):
        pattern = re.compile(r'\$\{([^}]+)\}')

        def replacer(m: re.Match) -> str:
            var_name = m.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                logger.warning(
                    f"环境变量 ${var_name} 未设置，保留原始占位符。"
                    f" 请设置该环境变量或在配置文件中替换为实际值。"
                )
                return m.group(0)
            return env_value

        return pattern.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


def safe_load_yaml(path: Path) -> dict:
    """安全加载 YAML 文件，文件不存在时返回空 dict"""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
