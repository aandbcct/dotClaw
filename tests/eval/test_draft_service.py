"""PR5 验收 5 / 6：审阅保存、Case 原子创建与冲突语义，以及 Channel 只经服务访问。"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from dotclaw.eval.dataset import case_exists, case_path, draft_path, save_draft
from dotclaw.eval.draft import EvalCaseDraft
from dotclaw.eval.draft_service import EvalCaseDraftService
from dotclaw.eval.models import EvalCaseValidationError
from dotclaw.trace.models import RunTrace

from .helpers import build_case, llm_response, make_llm_fixture, make_terminal_trace

DATASET = "ds-1"


def service_at(tmp_path: Path) -> EvalCaseDraftService:
    """在临时目录上构造服务。"""
    return EvalCaseDraftService(tmp_path)


def seed_pending_draft(root: Path, draft_id: str = "draft-pending") -> EvalCaseDraft:
    """直接落一份待人工审阅的草案（测试装配，不经 Channel）。"""
    draft = EvalCaseDraft(
        draft_id=draft_id,
        source_run_id="run-pending",
        source_record_hash="hash-pending",
        source_trace_schema_version="1.0",
        case=build_case(case_id="case-pending"),
        requires_review=True,
    )
    save_draft(root, DATASET, draft)
    return draft


# ---------------------------------------------------------------------------
# 生成与读取
# ---------------------------------------------------------------------------


async def test_create_draft_from_trace_persists_redacted_draft(tmp_path: Path) -> None:
    """从终态 Trace 生成草案，经脱敏后落盘并可读回。"""
    service = service_at(tmp_path)
    draft = await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    assert draft.draft_id == "draft-run-1"
    assert draft_path(tmp_path, DATASET, "draft-run-1").exists()
    assert (await service.load_draft(DATASET, "draft-run-1")).to_dict() == draft.to_dict()


async def test_create_draft_rejects_partial_trace(tmp_path: Path) -> None:
    """部分 Trace 在服务层同样被拒绝。"""
    service = service_at(tmp_path)
    with pytest.raises(EvalCaseValidationError):
        await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1", ended=False))


async def test_create_draft_does_not_silently_overwrite(tmp_path: Path) -> None:
    """同一 Trace 重复生成不会覆盖既有审阅进度。"""
    service = service_at(tmp_path)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    with pytest.raises(FileExistsError):
        await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))


async def test_load_missing_draft_raises_file_not_found(tmp_path: Path) -> None:
    """读取不存在草案抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        await service_at(tmp_path).load_draft(DATASET, "nope")


async def test_illegal_dataset_name_is_rejected(tmp_path: Path) -> None:
    """服务只能操作根目录下的单层数据集。"""
    with pytest.raises(ValueError):
        await service_at(tmp_path).load_draft("../escape", "d1")


# ---------------------------------------------------------------------------
# 审阅保存
# ---------------------------------------------------------------------------


async def test_save_reviewed_draft_clears_review_flag(tmp_path: Path) -> None:
    """审阅保存显式清除 requires_review，并持久化审阅后的载荷。"""
    service = service_at(tmp_path)
    seed_pending_draft(tmp_path)
    edited = EvalCaseDraft(
        draft_id="draft-pending",
        source_run_id="run-pending",
        source_record_hash="hash-pending",
        source_trace_schema_version="1.0",
        case=build_case(
            case_id="case-pending",
            llm_fixture=make_llm_fixture("llm-1", (llm_response("m1", content="人工替换后的回复"),)),
        ),
    )
    reviewed = await service.save_reviewed_draft(DATASET, "draft-pending", edited)
    assert reviewed.requires_review is False

    reloaded = await service.load_draft(DATASET, "draft-pending")
    assert reloaded.requires_review is False
    assert reloaded.case.llm_fixture.responses[0].content == "人工替换后的回复"


