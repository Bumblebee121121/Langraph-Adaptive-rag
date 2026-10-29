import { describe, expect, it } from "vitest";
import { chatReducer } from "./useChat";
import { SseDecoder } from "../lib/sse";
import type { ChatResponse, Conversation, StreamEvent } from "../types";

describe("streamed chat progress", () => {
  it("keeps completed nodes visible before the answer and preserves the confirmation action", () => {
    let state: Parameters<typeof chatReducer>[0] = {
      activeId: "chat",
      conversations: [{ id: "chat", title: "取消订单", updatedAt: 0, messages: [
        { id: "answer", role: "assistant", content: "", state: "streaming", progressNodes: [] },
      ] }],
    };
    const decoder = new SseDecoder();
    const receive = (event: StreamEvent) => {
      const frame = new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`);
      for (const part of [frame.slice(0, 12), frame.slice(12)]) {
        for (const decoded of decoder.feed(part)) {
          state = chatReducer(state, { type: "stream", conversationId: "chat", messageId: "answer", event: decoded });
        }
      }
    };
    receive({ event: "started", trace_id: "trace" });
    receive({ event: "stage", stage: "restore_memory", status: "running" });
    expect(state.conversations[0]!.messages[0]!.preparationSteps).toEqual([{ stage: "restore_memory", status: "running" }]);
    expect(state.conversations[0]!.messages[0]!.progressNodes).toEqual([]);
    receive({ event: "stage", stage: "restore_memory", status: "completed" });
    receive({ event: "node", node: "classify_intent" });
    receive({ event: "node", node: "plan_tasks" });
    receive({ event: "node", node: "plan_tasks" });
    const processing = state.conversations[0]!.messages[0]!;
    expect(processing.state).toBe("streaming");
    expect(processing.content).toBe("");
    expect(processing.traceId).toBe("trace");
    expect(processing.progressNodes).toEqual(["classify_intent", "plan_tasks"]);

    const response: ChatResponse = {
      answer: "请确认取消订单 ORD10003",
      session_id: "session", trace_id: "trace", intent: "cancel_order", complexity: "tool_required",
      strategy: "agent", citations: [], refused: false, error_code: null, latency: { total_ms: 100 },
      pending_action: { action_id: "act", tool_name: "cancel_order", arguments: { order_id: "ORD10003" }, risk: "destructive", summary: "确认后取消订单" },
    };
    receive({ event: "token", text: "请确认" });
    receive({ event: "answer", data: response });
    receive({ event: "done", trace_id: "trace" });
    const completed = state.conversations[0]!.messages[0]!;
    expect(completed.state).toBe("complete");
    expect(completed.content).toBe(response.answer);
    expect(completed.pendingAction?.action_id).toBe("act");
    expect(completed.progressNodes).toEqual(processing.progressNodes);
    expect(completed.preparationSteps).toEqual([{ stage: "restore_memory", status: "completed" }]);
    expect(state.conversations[0]!.sessionId).toBe("session");
  });
});

describe("server history hydration", () => {
  it("keeps a chat completed while the initial session list was still loading", () => {
    const current: Conversation = { id: "new", sessionId: "new-session", title: "刚完成的对话", updatedAt: 20, messages: [] };
    const state = chatReducer({ activeId: "new", conversations: [current] }, { type: "sync", conversations: [], startedAt: 10 });
    expect(state.activeId).toBe("new");
    expect(state.conversations[0]?.sessionId).toBe("new-session");
  });
  it("merges by session id without duplicating locally cached chats", () => {
    const cached: Conversation = { id: "local", sessionId: "sess", title: "缓存", updatedAt: 0, messages: [] };
    const state = { activeId: "local", conversations: [cached] };
    const result = chatReducer(state, { type: "sync", conversations: [
      { ...cached, id: "sess" }, { id: "other", sessionId: "other", title: "服务端对话", updatedAt: 1, messages: [] },
    ] });
    expect(result.conversations).toHaveLength(2);
    expect(result.activeId).toBe("local");
  });

  it("does not overwrite messages sent after a history fetch began", () => {
    const previousMessages: Conversation["messages"] = [];
    const current: Conversation = { id: "local", sessionId: "sess", title: "新消息", updatedAt: 0, messages: [
      { id: "new", role: "user", state: "complete", content: "刚发出的消息" },
    ] };
    const state = { activeId: "local", conversations: [current] };
    const result = chatReducer(state, { type: "hydrate", id: "local", previousMessages, conversation: { ...current, messages: [] } });
    expect(result.conversations[0]?.messages[0]?.content).toBe("刚发出的消息");
  });
});
