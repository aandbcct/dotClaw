import { isValidElement, useEffect, useRef, useState, type ComponentProps } from "react";
import Markdown, { type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

/** 复制原文并明确反馈成功或失败，避免把失败显示成已复制。 */
export function CopyButton({ getText, label }: { getText: () => string; label: string }) {
  const [feedback, setFeedback] = useState("");
  useEffect(() => {
    if (!feedback) return;
    const timer = window.setTimeout(() => setFeedback(""), 2200);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(getText());
      setFeedback("已复制");
    } catch {
      setFeedback("复制失败，请手动选择");
    }
  }

  return (
    <button className="copy-button" type="button" onClick={() => void copy()} aria-label={label}>
      <span aria-live="polite">{feedback || label}</span>
    </button>
  );
}

/** 从代码节点复制文本，保留缩进且不混入工具栏内容。 */
function CodeBlock({ node: _node, children, ...props }: ComponentProps<"pre"> & ExtraProps) {
  const codeRef = useRef<HTMLPreElement>(null);
  const language = isValidElement<{ className?: string }>(children)
    ? children.props.className?.match(/language-([^\s]+)/)?.[1]
    : undefined;
  return (
    <div className="code-block">
      <div className="code-toolbar">
        <span>{language || "代码"}</span>
        <CopyButton label="复制代码" getText={() => codeRef.current?.textContent || ""} />
      </div>
      <pre {...props} ref={codeRef}>{children}</pre>
    </div>
  );
}

/** Markdown 保持默认 HTML 隔离，仅增加表格、任务列表和代码高亮。 */
export function MessageContent({ content }: { content: string }) {
  return (
    <Markdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
      components={{
        pre: CodeBlock,
        table: ({ node: _node, ...props }) => <div className="table-scroll"><table {...props} /></div>,
      }}
    >{content}</Markdown>
  );
}
