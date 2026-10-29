import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type { ChatResponse, Conversation, Message, StreamEvent } from "../types";
import { streamChat, DEMO_USER_ID, listConversations, getConversation, deleteConversation, forgetConversation, cancelAction } from "../lib/api";
import { loadConversations, saveConversations } from "../lib/storage";

interface State {
  conversations: Conversation[];
  activeId: string | null;
}

type Action =
  | { type: "select"; id: string | null }
  | { type: "sync"; conversations: Conversation[]; startedAt?: number }
  | { type: "hydrate"; id: string; conversation: Conversation; previousMessages: Message[] }
  | { type: "forget"; id: string }
  | { type: "delete"; id: string }
  | { type: "start"; conversation: Conversation; user: Message; assistant: Message }
  | { type: "stream"; conversationId: string; messageId: string; event: StreamEvent }
  | { type: "token"; conversationId: string; messageId: string; text: string }
  | { type: "complete"; conversationId: string; messageId: string; response: ChatResponse }
  | { type: "fail"; conversationId: string; messageId: string; message: string; stopped: boolean }
  | { type: "dismiss"; conversationId: string; messageId: string };

function updateConversation(
  state: State,
  id: string,
  update: (conversation: Conversation) => Conversation,
): State {
  return {
    ...state,
    conversations: state.conversations.map((item) => item.id === id ? update(item) : item),
  };
}

export function chatReducer(state: State, action: Action): State {
  switch (action.type) {
    case "sync": {
      const serverIds = new Set(action.conversations.map((c) => c.sessionId));
      const local = state.conversations.filter((c) => !c.sessionId || serverIds.has(c.sessionId) || c.updatedAt >= (action.startedAt ?? Infinity) || c.messages.some((m) => m.state === "streaming"));
      const known = new Set(local.map((c) => c.sessionId));
      const conversations = [...local, ...action.conversations.filter((c) => !known.has(c.sessionId))].sort((a, b) => b.updatedAt - a.updatedAt);
      return { ...state, conversations, activeId: conversations.some((c) => c.id === state.activeId) ? state.activeId : null };
    }
    case "hydrate":
      return updateConversation(state, action.id, (item) => item.messages !== action.previousMessages || item.messages.some((m) => m.state === "streaming")
        ? item : { ...action.conversation, id: item.id, title: item.title });
    case "forget":
      return updateConversation(state, action.id, (item) => ({ ...item, messages: item.messages.map((m) => ({ ...m, pendingAction: null })) }));
    case "select":
      return { ...state, activeId: action.id };
    case "delete":
      return {
        conversations: state.conversations.filter((item) => item.id !== action.id),
        activeId: state.activeId === action.id ? null : state.activeId,
      };
    case "start": {
      const existing = state.conversations.find((item) => item.id === action.conversation.id);
      const next = existing
        ? { ...existing, updatedAt: Date.now(), messages: [...existing.messages, action.user, action.assistant] }
        : { ...action.conversation, messages: [action.user, action.assistant] };
      return {
        activeId: next.id,
        conversations: [next, ...state.conversations.filter((item) => item.id !== next.id)],
      };
    }
    case "stream": {
      const { event, conversationId, messageId } = action;
      if (event.event === "token") {
        return chatReducer(state, { type: "token", conversationId, messageId, text: event.text });
      }
      if (event.event === "answer") {
        return chatReducer(state, { type: "complete", conversationId, messageId, response: event.data });
      }
      if (event.event === "stage") {
        return updateConversation(state, conversationId, (item) => ({
          ...item,
          messages: item.messages.map((message) => {
            if (message.id !== messageId) return message;
            const steps = message.preparationSteps ?? [];
            const step = { stage: event.stage, status: event.status };
            return { ...message, preparationSteps: steps.some((s) => s.stage === event.stage)
              ? steps.map((s) => s.stage === event.stage ? step : s) : [...steps, step] };
          }),
        }));
      }
      if (event.event === "started" || event.event === "node") {
        return updateConversation(state, conversationId, (item) => ({
          ...item,
          messages: item.messages.map((message) => {
            if (message.id !== messageId) return message;
            if (event.event === "started") return { ...message, traceId: event.trace_id };
            const nodes = message.progressNodes ?? [];
            return { ...message, progressNodes: nodes.includes(event.node) ? nodes : [...nodes, event.node] };
          }),
        }));
      }
      return state;
    }
    case "token":
      return updateConversation(state, action.conversationId, (item) => ({
        ...item,
        messages: item.messages.map((message) => message.id === action.messageId
          ? { ...message, content: message.content + action.text }
          : message),
      }));
    case "complete":
      return updateConversation(state, action.conversationId, (item) => ({
        ...item,
        sessionId: action.response.session_id,
        updatedAt: Date.now(),
        messages: item.messages.map((message) => message.id === action.messageId
          ? {
              ...message,
              content: action.response.answer,
              state: "complete",
              citations: action.response.citations,
              pendingAction: action.response.pending_action,
              strategy: action.response.strategy,
              traceId: action.response.trace_id,
              latencyMs: action.response.latency.total_ms,
              progressNodes: action.response.progress_nodes?.length ? action.response.progress_nodes : message.progressNodes,
              preparationSteps: action.response.preparation_steps ?? message.preparationSteps,
            }
          : { ...message, pendingAction: null }),
      }));
    case "fail":
      return updateConversation(state, action.conversationId, (item) => ({
        ...item,
        messages: item.messages.map((message) => message.id === action.messageId
          ? {
              ...message,
              content: action.stopped ? message.content || "已停止生成。" : action.message,
              state: action.stopped ? "stopped" : "error",
            }
          : message),
      }));
    case "dismiss":
      return updateConversation(state, action.conversationId, (item) => ({
        ...item,
        messages: item.messages.map((message) => message.id === action.messageId
          ? { ...message, pendingAction: null }
          : message),
      }));
  }
}

