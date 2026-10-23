import { isValidElement, useEffect, useRef, useState, type ComponentProps } from "react";
import Markdown, { type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import "katex/dist/katex.min.css";
import { Dialog } from "./Dialog";

/** 复制始终取原文，反馈不改变按钮尺寸。 */
export function CopyButton({ getText, label }: { getText: () => string; label: string }) {
  const [feedback, setFeedback] = useState("");
  useEffect(() => {
    if (!feedback) return;
    const timer = window.setTimeout(() => setFeedback(""), 2200);
    return () => window.clearTimeout(timer);
  }, [feedback]);
  async function copy(): Promise<void> {
    try { await navigator.clipboard.writeText(getText()); setFeedback("已复制"); }
    catch { setFeedback("复制失败，请手动选择"); }
  }
  return <button className="copy-button" type="button" onClick={() => void copy()} aria-label={label}>
    <span aria-hidden="true">{label}</span><span className="copy-feedback" role="status">{feedback}</span>
  </button>;
}

/** 代码超过阅读高度后可展开，复制目标始终是完整代码节点。 */
function CodeBlock({ node: _node, children, ...props }: ComponentProps<"pre"> & ExtraProps) {
  const codeRef = useRef<HTMLPreElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [long, setLong] = useState(false);
  const language = isValidElement<{ className?: string }>(children) ? children.props.className?.match(/language-([^\s]+)/)?.[1] : undefined;
  useEffect(() => {
    const element = codeRef.current;
    const measure = () => setLong((element?.scrollHeight || 0) > 360 || (element?.textContent?.split("\n").length || 0) > 14);
    measure();
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure); observer.observe(element);
    return () => observer.disconnect();
  }, [children]);
  return <div className={`code-block ${!expanded ? "code-block--collapsed" : ""}`}>
    <div className="code-toolbar"><span>{language || "代码"}</span><CopyButton label="复制代码" getText={() => codeRef.current?.textContent || ""} /></div>
    <pre {...props} ref={codeRef}>{children}</pre>
    {long && <button className="code-expand" type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "收起代码" : "展开完整代码"}</button>}
  </div>;
}
function ZoomImage({ src, alt, node: _node, ...props }: ComponentProps<"img"> & ExtraProps) {
  const [open, setOpen] = useState(false);
  if (!src) return null;
  return <>
    <button className="image-zoom" type="button" aria-label={`放大图片：${alt || "图片"}`} onClick={() => setOpen(true)}><img src={src} alt={alt || ""} {...props} loading="lazy" /></button>
    {open && <Dialog title="图片预览" className="image-dialog" onClose={() => setOpen(false)}><img src={src} alt={alt || ""} /></Dialog>}
  </>;
}
function ScrollTable({ node: _node, ...props }: ComponentProps<"table"> & ExtraProps) {
  return <div className="table-scroll"><table {...props} /></div>;
}

/** 原始 HTML 保持隔离，公式仅渲染数学内容，不允许受信命令。 */
export function MessageContent({ content }: { content: string }) {
  return <Markdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }], [rehypeKatex, { trust: false }]]} components={{
    pre: CodeBlock, img: ZoomImage,
    table: ScrollTable,
  }}>{content}</Markdown>;
}
