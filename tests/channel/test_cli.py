"""CLIChannel 交互式模型选择测试。"""

from __future__ import annotations

import types

import pytest

import dotclaw.channel.cli as cli_module
from dotclaw.channel.cli import CLIChannel


@pytest.mark.asyncio
async def test_select_model_lists_models_and_returns_valid_choice(monkeypatch) -> None:
    """模型选择应展示当前项与可选项，并返回合法输入。"""
    printed: list[str] = []
    prompts: list[str] = []
    channel = CLIChannel()

    async def _ask_user(prompt: str) -> str:
        prompts.append(prompt)
        return "m2"

    monkeypatch.setattr(channel, "ask_user", _ask_user)
    monkeypatch.setattr(
        cli_module,
        "console",
        types.SimpleNamespace(print=lambda message, **kwargs: printed.append(str(message))),
    )

    selected = await channel.select_model("m1", ("m1", "m2"))

    assert selected == "m2"
    assert printed == ["当前模型: m1", "可用模型:", "  - m1 [当前]", "  - m2"]
    assert prompts == ["请输入要切换的模型名称: "]


@pytest.mark.asyncio
async def test_select_model_rejects_unknown_and_exits(monkeypatch) -> None:
    """非法输入应提示不存在并退出本次选择。"""
    printed: list[str] = []
    channel = CLIChannel()

    async def _ask_user(prompt: str) -> str:
        return "missing"

    monkeypatch.setattr(channel, "ask_user", _ask_user)
    monkeypatch.setattr(
        cli_module,
        "console",
        types.SimpleNamespace(print=lambda message, **kwargs: printed.append(str(message))),
    )

    selected = await channel.select_model("m1", ("m1", "m2"))

    assert selected is None
    assert printed[-1] == "[red]模型不存在或未启用: missing[/red]"
