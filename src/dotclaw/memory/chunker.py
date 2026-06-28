"""TextChunker — Markdown 结构感知文本分块"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextChunk:
    text: str
    start_line: int
    end_line: int
    title: str = ""  # 所属 ## 标题


class TextChunker:
    """按行 + token 估算分块，Markdown 结构感知（不切断 ## 标题边界）"""

    def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50):
        self._max_tokens: int = max_tokens
        self._overlap_tokens: int = overlap_tokens

    def chunk_text(self, text: str) -> list[TextChunk]:
        """分块主逻辑，提取 ## 标题作为 title"""
        lines: list[str] = text.split("\n")
        if not lines:
            return []

        chunks: list[TextChunk] = []
        current_tokens: int = 0
        current_start: int = 0
        current_lines: list[str] = []
        current_title: str = ""

        for i, line in enumerate(lines):
            line_tokens: int = self._estimate_tokens(line)

            # 记录当前 ## 标题
            stripped: str = line.strip()
            if stripped.startswith("## ") and not stripped.startswith("###"):
                # 提取标题文本（去掉 ## 前缀和前后空格）
                new_title: str = stripped[3:].strip()
                # 遇到新标题 + 当前 chunk 不为空 → 切分
                if current_lines:
                    chunks.append(TextChunk(
                        text="\n".join(current_lines),
                        start_line=current_start,
                        end_line=i - 1,
                        title=current_title,
                    ))
                    # 重叠：保留最后 overlap_tokens 的内容
                    overlap_lines: list[str] = self._get_overlap(current_lines, self._overlap_tokens)
                    current_lines = overlap_lines
                    current_start = max(current_start, i - len(overlap_lines))
                    current_tokens = sum(self._estimate_tokens(l) for l in current_lines)

                current_title = new_title

            current_lines.append(line)
            current_tokens += line_tokens

            if current_tokens >= self._max_tokens:
                chunks.append(TextChunk(
                    text="\n".join(current_lines[:-1]),
                    start_line=current_start,
                    end_line=i - 1,
                    title=current_title,
                ))
                current_lines = [current_lines[-1]]
                current_start = i
                current_tokens = self._estimate_tokens(current_lines[-1])

        # 最后一块
        if current_lines:
            chunks.append(TextChunk(
                text="\n".join(current_lines),
                start_line=current_start,
                end_line=len(lines) - 1,
                title=current_title,
            ))

        return chunks if chunks else [
            TextChunk(text=text, start_line=0, end_line=len(lines) - 1)
        ]

    def _estimate_tokens(self, text: str) -> int:
        """中英文差异化估算（P3 公式，P4 由 tiktoken 替代）"""
        chinese_chars: int = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars: int = len(text) - chinese_chars
        return max(1, chinese_chars + (other_chars // 4))

    def _get_overlap(self, lines: list[str], max_tokens: int) -> list[str]:
        """从末尾取不超过 max_tokens 的行"""
        result: list[str] = []
        tokens: int = 0
        for line in reversed(lines):
            t: int = self._estimate_tokens(line)
            if tokens + t > max_tokens:
                break
            result.insert(0, line)
            tokens += t
        return result
