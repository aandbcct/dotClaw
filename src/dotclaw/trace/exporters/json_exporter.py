"""RunTrace 的显式 JSON 导出器。

``JsonTraceExporter.export`` 是 Trace 侧唯一的文件写动作。默认导出 schema、来源
元数据、Span、Issue、消息 ID 与 ContextVersion 引用和脱敏预览，不导出 Prompt、模型
正文、工具完整输出或 Secret；``include_content=True`` 才导出完整内容。无论内容模式
如何，``record_hash`` 都指向原始权威事实，不参与内容开关。

部分 Trace（运行未完成、存在未闭合 Span 或关键关联缺失）必须显式 ``allow_partial=True``
才能导出，避免把不完整的重建误当成完整事实消费。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import RunTrace


class JsonTraceExporter:
    """把 RunTrace 显式导出为 JSON 文件。"""

    def export(
        self,
        trace: RunTrace,
        output_path: str | Path,
        *,
        include_content: bool = False,
        allow_partial: bool = False,
    ) -> Path:
        """将追踪写入 ``output_path`` 并返回该路径；同一路径允许覆盖。"""
        if trace.is_partial and not allow_partial:
            raise ValueError("部分 Trace 必须显式 allow_partial=True 才能导出")
        data = trace.to_dict(include_content=include_content)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
