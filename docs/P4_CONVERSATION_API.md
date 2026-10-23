# P4 持久会话与恢复协议

日期：2026-10-18（Asia/Shanghai）。P4 使用 P3 的视频隔离和统一证据协议。

## HTTP

| 方法与路径 | 行为 |
| --- | --- |
| POST /api/conversations | `{"video_id":"v_…"}` 创建永久绑定该视频的 `c_` + 32 位小写十六进制会话；201 |
| GET /api/conversations?video_id=v_…&offset=0&limit=50 | 按更新时间倒序列表；limit 1–200 |
| GET /api/conversations/{conversation_id} | 所属视频、摘要、summary_through、summary_attempt_through、summary_status |
| GET /api/conversations/{conversation_id}/messages?after=0&limit=50 | 按 message_id 正序；after 为独占游标；next_after 非空时继续；limit 1–200 |
| GET /api/conversations/{conversation_id}/requests/{request_id} | 恢复请求状态、原问题、改写、错误或已完成 final |
| DELETE /api/conversations/{conversation_id} | 无 accepted/running 请求时删除；204，否则 409 conversation_busy |

历史消息包括 role、content、request_id、status、rewritten_question、resolution、created_at；assistant 附完整 final 快照（`result.result` 为 P3 结果）。原始问题永不被检索改写覆盖。失败/中断仅保留 user 消息与请求状态，不创建正常 assistant 回答。GET request 返回的 `result` 是持久化的完整 final 事件。

历史证据的 evidence_id、index_version、来源、时间、引用保持原样。读取时在 Evidence 加 `historical_snapshot=true`、`stale_evidence`；分别比较当前视觉版本或字幕搜索版本。文本问答的字幕证据另带 `version_basis=text`，按 `text_version` 校验，不依赖语义索引。版本变化时，旧图片链接仍遵循 P3 的过期错误，不重定向到新记录。引用快照和原始回答不被重写。

删除视频受到请求资源租用保护，运行中返回 409 video_busy；删除成功后数据库外键级联删除所属会话、消息、请求。

## WebSocket v2

先创建会话，客户端为每个新问题生成新的 `r_` + 32 位小写十六进制 ID，并保存三个 ID 供恢复。连接 `/ws/agent`：

```json
{
  "type": "text",
  "video_id": "v_0123456789abcdef0123456789abcdef",
  "conversation_id": "c_0123456789abcdef0123456789abcdef",
  "request_id": "r_0123456789abcdef0123456789abcdef",
  "text": "这个公式后面有代码实现吗？"
}
```

text 最多 4000 字符，且受服务端 CONVERSATION_MESSAGE_CHARS（默认 2000）与上下文预算限制；整条 WS JSON 最多 16000 字符。conversation_id/request_id 必须同时提供或同时省略。工具请求也支持同样的 ID：`type=tool,name=retrieve,args={question:…}` 或原 summarize 参数。所有浏览器工具在同一持久化和资源隔离路径运行。

事件顺序：`accepted` → `resolved` → 原有 tool_use/plan/search/inspection 等执行事件 → `final` 或 `error`。请求事件含 `protocol_version=2,video_id,conversation_id,request_id`。连接 info/ping/pong 不需请求 ID；reset 的 conversation_created 含新会话和视频 ID。客户端使用结构化阶段状态，不需要显示原始 Planner 文本。

accepted 含 status、replayed。相同会话相同 request_id/输入复用原请求：运行中订阅，已完成重放 final，失败/中断重放 error，不重新执行。相同 ID 配不同输入返回 409 对应的 `request_conflict` 事件；跨视频会话返回 `conversation_video_mismatch`。同会话另一请求正在运行时拒绝为 `conversation_busy`。全局达到默认 4 个 accepted/running 请求时返回 `conversation_capacity`，不插入消息。WS 错误码不使用 HTTP 状态行。

resolved/final 保留 original_question、rewritten_question、anchor_ids、needs_clarification、advisory_time_range 等结构化结果。引用不足、模型解析失败、输出未知 ID、选择多个锚点或歧义时返回请求澄清的 final，没有虚构引用。后续使用新的 request_id 补充问题。

后台任务从接受时起持有视频资源租用，默认 180 秒总预算（含摘要/解析/模型调用），独立于连接生命周期。final **先提交数据库再发布**。WS 的进度订阅最多缓存 64 个事件，慢客户端可能丢失中间进度；历史/最终结果不依赖中间事件重放。断线后用 HTTP 查询请求，或在新 WS 用完全相同请求订阅/重放。客户端不要为同一逻辑请求随机生成新 ID 来恢复，否则将被视为新问题。

进程关闭会取消运行任务并保存 interrupted/server_shutdown；异常重启的 accepted/running 会在下次单 worker 启动标记 interrupted/server_restart。中断不能从某个模型步骤继续，应检查状态后用新 ID 重试。失败/超时不会把半段输出当正常回答；request_timeout 的总预算可配置。

## 上下文与追问

Context Manager 给顶层 router 和内部 Planner 同一份视频专属 JSON 历史，包含摘要、默认最近 6 个已完成完整问答轮次、最后相关引用证据的文本/来源版本/时间锚点。失败/未完成请求不进入完整轮次。最近一次回答是澄清且没有引用时，在近期窗口中寻找最后有引用的回答。

