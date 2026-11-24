import { useEffect, useRef, useState } from "react";
import { ensureAssistantSession } from "./api";
import { ChatWorkspace } from "./ChatWorkspace";
import { useDraft } from "./useDraft";
import { useAssistantResults } from "./useAssistantResults";
import { useSessionWorkspace } from "./useSessionWorkspace";
import "./assistant-workspace.css";

const ASSISTANT_SESSION_ID = "personal-assistant";
const ASSISTANT_PREFERENCE_KEY = "dotclaw.last-assistant-session.v1";
type Workspace = ReturnType<typeof useSessionWorkspace>;

/** 助理固定入口长驻；切换工作区时不卸载其运行观察和审批状态。 */
export function AssistantWorkspace({
  workspace,
  active: enabled,
  theme,
  onToggleTheme,
  onOpenNavigation,
  onFocusTask,
  onOperationsCommitted,
}: {
  workspace: Workspace;
  active: boolean;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  onOpenNavigation: () => void;
  onFocusTask: (taskId: string) => void;
  onOperationsCommitted: () => void;
}) {
  const [entry, setEntry] = useState<{ status: "loading" | "ready" | "error"; error: string }>({ status: "loading", error: "" });
  const [retry, setRetry] = useState(0);
  const workspaceRef = useRef(workspace);
  workspaceRef.current = workspace;
  const { draft, setDraft, storageFailed } = useDraft(ASSISTANT_SESSION_ID);
  const selectedView = workspace.selectedId ? workspace.views[workspace.selectedId] : undefined;
  const runIds = Array.from(new Set([
    ...workspace.view.messages.flatMap((message) => message.run_ids || []),
    ...(workspace.view.runId ? [workspace.view.runId] : []),
  ]));
  const activities = selectedView?.activitiesByRunId || {};
  const results = useAssistantResults(runIds, activities, onOperationsCommitted);

  useEffect(() => {
    if (!enabled) return;
    let mounted = true;
    setEntry({ status: "loading", error: "" });
    void (async () => {
      try {
        const detail = await ensureAssistantSession();
        if (!mounted) return;
        workspaceRef.current.upsert(detail);
        await workspaceRef.current.open(detail);
        if (mounted) setEntry({ status: "ready", error: "" });
      } catch (error) {
        if (mounted) setEntry({ status: "error", error: error instanceof Error ? error.message : "助理入口暂时无法打开" });
      }
    })();
    return () => { mounted = false; };
  }, [enabled, retry]);

  return <ChatWorkspace
    workspace={workspace}
    assistantMode
    active={enabled}
    ready={entry.status === "ready" && workspace.selectedId === ASSISTANT_SESSION_ID}
    statusMessage={entry.status === "loading" ? "正在准备固定助理会话…" : entry.status === "error" ? entry.error : ""}
    onRetryEntry={() => setRetry((value) => value + 1)}
    showRetryEntry={entry.status === "error"}
    draft={draft}
    setDraft={setDraft}
    storageFailed={storageFailed}
    theme={theme}
    onToggleTheme={onToggleTheme}
    onOpenNavigation={onOpenNavigation}
    onFocusTask={onFocusTask}
    results={results.results}
    resultErrors={results.errors}
    resultsLoading={results.loading}
    onRetryResults={(runId) => void results.refreshRun(runId)}
    assistantPreviewFor={results.previewFor}
    onRetryAssistantPreview={results.retryPreview}
  />;
}

export { ASSISTANT_PREFERENCE_KEY };