async def test_save_reviewed_draft_on_missing_draft_raises(tmp_path: Path) -> None:
    """审阅不存在的草案抛 FileNotFoundError。"""
    service = service_at(tmp_path)
    draft = EvalCaseDraft(
        draft_id="ghost",
        source_run_id="run-1",
        source_record_hash="hash-1",
        source_trace_schema_version="1.0",
        case=build_case(),
    )
    with pytest.raises(FileNotFoundError):
        await service.save_reviewed_draft(DATASET, "ghost", draft)


# ---------------------------------------------------------------------------
# 确认为 Case
# ---------------------------------------------------------------------------


async def test_confirm_draft_creates_case_and_writes_back(tmp_path: Path) -> None:
    """确认成功后 Case 落库、草案保留并回写 confirmed_case_id。"""
    service = service_at(tmp_path)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    case = await service.confirm_draft(DATASET, "draft-run-1", "case-final")

    assert case.case_id == "case-final"
    assert case_path(tmp_path, DATASET, "case-final").exists()
    assert draft_path(tmp_path, DATASET, "draft-run-1").exists()
    assert (await service.load_draft(DATASET, "draft-run-1")).confirmed_case_id == "case-final"
    assert [item.case_id for item in await service.list_cases(DATASET)] == ["case-final"]


async def test_confirm_rejects_draft_pending_review(tmp_path: Path) -> None:
    """待人工审阅的草案不可确认；服务不信任客户端仅传确认参数。"""
    service = service_at(tmp_path)
    seed_pending_draft(tmp_path)
    with pytest.raises(ValueError):
        await service.confirm_draft(DATASET, "draft-pending", "case-x")
    assert case_exists(tmp_path, DATASET, "case-x") is False


async def test_confirm_rejects_duplicate_case_id(tmp_path: Path) -> None:
    """目标 Case 已存在时报错，绝不覆盖既有 Case。"""
    service = service_at(tmp_path)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-2"))
    await service.confirm_draft(DATASET, "draft-run-1", "case-final")

    before = case_path(tmp_path, DATASET, "case-final").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        await service.confirm_draft(DATASET, "draft-run-2", "case-final")
    assert case_path(tmp_path, DATASET, "case-final").read_text(encoding="utf-8") == before


async def test_confirm_twice_is_rejected(tmp_path: Path) -> None:
    """已确认草案重复确认失败。"""
    service = service_at(tmp_path)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    await service.confirm_draft(DATASET, "draft-run-1", "case-final")
    with pytest.raises(ValueError):
        await service.confirm_draft(DATASET, "draft-run-1", "case-another")
    assert case_exists(tmp_path, DATASET, "case-another") is False


async def test_reviewing_confirmed_draft_is_rejected(tmp_path: Path) -> None:
    """已确认草案不可再走审阅保存。"""
    service = service_at(tmp_path)
    draft = await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    await service.confirm_draft(DATASET, "draft-run-1", "case-final")
    with pytest.raises(ValueError):
        await service.save_reviewed_draft(DATASET, "draft-run-1", draft)


