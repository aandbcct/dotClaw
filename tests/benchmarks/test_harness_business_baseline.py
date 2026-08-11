"""PR8 Agent Harness CLI 参数与装配边界测试。"""

from pathlib import Path

import pytest

import benchmarks.harness_business_baseline as baseline


def test_cli_requires_explicit_candidate_and_judge_conditions(tmp_path: Path) -> None:
    """缺少候选模型或 Judge 条件时不得隐式启动真实 API。"""
    with pytest.raises(SystemExit) as error:
        baseline.main(["--output", str(tmp_path)])
    assert error.value.code == 2


def test_cli_rejects_formal_diagnostic_mix(tmp_path: Path) -> None:
    """单实例诊断不能携带正式采样资格。"""
    with pytest.raises(SystemExit) as error:
        baseline.main([
            "--output", str(tmp_path),
            "--provider", "provider", "--model", "model",
            "--judge-provider", "provider", "--judge-model", "judge",
            "--diagnostic-instance", "evidence-01-vendor-decision",
            "--repeat", "1", "--formal-sampling",
        ])
    assert error.value.code == 2


def test_cli_rejects_diagnostic_repeat_other_than_one(tmp_path: Path) -> None:
    """开发诊断不能通过重复多次伪装为稳定性实验。"""
    with pytest.raises(SystemExit) as error:
        baseline.main([
            "--output", str(tmp_path),
            "--provider", "provider", "--model", "model",
            "--judge-provider", "provider", "--judge-model", "judge",
            "--diagnostic-instance", "evidence-01-vendor-decision",
        ])
    assert error.value.code == 2


def test_fixed_model_proxy_rejects_provider_label_mismatch() -> None:
    """CLI 记录的 Provider 必须与模型路由的真实归属一致。"""
    with pytest.raises(ValueError, match="与声明"):
        baseline._build_fixed_model_proxy("openai", "qwen3.7-max", Path.cwd())


def test_fixed_model_proxy_has_no_fallback_candidates() -> None:
    """固定模型路由只能返回被测模型，禁止故障时切换其他候选。"""
    proxy = baseline._build_fixed_model_proxy("qwen", "qwen3.7-max", Path.cwd())

    assert proxy.available_models == ["qwen3.7-max"]


def test_cli_does_not_accept_unpropagated_temperature(tmp_path: Path) -> None:
    """传输端未支持请求级温度时，CLI 不得只记录而不实际下发。"""
    with pytest.raises(SystemExit) as error:
        baseline.main([
            "--output", str(tmp_path),
            "--provider", "qwen", "--model", "qwen3.7-max",
            "--judge-provider", "qwen", "--judge-model", "qwen3.7-max",
            "--temperature", "0",
        ])
    assert error.value.code == 2
