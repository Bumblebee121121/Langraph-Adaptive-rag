import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MessageView } from "./MessageView";
import type { Conversation, Message } from "../types";

describe("answer presentation", () => {
  it("shows the answer and source without internal timing or route metadata", () => {
    const conversation: Conversation = {
      id: "chat-1",
      title: "退货政策",
      updatedAt: 0,
      messages: [{
        id: "answer-1",
        role: "assistant",
        state: "complete",
        content: "根据退货规则，质量问题可以申请售后。",
        strategy: "multi",
        latencyMs: 4200,
        citations: [{ chunk_id: "chunk-1", document_id: "policy-1", title: "退货规则", section: null }],
      }],
    };
    const html = renderToStaticMarkup(
      <MessageView conversation={conversation} busy={false} onConfirm={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain("质量问题可以申请售后");
    expect(html).toContain("退货规则");
    expect(html).not.toContain("多跳检索");
    expect(html).not.toContain("4.2 秒");
  });
});

function markup(message: Partial<Message>, busy = false) {
  const conversation: Conversation = { id: "chat", title: "订单操作", updatedAt: 0, messages: [
    { id: "answer", role: "assistant", content: "", state: "streaming", ...message },
  ] };
  return renderToStaticMarkup(<MessageView conversation={conversation} busy={busy} onConfirm={() => {}} onCancel={() => {}} />);
}

describe("progress and confirmation", () => {
  it("shows a running preparation phase before any graph node and counts nodes separately", () => {
    const html = markup({ progressNodes: [], preparationSteps: [
      { stage: "rate_limit", status: "completed" }, { stage: "restore_memory", status: "running" },
    ] }, true);
    expect(html).toContain("图外准备");
    expect(html).toContain("限流检查");
    expect(html).toContain("恢复会话记忆");
    expect(html).toContain("进行中");
    expect(html).toContain("准备 1 项已完成");
    expect(html).not.toContain("个节点已完成");
    const both = markup({ progressNodes: ["classify_intent"], preparationSteps: [{ stage: "restore_memory", status: "completed" }] }, true);
    expect(both).toContain("图内工作流");
    expect(both).toContain("1 个节点已完成");
    expect(both).not.toContain("2 个节点已完成");
  });

  it.each(["error", "stopped"] as const)("ends an unfinished preparation phase on %s", (state) => {
    const html = markup({ state, content: "请求已结束", preparationSteps: [{ stage: "restore_memory", status: "running" }] });
    expect(html).toContain(state === "error" ? "失败" : "已停止");
    expect(html).not.toContain("进行中");
    expect(html).not.toContain("正在恢复会话记忆");
    expect(html).not.toContain("准备 1 项已完成");
  });
  it("shows real completed nodes while waiting for an answer", () => {
    const html = markup({ progressNodes: ["apply_guardrail", "classify_intent"] }, true);
    expect(html).toContain("正在处理");
    expect(html).toContain("2 个节点已完成");
    expect(html).toContain("安全检查");
    expect(html).toContain("意图识别");
    expect(html).toContain("open=");
    expect(html).not.toContain("确认执行");
  });

  it("renders confirmation only for a backend proposal and keeps progress collapsed after completion", () => {
    const html = markup({
      state: "complete", content: "确认后取消订单 ORD10003", progressNodes: ["plan_tasks"],
      pendingAction: { action_id: "act", tool_name: "cancel_order", arguments: { order_id: "ORD10003" }, risk: "destructive", summary: "取消订单 ORD10003" },
    });
    expect(html).toContain("处理完成");
    expect(html).toContain("规划处理步骤");
    expect(html).toContain("确认执行");
    expect(html).toContain("取消</button>");
    expect(html).not.toContain("open=");
    expect(html).not.toContain("正在处理后续步骤");
    expect(markup({ state: "complete", content: "请提供订单号", pendingAction: null })).not.toContain("确认执行");
  });

  it.each(["error", "stopped"] as const)("ends progress on %s", (state) => {
    const html = markup({ state, content: "连接已结束", progressNodes: ["classify_intent"] });
    expect(html).toContain(state === "error" ? "处理失败" : "已停止");
    expect(html).not.toContain("正在处理后续步骤");
  });
});
