"""PR8 Agent Harness 业务效果 Benchmark 的显式真实 LLM CLI。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from dotclaw.config import _build_router_config_from_legacy, load_router_config
from dotclaw.config.settings import DefaultsConfig, PurposeConfig, PurposePriority, RouterConfig, load_config
from dotclaw.eval.environment import EvalDependencies
from dotclaw.llm.circuit_breaker import BreakerConfig, CircuitBreaker
from dotclaw.llm.model_router import ModelRouter
from dotclaw.llm.proxy import LLMProxy
from dotclaw.llm.rate_limiter import RateLimitConfig, RateLimiter
from dotclaw.runtime.adapters import LLMProxyAdapter

from .business_baseline import _TimeoutBoundLLMPort
from .business_judge import LLMProxyJudge
from .harness_business_dataset import load_harness_dataset
from .harness_business_runner import run_harness_business_matrix
from .harness_eval_executor import EvalHarnessTaskExecutor


def main(argv: Sequence[str] | None = None) -> int:
    """只在显式提供候选/Judge 条件后装配真实调用；默认不启动正式采样。"""
    parser = argparse.ArgumentParser(description="PR8 Agent Harness 业务效果 Benchmark")
    parser.add_argument("--dataset-root", type=Path, default=Path("benchmarks/datasets"))
    parser.add_argument("--dataset", default="agent_harness_business_v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--judge-provider", required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--retry-count", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--diagnostic-instance")
    parser.add_argument("--formal-sampling", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0 or args.retry_count < 0:
        parser.error("timeout-seconds 必须大于零，retry-count 不得为负数")
    if args.formal_sampling and args.diagnostic_instance:
        parser.error("正式采样不得选择单个 diagnostic-instance")
    if args.diagnostic_instance and args.repeat != 1:
        parser.error("单实例诊断固定 repeat=1")

    dataset = load_harness_dataset(args.dataset_root, args.dataset)
    candidate_proxy = _build_fixed_model_proxy(args.provider, args.model, Path.cwd())
    judge_proxy = _build_fixed_model_proxy(args.judge_provider, args.judge_model, Path.cwd())
    llm_port = _TimeoutBoundLLMPort(LLMProxyAdapter(candidate_proxy), args.timeout_seconds, args.retry_count)
    executor = EvalHarnessTaskExecutor(EvalDependencies(llm_port=llm_port), model=args.model)
    judge = LLMProxyJudge(judge_proxy, args.judge_model, args.timeout_seconds, args.retry_count)
    instance_ids = None if args.diagnostic_instance is None else (args.diagnostic_instance,)
    asyncio.run(
        run_harness_business_matrix(
            dataset,
            executor,
            judge,
            output=args.output,
            provider=args.provider,
            model=args.model,
            temperature=None,
            judge_provider=args.judge_provider,
            judge_model=args.judge_model,
            formal_sampling=args.formal_sampling,
            repeat=args.repeat,
            instance_ids=instance_ids,
            timeout_seconds=args.timeout_seconds,
            retry_count=args.retry_count,
        )
    )
    return 0


def _build_fixed_model_proxy(provider: str, model: str, project_root: Path) -> LLMProxy:
    """构建只含声明模型的路由，禁止候选与 Judge 在失败时跨模型降级。"""
    router_path = project_root / "model_router_config.yaml"
    source = load_router_config(router_path) if router_path.exists() else _build_router_config_from_legacy(load_config().llm)
    model_config = source.models.get(model)
    if model_config is None:
        raise ValueError(f"模型 {model!r} 未在路由配置中声明")
    if model_config.provider != provider:
        raise ValueError(f"模型 {model!r} 实际属于 Provider {model_config.provider!r}，与声明 {provider!r} 不一致")
    provider_config = source.providers.get(provider)
    if provider_config is None:
        raise ValueError(f"Provider {provider!r} 未在路由配置中声明")
    narrowed = RouterConfig(
        defaults=DefaultsConfig(parameters=dict(source.defaults.parameters), fallback_enabled=False),
        providers={provider: provider_config},
        models={model: replace(model_config)},
        purposes={"chat": PurposeConfig(description="PR8 固定模型", priority=[PurposePriority(model=model, priority=1)])},
    )
    limiter = RateLimiter({provider: RateLimitConfig(requests_per_minute=provider_config.rate_limit.get("requests_per_minute", 0))})
    breaker_raw = getattr(provider_config, "circuit_breaker", {})
    breaker = CircuitBreaker({provider: BreakerConfig(
        failure_threshold=breaker_raw.get("failure_threshold", 5),
        cooldown_seconds=breaker_raw.get("cooldown_seconds", 30),
        half_open_max=breaker_raw.get("half_open_max", 1),
    )})
    return LLMProxy(ModelRouter(narrowed, limiter, breaker))


if __name__ == "__main__":
    raise SystemExit(main())
