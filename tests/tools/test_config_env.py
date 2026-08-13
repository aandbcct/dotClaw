"""项目根目录 .env 加载与环境变量优先级测试。"""

from __future__ import annotations

from dotclaw.config import settings


def _write_config(tmp_path) -> None:
    """写入引用测试环境变量的最小配置。"""
    (tmp_path / "config.yaml").write_text(
        "llm:\n  clients:\n    test:\n      api_key: ${DOTCLAW_TEST_API_KEY}\n",
        encoding="utf-8",
    )


def test_load_config_reads_project_root_dotenv(tmp_path, monkeypatch) -> None:
    """配置加载前读取项目根 .env，使 ${VAR} 可被展开。"""
    _write_config(tmp_path)
    (tmp_path / ".env").write_text("DOTCLAW_TEST_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(settings, "_find_project_root", lambda: tmp_path)
    monkeypatch.delenv("DOTCLAW_TEST_API_KEY", raising=False)

    config = settings.load_config()

    assert config.llm.clients["test"].api_key == "from-dotenv"


def test_project_dotenv_overrides_system_environment(tmp_path, monkeypatch) -> None:
    """项目 .env 优先，覆盖已有的同名系统环境变量。"""
    _write_config(tmp_path)
    (tmp_path / ".env").write_text("DOTCLAW_TEST_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(settings, "_find_project_root", lambda: tmp_path)
    monkeypatch.setenv("DOTCLAW_TEST_API_KEY", "from-system")

    config = settings.load_config()

    assert config.llm.clients["test"].api_key == "from-dotenv"
