import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getAssistantProposal, getAssistantResults } from "./api";
import type { AssistantProposalPreview, AssistantRunResults, RunActivity, ToolExecution } from "./types";

export interface AssistantPreviewState {
  status: "loading" | "loaded" | "error" | "mismatch";
  preview: AssistantProposalPreview | null;
  error: string;
}
interface PreviewRequest {
  runId: string;
  callId: string;
  requestId: string;
  version: number;
  hash: string;
}
interface RunResultState { result: AssistantRunResults | null; error: string; loading: boolean; }
const WRITING_OPERATIONS = new Set([
  "tasks.create", "tasks.update", "tasks.status", "reminders.create", "reminders.update", "reminders.status", "batch.apply",
]);

function proposalRequest(runId: string, tool: ToolExecution | undefined): PreviewRequest | null {
  if (!tool || tool.name !== "builtin.assistant.batch.apply") return null;
  const args = tool.arguments;
  const { request_id: requestId, expected_version: version, proposal_hash: hash } = args;
  if (typeof requestId !== "string" || !Number.isInteger(version) || typeof hash !== "string") return null;
  return { runId, callId: tool.call_id, requestId, version: version as number, hash };
}

/** 运行结果按run隔离请求序号；失败保留上次数据，并允许逐run重试。 */
export function useAssistantResults(
  runIds: string[],
  activities: Record<string, RunActivity>,
  onOperationsCommitted?: () => void,
) {
  const [runs, setRuns] = useState<Record<string, RunResultState>>({});
  const [previews, setPreviews] = useState<Record<string, AssistantPreviewState>>({});
  const previewsRef = useRef(previews);
  previewsRef.current = previews;
  const runSequence = useRef(new Map<string, number>());
  const previewSequence = useRef(new Map<string, number>());
  const committed = useRef(new Set<string>());
  const callback = useRef(onOperationsCommitted);
  callback.current = onOperationsCommitted;

  const pendingPreviews = useMemo(() => Object.values(activities).flatMap((activity) => {
    if (activity.status !== "waiting_approval" || !activity.approval_call_id) return [];
    const request = proposalRequest(activity.run_id, activity.tools.find((tool) => tool.call_id === activity.approval_call_id));
    return request ? [request] : [];
  }), [activities]);
  const runKey = runIds.join("|");
  const activityKey = Object.values(activities).map((activity) =>
    `${activity.run_id}:${activity.status}:${activity.event_sequence || 0}:${activity.control_revision || 0}:${activity.approval_call_id || ""}:${activity.tools.map((tool) => `${tool.call_id}:${tool.status}`).join(",")}`,
  ).join("|");
  const previewKey = pendingPreviews.map((item) => `${item.runId}:${item.callId}:${item.requestId}:${item.version}:${item.hash}`).join("|");

  const refreshRun = useCallback(async (runId: string) => {
    const sequence = (runSequence.current.get(runId) || 0) + 1;
    runSequence.current.set(runId, sequence);
    setRuns((current) => ({ ...current, [runId]: { result: current[runId]?.result || null, error: "", loading: true } }));
    try {
      const result = await getAssistantResults(runId);
      if (runSequence.current.get(runId) !== sequence) return;
      setRuns((current) => ({ ...current, [runId]: { result, error: "", loading: false } }));
      for (const operation of result.operations) {
        const hasCommittedItems = operation.result.outcome === "succeeded"
          || (operation.result.operation === "batch.apply" && operation.result.items.some((item) => item.outcome === "succeeded"));
        const key = `${runId}:${operation.operation_key}`;
        if (hasCommittedItems && WRITING_OPERATIONS.has(operation.result.operation) && !committed.current.has(key)) {
          committed.current.add(key);
          callback.current?.();
        }
      }
    } catch (error) {
      if (runSequence.current.get(runId) !== sequence) return;
      setRuns((current) => ({ ...current, [runId]: { result: current[runId]?.result || null, error: error instanceof Error ? error.message : "读取助理结果失败", loading: false } }));
    }
  }, []);

  const refreshPreview = useCallback(async (request: PreviewRequest) => {
    const key = `${request.runId}:${request.callId}`;
    const sequence = (previewSequence.current.get(key) || 0) + 1;
    previewSequence.current.set(key, sequence);
    setPreviews((current) => ({ ...current, [key]: { status: "loading", preview: current[key]?.preview || null, error: "" } }));
    try {
      const preview = await getAssistantProposal(request.requestId, request.version, request.hash);
      if (previewSequence.current.get(key) !== sequence) return;
      const matches = preview.request_id === request.requestId && preview.version === request.version && preview.hash === request.hash;
      setPreviews((current) => ({ ...current, [key]: {
        status: matches ? "loaded" : "mismatch",
        preview: matches ? preview : null,
        error: matches ? "" : "提案版本或哈希已变化，请重新准备并确认。",
      } }));
    } catch (error) {
      if (previewSequence.current.get(key) !== sequence) return;
      const mismatch = error instanceof Error && "status" in error && (error as { status?: number }).status === 409;
      setPreviews((current) => ({ ...current, [key]: {
        status: mismatch ? "mismatch" : "error",
        preview: null,
        error: mismatch ? "提案版本或哈希已变化，请重新准备并确认。" : error instanceof Error ? error.message : "读取批准预览失败",
      } }));
    }
  }, []);

  const refreshAll = useCallback(() => {
    for (const runId of runIds) void refreshRun(runId);
    for (const request of pendingPreviews) void refreshPreview(request);
  }, [pendingPreviews, refreshPreview, refreshRun, runKey]);

  useEffect(() => {
    if (runIds.length) for (const runId of runIds) void refreshRun(runId);
  }, [runKey, activityKey, refreshRun]);
  useEffect(() => {
    for (const request of pendingPreviews) {
      const key = `${request.runId}:${request.callId}`;
      const current = previewsRef.current[key];
      const matches = current?.preview?.request_id === request.requestId
        && current.preview.version === request.version && current.preview.hash === request.hash;
      if (current?.status === "loaded" && matches) continue;
      void refreshPreview(request);
    }
  }, [previewKey, refreshPreview]);
  useEffect(() => {
    const refreshOnFocus = () => {
      if (document.visibilityState === "visible") refreshAll();
    };
    window.addEventListener("focus", refreshOnFocus);
    window.addEventListener("online", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnFocus);
    return () => {
      window.removeEventListener("focus", refreshOnFocus);
      window.removeEventListener("online", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnFocus);
    };
  }, [refreshAll]);

  return {
    results: runIds.map((runId) => runs[runId]?.result).filter((result): result is AssistantRunResults => Boolean(result)),
    errors: runIds.flatMap((runId) => runs[runId]?.error ? [{ runId, message: runs[runId].error }] : []),
    loading: runIds.some((runId) => runs[runId]?.loading),
    refreshRun,
    previewFor: (runId: string, callId: string) => {
      const current = previews[`${runId}:${callId}`] || null;
      const request = pendingPreviews.find((item) => item.runId === runId && item.callId === callId);
      if (!current || !request || current.status !== "loaded") return current;
      const matches = current.preview?.request_id === request.requestId
        && current.preview.version === request.version && current.preview.hash === request.hash;
      return matches ? current : { status: "mismatch" as const, preview: null, error: "提案版本或哈希已变化，请重新准备并确认。" };
    },
    retryPreview: (runId: string) => {
      const request = pendingPreviews.find((item) => item.runId === runId);
      if (request) void refreshPreview(request);
    },
  };
}