预算同时约束字符数和保守 token 估算（按 UTF-8 字节计数，故中文可用窗口更短），单条历史消息最多默认 2000 字符；并为当前问题与封装预留空间。改写再次检查预算。超出时删去较旧 Prompt 轮次、缩短摘要、减少锚点和最后一轮文本；数据库原文保留。这里的预算作用于**会话上下文部分**，检索候选、视觉附件及 Planner 自身系统提示使用 P3 的独立限制，不是整个多模态模型调用的精确 tokenizer 上限。

较早的完成轮次使用现有服务端模型增量摘要，一次最多默认 12 轮、输入受相同预算约束。summary_through 是成功压缩到的 assistant message_id；summary_attempt_through 记录上次尝试的边界。摘要失败不覆盖成功摘要，不删除消息，不在同一未变化边界无限重试；Prompt 发出 context_summary_failed/窗口裁剪提示，并继续近期历史。新消息到来可再做有界尝试。历史数据库允许增长；摘要是模型调用，可能产生配置提供商的费用。

追问先解析再选择 retrieve/summarize；“后面”只保存候选锚点并通过上下文提示允许扩展时间窗口，不永久继承上一轮的硬过滤。旧引用可辅助理解，最终答案必须由 P3 重新检索本视频当前证据。语义解析和摘要是模型行为，自动 Mock 回归不代表真实课程准确率。

## 旧客户端

省略两个 ID 时，为当前 socket 的每个视频分配私有持久会话与每问题 ID，accepted 会返回 ID；其他连接不会隐式获得它。需要重连恢复的客户端必须保存并传回 ID。`{"type":"reset","video_id":"v_…"}` 明确创建新会话，返回 conversation_created，旧历史仍保存；删除走 HTTP。原 TUI 使用自己的库入口，不共用浏览器默认会话。

本项目没有用户登录/授权体系；会话 ID 是资源定位符，不是权限边界。维持现有可信单用户部署与单 worker 数据目录约束。P5 已提供浏览器界面。

## 2026-10-23 快捷动作与文本问答扩展

`GET /api/videos/{video_id}` 新增 `chat`：`available`、`mode=text|retrieval|unavailable`、`reason`、当前来源 `version` 及 `actions={notes,video,transcript,slides}` 布尔能力。每个功能独立判定：缺少模型时，已有成品仍可读取；语义索引失败但存在可读文稿时仍可问答。

持久化 `type=text` 请求支持以下可选字段，旧请求保持兼容：

```json
{
  "action": "video",
  "scope": {"start_t": 0, "end_t": 60, "version": "0123456789abcdef0123456789abcdef"}
}
```

这是附加字段示例，实际请求仍须提供原有视频、会话、请求 ID 和 text。`action` 严格枚举为 `notes|video|transcript|slides`；普通问题不带 action。`scope` 独立可选，起止必须有限、非负、有序且不超过真实视频时长；version 若提供必须等于当前来源版本。范围无效返回 `invalid_time_range`，过时返回 `chat_context_stale`。相同请求 ID 更改 action/scope 同样触发 `request_conflict`。历史 user 消息返回原 action/scope，重试可以保留它们。

笔记、文稿、课件动作返回 final，`result.resources` 保存经服务器生成的视频归属和成品/任务标识，例如：

```json
{"kind":"study","video_id":"v_0123456789abcdef0123456789abcdef","job_id":"0123456789abcdef0123456789abcdef","label":"学习笔记"}
```

`study` 资源使用 job_id 或 artifact_id；`transcript` 使用 job_id 或文稿 version。任务确认不是生成完成。客户端通过 `GET /api/videos/{video_id}/chat/resource?owner={video_id}&kind=study&job_id=…&offset=0` 读取实际状态。owner 必须与路径视频一致，任务和成品再次检查归属；资源 ID 不是用户授权凭证。

响应包括 status、可选进度/error_code、ready 时的 total、preview 和 links。笔记每页 5 章，文稿每页 10 段；offset 为已读取条数。服务器构造 Markdown/PPTX/PDF 或 TXT/SRT 下载链接，不使用模型输出的资源 URL。历史笔记返回 stale 并禁用时间定位；旧文稿返回 `chat_context_stale`，不会静默下载新版。`transcripts/export` 增加可选 version，并在读取锁内校验。

文本问答返回 `material_mode=transcript|events` 及 `text_basis_only`，发送 `status(stage=answering)` 后在结构、引用通过验证及持久化完成时发布 final。正文不发布未经验证的流式片段。当前来源用短别名传给模型，服务器映射回真实证据；历史引用不成为本次来源。超长整片概览转为完整学习任务资源卡，而不是只总结开头。`CHAT_TEXT_CHARS` 默认 12000，可配置 1000–30000；最多 24 条材料，模型结构校验最多重试一次，总请求仍受会话预算限制。

错误分类包括 `chat_source_not_ready`、`chat_context_limit`、`chat_context_stale`、`chat_invalid_output`、`chat_model_auth_failed`、`chat_model_rate_limited`、`chat_model_connection_failed`、`chat_model_unavailable`、`chat_model_request_rejected` 和 `request_timeout`。前端提供对应中文提示；失败仅保留失败 user 请求，不保存伪装成正常回答的半成品。
