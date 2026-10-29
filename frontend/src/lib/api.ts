import type { Capabilities, ChatRequest, Conversation, Message, PreparationStep, StreamEvent } from "../types";
import { SseDecoder } from "./sse";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
export const DEMO_USER_ID = import.meta.env.VITE_DEMO_USER_ID || "u_demo";

async function sessionRequest(path: string, method = "GET", signal?: AbortSignal): Promise<Response> {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(`${API_BASE}/api/v1/sessions${path}${separator}user_id=${encodeURIComponent(DEMO_USER_ID)}`, { method, signal });
  if (!response.ok) throw new Error("会话同步失败，请稍后重试。");
  return response;
}

export async function listConversations(signal?: AbortSignal): Promise<Conversation[]> {
  const data = await (await sessionRequest("", "GET", signal)).json() as { sessions: { session_id: string; title: string; updated_at: string }[] };
  return data.sessions.map((session) => ({ id: session.session_id, sessionId: session.session_id, title: session.title, updatedAt: Date.parse(session.updated_at), messages: [] }));
}

export async function getConversation(sessionId: string, signal?: AbortSignal): Promise<Conversation> {
  const data = await (await sessionRequest(`/${encodeURIComponent(sessionId)}`, "GET", signal)).json() as {
    session_id: string; updated_at: string; messages: { id: string; role: Message["role"]; content: string; citations: Message["citations"]; pending_action: Message["pendingAction"]; trace_id: string; progress_nodes: string[]; preparation_steps?: PreparationStep[] }[];
  };
  return {
    id: data.session_id, sessionId: data.session_id, updatedAt: Date.parse(data.updated_at),
    title: data.messages.find((m) => m.role === "user")?.content.slice(0, 60) ?? "对话",
    messages: data.messages.map((m) => ({ id: m.id, role: m.role, content: m.content, state: "complete", citations: m.citations, pendingAction: m.pending_action, traceId: m.trace_id, progressNodes: m.progress_nodes?.length ? m.progress_nodes : undefined, preparationSteps: m.preparation_steps?.length ? m.preparation_steps : undefined })),
  };
}

export async function deleteConversation(sessionId: string): Promise<void> {
  await sessionRequest(`/${encodeURIComponent(sessionId)}`, "DELETE");
}

export async function forgetConversation(sessionId: string): Promise<void> {
  await sessionRequest(`/${encodeURIComponent(sessionId)}/memory`, "DELETE");
}

export async function cancelAction(sessionId: string, actionId: string): Promise<void> {
  await sessionRequest(`/${encodeURIComponent(sessionId)}/actions/${encodeURIComponent(actionId)}`, "DELETE");
}

export async function checkHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/api/v1/health`, { signal });
    return response.ok;
  } catch {
    return false;
  }
}

export async function getCapabilities(signal?: AbortSignal): Promise<Capabilities | null> {
  try {
    const response = await fetch(`${API_BASE}/api/v1/capabilities`, { signal });
    return response.ok ? await response.json() as Capabilities : null;
  } catch {
    return null;
  }
}

export async function streamChat(
  payload: ChatRequest,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/v1/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    let message = response.status >= 500
      ? "服务暂时不可用，请稍后重试。"
      : `请求失败（HTTP ${response.status}）`;
    try {
      const error: unknown = await response.json();
      if (typeof error === "object" && error !== null && "message" in error) {
        const detail = String(error.message);
        message = detail === "internal error" ? "服务暂时不可用，请稍后重试。" : detail;
      }
  } catch { /* Use the HTTP status when no JSON error body is available. */ }
    throw new Error(message);
  }
  if (!response.body) throw new Error("浏览器不支持流式响应");

  const reader = response.body.getReader();
  const decoder = new SseDecoder();
  function deliver(event: StreamEvent) {
    if (event.event === "error") throw new Error(event.message);
    onEvent(event);
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const event of decoder.feed(value)) deliver(event);
    }
    for (const event of decoder.finish()) deliver(event);
  } finally {
    reader.releaseLock();
  }
}
