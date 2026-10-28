"""CLI 与 Web 共用的日志配置及请求关联上下文。"""

from __future__ import annotations

import logging
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from typing import Iterator

_session_id: ContextVar[str] = ContextVar("dotclaw_log_session", default="-")
_run_id: ContextVar[str] = ContextVar("dotclaw_log_run", default="-")
_HANDLER_MARK = "_dotclaw_logging_setup"
_lock = RLock()


class LogContextFilter(logging.Filter):
    """仅向本模块的处理器补齐并发隔离的关联字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = getattr(record, "session_id", _session_id.get())
        record.run_id = getattr(record, "run_id", _run_id.get())
        return True


@contextmanager
def bind_log_context(session_id: str, run_id: str) -> Iterator[None]:
    """绑定一次调用的关联标识，退出或取消均恢复旧上下文。"""
    session_token = _session_id.set(session_id or "-")
    run_token = _run_id.set(run_id or "-")
    try:
        yield
    finally:
        _run_id.reset(run_token)
        _session_id.reset(session_token)


def exception_diagnostic(error: BaseException) -> str:
    """仅保留异常类型、调用位置及原因链，不输出第三方错误正文或局部变量。"""
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        frames = traceback.extract_tb(current.__traceback__)[-3:]
        locations = ",".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
        chain.append(f"{type(current).__name__}[{locations}]")
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return " <- ".join(chain)


def configure_logging(level: str, log_file: str, project_root: Path) -> None:
    """幂等配置自有控制台与 UTF-8 文件处理器，文件不可写时明确终止启动。"""
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError("日志级别配置无效")
    target = Path(log_file).expanduser()
    target = (target if target.is_absolute() else project_root / target).resolve()
    logger = logging.getLogger("dotclaw")
    with _lock:
        owned = [handler for handler in logger.handlers if getattr(handler, _HANDLER_MARK, False)]
        same_file = next((handler for handler in owned if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == target), None)
        if same_file is None:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(target, encoding="utf-8")
            except OSError as error:
                raise ValueError("日志文件无法创建，请检查日志路径及写入权限") from error
            handlers: list[logging.Handler] = [logging.StreamHandler(), file_handler]
            for handler in owned:
                logger.removeHandler(handler)
                handler.close()
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s session_id=%(session_id)s run_id=%(run_id)s %(message)s")
            for handler in handlers:
                setattr(handler, _HANDLER_MARK, True)
                handler.setFormatter(formatter)
                handler.addFilter(LogContextFilter())
                logger.addHandler(handler)
        logger.setLevel(numeric_level)
        for handler in logger.handlers:
            if getattr(handler, _HANDLER_MARK, False):
                handler.setLevel(numeric_level)
        logger.propagate = False
        # 禁止 SDK 的请求/响应调试正文进入共享日志。
        for name in ("httpx", "httpcore", "openai"):
            logging.getLogger(name).setLevel(logging.WARNING)
