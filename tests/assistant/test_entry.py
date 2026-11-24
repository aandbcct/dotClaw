import asyncio

import pytest

from dotclaw.assistant.entry_service import AssistantEntryService, AssistantSessionMissingError
from dotclaw.assistant.schemas import RequestSaveInput
from dotclaw.session.session import SessionManager

from .test_service import make_context, make_service


class SessionInteraction:
    chat_models = ("test-model",)

    def __init__(self, session_manager):
        self.session_manager = session_manager
        self.creations = 0

    async def ensure_assistant_session(self):
        self.creations += 1
        return await self.session_manager.create(
            agent_id="personal-assistant",
            title="个人助理",
            model="test-model",
            session_id="personal-assistant",
        )

    def get_identity(self, session):
        assert session.agent_id == "personal-assistant"
        return object()


@pytest.mark.asyncio
async def test_fixed_session_is_created_once_and_existing_database_record_is_not_recreated(tmp_path):
    service, _, _ = await make_service(tmp_path / "assistant.sqlite3")
    sessions = SessionManager(tmp_path / "sessions")
    interaction = SessionInteraction(sessions)
    entry = AssistantEntryService(interaction, sessions, service, service._repository)

    first, second = await asyncio.gather(entry.ensure_session(), entry.ensure_session())
    assert first.id == second.id == "personal-assistant"
    assert interaction.creations == 1

    payload = {
        "intent_args": {"action": "create_task", "title": None},
        "missing_fields": ["title"],
        "candidate_task_ids": [],
        "selected_task_ids": [],
        "temporal_values": [],
        "last_error": None,
    }
    # 请求使用真实运行来源，随后删除固定会话文件，验证入口不会悄悄重建。
    draft = await service.save_request(
        RequestSaveInput(kind="create_task", summary="有未结事项", payload=payload, source_refs=[]),
        make_context(),
    )
    assert draft.outcome == "succeeded"
    session_file = sessions.session_directory("personal-assistant") / "session.json"
    session_file.unlink()

    with pytest.raises(AssistantSessionMissingError, match="session.json"):
        await entry.ensure_session()
    assert interaction.creations == 1
    assert await service._repository.get_request("personal-assistant", draft.data.request.id) is not None
