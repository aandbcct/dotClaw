"""Windows 原子替换共享占用只允许同一临时文件的有界重试。"""

import json
from pathlib import Path

import pytest

from dotclaw.runtime.adapters import _file_support as support


def _permission(code: int) -> PermissionError:
    error = PermissionError("模拟原子替换被占用")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_windows_sharing_error_reuses_complete_temporary_file(tmp_path, monkeypatch, code):
    target = tmp_path / "run.json"
    target.write_text('{"old": true}', encoding="utf-8")
    original_replace = support.os.replace
    attempts, sleeps = [], []
    def occupied_then_available(source, destination):
        source = Path(source)
        attempts.append(source)
        assert json.loads(source.read_text(encoding="utf-8")) == {"new": True}
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
        if len(attempts) < 3:
            raise _permission(code)
        original_replace(source, destination)
    monkeypatch.setattr(support, "_IS_WINDOWS", True)
    monkeypatch.setattr(support.os, "replace", occupied_then_available)
    monkeypatch.setattr(support.time, "sleep", sleeps.append)
    support.write_json_atomic(target, {"new": True})
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert sleeps == [0.005, 0.01]
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert not attempts[0].exists()


def test_persistent_windows_sharing_error_raises_original_after_five_attempts(tmp_path, monkeypatch):
    target = tmp_path / "run.json"
    target.write_text('{"old": true}', encoding="utf-8")
    error = _permission(5)
    attempts, sleeps = [], []
    def occupied(source, destination):
        attempts.append(Path(source))
        assert Path(source).is_file()
        raise error
    monkeypatch.setattr(support, "_IS_WINDOWS", True)
    monkeypatch.setattr(support.os, "replace", occupied)
    monkeypatch.setattr(support.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as captured:
        support.write_json_atomic(target, {"new": True})
    assert captured.value is error
    assert len(attempts) == 5 and len(set(attempts)) == 1 and len(sleeps) == 4
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert not attempts[0].exists()


@pytest.mark.parametrize("case", ["other_permission", "other_error", "non_windows"])
def test_non_target_errors_are_never_retried(tmp_path, monkeypatch, case):
    target = tmp_path / "run.json"
    error = _permission(13 if case == "other_permission" else 5) if case != "other_error" else OSError("磁盘故障")
    attempts, sleeps = [], []
    def fail(source, destination):
        attempts.append(source)
        raise error
    monkeypatch.setattr(support, "_IS_WINDOWS", case != "non_windows")
    monkeypatch.setattr(support.os, "replace", fail)
    monkeypatch.setattr(support.time, "sleep", sleeps.append)
    with pytest.raises(OSError) as captured:
        support.write_json_atomic(target, {"new": True})
    assert captured.value is error
    assert len(attempts) == 1 and sleeps == []
    assert not target.exists()


def test_atomic_text_replace_keeps_previous_complete_file_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "events.jsonl"
    target.write_text('{"sequence":1}\n', encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("模拟原子替换前故障")

    monkeypatch.setattr(support.os, "replace", fail_replace)
    with pytest.raises(OSError, match="替换前故障"):
        support.write_text_atomic(target, '{"sequence":1}\n{"sequence":2}\n')
    assert target.read_text(encoding="utf-8") == '{"sequence":1}\n'
    assert not tuple(tmp_path.glob("*.tmp"))
