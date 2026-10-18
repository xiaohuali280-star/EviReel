# EviReel P4：阶段四交付报告

日期：2026-10-18（Asia/Shanghai）
仓库：xiaohuali280-star/EviReel；仅在默认 main 修改与上传。
起始提交：562f62ca419db8e4ea3adc31948d6e353d9d97ae。

## 1. 阶段目标与范围

完成 P4-A 至 P4-D：持久会话/消息、受预算限制的上下文与增量摘要、带证据锚点的指代改写、幂等后台请求与断线/重启恢复。会话永久绑定视频，复用 P3 检索与引用校验。原始问题、改写、来源版本与证据快照可查询。

按用户要求直接在 main 开发，不创建分支或 PR；synced sources/ 未修改。没有提前实现 P5 网页、账号系统或任意模型步骤的断点续跑。

## 2. 修改文件

| 文件 | 行为 |
| --- | --- |
| vam/storage/migrations/003_conversations.sql、storage/database.py | registry schema 3，增加会话、消息、请求；事务与唯一运行请求约束，保留旧数据 |
| vam/memory/conversation.py | 视频绑定、原始消息与 final 快照、分页、幂等、终态事务、摘要边界、旧证据标记及重启恢复 |
| vam/memory/context_manager.py | 默认 6 个完整完成轮次，字符/保守 token/单消息预算；有界增量摘要、失败降级、结构化引用解析 |
| vam/conversation_service.py | 全局受限后台任务、每会话并发拒绝、资源租用、超时、持久化后发布与进度订阅清理 |
| vam/server/api/conversations.py | 创建、列表、详情、消息分页、请求恢复、删除 |
| vam/server/ws/agent_chat.py | 协议 v2、accepted/resolved/终态；连接不拥有任务生命周期，旧请求私有会话适配，reset 明确新建会话 |
| vam/agent.py、prompts.py、protocol.py | 顶层路由收到同一历史；内部 Planner 通过现有 chat_history 接收；保存并使用规范改写，增加持久请求 ID |
| vam/server/app.py、config.py、.env.example | 服务启动/关闭/恢复、路由与所有 P4 限额 |
| tests/test_p4_conversations.py | 持久化、预算、追问与恢复的 Mock/真实媒体和 HTTP/WS 回归 |
| tests/test_p1_video_management.py、test_p2_transcripts.py | 旧隔离回归观察结构化完整轮次；迁移预期升至 schema 3 |
| README.md、plan.md、docs/P4_CONVERSATION_API.md、本报告 | 入口、阶段状态、接口、配置与验证边界 |

没有新增依赖；wheel 继续包含 SQL 迁移，视觉 schema 2 和字幕 schema 1 不变。历史库应使用 schema 3 服务运行，旧版程序会拒绝更高版本业务库。

## 3. 核心流程

```text
video_id + conversation_id + request_id + 原问题
  → 校验归属 / 同 ID 输入 / 并发与容量
  → 事务保存 accepted + user 消息 → 后台任务持有视频租用
  → 有界摘要与完整近期轮次 / 上次相关证据
  → 指代解析、保存规范改写 → 顶层路由 + 内部 Planner 共用上下文
  → P3 当前视频证据检索与回答（锚点是建议，不永久限定窗口）
  → 事务保存 completed + assistant + final 引用快照
  → 发布 final；断线后 HTTP 恢复或同 ID 重放
```

不确定指代产生澄清 final，不虚构引用。旧证据仅辅助理解，最终回答仍须 P3 当前证据。摘要失败保留原文和最后成功摘要，记录尝试边界，不对未变化边界无限重试，缩小 Prompt 窗口并返回 context_warnings。字节估算是保守预算，会话上下文与 P3 检索候选/多模态附件各有独立上限。

同一 request_id 的不同输入冲突；运行中重复只订阅；完成/失败/中断只重放、不重新执行。同会话新问题运行中明确拒绝并发。执行默认全局 4 个、180 秒总预算，断线不取消任务。服务关闭保存 interrupted；异常进程重启恢复遗留请求为 interrupted，用户用新 ID 重试。

## 4. 实际验证

自动验证使用显式 Mock Embedding/LLM/VLM/ASR；SQLite、迁移、FFmpeg 媒体、HTTP 与 WebSocket 确实运行，不调用真实付费模型。测试包括：

