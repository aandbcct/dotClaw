import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getAssistantResults } from "./api";
import { useAssistantResults } from "./useAssistantResults";
import type { AssistantRunResults } from "./types";

vi.mock("./api", () => ({ getAssistantResults: vi.fn(), getAssistantProposal: vi.fn() }));

function result(id: string): AssistantRunResults {
  return { schema_version: 1, run_id: "run-1", result_sets: [{ id, session_id: "personal-assistant", run_id: "run-1", call_id: "call", items: [], created_at: "" }], operations: [] };
}

describe("useAssistantResults", () => {
  beforeEach(() => vi.mocked(getAssistantResults).mockReset());

  it("聚焦时刷新结果，并忽略同一运行的迟到响应", async () => {
    vi.mocked(getAssistantResults).mockResolvedValue(result("initial"));
    const { result: hook } = renderHook(() => useAssistantResults(["run-1"], {}));
    await waitFor(() => expect(hook.current.loading).toBe(false));

    let resolveOld!: (value: AssistantRunResults) => void;
    let resolveNew!: (value: AssistantRunResults) => void;
    vi.mocked(getAssistantResults)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve; }))
      .mockResolvedValueOnce(result("focus"));
    let oldRequest!: Promise<void>;
    let newRequest!: Promise<void>;
    act(() => { oldRequest = hook.current.refreshRun("run-1"); newRequest = hook.current.refreshRun("run-1"); });
    await act(async () => { resolveNew(result("newest")); await newRequest; });
    await act(async () => { resolveOld(result("stale")); await oldRequest; });
    expect(hook.current.results[0].result_sets[0].id).toBe("newest");

    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(hook.current.results[0].result_sets[0].id).toBe("focus"));
  });
});
