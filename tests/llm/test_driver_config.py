"""LLM provider/driver 配置与启动校验。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dotclaw.config.settings import (
    DefaultsConfig,
    LLMDriver,
    ModelConfig,
    ProviderConfig,
    PurposeConfig,
    PurposePriority,
    RouterConfig,
    load_router_config,
)
from dotclaw.llm.circuit_breaker import CircuitBreaker
from dotclaw.llm.drivers.openai_chat_completions import (
    OpenAIChatCompletionsClient,
)
from dotclaw.llm.model_router import ModelRouter
from dotclaw.llm.rate_limiter import RateLimiter


def _write_router_config(tmp_path: Path, body: str) -> Path:
    """写入单个临时 Router 配置并返回路径。"""
    path = tmp_path / "model_router_config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_router_config(
    *,
    provider_name: str = "jojocode",
    model_provider: str = "jojocode",
    capabilities: list[str] | None = None,
) -> RouterConfig:
    """构造可在启动阶段完整校验的最小配置。"""
    model_name = "jojo-test"
    return RouterConfig(
        defaults=DefaultsConfig(),
        providers={
            provider_name: ProviderConfig(
                driver=LLMDriver.OPENAI_CHAT_COMPLETIONS,
                api_key="test-key",
                base_url="https://example.test/v1",
            )
        },
        models={
            model_name: ModelConfig(
                provider=model_provider,
                model_id="jojo-model-id",
                capabilities=capabilities or ["chat"],
            )
        },
        purposes={
            "chat": PurposeConfig(
                priority=[PurposePriority(model=model_name, priority=1)]
            )
        },
    )


def test_provider_driver_is_required_when_loading_yaml(tmp_path) -> None:
    path = _write_router_config(
        tmp_path,
        """
        providers:
          jojocode:
            api_key: test-key
        """,
    )

    with pytest.raises(ValueError, match=r"providers\.jojocode 缺少必填字段 driver"):
        load_router_config(path)


def test_unknown_driver_reports_exact_config_path(tmp_path) -> None:
    path = _write_router_config(
        tmp_path,
        """
        providers:
          jojocode:
            driver: openai_typo
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"providers\.jojocode\.driver='openai_typo' 未注册",
    ):
        load_router_config(path)


def test_provider_circuit_breaker_is_loaded(tmp_path) -> None:
    path = _write_router_config(
        tmp_path,
        """
        providers:
          jojocode:
            driver: openai_chat_completions
            circuit_breaker:
              failure_threshold: 9
              cooldown_seconds: 45
        """,
    )

    config = load_router_config(path)

    assert config.providers["jojocode"].circuit_breaker == {
        "failure_threshold": 9,
        "cooldown_seconds": 45,
    }


def test_jojocode_uses_openai_chat_completions_driver() -> None:
    router = ModelRouter(
        _minimal_router_config(),
        RateLimiter({}),
        CircuitBreaker({}),
    )

    client = router.get_client("jojo-test")

    assert isinstance(client, OpenAIChatCompletionsClient)
    assert client._get_api_key() == "test-key"
    assert client._get_base_url() == "https://example.test/v1"
    assert client._get_model_id() == "jojo-model-id"


def test_unknown_provider_fails_during_router_startup() -> None:
    config = _minimal_router_config(model_provider="jojocode-typo")

    with pytest.raises(
        ValueError,
        match=r"models\.jojo-test\.provider 引用未知 provider: 'jojocode-typo'",
    ):
        ModelRouter(config, RateLimiter({}), CircuitBreaker({}))


def test_unsupported_driver_capability_fails_during_router_startup() -> None:
    config = _minimal_router_config(capabilities=["chat", "vision"])

    with pytest.raises(
        ValueError,
        match=r"driver 'openai_chat_completions' 不支持能力: vision",
    ):
        ModelRouter(config, RateLimiter({}), CircuitBreaker({}))


def test_unknown_purpose_model_fails_during_router_startup() -> None:
    config = _minimal_router_config()
    config.purposes["chat"].priority[0].model = "missing-model"

    with pytest.raises(
        ValueError,
        match=r"purposes\.chat\.priority\[0\] 引用未知模型: 'missing-model'",
    ):
        ModelRouter(config, RateLimiter({}), CircuitBreaker({}))
