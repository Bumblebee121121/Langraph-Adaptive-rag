# 06 · API 参考

> 一句话：业务拒答可以 HTTP 200；系统故障走 4xx/5xx + `error_code`。流式接口使用 SSE，`token` 只包含完成校验后的可展示答案。

## 本章目录

1. [约定](#约定)
2. [探活](#探活)
3. [对话](#对话)
4. [流式](#流式)
5. [会话历史与记忆](#会话历史与记忆)
6. [管理面](#管理面)
7. [指标](#指标)
8. [错误体](#错误体)

前缀默认 `/api/v1`。OpenAPI：`http://localhost:8000/docs`。  
对应：`app/api/routes/`、`app/schemas/chat.py`、`app/main.py`。

## 约定

每个响应带 `x-trace-id`、`x-request-id`（可从请求头传入）。空 `message` → **422**。

### 代码摘要

```python
# app/schemas/chat.py
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="anonymous", min_length=1, max_length=64)
    session_id: str | None = None
    confirm_action_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
```

`stream` 字段在 `ChatRequest` 上存在，但 **`POST /chat` 忽略它**；流式必须打 `/chat/stream`。

## 探活

### `GET /api/v1/health`

进程存活，不查库。`{ "status": "ok" }`。给 Kubernetes **liveness**。

### `GET /api/v1/ready`

硬依赖：**数据库**。Redis / Qdrant / LLM key 写入 `dependencies[]`，失败**不**把 ready 打成 false（避免 DeepSeek 抖动踢光探针）。给 **readiness**。

实现：`app/api/routes/health.py`。`DeepSeekProvider.ping()` 只检查 key 是否配置，不发 `chat/completions`。

## 对话

### `POST /api/v1/chat`

**Request** 见上表。**Response**（`ChatResponse`）：

| 字段 | 含义 |
|---|---|
| `answer` | 对用户可见文本 |
| `session_id` / `trace_id` | 会话与链路 |
| `intent` / `complexity` / `strategy` | 路由解释性字段 |
| `citations` | `document_id` / `title` / `chunk_id` |
| `pending_action` | HITL 待确认 |
| `preparation_steps` | 图外准备阶段及其最终状态，不计入图节点 |
| `progress_nodes` | SSE 请求中实际完成的图内节点 |
| `refused` | 护栏拒答 |
| `error_code` | 业务侧拒绝码（仍可能 HTTP 200） |
| `token_usage` / `latency` | 分阶段毫秒 |

```bash
curl -s localhost:8000/api/v1/chat \
  -H 'content-type: application/json' \
  -H 'x-trace-id: tr_debug_001' \
  -d '{"message":"电子发票多久能开","user_id":"u_demo"}'
```

路由：`app/api/routes/chat.py` → `container.chat_service.chat(...)`。

## 流式

### `POST /api/v1/chat/stream`

SSE。事件 JSON：

| `event` | 何时 |
|---|---|
| `started` | 开始处理请求，不表示已通过限流 |
| `stage` | 图外准备阶段开始、完成或失败，包含 `stage` 和 `status`（`running` / `completed` / `failed`） |
| `node` | LangGraph 节点完成（如 `classify_intent`） |
| `token` | 最终答案经忠实度检查后分段发送；不包含模型的 JSON 包装或内部字段 |
| `answer` | 最终 `ChatResponse` |
| `done` | 结束 |
| `error` | 流建立后的系统/请求错误，包含 `message`、`error_code`、`trace_id`，随后关闭流 |

实现：`sse-starlette` + `ChatService.stream`。详见 [05 · Token 流式](05-agents-and-tools.md#token-流式)。

图外阶段依次为 `rate_limit`、`apply_guardrail`、`validate_session`（包括确认凭证校验）、`restore_memory`、`resolve_context`、`initialize_state`。阶段事件在对应操作开始和结束时发送，慢速记忆恢复期间也会立即显示 `running`，不会等工作流结束后补发；LangGraph 仍为 9 个业务节点。最终响应与历史消息的 `preparation_steps` 保存阶段状态，不需新增数据库列。

SSE 连接建立后 HTTP 状态通常已是 200，后续限流或异常通过 `error` 事件报告，客户端应显示其错误信息并结束等待；JSON `/chat` 仍使用原来的 HTTP 错误码。原始输入护栏拦截会返回 `refused=true` 的最终回答，仅包含实际执行过的准备阶段。中止流时会取消后台任务并关闭本轮数据库事务。

## 会话历史与记忆

以下接口均要求 `user_id`，服务端校验会话所属用户。当前仍使用演示身份，公开部署应由认证层提供可信身份。

| 方法 | 路径（前缀 `/api/v1`） | 用途 |
|---|---|---|
| GET | `/sessions?user_id=u_demo&limit=50` | 最近会话列表，最多 100 条 |
| GET | `/sessions/{id}?user_id=u_demo&limit=100` | 最近消息、引用及仍有效的确认卡片；用返回的 `next_before_id` 作为 `before_id` 读取更早消息 |
| DELETE | `/sessions/{id}/memory?user_id=u_demo` | 清空摘要、订单/诉求上下文并作废待确认操作；保留可见历史，旧消息不再用于记忆 |
| DELETE | `/sessions/{id}/actions/{action_id}?user_id=u_demo` | 取消指定待确认操作 |
| DELETE | `/sessions/{id}?user_id=u_demo` | 删除服务端会话、消息、摘要和提案；保留业务订单和已执行结果 |

删除/清空成功返回 204；会话不存在或用户不匹配返回 400。JSON 与 SSE 对话复用同一记忆与确认存储。详见[会话记忆](21-conversation-memory.md)。

## 管理面

`GET /api/v1/admin/tools`、`/admin/skills`。  
`APP_ENV=production` 必须 `x-admin-token` 且配置 `ADMIN_API_TOKEN`。开发未配 token 则开放（勿对公网暴露）。比对：`hmac.compare_digest`。

## 指标

`GET /metrics`：Prometheus 文本。请求量、分段延迟、路由、工具状态（含 `fallback`）、token、幻觉计数。

## 错误体

系统异常走 HTTP 4xx/5xx：

```json
{ "error_code": "LLM_TIMEOUT", "message": "DeepSeek request timed out", "trace_id": "tr_..." }
```

### 代码摘要

```python
# app/main.py
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    body = APIErrorBody(
        error_code=exc.error_code,
        message=exc.message,
        trace_id=getattr(request.state, "trace_id", "-"),
    )
    return JSONResponse(status_code=exc.http_status, content=body.model_dump(mode="json"))
```

| HTTP | 典型 `error_code` |
|---|---|
| 400 | `INVALID_REQUEST`、`PROMPT_INJECTION_BLOCKED`、`QUERY_OUT_OF_SCOPE` |
| 401 | `UNAUTHORIZED` |
| 409 | `IDEMPOTENCY_CONFLICT` |
| 422 | 校验失败 |
| 429 | `RATE_LIMITED` |
| 502/503 | `LLM_*`、`VECTOR_DB_UNAVAILABLE`、`DATABASE_UNAVAILABLE`、`TOOL_EXECUTION_FAILED` |
| 500 | `INTERNAL_ERROR`（响应无栈） |

「证据不足」是 **200 + 文案**，不要和 503 混用。枚举：`app/core/error_codes.py`。

---

上一章 [05](05-agents-and-tools.md) · 下一章 [07 · 配置](07-configuration.md)
