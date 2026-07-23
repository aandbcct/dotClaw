"""AgentPolicyResolver 压缩模型/Tokensizer 确定性解析的缺省行为测试（阶段4 修改项5）。"""

from __future__ import annotations

from dotclaw.config.settings import ModelConfig, RouterConfig
from dotclaw.runtime.adapters.agent_policy_resolver import resolve_compaction_settings


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
