"""PR6 固定工作负载测试。"""

from benchmarks.context_workloads import compression_corpus, fixed_context_fixtures
from dotclaw.runtime.domain.context import ContextOwner


def test_fixed_fixtures_cover_each_owner_once() -> None:
    """有限 Owner 表覆盖四层归属。"""
    assert {item.owner for item in fixed_context_fixtures()} == set(ContextOwner)


def test_compression_corpus_keeps_latest_conversation() -> None:
    """固定语料有可压缩旧项和一条最新保留项。"""
    assert len(compression_corpus().conversations) == 3
