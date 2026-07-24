"""模型级 reasoning 配置加载测试（开发计划阶段二）。

覆盖：none/native/tags 配置解析、旧 YAML 默认兼容为 none、
非法 mode、tags 模式下空标签与相同起止标签直接配置加载失败。
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

from dotclaw.config.settings import ModelReasoningConfig, load_router_config
from dotclaw.llm.reasoning import ReasoningMode, ReasoningPolicy


def _load_inline(yaml_body: str, suffix: str = "yaml") -> object:
    """把内联 YAML 写入临时文件并加载。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f".{suffix}", delete=False, encoding="utf-8"
    ) as f:
        f.write(textwrap.dedent(yaml_body))
        path = Path(f.name)

    try:
        return load_router_config(path)
    finally:
        path.unlink()


# ── 旧 YAML 兼容（默认 none） ─────────────────────────────

def test_legacy_model_without_reasoning_defaults_to_none() -> None:
    """未提供 reasoning 字段的模型，加载后 mode 为 none（保持旧文本行为）。"""
    config = _load_inline(
        """
        models:
          m1:
            provider: qwen
            model_id: m1
        """
    )
    assert config.models["m1"].reasoning.mode == "none"


def test_actual_router_config_qwen_native_others_none() -> None:
    """真实 model_router_config.yaml：qwen3.7-max 为 native，其他模型为 none。"""
    config = load_router_config()
    assert config.models["qwen3.7-max"].reasoning.mode == "native"
    # 未显式写 reasoning 的模型回退为 none
    assert config.models["deepseek-v4-flash"].reasoning.mode == "none"
    assert config.models["gpt-4o-mini"].reasoning.mode == "none"


# ── ReasoningPolicy.from_config 转换 ───────────────────────

def test_policy_from_config_tags_retains_fields() -> None:
    """tags 模式的 ModelReasoningConfig 转换为策略时保留标签字段。"""
    cfg = ModelReasoningConfig(
        mode="tags", reasoning_start="[[s]]", reasoning_end="[[e]]"
    )
    policy = ReasoningPolicy.from_config(cfg)
    assert policy.mode is ReasoningMode.TAGS
    assert policy.reasoning_start == "[[s]]"
    assert policy.reasoning_end == "[[e]]"


def test_policy_from_config_none_ignores_tags() -> None:
    """none 模式转换为策略时仅保留 mode（标签值无关紧要）。"""
    cfg = ModelReasoningConfig(mode="none")
    policy = ReasoningPolicy.from_config(cfg)
    assert policy.mode is ReasoningMode.NONE


# ── none / native / tags 解析 ─────────────────────────────

def test_explicit_none_mode() -> None:
    """显式 mode: none 解析正确。"""
    config = _load_inline(
        """
        models:
          m1:
            provider: qwen
            model_id: m1
            reasoning:
              mode: none
        """
    )
    assert config.models["m1"].reasoning.mode == "none"


def test_native_mode_parsed() -> None:
    """mode: native 解析正确。"""
    config = _load_inline(
        """
        models:
          m1:
            provider: qwen
            model_id: m1
            reasoning:
              mode: native
        """
    )
    assert config.models["m1"].reasoning.mode == "native"


def test_tags_mode_with_default_tags() -> None:
    """mode: tags 且未覆盖标签时，使用标准默认标签。"""
    config = _load_inline(
        """
        models:
          m1:
            provider: qwen
            model_id: m1
            reasoning:
              mode: tags
        """
    )
    reasoning = config.models["m1"].reasoning
    assert reasoning.mode == "tags"
    assert reasoning.reasoning_start == "<think>"
    assert reasoning.reasoning_end == "</think>"
    assert reasoning.response_start == "<response>"
    assert reasoning.response_end == "</response>"


def test_tags_mode_with_custom_tags() -> None:
    """mode: tags 且显式覆盖标签时，使用用户配置。"""
    config = _load_inline(
        """
        models:
          m1:
            provider: qwen
            model_id: m1
            reasoning:
              mode: tags
              reasoning_tags:
                start: "[[think]]"
                end: "[[/think]]"
        """
    )
    reasoning = config.models["m1"].reasoning
    assert reasoning.reasoning_start == "[[think]]"
    assert reasoning.reasoning_end == "[[/think]]"


# ── 配置加载失败 ───────────────────────────────────────────

def test_invalid_mode_raises() -> None:
    """非法 mode 值直接导致配置加载失败。"""
    import pytest

    with pytest.raises(ValueError, match="非法 reasoning.mode"):
        _load_inline(
            """
            models:
              m1:
                provider: qwen
                model_id: m1
                reasoning:
                  mode: weird
            """
        )


def test_tags_mode_empty_reasoning_tag_raises() -> None:
    """tags 模式下 reasoning 标签为空直接失败。"""
    import pytest

    with pytest.raises(ValueError, match="reasoning_tags"):
        _load_inline(
            """
            models:
              m1:
                provider: qwen
                model_id: m1
                reasoning:
                  mode: tags
                  reasoning_tags:
                    start: ""
                    end: "</think>"
            """
        )


def test_tags_mode_same_start_end_raises() -> None:
    """tags 模式下 reasoning 起止标签相同直接失败。"""
    import pytest

    with pytest.raises(ValueError, match="reasoning_tags.start 与 reasoning_tags.end"):
        _load_inline(
            """
            models:
              m1:
                provider: qwen
                model_id: m1
                reasoning:
                  mode: tags
                  reasoning_tags:
                    start: "<x>"
                    end: "<x>"
            """
        )


def test_tags_mode_same_response_start_end_raises() -> None:
    """tags 模式下 response 起止标签相同直接失败。"""
    import pytest

    with pytest.raises(ValueError, match="response_tags.start 与 response_tags.end"):
        _load_inline(
            """
            models:
              m1:
                provider: qwen
                model_id: m1
                reasoning:
                  mode: tags
                  response_tags:
                    start: "<r>"
                    end: "<r>"
            """
        )
