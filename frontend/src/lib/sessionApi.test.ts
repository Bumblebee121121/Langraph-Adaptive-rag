import { afterEach, describe, expect, it, vi } from "vitest";
import { cancelAction, deleteConversation, forgetConversation, getConversation, listConversations, streamChat } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("server conversation memory", () => {
  it("loads the owned session list and restores citations, progress and a pending action", async () => {
    const action = { action_id: "act", tool_name: "cancel_order", arguments: { order_id: "ORD10003" }, risk: "destructive", summary: "取消订单" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ sessions: [{ session_id: "sess", title: "取消订单", updated_at: "2026-10-29T08:00:00+00:00" }] }))
      .mockResolvedValueOnce(Response.json({ session_id: "sess", updated_at: "2026-10-29T08:00:00+00:00", messages: [
        { id: "1", role: "assistant", content: "请确认", citations: [], pending_action: action, progress_nodes: ["classify_intent", "plan_tasks"], preparation_steps: [{ stage: "restore_memory", status: "completed" }], trace_id: "t" },
      ] }));
    vi.stubGlobal("fetch", fetchMock);
    const sessions = await listConversations();
    expect(sessions[0]?.sessionId).toBe("sess");
    const conversation = await getConversation("sess");
    expect(conversation.messages[0]?.pendingAction?.action_id).toBe("act");
    expect(conversation.messages[0]?.progressNodes).toEqual(["classify_intent", "plan_tasks"]);
    expect(conversation.messages[0]?.preparationSteps).toEqual([{ stage: "restore_memory", status: "completed" }]);
    expect(fetchMock.mock.calls.every(([url]) => String(url).includes("user_id=u_demo"))).toBe(true);
  });

  it("delivers a failed phase before surfacing an SSE error", async () => {
    const frame = [
      { event: "stage", stage: "rate_limit", status: "failed" },
      { event: "error", message: "请求过于频繁，请稍后再试。", error_code: "RATE_LIMITED", trace_id: "t" },
    ].map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(frame)));
    const onEvent = vi.fn();
    await expect(streamChat({ message: "你好", user_id: "u_demo" }, onEvent, new AbortController().signal)).rejects.toThrow("请求过于频繁");
    expect(onEvent).toHaveBeenCalledExactlyOnceWith({ event: "stage", stage: "rate_limit", status: "failed" });
  });

  it("persists cancellation, forgetting and deletion and reports failures", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await cancelAction("sess", "act");
    await forgetConversation("sess");
    await deleteConversation("sess");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/sessions/sess/actions/act?user_id=u_demo", "/api/v1/sessions/sess/memory?user_id=u_demo", "/api/v1/sessions/sess?user_id=u_demo",
    ]);
    expect(fetchMock.mock.calls.every(([, options]) => options.method === "DELETE")).toBe(true);
    fetchMock.mockResolvedValue(new Response(null, { status: 500 }));
    await expect(deleteConversation("sess")).rejects.toThrow("同步失败");
  });
});
