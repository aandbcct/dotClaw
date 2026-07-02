"""测试 Task —— Agent 间通信的聚合实体 + 句柄能力。"""

import asyncio

import pytest
from datetime import datetime

from dotclaw.agent.task import Task, TaskStatus
from dotclaw.agent.artifact import Artifact, ArtifactType


class TestTaskStatus:
    """TaskStatus 枚举。"""

    def test_all_states_defined(self) -> None:
        """包含 A2A 定义的全部 5 个状态。"""
        values: set[str] = {s.value for s in TaskStatus}
        assert values == {"submitted", "working", "completed", "failed", "canceled"}

    def test_string_conversion(self) -> None:
        """从字符串构造枚举。"""
        assert TaskStatus("submitted") == TaskStatus.SUBMITTED
        assert TaskStatus("working") == TaskStatus.WORKING
        assert TaskStatus("completed") == TaskStatus.COMPLETED
        assert TaskStatus("failed") == TaskStatus.FAILED
        assert TaskStatus("canceled") == TaskStatus.CANCELED

    def test_is_terminal(self) -> None:
        """terminal 状态为 completed/failed/canceled。"""
        assert TaskStatus.SUBMITTED.is_terminal() is False
        assert TaskStatus.WORKING.is_terminal() is False
        assert TaskStatus.COMPLETED.is_terminal() is True
        assert TaskStatus.FAILED.is_terminal() is True
        assert TaskStatus.CANCELED.is_terminal() is True


class TestTask:
    """Task 构造、序列化、生命周期。"""

    def test_minimal_construction(self) -> None:
        """最小构造：仅需 task_id + requester + description。"""
        t = Task(
            task_id="t1",
            requester="parent-agent",
            description="分析用户数据",
        )
        assert t.task_id == "t1"
        assert t.requester == "parent-agent"
        assert t.description == "分析用户数据"
        assert t.context == ""
        assert t.constraints == ""
        assert t.input_artifacts == []
        assert t.status == TaskStatus.SUBMITTED
        assert t.final_result == ""
        assert t.output_artifacts == []
        assert t.error == ""
        assert t.parent_run_id == ""
        assert t.sub_run_id == ""

    def test_full_input_construction(self) -> None:
        """父 Agent 填充完整输入字段。"""
        t = Task(
            task_id="t2",
            requester="master",
            description="整理数据",
            context="最近3天日志: ...",
            constraints="仅用内置工具",
            input_artifacts=[
                Artifact(name="log.txt", artifact_type=ArtifactType.FILE, uri="/tmp/log.txt"),
            ],
            parent_run_id="run-001",
        )
        assert t.description == "整理数据"
        assert t.context == "最近3天日志: ..."
        assert t.constraints == "仅用内置工具"
        assert len(t.input_artifacts) == 1
        assert t.input_artifacts[0].name == "log.txt"
        assert t.parent_run_id == "run-001"

    def test_mark_working(self) -> None:
        """子 Agent 开始执行，标记 working。"""
        t = Task(task_id="t3", requester="p", description="x")
        t.mark_working()
        assert t.status == TaskStatus.WORKING

    def test_mark_completed(self) -> None:
        """子 Agent 执行完毕，标记 completed 并填充结果。"""
        t = Task(task_id="t4", requester="p", description="x")
        t.mark_completed(
            final_result="分析完成：共找到3个匹配项",
            output_artifacts=[
                Artifact(name="result.json", artifact_type=ArtifactType.JSON, content='{"matches":3}'),
            ],
            sub_run_id="sub-run-1",
        )
        assert t.status == TaskStatus.COMPLETED
        assert t.final_result == "分析完成：共找到3个匹配项"
        assert len(t.output_artifacts) == 1
        assert t.output_artifacts[0].name == "result.json"
        assert t.sub_run_id == "sub-run-1"

    def test_mark_failed(self) -> None:
        """子 Agent 执行失败，标记 failed 并填充 error。"""
        t = Task(task_id="t5", requester="p", description="x")
        t.mark_failed(error="API 调用超时")
        assert t.status == TaskStatus.FAILED
        assert t.error == "API 调用超时"

    def test_mark_canceled(self) -> None:
        """父 Agent 取消任务，标记 canceled。"""
        t = Task(task_id="t6", requester="p", description="x")
        t.mark_canceled()
        assert t.status == TaskStatus.CANCELED

    def test_is_terminal(self) -> None:
        """Task 级别的 is_terminal 委托给 status 枚举。"""
        t = Task(task_id="t7", requester="p", description="x")
        assert t.is_terminal() is False  # submitted
        t.mark_completed(final_result="done")
        assert t.is_terminal() is True

    def test_to_dict(self) -> None:
        """序列化完整的 Task 为 dict。"""
        t = Task(
            task_id="t10",
            requester="master",
            description="任务描述",
            context="context数据",
            constraints="限制",
            input_artifacts=[Artifact(name="file.txt")],
            parent_run_id="run-p",
        )
        t.mark_working()
        t.mark_completed(
            final_result="完成",
            output_artifacts=[Artifact(name="out.json", artifact_type=ArtifactType.JSON)],
            sub_run_id="sub-1",
        )
        d = t.to_dict()
        assert d["task_id"] == "t10"
        assert d["requester"] == "master"
        assert d["status"] == "completed"
        assert d["final_result"] == "完成"
        assert d["parent_run_id"] == "run-p"
        assert d["sub_run_id"] == "sub-1"
        assert len(d["input_artifacts"]) == 1
        assert len(d["output_artifacts"]) == 1
        # created_at / updated_at 由 mark_* 自动填充
        assert t.created_at != ""
        assert t.updated_at != ""

    def test_from_dict(self) -> None:
        """反序列化 dict 为 Task。"""
        d = {
            "task_id": "t20",
            "requester": "master",
            "description": "d",
            "context": "c",
            "status": "completed",
            "final_result": "r",
            "parent_run_id": "p1",
            "sub_run_id": "s1",
            "input_artifacts": [{"name": "a1", "artifact_type": "text"}],
            "output_artifacts": [{"name": "a2", "artifact_type": "json", "content": "{}"}],
        }
        t = Task.from_dict(d)
        assert t.task_id == "t20"
        assert t.status == TaskStatus.COMPLETED
        assert t.final_result == "r"
        assert len(t.input_artifacts) == 1
        assert t.input_artifacts[0].name == "a1"
        assert len(t.output_artifacts) == 1
        assert t.output_artifacts[0].name == "a2"

    def test_roundtrip(self) -> None:
        """序列化再反序列化完整一致性。"""
        t = Task(
            task_id="t30",
            requester="m",
            description="desc",
            context="ctx",
            constraints="cst",
            input_artifacts=[Artifact(name="in", content="hello")],
            parent_run_id="rp",
        )
        t.mark_completed(
            final_result="done",
            output_artifacts=[Artifact(name="out", artifact_type=ArtifactType.JSON, content='{"x":1}')],
            sub_run_id="rs",
        )
        t2 = Task.from_dict(t.to_dict())
        assert t2.task_id == t.task_id
        assert t2.requester == t.requester
        assert t2.status == t.status
        assert t2.final_result == t.final_result
        assert t2.error == t.error
        assert t2.parent_run_id == t.parent_run_id
        assert t2.sub_run_id == t.sub_run_id
        assert len(t2.input_artifacts) == len(t.input_artifacts)
        assert len(t2.output_artifacts) == len(t.output_artifacts)


