"""benchmarks/cases/llm_stream.py — LLM 流式延迟评测。

默认直连真实 LLM API，测量 TTFT（首 token 延迟）和 TPS（吞吐量）。
全部指标标记 [EXT]（含网络延迟）。
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from dotclaw.journal.metrics_types import AgentRunSnapshot
from dotclaw.journal.storage import build_run_meta
from dotclaw.llm.base import Message, ChatChunk


# 固定的评测 prompt — 控制输出长度稳定，保证横向可比
_BENCH_PROMPT = "用一句话介绍 Python 编程语言，不超过 50 字。"


async def run(
    warmup: int = 3,
    repeat: int = 10,
    project_root: str | Path | None = None,
) -> AgentRunSnapshot:
    """运行 LLM 流式延迟评测（真实 API）。

    从 config.yaml 加载 LLM 配置，调用默认模型发送固定 prompt，
    测量 TTFT 和 TPS。首轮 warmup 丢弃以消除冷启动偏差。

    Args:
        warmup: 前 N 次迭代丢弃。
        repeat: 实际测量迭代次数。
        project_root: 项目根目录。

    Returns:
        AgentRunSnapshot。
    """
    root = Path(project_root) if project_root else Path(__file__).parent.parent.parent

    # ── 构建 LLM（复用 factory）──
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

    # ── 打印统计 ──
    _print_stats("TTFT [EXT]", ttft_list, "ms")
    _print_stats("TPS  [EXT]", tps_list, "tokens/s")
    _print_stats("E2E  [EXT]", e2e_list, "ms")
    print("\n  [EXT] = Includes network/API latency")

    # ── 构建 Snapshot ──
    meta = build_run_meta(
        run_id=f"bench_llm_stream_{time.strftime('%H%M%S')}",
        test_dataset="framework_perf",
        test_dataset_size=repeat,
    )

    from dotclaw.journal.metrics_types import (
        AgentGeneralMetrics, ReactLoopMetrics,
        ToolCallMetrics, SkillMetrics, MemoryMetrics,
    )

    snapshot = AgentRunSnapshot(
        run_id=meta.run_id,
        timestamp=meta.timestamp,
        git_commit=meta.git_commit,
        config_hash=meta.config_hash,
        test_dataset=meta.test_dataset,
        test_dataset_size=meta.test_dataset_size,
        react=ReactLoopMetrics(),
        tools=ToolCallMetrics(),
        skills=SkillMetrics(),
        memory=MemoryMetrics(),
        general=AgentGeneralMetrics(
            total_input_tokens=int(_p50(in_tok_list)) if in_tok_list else 0,
            total_output_tokens=int(_p50(out_tok_list)) if out_tok_list else 0,
            avg_ttft_ms=_p50(ttft_list),
            avg_tps=_p50(tps_list),
            avg_e2e_latency_ms=_p50(e2e_list),
            p95_e2e_latency_ms=_p95(e2e_list),
        ),
    )
    return snapshot


async def _measure_real_stream(llm) -> tuple[float, float, float, int, int]:
    """发一次真实 API 调用，返回 (TTFT_ms, TPS, E2E_ms, input_tokens, output_tokens)。

    TTFT: 从调用开始到收到第一个非空 content chunk 的时间
    TPS:  总输出字符数 / 总耗时（字符级近似 token）
    E2E:  从调用开始到最后一个 chunk 的时间
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

        # 持续采集 token 数据（token 出现在最后一个 chunk，可能被多个 final chunk 携带）
        if chunk.input_tokens:
            input_tokens = chunk.input_tokens
        if chunk.output_tokens:
            output_tokens = chunk.output_tokens

    total_ms = (time.perf_counter() - t_start) * 1000
    tps = output_tokens / (total_ms / 1000) if total_ms > 0 else 0

    return ttft_ms, tps, total_ms, input_tokens, output_tokens


def _get_provider_name(llm) -> str:
    """从 LLMProxy 提取 provider 名称。"""
    try:
        resolved = llm._router.resolve("chat", None)
        return resolved[0].__class__.__name__
    except Exception:
        return "unknown"


def _print_stats(label: str, values: list[float], unit: str) -> None:
    if not values:
        print(f"  {label:12s} (no data)")
        return
    s = sorted(values)
    print(f"  {label:12s} P50={s[int(len(s)*0.50)]:.1f}{unit}  "
          f"P95={s[int(len(s)*0.95)]:.1f}{unit}  "
          f"Avg={sum(values)/len(values):.1f}{unit}  "
          f"Min={min(values):.1f}{unit}  Max={max(values):.1f}{unit}")


def _p50(v: list[float]) -> float:
    if not v: return 0.0
    s = sorted(v)
    return s[int(len(s) * 0.50)]


def _p95(v: list[float]) -> float:
    if not v: return 0.0
    s = sorted(v)
    return s[int(len(s) * 0.95)]
