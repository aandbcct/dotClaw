"""浏览器基准统计约定：保留失败分母并拒绝把右删失当成功延迟。"""
from benchmarks.cancellation_e2e.run import summarize
from benchmarks.cancellation_e2e.worker import classify_failure


def sample(**overrides):
    """构造只含统计所需事实的最小样本。"""
    return dict(version="7860daa", scenario="before_first", phase="formal", status="success", cancel_success=True, overall_success=True, browser_click_to_terminal_ms=100.0, backend_receive_to_resource_exit_ms=20.0, backend_receive_to_terminal_commit_ms=80.0) | overrides


def test_timeout_and_missing_entry_stay_in_denominator_without_latency():
    """超时没有伪造的十秒延迟，入口缺失也不能从正式分母移除。"""
    result = summarize([sample(), sample(status="timeout", cancel_success=False, overall_success=False, browser_click_to_terminal_ms=None), sample(status="entry_unavailable", cancel_success=False, overall_success=False, browser_click_to_terminal_ms=None)])
    stats = result["7860daa"]["before_first"]
    assert stats["n"] == 3
    assert stats["cancel_success_rate"] == 1 / 3
    assert stats["timeout_rate"] == 1 / 3
    assert stats["latency"]["browser_click_to_terminal_ms"] == dict(n=1, p50=100.0, p95=100.0, max=100.0)
    assert stats["resource_exit_observed_all_statuses"]["n"] == 3


def test_functional_failure_keeps_core_success_but_fails_overall():
    """刷新错误必须保留，并区分取消核心与整体验收。"""
    stats = summarize([sample(status="functional_failure", overall_success=False, refresh_error=True)])["7860daa"]["before_first"]
    assert stats["cancel_success_n"] == 1
    assert stats["overall_success_n"] == 0
    assert stats["refresh_error_n"] == 1


def test_debug_and_warmup_do_not_enter_formal_metrics():
    """调试和预热样本不能选择性合并到正式延迟。"""
    stats = summarize([sample(), sample(phase="debug", browser_click_to_terminal_ms=1), sample(phase="warmup", browser_click_to_terminal_ms=2)])["7860daa"]["before_first"]
    assert stats["n"] == 1
    assert stats["latency"]["browser_click_to_terminal_ms"]["p50"] == 100.0


def test_slow_completed_and_not_completed_are_distinct():
    """大于阈值完成率与截止阈值未完成数不能混为一个比例。"""
    stats = summarize([sample(browser_click_to_terminal_ms=6000), sample(status="timeout", cancel_success=False, overall_success=False, browser_click_to_terminal_ms=None)])["7860daa"]["before_first"]
    assert stats["completed_gt_5000ms_rate"] == .5
    assert stats["not_completed_by_5000ms_n"] == 2
    assert stats["completed_gt_10000ms_n"] == 0
    assert stats["completed_gt_10000ms_rate"] is None
    assert stats["not_completed_by_10000ms_rate"] == .5


def test_only_verified_target_without_stop_entry_is_entry_unavailable():
    """目标归属校验失败和初始化失败必须保留为无效准备。"""
    assert classify_failure(False, False, True) == "invalid_fixture"
    assert classify_failure(False, True, False) == "invalid_fixture"
    assert classify_failure(False, True, True) == "entry_unavailable"
    assert classify_failure(True, True, False) == "failure"