- v1/v2 业务库升级保留视频，schema 3 的会话/消息/请求可用；wheel 内迁移资源导入和初始化通过。
- 原问题、回答、规范改写和引用持久化；同 ID 不增加重复消息/执行，输入冲突、A/B 视频绑定、分页、删除级联。
- 最近 N 以完整已完成轮次计数，失败/运行请求不计入；增量摘要边界、不重复压缩、消息长度与字符/估算 token 硬预算。
- 摘要失败保留原文、同边界不重试；引用未知 ID、多个锚点、无锚点和歧义时澄清。
- 公式→后续代码追问：Mock 解析选择实际证据 ID，顶层 Router 与内部 Planner 都看到同视频历史及锚点，P3 真实存储/媒体检索生成引用。
- 断线后任务落库、重连重放、终态落库后未发送仍恢复；忙碌会话/视频删除被拒绝。
- 失败不保存半段答案，重放错误不重复执行；超时、关闭/重启中断、完成结果跨重启保留。
- 索引版本改变后仍展示旧快照并标记 stale_evidence，不把旧 ID 解析为新记录；澄清后保留最近相关锚点。
- 输入长度/ID 限制、reset 新会话、完成后订阅释放及旧 P1–P3 回归。

本地最终 P0–P4 全套回归 **173 passed，0 failures/errors/skipped，1 warning，105.29 秒**（原 149 项 + P4 新增 24 项）；P4 专项另有 **24 passed，1 warning，11.34 秒**。warning 为 Starlette/httpx 弃用提示。本地环境为 Windows、Python 3.12.14、CPU PyTorch、FFmpeg 9.0.2，测试按既有环境约束使用本机回环。静态未定义名/语法检查、新模块 F 类检查、全库编译与 wheel 验证均通过。

上传 main 后，[GitHub Actions #39105725168](https://github.com/xiaohuali280-star/EviReel/actions/runs/39105725168) 在 Ubuntu / Python 3.11 上验证代码提交 `797cc042202bfc20403979f26bcba5dfeb0973e5`：**173 passed，1 warning，54.74 秒**，结论 success。后续收尾提交仅更新交付报告，运行代码与本次云端验证一致。

## 5. 限制与风险

1. 真实模型的摘要保真度、指代理解、课程问答效果与费用未验证；Mock 验证接口与控制流程，不能替代质量评估。
2. token 预算采用保守 UTF-8 字节估算，作用于会话上下文；并非提供商精确 tokenizer 或整个多模态请求的统一上限。超出时进入 Prompt 的历史会缩短，数据库原文仍在。
3. 语义摘要可能遗漏内容；摘要失败只回退近期窗口，不保证所有早期信息都能参与问答。最后相关证据从近期窗口寻找，过远引用需澄清。
4. 断线可恢复已完成结果，进度事件不持久重放；单 worker 进程重启不恢复模型执行步骤。失败后新 request_id 可能再次产生模型费用。
5. 保持现有单用户可信部署，无账号权限体系；会话数据不按用户授权隔离。数据库历史持续增长，尚无留存/归档策略。
6. P4 新增业务迁移为前向升级；部署前应保存现有 DATA_ROOT。视频删除会级联删除会话，运行中请求受资源租用保护。
7. 浏览器界面未实现；需通过接口/现有工具交互。P5 需保存三个 ID、展示阶段与终态、使用状态接口恢复。

## 6. GitHub 交付

代码、测试和说明直接上传 [main](https://github.com/xiaohuali280-star/EviReel/tree/main)。提交前核对最新分支头，使用非强制更新，不覆盖并发改动。代码提交为 [797cc04](https://github.com/xiaohuali280-star/EviReel/commit/797cc042202bfc20403979f26bcba5dfeb0973e5)，[对应云端检查](https://github.com/xiaohuali280-star/EviReel/actions/runs/39105725168) 已成功；随后仅更新本报告记录结果。

## 7. 下一阶段（未执行）

P5 才实现 React/TypeScript/Vite 的视频管理、播放、字幕/事件时间轴、连续问答、证据卡片与恢复界面。本次完成后停在 P4，等待用户下一阶段指令。
