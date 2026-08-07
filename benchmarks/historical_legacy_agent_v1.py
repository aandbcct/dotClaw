"""旧 Agent v1（``AgentLoop``）单工具场景的具体外围适配（PR2 唯一历史入口）。

本模块只服务于经过审计的历史提交：生成一个不改动历史 worktree 的启动脚本，用
历史解释器在历史 ``src`` 上装配 ``AgentRuntime``（固定脚本 LLM + 记录型 ``search``
替身工具），执行与 PR1 ``tool_success`` 等价的业务语义，并把 AgentRun 的终态与
统计、替身工具日志映射为审计模块的 ``ScenarioSample``。

不复制或扩展 Runtime 业务规则：只处理旧入口的调用形状。每次采样使用独立
``state_dir``（Session / AgentRun 状态目录）与独立 ``evidence_dir``（替身日志），
避免跨样本泄漏。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from .eval_baseline_models import SCENARIO_TOOL_SUCCESS
from .historical_audit import AuditError, AuditGate, HistoricalScenarioAdapter, ScenarioSample

# 固定业务场景常量（与 PR1 tool_success Case 语义一致）。
_USER_MESSAGE: str = "今天天气怎么样？"
_SYSTEM_PROMPT: str = "你是一个乐于助人的助手。"
_TOOL_NAME: str = "search"
_TOOL_KEY_ARGUMENT: str = "q"
_TOOL_KEY_VALUE: str = "weather"
_FINAL_OUTPUT_MARKER: str = "sunny"
_MODEL: str = "fixed-model"
_MAX_LOOP_STEPS: int = 5

_ADAPTER_CONFIG_HASH: str = "historical-agent-v1-fixed-fixture"
"""历史替身配置的稳定标识：当前/历史对照时要求两侧配置均已固定记录。"""


def _build_launch_script() -> str:
    """生成历史 Agent v1 场景启动脚本源码。

    脚本由历史解释器执行，只通过命令行 JSON 参数接收 worktree / 状态目录 /
    证据目录，因此可复用同一脚本完成任意多次采样；脚本本身不改写历史 worktree。
    """
    return '''"""PR2 历史 Agent v1（AgentLoop）tool_success 场景启动脚本（自动生成，勿手改）。"""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

_ARGS = json.loads(sys.argv[1])
_WORKTREE = Path(_ARGS["worktree"])
_STATE_DIR = Path(_ARGS["state_dir"])
_EVIDENCE_DIR = Path(_ARGS["evidence_dir"])
_STATE_DIR.mkdir(parents=True, exist_ok=True)
_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

# 显式从历史 src 导入，拒绝当前 checkout
sys.path.insert(0, str(_WORKTREE / "src"))

from dotclaw.agent.loop import AgentLoop
from dotclaw.agent.runtime import AgentRuntime
from dotclaw.llm.base import ChatChunk, LLMClient, ToolCall
from dotclaw.llm.proxy import LLMProxy
from dotclaw.session.agent_run import AgentRunManager
from dotclaw.session.session import Session, SessionManager
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.handler import BuiltinToolHandler
from dotclaw.tools.registry import ToolRegistry

_FINAL_MARKER = "sunny"


class FixedLLMClient(LLMClient):
    """固定脚本 LLM：第 1 次请求返回 search 工具调用，第 2 次返回最终回答。"""

    def __init__(self) -> None:
        self._calls = 0

    async def chat(self, messages, tools=None, stream=True):
        self._calls += 1
        if self._calls == 1:
            yield ChatChunk(
                tool_call=ToolCall(id="call-1", name="search", arguments='{"q": "weather"}'),
                is_final=True,
                finish_reason="tool_calls",
            )
        else:
            yield ChatChunk(content="The weather is sunny today", is_final=True, finish_reason="stop")

    async def embed(self, texts, dimensions):
        return []


class FixedRouter:
    """固定路由器：只返回替身 provider 与脚本 LLM 客户端。

    持有单个 ``FixedLLMClient`` 实例，保证跨 LLM 调用的脚本计数连续：
    第 1 次调用返回工具调用，第 2 次调用返回最终回答。
    """

    def __init__(self):
        self._client = FixedLLMClient()

    def select(self, purpose, forced_model=None):
        return ["fixed-provider"]  # 历史 ModelRouter 返回候选模型名列表

    def get_provider_name(self, model_name=None):
        return "fixed-provider"

    def get_client(self, model_name):
        return self._client

    async def try_acquire(self, provider, timeout=0.1):
        return None  # 历史 ModelRouter.try_acquire 是协程，返回 None

    def report_success(self, *args, **kwargs):
        return None

    def report_failure(self, *args, **kwargs):
        return None

    def _get_retry_config(self, model_name=None):
        return 1  # 历史 ModelRouter 返回 int（max_attempts）

    def _get_backoff_config(self, model_name=None):
        return 0.0  # 历史 ModelRouter 返回 float（backoff_factor）


async def _search_handler(q: str) -> str:
    """记录型替身工具：把调用参数写入独立证据日志后返回固定输出。"""
    log = _EVIDENCE_DIR / "tool_log.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": "search", "arguments": {"q": q}}) + "\\n")
    return "sunny"


async def _run_scene():
    registry = ToolRegistry()
    registry.register(BuiltinToolHandler(
        name="search",
        description="查询天气",
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        handler_fn=_search_handler,
        needs_approval=False,
        timeout=60.0,
    ))
    executor = ToolExecutor(registry=registry)

    runtime = AgentRuntime(
        llm=LLMProxy(FixedRouter()),
        tool_executor=executor,
        assembler=None,
        session_mgr=SessionManager(_STATE_DIR / "sessions"),
        run_mgr=AgentRunManager(_STATE_DIR / "runs"),
        channel=None,
        config=None,
    )

    session = Session(id="hist-" + uuid.uuid4().hex[:8])
    started = time.perf_counter()
    run = await AgentLoop(runtime).run(
        session=session,
        user_message="今天天气怎么样？",
        system_prompt="你是一个乐于助人的助手。",
        tool_definitions=executor.get_definitions(),
        model="fixed-model",
        max_loop_steps=5,
    )
    wall_ms = (time.perf_counter() - started) * 1000.0

    return {
        "end_status": run.end_status,
        "tool_calls": run.tool_calls,
        "iterations": run.iterations,
        "final_output": run.final_output,
        "duration_ms": run.duration_ms,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "wall_duration_ms": wall_ms,
        "error": run.error,
        "evidence_refs": [str(_EVIDENCE_DIR / "tool_log.jsonl")],
    }


def main():
    # ensure_ascii 保持输出纯 ASCII，避免历史解释器 stdout 编码与解析端不一致
    print(json.dumps(asyncio.run(_run_scene()), ensure_ascii=True))


if __name__ == "__main__":
    main()
'''


class LegacyAgentV1Adapter:
    """旧 Agent v1（AgentLoop）入口的具体外围适配器。

    实现 ``HistoricalScenarioAdapter`` 协议：``verify_expected`` 校验场景匹配，
    ``run_scenario`` 在历史 worktree 环境子进程中执行启动脚本，并基于替身工具
    日志与最终回答做场景校验，产出 ``ScenarioSample``。
    """

    def __init__(
        self,
        *,
        dataset: str,
        case_id: str,
        scenario_id: str = SCENARIO_TOOL_SUCCESS,
        script_dir: Path,
        subprocess_timeout: float = 180.0,
    ) -> None:
        """绑定场景标识、脚本目录与子进程超时。"""
        if scenario_id != SCENARIO_TOOL_SUCCESS:
            raise AuditError(
                AuditGate.SCENARIO,
                "?",
                f"旧 Agent v1 适配器只支持场景 {SCENARIO_TOOL_SUCCESS!r}，实际 {scenario_id!r}",
            )
        self._dataset: str = dataset
        self._case_id: str = case_id
        self._scenario_id: str = scenario_id
        self._script_dir: Path = Path(script_dir)
        self._subprocess_timeout: float = subprocess_timeout

    @property
    def scenario_id(self) -> str:
        """返回适配器覆盖的业务场景标识。"""
        return self._scenario_id

    def verify_expected(self) -> None:
        """校验适配器与目标场景匹配；构造时已保证，此处幂等。"""
        if self._scenario_id != SCENARIO_TOOL_SUCCESS:
            raise AuditError(
                AuditGate.SCENARIO, self._scenario_id, "适配器场景与目标场景不一致"
            )

    async def run_scenario(
        self,
        *,
        worktree: Path,
        python: Path,
        state_dir: Path,
        evidence_dir: Path,
        attempt: int,
        is_warmup: bool,
    ) -> ScenarioSample:
        """在历史环境执行一次固定场景并返回校验后的语义事实。"""
        del attempt, is_warmup  # 场景语义与采样序号无关，但保持协议签名
        script_path: Path = self._ensure_script()
        args: dict[str, object] = {
            "worktree": str(worktree),
            "state_dir": str(state_dir),
            "evidence_dir": str(evidence_dir),
        }
        proc = subprocess.run(
            [str(python), str(script_path), json.dumps(args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._subprocess_timeout,
        )
        if proc.returncode != 0:
            raise AuditError(
                AuditGate.SCENARIO,
                "?",
                f"历史场景子进程失败（exit={proc.returncode}）：{proc.stderr.strip() or '无输出'}",
            )
        try:
            data: Mapping[str, object] = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            raise AuditError(
                AuditGate.SCENARIO, "?", f"历史场景输出无法解析：{error}"
            ) from error
        return self._validate(data, evidence_dir)

    def _ensure_script(self) -> Path:
        """确保启动脚本与模板一致并返回其路径（内容变化时重写）。"""
        self._script_dir.mkdir(parents=True, exist_ok=True)
        script_path: Path = self._script_dir / "legacy_agent_v1_scenario.py"
        content: str = _build_launch_script()
        if not script_path.exists() or script_path.read_text(encoding="utf-8") != content:
            script_path.write_text(content, encoding="utf-8")
        return script_path

    def _validate(self, data: Mapping[str, object], evidence_dir: Path) -> ScenarioSample:
        """把历史输出映射为 ScenarioSample 并执行场景校验。

        历史输出声称成功但工具名 / 参数 / 最终回答校验不匹配时，不得映射为通过。
        """
        end_status: str = str(data.get("end_status", ""))
        tool_calls: int = int(data.get("tool_calls", 0) or 0)
        iterations: int = int(data.get("iterations", 0) or 0)
        final_output: str | None = data.get("final_output")
        final_output = final_output if isinstance(final_output, str) else None
        wall_duration_ms: float = float(data.get("wall_duration_ms", 0.0) or 0.0)
        tokens_in: int | None = data.get("tokens_in")
        tokens_out: int | None = data.get("tokens_out")

        tool_name_ok, tool_arguments_ok = self._validate_tool_log(evidence_dir / "tool_log.jsonl")
        final_output_ok: bool = final_output is not None and _FINAL_OUTPUT_MARKER in final_output
        passed: bool = (
            end_status == "completed"
            and tool_name_ok
            and tool_arguments_ok
            and final_output_ok
        )

        evidence_refs: Sequence[str] = [str(evidence_dir / "tool_log.jsonl")]
        return ScenarioSample(
            end_status=end_status,
            passed=passed,
            failure_kind=None if passed else "assertion",
            wall_duration_ms=wall_duration_ms,
            loop_iterations=iterations,
            tool_call_count=tool_calls,
            tool_name_ok=tool_name_ok,
            tool_arguments_ok=tool_arguments_ok,
            final_output_ok=final_output_ok,
            final_output=final_output,
            evidence_refs=evidence_refs,
            tokens_in=tokens_in if isinstance(tokens_in, int) else None,
            tokens_out=tokens_out if isinstance(tokens_out, int) else None,
        )

    @staticmethod
    def _validate_tool_log(log_path: Path) -> tuple[bool, bool]:
        """从记录型替身日志校验工具名与关键参数。"""
        name_ok: bool = False
        arguments_ok: bool = False
        if not log_path.exists():
            return name_ok, arguments_ok
        for line in log_path.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("tool") == _TOOL_NAME:
                name_ok = True
            arguments = entry.get("arguments") or {}
            if isinstance(arguments, dict) and arguments.get(_TOOL_KEY_ARGUMENT) == _TOOL_KEY_VALUE:
                arguments_ok = True
        return name_ok, arguments_ok
