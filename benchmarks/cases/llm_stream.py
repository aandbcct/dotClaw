"""benchmarks/cases/llm_stream.py — LLM 流式延迟评测。

默认直连真实 LLM API，测量 TTFT（首 token 延迟）和 TPS（吞吐量）。
TPS 优先用 API 返回的 output_tokens（更准确），降级时用字符数近似。
全部指标标记 [EXT]（含网络延迟）。
"""

from __future__ import annotations

import time
from pathlib import Path

from benchmarks.stats import p50, p95
from dotclaw.journal.metrics_types import AgentGeneralMetrics
from dotclaw.journal.storage import build_run_meta
from dotclaw.llm.base import Message


_BENCH_PROMPT = "用一句话介绍 Python 编程语言，不超过 50 字。"


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> tuple[AgentGeneralMetrics, "RunMeta"]:
    """运行 LLM 流式延迟评测，返回 (AgentGeneralMetrics, RunMeta)。"""
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent

    from dotclaw.config.settings import load_config
    from dotclaw.agent.factory import _build_llm

    config = load_config(str(root / "config.yaml"))
    llm = _build_llm(config, root)

    print(f"  Model:   {config.llm.default_model} ({_get_provider_name(llm)})")
    print(f"  Prompt:  \"{_BENCH_PROMPT}\"")
    print(f"  Warmup:  {warmup} | Repeat: {repeat}")
    print()

    ttft_list, tps_list, e2e_list = [], [], []
    in_tok_list, out_tok_list = [], []

    for i in range(warmup + repeat):
        ttft, tps, e2e, in_tok, out_tok = await _measure_real_stream(llm)
        if i >= warmup:
            ttft_list.append(ttft)
            tps_list.append(tps)
            e2e_list.append(e2e)
            in_tok_list.append(in_tok)
            out_tok_list.append(out_tok)

    _print_stats("TTFT [EXT]", ttft_list, "ms")
    _print_stats("TPS  [EXT]", tps_list, "tokens/s")
    _print_stats("E2E  [EXT]", e2e_list, "ms")
    print("\n  [EXT] = Includes network/API latency")

    meta = build_run_meta(
        run_id=f"bench_llm_stream_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    metrics = AgentGeneralMetrics(
        total_input_tokens=int(p50(in_tok_list)) if in_tok_list else 0,
        total_output_tokens=int(p50(out_tok_list)) if out_tok_list else 0,
        avg_ttft_ms=p50(ttft_list),
        avg_tps=p50(tps_list),
        avg_e2e_latency_ms=p50(e2e_list),
        p95_e2e_latency_ms=p95(e2e_list),
    )
    return metrics, meta


async def _measure_real_stream(llm) -> tuple[float, float, float, int, int]:
    """发一次真实 API 调用，返回 (TTFT_ms, TPS, E2E_ms, input_tokens, output_tokens)。

    TPS 优先用 API 返回的 output_tokens，降级时用字符数近似。
    """
    messages = [Message(role="user", content=_BENCH_PROMPT)]
    ttft_ms = 0.0
    total_chars = 0
    first_token = True
    input_tokens = 0
    output_tokens = 0

    t_start = time.perf_counter()

    async for chunk in llm.chat(messages=messages, stream=True):
        content = chunk.content
        total_chars += len(content)

        if first_token and content:
            ttft_ms = (time.perf_counter() - t_start) * 1000
            first_token = False

        if chunk.input_tokens:
            input_tokens = chunk.input_tokens
        if chunk.output_tokens:
            output_tokens = chunk.output_tokens

    total_ms = (time.perf_counter() - t_start) * 1000
    if total_ms > 0:
        tps = output_tokens / (total_ms / 1000) if output_tokens > 0 else total_chars / (total_ms / 1000)
    else:
        tps = 0.0

    return ttft_ms, tps, total_ms, input_tokens, output_tokens


def _get_provider_name(llm) -> str:
    try:
        resolved = llm._router.resolve("chat", None)
        return resolved[0].__class__.__name__
    except Exception:
        return "unknown"


def _print_stats(label: str, values: list[float], unit: str) -> None:
    if not values:
        print(f"  {label:12s} (no data)")
        return
    print(f"  {label:12s} P50={p50(values):.1f}{unit}  "
          f"P95={p95(values):.1f}{unit}  "
          f"Avg={sum(values)/len(values):.1f}{unit}  "
          f"Min={min(values):.1f}{unit}  Max={max(values):.1f}{unit}")


def describe(metrics: AgentGeneralMetrics) -> str:
    """返回该 case 的 Markdown 详情。"""
    return "\n".join([
        "| Metric | Value |",
        "|--------|-------|",
        f"| TTFT P50 [EXT] | {metrics.avg_ttft_ms:.1f} ms |",
        f"| TPS P50 [EXT] | {metrics.avg_tps:.1f} tokens/s |",
        f"| E2E P50 [EXT] | {metrics.avg_e2e_latency_ms:.1f} ms |",
        f"| E2E P95 [EXT] | {metrics.p95_e2e_latency_ms:.1f} ms |",
        f"| Input Tokens (P50) | {metrics.total_input_tokens} |",
        f"| Output Tokens (P50) | {metrics.total_output_tokens} |",
    ])
