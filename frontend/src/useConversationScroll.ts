import { useLayoutEffect, useRef, useState } from "react";
import type { DisplayMessage } from "./types";

/** 只在用户仍停留底部时跟随流式输出，回看历史时保留阅读位置。 */
export function useConversationScroll(sessionId: string | undefined, messages: DisplayMessage[]) {
  const conversationRef = useRef<HTMLElement>(null);
  const following = useRef(true);
  const previousSession = useRef(sessionId);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const [hasNewContent, setHasNewContent] = useState(false);

  function scrollToBottom(): void {
    following.current = true;
    const element = conversationRef.current;
    if (element) element.scrollTop = element.scrollHeight;
    setAwayFromBottom(false);
    setHasNewContent(false);
  }

  function onScroll(): void {
    const element = conversationRef.current;
    if (!element) return;
    const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
    following.current = nearBottom;
    setAwayFromBottom(!nearBottom);
    if (nearBottom) setHasNewContent(false);
  }

  useLayoutEffect(() => {
    if (previousSession.current !== sessionId) {
      previousSession.current = sessionId;
      following.current = true;
    }
    if (following.current) scrollToBottom();
    else setHasNewContent(true);
  }, [sessionId, messages]);

  return { conversationRef, onScroll, scrollToBottom, awayFromBottom, hasNewContent };
}
