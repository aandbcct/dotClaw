import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from dotclaw.assistant.context import AssistantContextReader
from dotclaw.assistant.models import AssistantInputRef

from .test_service import make_service


@pytest.mark.asyncio
async def test_time_context_maps_two_weeks_from_local_input_date(tmp_path):
    service, _, _ = await make_service(tmp_path / "assistant.sqlite3")
    reader = AssistantContextReader(service._repository, ZoneInfo("Asia/Shanghai"), False)
    input_ref = AssistantInputRef(
        run_id="weekday-run",
        message_id="weekday-message",
        created_at=datetime.fromisoformat("2026-12-31T18:00:00+00:00"),
    )

    context = json.loads(await reader.read("personal-assistant", input_ref))
    time_context = context["time"]
    dates = time_context["date_weekday_reference"]

    assert time_context["business_timezone"] == "Asia/Shanghai"
    assert time_context["input_created_at"] == "2026-12-31T18:00:00.000000Z"
    assert time_context["input_local"] == "2027-01-01T02:00:00+08:00"
    assert len(dates) == 14
    assert dates[0] == {"date": "2027-01-01", "weekday": "周五", "days_from_input": 0}
    assert dates[3] == {"date": "2027-01-04", "weekday": "周一", "days_from_input": 3}
    assert dates[-1] == {"date": "2027-01-14", "weekday": "周四", "days_from_input": 13}
