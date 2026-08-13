"""AgentPolicyResolver 压缩模型/Tokensizer 确定性解析的缺省行为测试（阶段4 修改项5）。"""

from __future__ import annotations

from dotclaw.config.settings import DefaultsConfig, ModelConfig, RouterConfig
from dotclaw.runtime.adapters.agent_policy_resolver import (
    resolve_compaction_settings,
    resolve_default_model,
)


def test_compaction_uses_router_config_model_when_present() -> None:
    """RouterConfig 含该模型项时，压缩模型与 Tokenizer 取自配置。"""
    router = RouterConfig(models={"qwen-plus": ModelConfig(model_id="qwen-plus", tokenizer_encoding="cl100k_base")})
    model, tokenizer = resolve_compaction_settings("qwen-plus", router, "qwen-max")
    assert model == "qwen-plus"
    assert tokenizer == "cl100k_base"


def test_compaction_falls_back_to_requested_model_when_absent() -> None:
    """RouterConfig 缺该模型项时，压缩模型回退到请求模型名、Tokenizer 回退空串。"""
    router = RouterConfig(models={})
    model, tokenizer = resolve_compaction_settings("qwen-plus", router, "qwen-max")
    assert model == "qwen-plus"
    assert tokenizer == ""


def test_compaction_falls_back_to_default_model_when_no_router() -> None:
    """无 RouterConfig 时，压缩模型回退到默认模型、Tokenizer 回退空串。"""
    model, tokenizer = resolve_compaction_settings("qwen-plus", None, "qwen-max")
    assert model == "qwen-max"
    assert tokenizer == ""


def test_default_model_uses_router_config_as_authority() -> None:
    """Router 配置存在时不得继续读取 config.yaml 的默认模型。"""
    router = RouterConfig(defaults=DefaultsConfig(model="router-default"))

    assert resolve_default_model(router, "legacy-default") == "router-default"


def test_default_model_uses_legacy_only_without_router_config() -> None:
    """仅在不存在 Router 配置时保留旧主配置兼容。"""
    assert resolve_default_model(None, "legacy-default") == "legacy-default"
