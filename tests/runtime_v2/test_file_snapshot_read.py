"""Windows共享读取只做有界退避，不隐藏真正的磁盘或格式异常。"""

import json
from pathlib import Path

import pytest

from dotclaw.runtime.adapters import _file_support as storage
from dotclaw.runtime.adapters import RunRepositoryAdapter
from tests.runtime_v2.test_cancellation_persistence import _run


def _denied(winerror):
    error = PermissionError(13, "模拟共享占用")
    if winerror is not None:
        error.winerror = winerror
    return error


@pytest.mark.parametrize("winerror", [5, 32, 33, None])
def test_transient_windows_read_sharing_error_recovers(tmp_path, monkeypatch, winerror):
    path = tmp_path / "value.json"
    path.write_text('{"value":1}', encoding="utf-8")
    read = Path.read_text
    calls, waits = [], []
    def blocked(self, *args, **kwargs):
        calls.append(self)
        if len(calls) < 3:
            raise _denied(winerror)
        return read(self, *args, **kwargs)
    monkeypatch.setattr(storage, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage.time, "sleep", waits.append)
    monkeypatch.setattr(Path, "read_text", blocked)
    assert storage.load_json_map(path) == {"value": 1}
    assert len(calls) == 3 and waits == [0.005, 0.01]


def test_permanent_windows_read_permission_error_raises_original_after_five_attempts(tmp_path, monkeypatch):
    error = _denied(None)
    calls, waits = [], []
    def blocked(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr(storage, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage.time, "sleep", waits.append)
    monkeypatch.setattr(Path, "read_text", blocked)
    with pytest.raises(PermissionError) as captured:
        storage.load_json_map(tmp_path / "value.json")
    assert captured.value is error
    assert len(calls) == 5 and waits == [0.005, 0.01, 0.015, 0.02]


@pytest.mark.parametrize("windows,error", [(False, PermissionError(13, "POSIX权限拒绝")),
    (True, PermissionError(1, "其他权限错误")), (True, _denied(87)),
    (True, FileNotFoundError("不存在")), (True, OSError(5, "磁盘I/O错误"))])
def test_non_target_errors_are_not_retried(tmp_path, monkeypatch, windows, error):
    calls, waits = [], []
    def blocked(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr(storage, "_IS_WINDOWS", windows)
    monkeypatch.setattr(storage.time, "sleep", waits.append)
    monkeypatch.setattr(Path, "read_text", blocked)
    with pytest.raises(type(error)) as captured:
        storage.load_json_map(tmp_path / "value.json")
    assert captured.value is error and len(calls) == 1 and waits == []


def test_corrupt_json_is_never_retried(tmp_path, monkeypatch):
    calls, waits = [], []
    def corrupt(*args, **kwargs):
        calls.append(1)
        return "{bad"
    monkeypatch.setattr(storage, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage.time, "sleep", waits.append)
    monkeypatch.setattr(Path, "read_text", corrupt)
    with pytest.raises(json.JSONDecodeError):
        storage.load_json_map(tmp_path / "value.json")
    assert calls == [1] and waits == []


async def test_run_snapshot_does_not_multiply_read_retry_limit(tmp_path, monkeypatch):
    repository = RunRepositoryAdapter(tmp_path)
    await repository.create_run(_run())
    error = _denied(None)
    calls = []
    def blocked(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr(storage, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(Path, "read_text", blocked)
    with pytest.raises(PermissionError) as captured:
        await repository.load_run_snapshot("session", "run")
    assert captured.value is error and len(calls) == 5
