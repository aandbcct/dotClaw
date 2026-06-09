"""MemoryFlushManager — L2 日记忆写入（LLM 结构化决策：增量/修改/跳过）"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message as LLMMessage

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy

logger = logging.getLogger("dotclaw.memory.flush")

FLUSH_SYSTEM_PROMPT = """你是对话摘要助手。你会收到：
1. 当前的日记忆文件内容（可能为空）
2. 本轮对话记录

你的任务是生成一个 JSON 来决定如何更新日记忆。

### 输出格式

严格输出以下 JSON，不要包含任何其他文本：

{"action": "<action>", "text": "<summary>", "target_anchor": "<HH:MM 或 null>"}

### action 取值

- "append": 这是一个新话题，与已有记忆无关。text 是 2-3 句中文摘要。
- "modify": 这是已有话题的延续。target_anchor 指向要修改的那个段落的 ## HH:MM 时间戳，text 是合并后的完整摘要（覆盖旧内容）。
- "skip": 本轮对话没有需要记录的信息（如纯闲聊、问候、确认性回复）。

### 摘要要求

1. 提取用户的主要话题、提出的问题、做出的决策
2. 提取 AI 提供的关键信息、建议、结论
3. 忽略闲聊、问候等无信息量的内容
4. 2-3 句中文，纯文本

### 示例

日记忆：
## 14:22
用户讨论了数据库表设计，AI 建议了范式化方案。

本轮对话：
用户: 那索引应该怎么加？

输出：
{"action": "modify", "text": "用户讨论了数据库表设计与索引优化，AI 建议了范式化方案和聚簇索引策略。", "target_anchor": "14:22"}

---

日记忆：
（空）

本轮对话：
用户: 帮我写一个 Python 异步下载脚本

输出：
{"action": "append", "text": "用户请求编写 Python 异步下载脚本，涉及 asyncio 和 aiohttp 的使用。", "target_anchor": null}

---

日记忆：
## 09:00
用户讨论了低卡零食推荐。

本轮对话：
用户: 好的
AI: 有需要再找我

输出：
{"action": "skip", "text": "", "target_anchor": null}"""


class MemoryFlushManager:
    """将对话摘要写入日记忆文件，支持增量/修改/跳过"""

    def __init__(self, workspace_dir: Path, llm: "LLMProxy | None" = None):
        self._workspace = workspace_dir
        self._memory_dir = workspace_dir / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._llm = llm

    async def flush_from_messages(
        self,
        messages: list,
        reason: str = "round_end",
    ) -> bool:
        """
        每轮对话结束后调用。

        1. 读取当前日记忆文件全文
        2. 构建对话文本（本轮全量消息）
        3. 调用 LLM 返回结构化 JSON（action / text / target_anchor）
        4. 根据 action 执行 append / modify / skip
        5. 异常时降级处理
        """
        if not messages:
            return False

        today = datetime.now().strftime("%Y-%m-%d")
        path = self._memory_dir / f"{today}.md"

        # 读取当前日记忆
        existing_memory = ""
        if path.exists():
            existing_memory = path.read_text(encoding="utf-8")

        # 构建本轮对话文本
        dialog_text = self._build_dialog_text(messages)

        # 调用 LLM 获取结构化决策
        try:
            decision = await self._decide_with_llm(existing_memory, dialog_text)
        except Exception as e:
            logger.warning(f"LLM 决策失败，降级为追加摘要: {e}")
            decision = self._fallback_decision(messages)

        action = decision.get("action", "append")
        text = decision.get("text", "").strip()
        target_anchor = decision.get("target_anchor")

        logger.debug(f"Flush 决策: action={action}, anchor={target_anchor}")

        if action == "skip":
            logger.info("日记忆跳过（无信息量）")
            return False

        if not text:
            logger.warning("LLM 返回空摘要，跳过写入")
            return False

        if action == "modify" and target_anchor:
            return self._modify_section(path, existing_memory, target_anchor, text)
        else:
            # append（含 modify 降级）
            return self._append_section(path, text)

    async def _decide_with_llm(self, existing_memory: str, dialog_text: str) -> dict:
        """调用 LLM 获取结构化决策"""
        if not self._llm:
            raise RuntimeError("LLM 未配置")

        user_content = f"""当前日记忆：
{existing_memory if existing_memory else "（空）"}

本轮对话：
{dialog_text}"""

        llm_messages = [
            LLMMessage(role="system", content=FLUSH_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content),
        ]

        result = ""
        async for chunk in self._llm.chat(messages=llm_messages, stream=False):
            result += chunk.content

        return self._parse_decision(result.strip())

    def _parse_decision(self, raw: str) -> dict:
        """解析 LLM 返回的 JSON，兼容 markdown code block 包裹"""
        # 尝试提取 ```json ... ``` 中的内容
        code_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', raw)
        if code_match:
            raw = code_match.group(1).strip()

        # 尝试提取第一个 { ... } JSON 对象
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            raw = json_match.group(0)

        try:
            decision = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}, raw={raw[:200]}")
            raise

        # 校验必填字段
        action = decision.get("action", "append")
        if action not in ("append", "modify", "skip"):
            logger.warning(f"未知 action: {action}，降级为 append")
            decision["action"] = "append"

        return decision

    def _build_dialog_text(self, messages: list) -> str:
        """将消息列表转换为可读的对话文本"""
        lines = []
        for m in messages:
            role = getattr(m, "role", "unknown")
            content = getattr(m, "content", "")
            label = {"user": "用户", "assistant": "AI", "tool": "工具", "system": "系统"}.get(role, role)
            lines.append(f"{label}: {content}")
        return "\n".join(lines)

    def _modify_section(
        self, path: Path, existing: str, anchor: str, new_text: str
    ) -> bool:
        """替换日记忆文件中的指定 ## HH:MM 段落"""
        # 找目标段落：从 ## {anchor} 到下一个 ## 或 EOF
        pattern = re.compile(
            rf'^## {re.escape(anchor)}\n.*?(?=\n## |\Z)',
            re.MULTILINE | re.DOTALL,
        )
        replacement = f"## {anchor}\n{new_text}\n"

        if not pattern.search(existing):
            logger.warning(f"锚点 {anchor} 不存在，降级为追加")
            return self._append_section(path, new_text)

        new_content = pattern.sub(replacement, existing)
        path.write_text(new_content, encoding="utf-8")
        logger.info(f"日记忆已修改: {path} (anchor={anchor})")
        return True

    def _append_section(self, path: Path, text: str) -> bool:
        """在日记忆文件末尾追加新段落"""
        timestamp = datetime.now().strftime("%H:%M")
        entry = f"\n## {timestamp}\n{text}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.info(f"日记忆已追加: {path} (anchor={timestamp})")
        return True

    def _fallback_decision(self, messages: list) -> dict:
        """LLM 不可用时的降级决策（始终追加）"""
        summary = self._generate_fallback_summary(messages)
        return {"action": "append", "text": summary, "target_anchor": None}

    def _generate_fallback_summary(self, messages: list) -> str:
        """LLM 不可用时的降级摘要"""
        if not messages:
            return "（空对话）"

        user_parts = []
        assistant_parts = []
        for m in messages:
            content = getattr(m, "content", "")[:100]
            role = getattr(m, "role", "unknown")
            if role == "user":
                user_parts.append(content)
            elif role == "assistant":
                assistant_parts.append(content)

        user_text = " ".join(user_parts)
        assistant_text = " ".join(assistant_parts)

        return f"- 用户讨论了: {user_text[:200]}\n- 助手回复了: {assistant_text[:200]}"
