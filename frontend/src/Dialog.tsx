import { useEffect, useRef, type ReactNode } from "react";

/** 共用原生模态语义，限制焦点、处理 Esc 并在关闭后归还焦点。 */
export function Dialog({ title, className = "", onClose, children }: { title: string; className?: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const element = ref.current;
    element?.showModal();
    element?.querySelector<HTMLElement>("[data-initial-focus]")?.focus();
    return () => {
      element?.close();
      const target = opener.current?.isConnected ? opener.current : document.querySelector<HTMLElement>('[aria-label="打开会话列表"]');
      target?.focus();
    };
  }, []);
  return <dialog ref={ref} className={`modal ${className}`} aria-label={title}
    onKeyDown={(event) => {
      if (event.key !== "Tab") return;
      const elements = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex="0"]')).filter((element) => element.getClientRects().length > 0);
      const first = elements[0], last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }} onCancel={(event) => { event.preventDefault(); closeRef.current(); }}>
    <header className="modal-header"><h2>{title}</h2><button type="button" aria-label={`关闭${title}`} onClick={onClose}>×</button></header>
    {children}
  </dialog>;
}