async def test_interrupted_confirm_keeps_case_and_reports_manual_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回写 confirmed_case_id 失败时：Case 已落库不被覆盖，后续确认报告需人工处理。"""
    service = service_at(tmp_path)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("模拟回写中断")

    monkeypatch.setattr("dotclaw.eval.draft_service.save_draft", boom)
    with pytest.raises(OSError):
        await service.confirm_draft(DATASET, "draft-run-1", "case-final")
    monkeypatch.undo()

    # 中断后的可见状态：Case 已存在，草案仍未标记确认。
    assert case_exists(tmp_path, DATASET, "case-final") is True
    assert (await service.load_draft(DATASET, "draft-run-1")).confirmed_case_id is None

    saved = case_path(tmp_path, DATASET, "case-final").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        await service.confirm_draft(DATASET, "draft-run-1", "case-final")
    assert case_path(tmp_path, DATASET, "case-final").read_text(encoding="utf-8") == saved


async def test_confirm_uses_given_case_id_as_authority(tmp_path: Path) -> None:
    """落库 Case 以确认时传入的 case_id 为准。"""
    service = service_at(tmp_path)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    case = await service.confirm_draft(DATASET, "draft-run-1", "case-renamed")
    assert case.case_id == "case-renamed"
    assert case_exists(tmp_path, DATASET, "case-run-1") is False


async def test_service_is_confined_to_configured_root(tmp_path: Path) -> None:
    """服务只在配置的 Dataset 根目录下产生文件。"""
    root = tmp_path / "datasets"
    other = tmp_path / "elsewhere"
    other.mkdir()
    service = EvalCaseDraftService(root)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    await service.confirm_draft(DATASET, "draft-run-1", "case-final")

    assert service.datasets_root == root
    assert list(other.iterdir()) == []
    written = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert written == {
        f"{DATASET}/drafts/draft-run-1.draft.json",
        f"{DATASET}/cases/case-final.json",
    }


async def test_listing_is_stable(tmp_path: Path) -> None:
    """草案与 Case 列表稳定排序。"""
    service = service_at(tmp_path)
    for run_id in ("run-c", "run-a", "run-b"):
        await service.create_draft_from_trace(DATASET, make_terminal_trace(run_id))
    assert await service.list_drafts(DATASET) == ["draft-run-a", "draft-run-b", "draft-run-c"]


# ---------------------------------------------------------------------------
# 验收 6：Channel 只经服务加载 / 审阅 / 确认，无直接文件访问
# ---------------------------------------------------------------------------


class RecordingChannel:
    """记录输出的最小 Channel 替身。"""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str) -> None:
        self.errors.append(message)


class RecordingTraceService:
    """返回固定 Trace 的最小只读服务替身。"""

    def __init__(self, trace: RunTrace) -> None:
        self._trace = trace
        self.requested_run_ids: list[str] = []

    async def get_trace(self, run_id: str) -> RunTrace:
        self.requested_run_ids.append(run_id)
        if run_id != self._trace.source.run_id:
            raise LookupError(f"未找到 Run: {run_id}")
        return self._trace


async def test_channel_command_walks_full_review_flow(tmp_path: Path) -> None:
    """Channel 命令依次完成列出 / 查看 / 审阅 / 确认，全部经服务。"""
    from dotclaw.main import _cmd_eval

    service = service_at(tmp_path)
    await service.create_draft_from_trace(DATASET, make_terminal_trace("run-1"))
    channel = RecordingChannel()
    trace_service = RecordingTraceService(make_terminal_trace("run-1"))

    await _cmd_eval(channel, service, trace_service, f"list {DATASET}")
    assert any("1 个草案" in text for text in channel.infos)

    await _cmd_eval(channel, service, trace_service, f"show {DATASET} draft-run-1")
    assert any("draft-run-1" in text for text in channel.infos)

    await _cmd_eval(channel, service, trace_service, f"review {DATASET} draft-run-1")
    assert (await service.load_draft(DATASET, "draft-run-1")).requires_review is False

    await _cmd_eval(channel, service, trace_service, f"confirm {DATASET} draft-run-1 case-final")
    assert channel.errors == []
    assert case_exists(tmp_path, DATASET, "case-final") is True

    channel.infos.clear()
    await _cmd_eval(channel, service, trace_service, f"list {DATASET}")
    assert any("1 个 Case" in text for text in channel.infos)


async def test_channel_reports_service_errors_without_crashing(tmp_path: Path) -> None:
    """服务错误被转为用户可读提示，不向上抛出。"""
    from dotclaw.main import _cmd_eval

    service = service_at(tmp_path)
    channel = RecordingChannel()
    trace_service = RecordingTraceService(make_terminal_trace("run-1"))

    await _cmd_eval(channel, service, trace_service, f"show {DATASET} missing")
    await _cmd_eval(channel, service, trace_service, f"confirm {DATASET} missing case-x")
    await _cmd_eval(channel, service, trace_service, "bogus")
    await _cmd_eval(channel, service, trace_service, "")

    assert len(channel.errors) == 3
    assert all(text for text in channel.errors)


def test_channel_command_has_no_direct_file_access() -> None:
    """Channel 命令实现中不出现任何直接文件 / JSON 访问。"""
    from dotclaw.main import _cmd_eval

    source = inspect.getsource(_cmd_eval)
    for forbidden in ("open(", "json.", "Path(", "write_text", "read_text", "glob(", "dataset_path"):
        assert forbidden not in source, f"Channel 命令不应直接访问文件：{forbidden}"


async def test_channel_reads_trace_and_creates_draft_from_it(tmp_path: Path) -> None:
    """CLI 先读取 RunTrace，再从该 Trace 生成待审阅 Draft。"""
    from dotclaw.main import _cmd_eval, _cmd_trace

    trace = make_terminal_trace("run-1")
    trace_service = RecordingTraceService(trace)
    service = service_at(tmp_path)
    channel = RecordingChannel()

    await _cmd_trace(channel, trace_service, "run-1")
    await _cmd_eval(channel, service, trace_service, f"create {DATASET} run-1")

    assert trace_service.requested_run_ids == ["run-1", "run-1"]
    assert (await service.load_draft(DATASET, "draft-run-1")).source_run_id == "run-1"
    assert any(text.startswith("Trace run-1") for text in channel.infos)
    assert any(text.startswith("已创建 Draft: draft-run-1") for text in channel.infos)


def test_channel_package_does_not_import_dataset_layer() -> None:
    """Channel 包不依赖 Dataset 文件仓储，只由服务中介。"""
    channel_dir = Path(__file__).resolve().parents[2] / "src" / "dotclaw" / "channel"
    for path in channel_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "eval.dataset" not in text
        assert "from ..eval" not in text


# ---------------------------------------------------------------------------
# 组合根：Dataset 根路径由配置解析后注入
# ---------------------------------------------------------------------------


def test_dataset_directory_has_default_config() -> None:
    """未配置时使用默认 Dataset 目录。"""
    from dotclaw.config.settings import Config, EvalConfig

    assert EvalConfig().dataset_directory == "./data/datasets"
    assert Config().eval.dataset_directory == "./data/datasets"


def test_dataset_root_resolution_matches_session_storage(tmp_path: Path) -> None:
    """相对目录相对项目根解析，绝对目录原样保留。"""
    from dotclaw.bootstrap.runtime_factory import _storage_root

    assert _storage_root(tmp_path, "./data/datasets") == tmp_path / "data" / "datasets"
    absolute = tmp_path / "elsewhere"
    assert _storage_root(tmp_path, str(absolute)) == absolute


def test_host_exposes_service_only_after_initialize() -> None:
    """未初始化时访问服务立即失败，避免拿到半成品依赖。"""
    from dotclaw.bootstrap import ApplicationHost

    host = ApplicationHost.__new__(ApplicationHost)
    host._eval_draft_service = None
    with pytest.raises(RuntimeError):
        _ = host.eval_draft_service


# ---------------------------------------------------------------------------
# E2E：Trace → Draft → confirm → EvalRunner（PR5 核心闭环）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_trace_to_case_through_runner_passes(tmp_path: Path) -> None:
    """从最简工具调用 Trace 到 Runner 执行全链路通过。

    链路：make_simple_trace → create_draft_from_trace → confirm_draft →
    EvalRunner.run() → passed=True。
    验证 Draft 生成的 Expectation 与 PR4 scorer 契约一致。
    """
    from dotclaw.eval.runner import EvalRunner
    from .helpers import make_simple_trace

    trace = make_simple_trace("run-e2e")
    svc = service_at(tmp_path)
    draft = await svc.create_draft_from_trace(DATASET, trace)
    case = await svc.confirm_draft(DATASET, draft.draft_id, "case-e2e")

    result = await EvalRunner().run(case)
    assert result.passed is True, f"E2E 全链路应通过，实际失败：{result.failure_detail}"
    assert result.failure_kind is None
