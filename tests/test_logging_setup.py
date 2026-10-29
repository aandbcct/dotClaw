"""共用日志初始化、上下文隔离及脱敏诊断验收。"""

import asyncio
import logging
from unittest.mock import Mock

import pytest

from dotclaw.bootstrap import application_host
from dotclaw.common.logging_setup import bind_log_context, configure_logging, exception_diagnostic
from dotclaw.config.settings import Config
from dotclaw.llm.base import ChatChunk, ChatTextDelta, EmptyModelResponseError, Message, TextDeltaKind
from dotclaw.runtime.adapters.llm_proxy_adapter import LLMProxyAdapter
from tests.llm.test_empty_model_response import _proxy
from tests.runtime_v2.test_llm_proxy_adapter import _make_context, _make_execution


@pytest.fixture(autouse=True)
def restore_logging():
    """每个测试只使用隔离文件，并归还原有日志处理器及级别。"""
    logger = logging.getLogger("dotclaw")
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    child = logging.getLogger("dotclaw.llm")
    child_level = child.level
    child.setLevel(logging.NOTSET)
    yield
    for handler in list(logger.handlers):
        if handler not in handlers:
            logger.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in logger.handlers:
            logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate
    child.setLevel(child_level)


def test_initialization_is_idempotent_and_preserves_foreign_handlers(tmp_path) -> None:
    root_handlers = list(logging.getLogger().handlers)
    uvicorn_handlers = list(logging.getLogger("uvicorn").handlers)
    logger = logging.getLogger("dotclaw")
    foreign = logging.NullHandler()
    logger.addHandler(foreign)
    try:
        configure_logging("INFO", "logs/run.log", tmp_path)
        owned = [h for h in logger.handlers if getattr(h, "_dotclaw_logging_setup", False)]
        configure_logging("DEBUG", "logs/run.log", tmp_path)
        assert owned == [h for h in logger.handlers if getattr(h, "_dotclaw_logging_setup", False)]
        with bind_log_context("会话", "运行"):
            logger.info("只记录一次")
        assert (tmp_path / "logs/run.log").read_text(encoding="utf-8").count("只记录一次") == 1
        file_handler = next(h for h in owned if isinstance(h, logging.FileHandler))
        configure_logging("INFO", "logs/other.log", tmp_path)
        assert file_handler.stream is None
        assert foreign in logger.handlers
        assert logging.getLogger().handlers == root_handlers
        assert logging.getLogger("uvicorn").handlers == uvicorn_handlers
        assert not logger.propagate
    finally:
        logger.removeHandler(foreign)


def test_invalid_log_path_is_startup_error(tmp_path) -> None:
    (tmp_path / "blocked").write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="日志文件无法创建"):
        configure_logging("INFO", "blocked/log.txt", tmp_path)


@pytest.mark.asyncio
async def test_host_configures_logging_before_infrastructure(tmp_path, monkeypatch) -> None:
    config = Config()
    config.debug.log_file = "shared/cli-web.log"
    def stop_after_log(*args):
        logging.getLogger("dotclaw.bootstrap").info("基础设施初始化")
        raise RuntimeError("测试停止，禁止真实基础设施调用")
    monkeypatch.setattr(application_host, "_build_llm", stop_after_log)
    for channel in (None, Mock()):
        host = application_host.ApplicationHost(config, tmp_path, channel)
        with pytest.raises(RuntimeError, match="测试停止"):
            await host.initialize()
    assert (tmp_path / "shared/cli-web.log").read_text(encoding="utf-8").count("基础设施初始化") == 2


@pytest.mark.asyncio
async def test_adapter_log_context_isolated_between_concurrent_calls(tmp_path) -> None:
    configure_logging("INFO", "calls.log", tmp_path)
    class ConcurrentProxy:
        """交错输出日志以检验适配器作用域。"""
        async def chat(self, **kwargs):
            await asyncio.sleep(0)
            logging.getLogger("dotclaw.llm").info("模型节点")
            yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "回答"),))
    adapter = LLMProxyAdapter(ConcurrentProxy())
    await asyncio.gather(*(adapter.complete(_make_context(), _make_execution("r" + str(i), "s" + str(i))) for i in range(2)))
    logging.getLogger("dotclaw.llm").info("作用域外")
    text = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "session_id=s0 run_id=r0 模型节点" in text
    assert "session_id=s1 run_id=r1 模型节点" in text
    assert "session_id=- run_id=- 作用域外" in text


@pytest.mark.asyncio
async def test_empty_call_logs_once_without_prompt_or_error_body(tmp_path) -> None:
    configure_logging("INFO", "calls.log", tmp_path)
    proxy, _, _ = _proxy([])
    with pytest.raises(EmptyModelResponseError):
        _ = [c async for c in proxy.chat([Message("user", "SECRET_PROMPT")])]
    text = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert text.count("llm.call.started") == text.count("llm.call.failed") == 1
    assert '"finish_reason": null' in text
    assert "SECRET_PROMPT" not in text
    try:
        try:
            raise ValueError("api_key=SECRET response=PRIVATE")
        except ValueError as error:
            raise RuntimeError("Authorization: SECRET") from error
    except RuntimeError as error:
        diagnostic = exception_diagnostic(error)
    assert "ValueError" in diagnostic and "RuntimeError" in diagnostic
    assert "test_logging_setup.py:" in diagnostic
    assert "SECRET" not in diagnostic and "PRIVATE" not in diagnostic


@pytest.mark.asyncio
async def test_call_logs_real_metadata_without_error_or_response_text(tmp_path) -> None:
    configure_logging("INFO", "metadata.log", tmp_path)
    proxy, router, client = _proxy([ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "PRIVATE_RESPONSE"),))])
    _ = [chunk async for chunk in proxy.chat([Message("user", "PRIVATE_PROMPT")])]
    async def failed(*args, **kwargs):
        error = RuntimeError("https://provider/?api_key=SECRET_KEY raw=PRIVATE_BODY")
        error.status_code = 503
        error.request_id = "req-isolated-1"
        raise error
        yield ChatChunk()
    client.chat.side_effect = failed
    router.select.return_value = ["primary"]
    router._get_retry_config.return_value = 1
    with pytest.raises(RuntimeError):
        _ = [chunk async for chunk in proxy.chat([Message("user", "PRIVATE_PROMPT")])]
    text = (tmp_path / "metadata.log").read_text(encoding="utf-8")
    assert '"http_status": 503' in text and '"request_id": "req-isolated-1"' in text
    assert '"response_chars": 16' in text and "llm.call.finished" in text
    assert "PRIVATE_" not in text and "SECRET_KEY" not in text
