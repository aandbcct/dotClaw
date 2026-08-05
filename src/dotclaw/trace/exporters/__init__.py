"""RunTrace 导出器集合。

``OtlpTraceExporter`` 依赖 OpenTelemetry SDK；模块级 import 为惰性——
仅当显式访问 ``OtlpTraceExporter`` 或 ``OtlpExportResult`` 时才触发 SDK 加载。
"""

from __future__ import annotations

from .json_exporter import JsonTraceExporter

__all__ = ["JsonTraceExporter", "OtlpTraceExporter", "OtlpExportResult"]

_OTLP_LOADED: bool = False
_OtlpTraceExporter: type | None = None
_OtlpExportResult: type | None = None


def __getattr__(name: str):
    global _OTLP_LOADED, _OtlpTraceExporter, _OtlpExportResult
    if name in ("OtlpTraceExporter", "OtlpExportResult"):
        if not _OTLP_LOADED:
            from .otlp_exporter import OtlpTraceExporter as _C1, OtlpExportResult as _C2
            _OtlpTraceExporter = _C1
            _OtlpExportResult = _C2
            _OTLP_LOADED = True
        if name == "OtlpTraceExporter":
            return _OtlpTraceExporter
        return _OtlpExportResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
