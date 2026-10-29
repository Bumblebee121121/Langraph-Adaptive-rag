import { Check, CircleAlert, LoaderCircle } from "lucide-react";
import type { Message } from "../types";

const NODE_LABELS: Record<string, string> = {
  apply_guardrail: "安全检查",
  resolve_context: "读取会话记忆",
  classify_intent: "意图识别",
  route_complexity: "选择处理流程",
  retrieve_docs: "检索知识库",
  plan_tasks: "规划处理步骤",
  execute_tools: "处理业务操作",
  generate_answer: "生成回答",
  check_faithfulness: "核对回答依据",
  compact_memory: "整理对话上下文",
  refuse_answer: "生成服务范围说明",
};

const PREPARATION_LABELS: Record<string, string> = {
  rate_limit: "限流检查",
  apply_guardrail: "原始输入护栏",
  validate_session: "校验会话归属与确认凭证",
  restore_memory: "恢复会话记忆",
  resolve_context: "解析本轮上下文",
  initialize_state: "初始化本轮状态",
};

export function ProcessingProgress({ message }: { message: Message }) {
  const nodes = message.progressNodes ?? [];
  const preparation = message.preparationSteps ?? [];
  if (!message.progressNodes && !message.preparationSteps) return null;
  const processing = message.state === "streaming";
  const current = preparation.find((s) => s.status === "running");
  const status = processing ? "正在处理" : message.state === "error" ? "处理失败" : message.state === "stopped" ? "已停止" : "处理完成";
  return <details className="processing-progress" open={processing || undefined}>
    <summary>{status}{preparation.length > 0 && <span> · 准备 {preparation.filter((s) => s.status === "completed").length} 项已完成</span>}{nodes.length > 0 && <span> · {nodes.length} 个节点已完成</span>}</summary>
    {preparation.length > 0 && <>
      <div className="progress-section-title">图外准备</div>
      <ol aria-label="图外准备进度">{preparation.map((step) => {
        const running = step.status === "running" && processing;
        const failed = step.status === "failed" || (step.status === "running" && message.state === "error");
        const label = step.status === "completed" ? "已完成" : failed ? "失败" : running ? "进行中" : "已停止";
        return <li key={step.stage} title={step.stage} className={running ? "progress-running" : failed ? "progress-failed" : ""}>
          {step.status === "completed" ? <Check size={13} aria-hidden="true" /> : running ? <LoaderCircle size={13} aria-hidden="true" /> : <CircleAlert size={13} aria-hidden="true" />}
          <span>{PREPARATION_LABELS[step.stage] ?? "准备请求"}</span><small>{label}</small>
        </li>;
      })}</ol>
    </>}
    {nodes.length > 0 && <>
    <div className="progress-section-title">图内工作流</div>
    <ol aria-label="节点处理进度">
      {nodes.map((node) => <li key={node} title={node}><Check size={13} aria-hidden="true" /><span>{NODE_LABELS[node] ?? "处理步骤"}</span><small>已完成</small></li>)}
    </ol>
    </>}
    {processing && <div className="progress-waiting" role="status"><span className="thinking" aria-hidden="true"><span /><span /><span /></span>{current ? `正在${PREPARATION_LABELS[current.stage] ?? "准备请求"}…` : nodes.length ? "正在处理后续步骤…" : preparation.length ? "正在执行工作流…" : "请求已发送，等待处理…"}</div>}
  </details>;
}
