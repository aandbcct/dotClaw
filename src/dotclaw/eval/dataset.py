"""目录即 Dataset 的文件仓储：drafts/ 与 cases/ 两种稳定 JSON 文档。

布局：``<root>/<dataset_name>/{drafts/<draft_id>.draft.json, cases/<case_id>.json}``。
所有写入复用 Runtime 既有的临时文件 + 原子替换原语；``dataset_name`` / ``draft_id`` /
``case_id`` 均经同一路径片段校验，杜绝越过 Dataset 根目录。本模块不解释审阅状态机，
也不承担 Manifest / Registry；仅提供底层读写与稳定排序加载。
"""

from __future__ import annotations

from pathlib import Path

from ..runtime.adapters._file_support import (
    load_json_map,
    validate_path_segment,
    write_json_atomic,
)
from ..runtime.domain.facts import JSONMap
from .draft import DRAFT_SCHEMA_VERSION, EvalCaseDraft
from .models import EvalCase, EvalCaseValidationError

DRAFT_FILE_SUFFIX: str = ".draft.json"
CASE_FILE_SUFFIX: str = ".json"
_DRAFTS_DIRNAME = "drafts"
_CASES_DIRNAME = "cases"


def _resolve_dataset_dir(root: Path, dataset_name: str) -> Path:
    """校验数据集标识并返回其绝对目录。"""
    validate_path_segment(dataset_name, "dataset_name")
    return root / dataset_name


def draft_path(root: Path, dataset_name: str, draft_id: str) -> Path:
    """计算 Draft 文件的绝对路径。"""
    validate_path_segment(dataset_name, "dataset_name")
    validate_path_segment(draft_id, "draft_id")
    return root / dataset_name / _DRAFTS_DIRNAME / f"{draft_id}{DRAFT_FILE_SUFFIX}"


def case_path(root: Path, dataset_name: str, case_id: str) -> Path:
    """计算 Case 文件的绝对路径。"""
    validate_path_segment(dataset_name, "dataset_name")
    validate_path_segment(case_id, "case_id")
    return root / dataset_name / _CASES_DIRNAME / f"{case_id}{CASE_FILE_SUFFIX}"


def load_draft(root: Path, dataset_name: str, draft_id: str) -> EvalCaseDraft:
    """读取单个 Draft；不存在抛 ``FileNotFoundError``。"""
    path: Path = draft_path(root, dataset_name, draft_id)
    if not path.exists():
        raise FileNotFoundError(f"Draft 不存在：{dataset_name}/{draft_id}")
    return EvalCaseDraft.from_dict(load_json_map(path))


def save_draft(
    root: Path,
    dataset_name: str,
    draft: EvalCaseDraft,
    *,
    overwrite: bool = False,
) -> None:
    """原子写入 Draft；已存在且未显式覆盖时抛 ``FileExistsError``。"""
    path: Path = draft_path(root, dataset_name, draft.draft_id)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Draft 已存在：{dataset_name}/{draft.draft_id}")
    write_json_atomic(path, draft.to_dict())


def load_case(root: Path, dataset_name: str, case_id: str) -> EvalCase:
    """读取单个 Case；不存在抛 ``FileNotFoundError``。"""
    path: Path = case_path(root, dataset_name, case_id)
    if not path.exists():
        raise FileNotFoundError(f"Case 不存在：{dataset_name}/{case_id}")
    return EvalCase.from_dict(load_json_map(path))


def case_exists(root: Path, dataset_name: str, case_id: str) -> bool:
    """判断目标 Case 是否已落库。"""
    return case_path(root, dataset_name, case_id).exists()


def save_case(root: Path, dataset_name: str, case: EvalCase) -> None:
    """原子写入 Case；目标已存在时抛 ``FileExistsError`` 以保留既有 Case。"""
    path: Path = case_path(root, dataset_name, case.case_id)
    if path.exists():
        raise FileExistsError(f"Case 已存在：{dataset_name}/{case.case_id}")
    write_json_atomic(path, case.to_dict())


def load_cases(root: Path, dataset_name: str) -> list[EvalCase]:
    """按文件名稳定排序加载数据集内全部 Case（Runner 只读此目录）。"""
    base: Path = _resolve_dataset_dir(root, dataset_name) / _CASES_DIRNAME
    if not base.exists():
        return []
    cases: list[EvalCase] = []
    for path in sorted(base.glob(f"*{CASE_FILE_SUFFIX}")):
        if not path.is_file():
            continue
        cases.append(EvalCase.from_dict(load_json_map(path)))
    return cases


def load_draft_ids(root: Path, dataset_name: str) -> list[str]:
    """按文件名稳定排序返回数据集内全部 Draft 标识。"""
    base: Path = _resolve_dataset_dir(root, dataset_name) / _DRAFTS_DIRNAME
    if not base.exists():
        return []
    draft_ids: list[str] = []
    for path in sorted(base.glob(f"*{DRAFT_FILE_SUFFIX}")):
        if not path.is_file():
            continue
        draft_ids.append(path.name[: -len(DRAFT_FILE_SUFFIX)])
    return draft_ids