export function useChat() {
  const [state, dispatch] = useReducer(chatReducer, undefined, () => ({
    conversations: loadConversations(),
    activeId: null,
  }));
  const stateRef = useRef(state);
  stateRef.current = state;
  const requestRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [syncMessage, setSyncMessage] = useState("");
  const removedSessions = useRef(new Set<string>());

  useEffect(() => {
    const controller = new AbortController();
    const startedAt = Date.now();
    void listConversations(controller.signal).then((conversations) => {
      dispatch({ type: "sync", conversations: conversations.filter((c) => !c.sessionId || !removedSessions.current.has(c.sessionId)), startedAt });
    }).catch(() => { if (!controller.signal.aborted) setSyncMessage("历史会话暂未同步，当前显示浏览器缓存。"); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => saveConversations(state.conversations), 300);
    return () => window.clearTimeout(timer);
  }, [state.conversations]);

  useEffect(() => {
    const flush = () => saveConversations(stateRef.current.conversations);
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);

  useEffect(() => () => requestRef.current?.abort(), []);

  const stop = useCallback(() => requestRef.current?.abort(), []);

  const send = useCallback(async (raw: string, confirmActionId?: string) => {
    const text = raw.trim();
    if (!text || text.length > 4000 || busyRef.current) return false;
    const current = stateRef.current;
    const existing = current.conversations.find((item) => item.id === current.activeId);
    const conversation: Conversation = existing ?? {
      id: crypto.randomUUID(),
      title: text.length > 32 ? `${text.slice(0, 32)}…` : text,
      updatedAt: Date.now(),
      messages: [],
    };
    const user: Message = { id: crypto.randomUUID(), role: "user", content: text, state: "complete" };
    const assistant: Message = { id: crypto.randomUUID(), role: "assistant", content: "", state: "streaming", progressNodes: [] };
    const controller = new AbortController();
    requestRef.current = controller;
    busyRef.current = true;
    setBusy(true);
    dispatch({ type: "start", conversation, user, assistant });

    let answered = false;
    try {
      await streamChat(
        {
          message: text,
          user_id: DEMO_USER_ID,
          ...(conversation.sessionId ? { session_id: conversation.sessionId } : {}),
          ...(confirmActionId ? { confirm_action_id: confirmActionId } : {}),
        },
        (event) => {
          if (event.event === "answer") answered = true;
          dispatch({ type: "stream", conversationId: conversation.id, messageId: assistant.id, event });
        },
        controller.signal,
      );
      if (!answered) throw new Error("连接已结束，但未收到最终回答。请重试。");
      return true;
    } catch (error) {
      const stopped = controller.signal.aborted;
      if (!answered) {
        dispatch({
          type: "fail",
          conversationId: conversation.id,
          messageId: assistant.id,
          stopped,
          message: error instanceof Error ? error.message : "请求失败，请稍后重试。",
        });
      }
      return false;
    } finally {
      requestRef.current = null;
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  const select = useCallback((id: string | null) => {
    stop();
    dispatch({ type: "select", id });
    setSyncMessage("");
    const conversation = stateRef.current.conversations.find((c) => c.id === id);
    if (conversation?.sessionId) {
      void getConversation(conversation.sessionId).then((server) => dispatch({ type: "hydrate", id: conversation.id, conversation: server, previousMessages: conversation.messages }))
        .catch(() => setSyncMessage("会话暂未同步，当前显示浏览器缓存。"));
    }
  }, [stop]);

  const remove = useCallback(async (id: string) => {
    if (stateRef.current.activeId === id) stop();
    const conversation = stateRef.current.conversations.find((c) => c.id === id);
    try {
      if (conversation?.sessionId) await deleteConversation(conversation.sessionId);
      if (conversation?.sessionId) removedSessions.current.add(conversation.sessionId);
      dispatch({ type: "delete", id });
      setSyncMessage("");
    } catch { setSyncMessage("删除失败，会话仍保留，请稍后重试。"); }
  }, [stop]);

  const dismissAction = useCallback((conversationId: string, messageId: string) => {
    dispatch({ type: "dismiss", conversationId, messageId });
  }, []);

  const cancelPending = useCallback(async (messageId: string) => {
    const conversation = stateRef.current.conversations.find((c) => c.id === stateRef.current.activeId);
    const proposal = conversation?.messages.find((m) => m.id === messageId)?.pendingAction;
    if (!conversation?.sessionId || !proposal) return;
    try {
      await cancelAction(conversation.sessionId, proposal.action_id);
      dispatch({ type: "dismiss", conversationId: conversation.id, messageId });
    } catch { setSyncMessage("取消操作失败，请稍后重试。"); }
  }, []);

  const forgetMemory = useCallback(async () => {
    const conversation = stateRef.current.conversations.find((c) => c.id === stateRef.current.activeId);
    if (!conversation?.sessionId || busyRef.current) return;
    try {
      await forgetConversation(conversation.sessionId);
      dispatch({ type: "forget", id: conversation.id });
      setSyncMessage("已清空本会话记忆。历史消息保留，后续对话从此刻重新记忆。");
    } catch { setSyncMessage("清空记忆失败，请稍后重试。"); }
  }, []);

  const active = state.conversations.find((item) => item.id === state.activeId) ?? null;
  return {
    conversations: state.conversations,
    active,
    busy,
    send,
    stop,
    select,
    remove,
    dismissAction,
    cancelPending,
    forgetMemory,
    syncMessage,
  };
}
