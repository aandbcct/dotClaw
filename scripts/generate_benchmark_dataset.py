"""生成 benchmark 评测用测试数据集。

用法:
    python scripts/generate_benchmark_dataset.py

产出:
    benchmarks/dataset/sample_skills/  - 100 个测试 SKILL.md
    benchmarks/dataset/memory_corpus/  - small/medium/large 文本语料
    benchmarks/dataset/stress_prompts.json - 压力测试 prompts
"""

import json
import os
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SKILLS_DIR = PROJECT_ROOT / "benchmarks" / "dataset" / "sample_skills"
CORPUS_DIR = PROJECT_ROOT / "benchmarks" / "dataset" / "memory_corpus"
STRESS_FILE = PROJECT_ROOT / "benchmarks" / "dataset" / "stress_prompts.json"

_SKILL_TEMPLATE = """---
name: benchmark-skill-{index:03d}
description: Auto-generated benchmark skill #{index} for performance testing
keywords: [benchmark, test, perf, auto]
lifecycle: persistent
---

# Benchmark Skill {index:03d}

This is an auto-generated skill for framework benchmark testing.
It simulates a typical skill with realistic content size.

## Usage

This skill helps with benchmark task #{index}.

## Parameters

- `input`: The input data for processing
- `output_dir`: Where to save results

## Example

```python
def process(input_data):
    return f"Processed: {{input_data}}"
```

## Additional Notes

This section adds more content to make the skill file more realistic
in terms of size. Real-world skills often contain detailed documentation,
examples, and configuration instructions.

### Configuration

- `mode`: {mode}
- `priority`: {priority}

### Changelog

- v1.0: Initial auto-generated version for benchmark #{index}
"""

_WORDS = [
    "the", "of", "and", "to", "in", "a", "is", "that", "for", "it",
    "as", "with", "on", "by", "at", "from", "or", "an", "be", "this",
    "system", "agent", "tool", "task", "context", "memory", "model",
    "prompt", "response", "loop", "session", "skill", "benchmark",
    "performance", "latency", "throughput", "initialization", "dispatch",
    "data", "process", "execute", "handler", "registry", "scanner",
    "configuration", "runtime", "framework", "abstraction", "interface",
    "implementation", "component", "module", "pipeline", "workflow",
    "async", "stream", "token", "embedding", "vector", "search",
    "index", "chunk", "retrieval", "storage", "serialization",
]


def _random_line(min_words: int = 5, max_words: int = 40) -> str:
    n = random.randint(min_words, max_words)
    return " ".join(random.choices(_WORDS, k=n)).capitalize() + "."


def _random_paragraph(min_sentences: int = 2, max_sentences: int = 8) -> str:
    n = random.randint(min_sentences, max_sentences)
    return " ".join(_random_line() for _ in range(n))


def generate_skills(count: int = 100) -> None:
    """生成 N 个测试 skill 目录."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(1, count + 1):
        skill_dir = SKILLS_DIR / f"skill_{i:03d}"
        skill_dir.mkdir(exist_ok=True)

        content = _SKILL_TEMPLATE.format(
            index=i,
            mode=random.choice(["auto", "manual", "hybrid"]),
            priority=random.randint(1, 10),
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    print(f"[skills] Generated {count} test skills in {SKILLS_DIR}")


def generate_memory_corpus() -> None:
    """生成 small(100行)/medium(1000行)/large(10000行) 语料."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    configs = {
        "small.txt": 100,
        "medium.txt": 1000,
        "large.txt": 10000,
    }

    for filename, lines in configs.items():
        content = []
        for i in range(lines):
            content.append(f"[doc_{i:05d}] {_random_paragraph()}")
        (CORPUS_DIR / filename).write_text("\n".join(content), encoding="utf-8")
        size_kb = (CORPUS_DIR / filename).stat().st_size / 1024
        print(f"[corpus] Generated {filename}: {lines} lines, {size_kb:.1f} KB")

    print(f"[corpus] All corpus files written to {CORPUS_DIR}")


def generate_stress_prompts() -> None:
    """生成压力测试 prompts."""
    prompts = {
        "short_prompt": "用一句话介绍 Python 编程语言。",
        "medium_prompt": (
            "请详细解释 Python 的异步编程模型，包括 asyncio 事件循环、"
            "协程、Future/Task 的概念，以及与多线程的区别。"
            "请给出至少 3 个代码示例。" * 3
        ),
        "long_prompt": (
            "请写一篇关于分布式系统设计的综合文章，涵盖以下主题："
            "CAP 定理、一致性模型、共识算法（Raft/Paxos）、"
            "微服务架构、事件驱动架构、CQRS、事件溯源、"
            "服务网格、可观测性（日志/指标/追踪）、"
            "以及 Kubernetes 编排。每个主题至少 500 字。" * 5
        ),
        "tool_usage": (
            "请帮我管理文件系统：读取 config.yaml 的内容，"
            "然后创建一个新的配置文件，最后搜索所有的 Python 文件。" * 10
        ),
        "multi_step": (
            "第一步：分析项目结构。第二步：找出所有依赖。"
            "第三步：评估代码质量。第四步：生成重构建议。"
            "第五步：执行重构。" * 4
        ),
    }

    STRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STRESS_FILE.write_text(
        json.dumps(prompts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lengths = {k: len(v) for k, v in prompts.items()}
    print(f"[stress] Generated stress prompts: {lengths}")
    print(f"[stress] Written to {STRESS_FILE}")


def main():
    random.seed(42)
    print("=== Generating Benchmark Datasets ===\n")

    generate_skills(100)
    print()

    generate_memory_corpus()
    print()

    generate_stress_prompts()
    print()

    print("=== Done ===")


if __name__ == "__main__":
    main()
