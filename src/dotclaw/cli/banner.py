"""CLI Banner —— Rich Panel 启动横幅"""

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.box import ROUNDED

DOT_ART = [
    "█████▌    ▐████▌  ████████",
    "█▌   █▌  ▐█    █▌    ██ ",
    "█▌   █▌  ▐█    █▌    ██ ",
    "█████▌    ▐████▌     ██ ",
]

CLAW_ART = [
    " ████▌    ██        ▐█████▌   █▌ █ ▐█",
    "█▌    ▌   ██        █▌   ▐█   █▌ █ ▐█",
    "█▌        ██        ███████   █▌ █ ▐█",
    " ████▀    ██████▌   █▌   ▐█    ▀███▀ ",
]

console = Console()


def build_banner(
    agent_name: str = "coding-assistant",
    model: str = "qwen3.7-max",
    session_title: str = "\u4e3b\u5bf9\u8bdd",
    workspace: str = ".",
) -> Panel:
    """构建 Rich Panel 启动横幅。"""

    dot_text = Text("\n".join(DOT_ART), style="bold cyan", justify="left")
    claw_text = Text("\n".join(CLAW_ART), style="bold yellow", justify="left")

    param_table = Table(box=None, show_header=False, padding=(0, 2))
    param_table.add_column(style="dim")
    param_table.add_column(style="white")
    param_table.add_column(style="dim")
    param_table.add_column(style="white")
    param_table.add_row("Agent", agent_name, "Model", model)
    param_table.add_row("Session", session_title, "Workspace", str(workspace))

    content = Group(
        dot_text,
        claw_text,
        Text(""),
        Text("Lightweight AI Agent", style="dim italic", justify="left"),
        Text(""),
        param_table,
        Text(""),
        Text("/help 查看命令", style="dim", justify="left"),
    )

    return Panel(content, box=ROUNDED, padding=(1, 3))
