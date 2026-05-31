"""TextChunker — Markdown 结构感知文本分块"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextChunk:
    text: str
    start_line: int
    end_line: int


class TextChunker:
    """按行 + token 估算分块，Markdown 结构感知（不切断 ## 标题边界）"""

    def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50):
        self._max_tokens = max_tokens
        self._overlap_tokens = overlap_tokens

    def chunk_text(self, text: str) -> list[TextChunk]:
        """分块主逻辑"""
        lines = text.split("\n")
        if not lines:
            return []

        chunks: list[TextChunk] = []
        current_tokens = 0
        current_start = 0
        current_lines: list[str] = []

        for i, line in enumerate(lines):
            line_tokens = self._estimate_tokens(line)

            # 遇到 ## 标题 + 当前 chunk 不为空 → 切分
            if line.strip().startswith("##") and current_lines:
                chunks.append(TextChunk(
                    text="\n".join(current_lines),
                    start_line=current_start,
                    end_line=i - 1,
                ))
                # 重叠：保留最后 overlap_tokens 的内容
                overlap_lines = self._get_overlap(current_lines, self._overlap_tokens)
                current_lines = overlap_lines
                current_start = max(current_start, i - len(overlap_lines))
                current_tokens = sum(self._estimate_tokens(l) for l in current_lines)

            current_lines.append(line)
            current_tokens += line_tokens

            if current_tokens >= self._max_tokens:
                chunks.append(TextChunk(
                    text="\n".join(current_lines[:-1]),
                    start_line=current_start,
                    end_line=i - 1,
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
            ))

        return chunks if chunks else [
            TextChunk(text=text, start_line=0, end_line=len(lines) - 1)
        ]

    def _estimate_tokens(self, text: str) -> int:
        """中英文差异化估算（P3 公式，P4 由 tiktoken 替代）"""
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        return max(1, chinese_chars + (other_chars // 4))

    def _get_overlap(self, lines: list[str], max_tokens: int) -> list[str]:
        """从末尾取不超过 max_tokens 的行"""
        result = []
        tokens = 0
        for line in reversed(lines):
            t = self._estimate_tokens(line)
            if tokens + t > max_tokens:
                break
            result.insert(0, line)
            tokens += t
        return result
