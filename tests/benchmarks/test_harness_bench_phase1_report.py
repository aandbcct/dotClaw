from benchmarks.harness_bench_phase1_report import normalize_result, summarize


def test_normalize_result_preserves_native_metrics() -> None:
    raw = {
        "task_id": "001-file",
        "api_model_slug": "fixed-model",
        "adapter_results": [{"ok": True, "metadata": {"returncode": 0}}],
        "usage_summary": {
            "available": True,
            "request_count": 3,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
        "oracle_result": {"checks": [{"id": "ok", "pass": True}]},
        "scoring": {
            "outcome_score": 1.0,
            "security_score": 0.5,
            "combined_score": 0.4,
            "rubric": {
                "scores": {"tool_use_appropriate": 0.8, "consistency": 0.9, "robustness": 1.0},
                "notes": "note",
            },
        },
        "elapsed_sec": 2.5,
    }

    record = normalize_result(raw, "dotClaw")

    assert record["metrics"] == {
        "completion": 1.0,
        "tool_use": 0.8,
        "consistency": 0.9,
        "robustness": 1.0,
        "security": 0.5,
        "combined": 0.4,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 15,
        "turns": 3,
        "elapsed_sec": 2.5,
    }
    assert record["execution_success"] is True
    assert record["completion_success"] is True


def test_summarize_keeps_zero_scores() -> None:
    first = normalize_result(
        {
            "task_id": "001-file",
            "adapter_results": [{"ok": True}],
            "usage_summary": {"available": True, "request_count": 1},
            "scoring": {
                "outcome_score": 0.0,
                "security_score": 1.0,
                "combined_score": 0.0,
                "rubric": {"scores": {"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}},
            },
        },
        "dotClaw",
    )
    second = {**first, "task_id": "002-exec", "metrics": {**first["metrics"], "completion": 1.0}}

    summary = summarize([first, second])

    assert summary["overall"]["mean_completion"] == 0.5
    assert summary["overall"]["mean_tool_use"] == 0.0
