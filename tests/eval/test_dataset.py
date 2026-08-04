"""PR5 验收 3：多 Draft 共存、稳定加载排序、非法路径片段与 schema 不兼容。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotclaw.eval.dataset import (
    CASE_FILE_SUFFIX,
    DRAFT_FILE_SUFFIX,
    case_exists,
    case_path,
    draft_path,
    load_case,
    load_cases,
    load_draft,
    load_draft_ids,
    save_case,
    save_draft,
)
from dotclaw.eval.draft import EvalCaseDraft
from dotclaw.eval.models import EvalCaseValidationError

from .helpers import build_case

DATASET = "ds-1"


def make_draft(draft_id: str, *, case_id: str | None = None) -> EvalCaseDraft:
    """构造最小合法草案。"""
    return EvalCaseDraft(
        draft_id=draft_id,
        source_run_id=f"run-{draft_id}",
        source_record_hash=f"hash-{draft_id}",
        source_trace_schema_version="1.0",
        case=build_case(case_id=case_id or f"case-{draft_id}"),
    )


# ---------------------------------------------------------------------------
# 目录布局与读写往返
# ---------------------------------------------------------------------------


def test_directory_layout_is_drafts_and_cases(tmp_path: Path) -> None:
    """目录即 Dataset：drafts/ 与 cases/ 各自固定后缀。"""
    assert draft_path(tmp_path, DATASET, "d1") == tmp_path / DATASET / "drafts" / f"d1{DRAFT_FILE_SUFFIX}"
    assert case_path(tmp_path, DATASET, "c1") == tmp_path / DATASET / "cases" / f"c1{CASE_FILE_SUFFIX}"


def test_draft_save_and_load_round_trip(tmp_path: Path) -> None:
    """草案写入后可原样读回。"""
    draft = make_draft("d1")
    save_draft(tmp_path, DATASET, draft)
    assert draft_path(tmp_path, DATASET, "d1").exists()
    assert load_draft(tmp_path, DATASET, "d1").to_dict() == draft.to_dict()


def test_case_save_and_load_round_trip(tmp_path: Path) -> None:
    """Case 写入后可原样读回。"""
    case = build_case(case_id="c1")
    save_case(tmp_path, DATASET, case)
    assert case_exists(tmp_path, DATASET, "c1") is True
    assert load_case(tmp_path, DATASET, "c1").to_dict() == case.to_dict()


# ---------------------------------------------------------------------------
# 缺失与冲突语义
# ---------------------------------------------------------------------------


def test_load_missing_draft_raises_file_not_found(tmp_path: Path) -> None:
    """读取不存在草案抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        load_draft(tmp_path, DATASET, "nope")


def test_load_missing_case_raises_file_not_found(tmp_path: Path) -> None:
    """读取不存在 Case 抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        load_case(tmp_path, DATASET, "nope")


def test_save_existing_draft_requires_explicit_overwrite(tmp_path: Path) -> None:
    """默认不覆盖既有草案，避免静默丢失审阅进度。"""
    save_draft(tmp_path, DATASET, make_draft("d1"))
    with pytest.raises(FileExistsError):
        save_draft(tmp_path, DATASET, make_draft("d1"))

    updated = EvalCaseDraft(
        draft_id="d1",
        source_run_id="run-d1",
        source_record_hash="hash-d1",
        source_trace_schema_version="1.0",
        case=build_case(case_id="case-d1"),
        requires_review=True,
    )
    save_draft(tmp_path, DATASET, updated, overwrite=True)
    assert load_draft(tmp_path, DATASET, "d1").requires_review is True


def test_save_existing_case_never_overwrites(tmp_path: Path) -> None:
    """Case 一旦落库不可被覆盖。"""
    save_case(tmp_path, DATASET, build_case(case_id="c1"))
    with pytest.raises(FileExistsError):
        save_case(tmp_path, DATASET, build_case(case_id="c1", agent_id="agent-other"))
    assert load_case(tmp_path, DATASET, "c1").agent_id != "agent-other"


# ---------------------------------------------------------------------------
# 多份共存与稳定排序
# ---------------------------------------------------------------------------


def test_multiple_drafts_coexist_with_stable_ordering(tmp_path: Path) -> None:
    """多个草案共存，标识按文件名稳定排序返回。"""
    for draft_id in ("d-c", "d-a", "d-b"):
        save_draft(tmp_path, DATASET, make_draft(draft_id))
    assert load_draft_ids(tmp_path, DATASET) == ["d-a", "d-b", "d-c"]
    assert load_draft_ids(tmp_path, DATASET) == load_draft_ids(tmp_path, DATASET)


def test_multiple_cases_load_in_stable_order(tmp_path: Path) -> None:
    """Case 按文件名稳定排序加载（Runner 只读 cases/）。"""
    for case_id in ("c-3", "c-1", "c-2"):
        save_case(tmp_path, DATASET, build_case(case_id=case_id))
    assert [item.case_id for item in load_cases(tmp_path, DATASET)] == ["c-1", "c-2", "c-3"]


def test_empty_dataset_loads_as_empty(tmp_path: Path) -> None:
    """空数据集返回空列表而非报错。"""
    assert load_cases(tmp_path, DATASET) == []
    assert load_draft_ids(tmp_path, DATASET) == []


def test_drafts_are_not_visible_to_case_loading(tmp_path: Path) -> None:
    """草案不会被 Case 加载看到：Runner 永不读取 drafts/。"""
    save_draft(tmp_path, DATASET, make_draft("d1"))
    assert load_cases(tmp_path, DATASET) == []


# ---------------------------------------------------------------------------
# 非法路径片段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".", "..", ""])
def test_illegal_dataset_name_is_rejected(tmp_path: Path, bad: str) -> None:
    """数据集标识必须是单个路径片段。"""
    with pytest.raises(ValueError):
        draft_path(tmp_path, bad, "d1")


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".", "..", ""])
def test_illegal_draft_and_case_ids_are_rejected(tmp_path: Path, bad: str) -> None:
    """草案与 Case 标识同样必须是单个路径片段。"""
    with pytest.raises(ValueError):
        draft_path(tmp_path, DATASET, bad)
    with pytest.raises(ValueError):
        case_path(tmp_path, DATASET, bad)


# ---------------------------------------------------------------------------
# schema 不兼容
# ---------------------------------------------------------------------------


def test_incompatible_draft_schema_is_rejected(tmp_path: Path) -> None:
    """磁盘上的草案 schema 版本不被支持时读取失败。"""
    save_draft(tmp_path, DATASET, make_draft("d1"))
    path = draft_path(tmp_path, DATASET, "d1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = "0.9"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(EvalCaseValidationError):
        load_draft(tmp_path, DATASET, "d1")


def test_incompatible_case_schema_is_rejected(tmp_path: Path) -> None:
    """磁盘上的 Case schema 版本不被支持时读取失败。"""
    save_case(tmp_path, DATASET, build_case(case_id="c1"))
    path = case_path(tmp_path, DATASET, "c1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = "0.9"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(EvalCaseValidationError):
        load_case(tmp_path, DATASET, "c1")
    with pytest.raises(EvalCaseValidationError):
        load_cases(tmp_path, DATASET)


def test_malformed_draft_payload_is_rejected(tmp_path: Path) -> None:
    """缺字段的草案文档读取时明确失败，不产出半成品对象。"""
    path = draft_path(tmp_path, DATASET, "broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    with pytest.raises(EvalCaseValidationError):
        load_draft(tmp_path, DATASET, "broken")
