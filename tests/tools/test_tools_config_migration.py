"""配置旧工具名 → 新规范名迁移测试（Tool v1 阶段二）。

验证 disabled_tools / approval_commands 的旧名被迁移并产生弃用警告；
冲突（旧名与新名同时出现）以新名为准。所有新增注释使用中文。
"""

from __future__ import annotations

from dotclaw.config.settings import _migrate_tool_names, _raw_to_config


def test_migrate_single_old_name() -> None:
    assert _migrate_tool_names(["exec"]) == ["builtin.process.execute"]


def test_migrate_mixed_keeps_unmapped() -> None:
    # 'python' 不是 builtin，原样保留。
    assert _migrate_tool_names(["exec", "python"]) == [
        "builtin.process.execute",
        "python",
    ]


def test_migrate_all_eight() -> None:
    old = [
        "read_file",
        "write_file",
        "list_dir",
        "exec",
        "memory_read",
        "memory_write",
        "system_info",
        "get_time",
    ]
    new = _migrate_tool_names(old)
    assert new == [
        "builtin.files.read_text",
        "builtin.files.write_text",
        "builtin.files.list_directory",
        "builtin.process.execute",
        "builtin.memory.read",
        "builtin.memory.write",
        "builtin.system.get_info",
        "builtin.system.get_time",
    ]


def test_conflict_new_name_wins() -> None:
    # 旧名与新名同时出现 → 以新名为准，旧名丢弃。
    assert _migrate_tool_names(["exec", "builtin.process.execute"]) == [
        "builtin.process.execute"
    ]


def test_raw_to_config_migrates_tools_section() -> None:
    cfg = _raw_to_config(
        {
            "tools": {
                "approval_commands": ["exec"],
                "disabled_tools": ["exec", "python"],
            }
        }
    )
    assert cfg.tools.approval_commands == ["builtin.process.execute"]
    assert cfg.tools.disabled_tools == ["builtin.process.execute", "python"]
