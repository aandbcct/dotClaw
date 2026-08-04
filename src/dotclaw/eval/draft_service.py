"""EvalCaseDraftService：Channel 处理 Draft 生成 / 审阅 / 确认的窄应用入口。

服务是 Channel 与 Dataset 文件仓储之间的唯一通道：Channel 只调用本服务并渲染结果，
绝不直接读写 Dataset 目录下的 JSON。所有文件访问经由 ``dataset`` 模块，且只作用于
配置的 Dataset 根目录；``dataset_name`` / ``draft_id`` / ``case_id`` 均经路径片段校验。

错误语义：
* 读取不存在 Draft → ``FileNotFoundError``；
* 确认目标已存在 Case → ``FileExistsError``；
* 待审核（``requires_review=True``）Draft 确认 → ``ValueError``；
* 已确认 Draft 重复确认 → ``ValueError``。
"""

from __future__ import annotations

from pathlib import Path

from ..trace.models import RunTrace
from .dataset import (
    case_exists,
    load_draft,
    load_cases,
    load_draft_ids,
    save_case,
    save_draft,
)
from .draft import EvalCaseDraft, trace_to_eval_case_draft
from .models import EvalCase
from .redaction import redact_draft


class EvalCaseDraftService:
    """Channel 侧的 Draft 审阅与确认服务；不持有任何 Runtime 状态。"""

    def __init__(self, datasets_root: Path) -> None:
        """绑定 Dataset 根目录；所有读写都限制在该根目录下。"""
        self._root: Path = datasets_root

    @property
    def datasets_root(self) -> Path:
        """返回受控的 Dataset 根目录。"""
        return self._root

    async def create_draft_from_trace(
        self,
        dataset_name: str,
        trace: RunTrace,
        *,
        case_id: str | None = None,
        name: str = "",
    ) -> EvalCaseDraft:
        """从终态 Trace 生成草案、脱敏并持久化（Channel 侧唯一生成入口）。"""
        draft = trace_to_eval_case_draft(trace, case_id=case_id, name=name)
        redacted = redact_draft(draft)
        # 已存在同 draft_id 时抛 FileExistsError，避免静默覆盖既有审阅进度。
        save_draft(self._root, dataset_name, redacted)
        return redacted

    async def load_draft(self, dataset_name: str, draft_id: str) -> EvalCaseDraft:
        """读取单个草案；不存在抛 ``FileNotFoundError``。"""
        return load_draft(self._root, dataset_name, draft_id)

    async def save_reviewed_draft(
        self, dataset_name: str, draft_id: str, draft: EvalCaseDraft
    ) -> EvalCaseDraft:
        """保存人工审阅后的内容并显式清除审阅标记；不存在抛 ``FileNotFoundError``。"""
        existing = load_draft(self._root, dataset_name, draft_id)
        if existing.confirmed_case_id is not None:
            raise ValueError(f"Draft {draft_id} 已确认（case={existing.confirmed_case_id}），不可再审阅")
        reviewed = EvalCaseDraft(
            draft_id=existing.draft_id,
            source_run_id=existing.source_run_id,
            source_record_hash=existing.source_record_hash,
            source_trace_schema_version=existing.source_trace_schema_version,
            case=draft.case,
            requires_review=False,
            confirmed_case_id=None,
        )
        save_draft(self._root, dataset_name, reviewed, overwrite=True)
        return reviewed

    async def confirm_draft(self, dataset_name: str, draft_id: str, case_id: str) -> EvalCase:
        """原子确认草案为 Case。

        步骤：加载并验证 Draft → 检查目标 Case 不存在 → 原子写 Case → 原子回写
        Draft 的 ``confirmed_case_id``。若最后一步失败，已落库的 Case 不会被覆盖，
        后续确认会因 Case 已存在而报告需人工处理。
        """
        draft = load_draft(self._root, dataset_name, draft_id)  # FileNotFoundError
        if draft.confirmed_case_id is not None:
            raise ValueError(
                f"Draft {draft_id} 已确认（case={draft.confirmed_case_id}），不可重复确认"
            )
        if draft.requires_review:
            raise ValueError(
                f"Draft {draft_id} 仍需人工审阅（requires_review=True），请先经审阅清除标记"
            )
        if case_exists(self._root, dataset_name, case_id):
            raise FileExistsError(
                f"目标 Case 已存在：{dataset_name}/{case_id}，需人工处理（不要覆盖既有 Case）"
            )
        # 以传入的 case_id 为权威标识重建确认用 Case。
        confirmed_case = EvalCase.from_dict({**draft.case.to_dict(), "case_id": case_id})
        save_case(self._root, dataset_name, confirmed_case)  # FileExistsError if exists
        confirmed_draft = EvalCaseDraft(
            draft_id=draft.draft_id,
            source_run_id=draft.source_run_id,
            source_record_hash=draft.source_record_hash,
            source_trace_schema_version=draft.source_trace_schema_version,
            case=draft.case,
            requires_review=draft.requires_review,
            confirmed_case_id=case_id,
        )
        try:
            save_draft(self._root, dataset_name, confirmed_draft, overwrite=True)
        except BaseException:
            # 回写失败：既有的 Case 已落库，不覆盖；下次确认会检测到 Case 存在并报告需人工处理。
            raise
        return confirmed_case

    async def list_drafts(self, dataset_name: str) -> list[str]:
        """列出数据集内全部 Draft 标识（稳定排序）。"""
        return load_draft_ids(self._root, dataset_name)

    async def list_cases(self, dataset_name: str) -> list[EvalCase]:
        """列出数据集内全部 Case（稳定排序，Runner 只读此目录）。"""
        return load_cases(self._root, dataset_name)