class TestTaskHandle:
    """Task 作为句柄的 result() / cancel() 能力。"""

    @pytest.mark.asyncio
    async def test_result_returns_on_completed(self) -> None:
        """result() 在 mark_completed 后立即返回。"""
        t = Task(task_id="h1", requester="p", description="x")

        async def _complete() -> None:
            await asyncio.sleep(0.01)
            t.mark_completed(final_result="done")

        asyncio.create_task(_complete())
        result: Task = await t.result()

        assert result.status == TaskStatus.COMPLETED
        assert result.final_result == "done"

    @pytest.mark.asyncio
    async def test_result_returns_on_failed(self) -> None:
        """result() 在 mark_failed 后立即返回。"""
        t = Task(task_id="h2", requester="p", description="x")

        async def _fail() -> None:
            await asyncio.sleep(0.01)
            t.mark_failed(error="boom")

        asyncio.create_task(_fail())
        result: Task = await t.result()

        assert result.status == TaskStatus.FAILED
        assert result.error == "boom"

    @pytest.mark.asyncio
    async def test_result_timeout_raises(self) -> None:
        """result() 超时时抛出 asyncio.TimeoutError。"""
        t = Task(task_id="h3", requester="p", description="x")

        with pytest.raises(asyncio.TimeoutError):
            await t.result(timeout=0.01)

    @pytest.mark.asyncio
    async def test_cancel_sets_status(self) -> None:
        """cancel() 设置 canceled 状态。"""
        t = Task(task_id="h4", requester="p", description="x")
        assert t.status == TaskStatus.SUBMITTED

        t.cancel()
        assert t.status == TaskStatus.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_notifies_result(self) -> None:
        """cancel() 后 result() 立即返回。"""
        t = Task(task_id="h5", requester="p", description="x")
        t.cancel()

        result: Task = await t.result()
        assert result.status == TaskStatus.CANCELED

    def test_completion_event_not_serialized(self) -> None:
        """_completion_event 不出现在 to_dict 中。"""
        t = Task(task_id="h6", requester="p", description="x")
        d: dict = t.to_dict()
        assert "_completion_event" not in d
        assert "completion_event" not in d

