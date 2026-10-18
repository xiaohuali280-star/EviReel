# EviReel P3：阶段三交付报告

日期：2026-10-18（Asia/Shanghai）
仓库：xiaohuali280-star/EviReel；仅在默认 main 修改与上传。
起始提交：ef62c920336e3ef91f09510af04bc1d099b4bfb2。

## 1. 阶段目标与范围

完成 P3-A 至 P3-E：统一 Evidence、四路召回与来源选择、RRF 与去重、可配置文本 Rerank、联合视觉检查与引用校验。直接复用 P1 视频隔离和 P2 字幕版本，没有改写视频资产或转写管线，没有提前实现 P4 会话持久化或 P5 网页。

全程使用 main，不创建分支或 PR，不修改 synced sources/。运行媒体、隔离依赖、模型缓存及实际密钥不进入源码提交。

## 2. 修改文件

| 文件 | 用途 |
| --- | --- |
| vam/retrieval/evidence.py | 来源适配、稳定 ID、统一字段、结构化回答与引用校验 |
| vam/retrieval/fusion.py | 从 1 开始排名的 RRF、列表内去重、确定性排序及原分数比较路径 |
| vam/retrieval/rerank.py | Reranker 协议、真实 Cohere v2 HTTP 适配、显式 Mock、候选/超时限制及文本槽位策略 |
| vam/retrieval/pipeline.py | 每请求视频/版本快照、四路检索、完整候选池融合、时间关联、检查与最终回答 |
| vam/retrieval/agent_search.py | 保留既有 Planner、多查询、时间锚点、visual_ref/direct inspect；按请求接入 P3 |
| vam/retrieval/frame_store.py | 托管视觉检查可禁用全局 latest-video 附件，保留旧库默认行为 |
| vam/memory/transcript.py | 指定已发布搜索版本、传播时间裁剪与适用 ASR 质量提示 |
| vam/protocol.py、vam/prompts.py | 所有嵌套来源支持 Transcript，限制非有限数值/查询数量，增加证据回答协议；旧库提示不宣称可检索字幕 |
| vam/indexing/service.py | 共享资源租用/锁、固定视觉与字幕版本、通道兼容性与 Reranker 注入 |
| vam/agent.py、server/ws/agent_chat.py | 视频作用域传递，统一 final 证据，显式摘要的来源 ID，受控浏览器事件 |
| vam/server/api/search.py、memory.py、app.py | 新统一 search，移除旧重复路由；保留帧/事件/摘要只读接口与 P2 独立字幕接口 |
| vam/config.py、.env.example | 融合/召回/检查资源上限与默认关闭的重排配置 |
| tests/test_p3_retrieval.py | 数值、模型协议、融合与多视频/多模态集成回归 |
| tests/test_p1_video_management.py | 旧 WS Stub 接受请求作用域，接到错误时立即失败，原隔离断言保留 |
| README.md、plan.md、docs/P3_SEARCH_API.md、P3_DELIVERY.md | P3 入口、阶段状态、协议和交付说明 |

P3 没有新增必装依赖或数据库迁移；使用已有 httpx，registry schema=2、视觉 schema=2、字幕独立 schema=1 保持不变。构建产物仍包含各迁移资源。

## 3. 核心流程

```text
video_id → 资源租用 / 每视频锁 → 固定视觉库与字幕搜索版本
                                  ↓
  来源选择 → 每来源先过滤视频/版本/时间 → 语义候选池
                                  ↓
  独立来源/查询排名 → RRF → 全局截断 → 可选文本重排
                                  ↓
  字幕关联实际帧/Event → 有界 inspect_frames → 保留检查范围
                                  ↓
  结构化 claims + evidence_ids → 校验本轮 ID → 程序构造时间/引用
                                  ↓
            answer / evidence / citations / warnings / execution
```

Evidence ID 绑定视频、来源、实际索引版本、记录、文字及时间。跨查询相同记录去重，跨来源同时间段建立关联而不互相覆盖；原相似度、通道排名和 RRF 贡献保留在审计字段。完整候选池先融合再全局截断，覆盖“各查询单独排名并不靠前、合并后最相关”的案例。

RRF 同分时按问题的来源优先级、时间及稳定 ID 排序；Frame+Event 的 legacy 路径明确排除 Summary/Transcript，比较时应关闭 Rerank。没有 Summary 时回退既有来源，不自动创建付费摘要；用户明确调用 summarize 工具仍可创建并返回真实 Summary Evidence。

文本 Rerank 使用服务端配置的固定 Cohere v2 接口，仅移动有文本候选的槽位，空文本帧保持融合位置。失败或超时回退 RRF，并分别记录请求策略和实际策略。VLM 检查与 Reranker 分开记录，不把视觉检查当作完成了视觉重排。

联合检查仅使用该视频实际帧，记录实际检查的 frame_ids/evidence_ids，不附带全局 latest-video。原始 Planner 草稿不直接当作最终回答发送。程序仅接受本轮真实证据 ID，并从记录生成结构化引用时间；模型提供无效引用、资源链接、常见自由时间标签或错误 JSON 时，返回可核对证据与 answer_validation_failed。该限制不保证语义正确。

## 4. 实际验证

本地完整回归为 **149 passed，0 failures/errors/skipped，1 warning，94.90 秒**；原 P0–P2 共 102 项，加 P3 的 47 项。引用标签专项另有 5 项通过。唯一 warning 为测试库的 Starlette/httpx 弃用提示。P3 覆盖：

- RRF 手算、不同原分数量纲、重复列表/记录、空来源、完整候选池融合后再截断及确定性排序。
- 四种来源、问题意图同分优先级、嵌套 Transcript 锚点、视觉引用、直接检查以及旧库三路提示词。
- 视频/时间闭区间隔离，视觉重建后的新 ID 与旧图片 stale_evidence，字幕版本在请求中途更新仍使用固定旧版本。
- 同维不同模型拒绝、维度异常、代表帧缺失、通道为空及无证据回答。
- Rerank 开关、覆盖/未覆盖槽位、候选限制、无文本帧、超时回退、401、重复/缺项索引及非有限分数。
- Cohere 实际 HTTP 适配器的序列化、Bearer Header 与响应索引合约，通过 MockTransport 验证，不发送真实付费请求。
- 无效/跨视频引用、模型自造 URL/时间/ID 的拒绝；最终引用由实际记录生成。
- 实际生成含 Attention/softmax 公式的幻灯片视频，Mock 教师口述，经过真实 SQLite/FFmpeg/HTTP 与 Mock Embedding/VLM/LLM 的组合证据链；检查字幕、画面、可读取图片链接与最终引用。
- WS final 的统一证据、受控事件、语音/画面冲突提示，以及检索不能偷偷创建 Summary。

本地环境为 Windows、Python 3.12.14、CPU PyTorch、FFmpeg 9.0.2。测试模型均为显式 Mock；HTTP/WS 和 SQLite/FFmpeg 确实运行。按 P1 环境要求允许测试使用本机回环。全库编译、未定义名/语法检查及新增模块 F 类静态检查通过，wheel 构建与迁移资源加载验证通过。上传 main 后核对 Linux Actions，不把本地结果等同于云端验证。

| 完成状态 | 结论 |
| --- | --- |
| P3-A 至 P3-E 实现 | 已完成 |
| 自动回归 | 本地 149 项通过；Mock 模型与实际媒体/存储/HTTP/WS，云端结果见对应 Actions |
| 服务适配器格式核对 | 依据 [Cohere 官方文档](https://docs.cohere.com/v2/reference/rerank)，HTTP 合约测试通过 |
| 真实付费 Rerank / Embedding / VLM / LLM | 未验证，未使用真实密钥 |
| 真实课程组合问答准确率 | 未验证，不报告虚构效果或费用 |

## 5. 限制与风险

1. 引用的程序校验只保证 ID、视频、来源版本及结构化时间来自真实检索记录，不保证答案理解正确；真实课程、真实模型间兼容性与问答效果需要单独验收。
2. ASR 的误识别和段级时间误差仍存在，延续 P2 的原始时间/裁剪标记。时间关联不是语音与画面语义一致的证明，冲突与不确定性应由证据和检查共同判断。
3. 文本 Rerank 默认关闭，空文本帧不参与文本模型排序。真实服务可用性、账号权限和计费未实测；无凭据、超时及错误回退有测试。
4. HTTP 来源选择是轻量规则；WS 使用 Planner，默认最多 4 回合。召回候选、检查帧、最终证据和上下文均有配置上限，但不是大规模课程检索性能基准。
5. 检索仍使用现有内存/SQLite 候选查询；旧索引与字幕版本按 P1/P2 保留到视频删除，未加入版本回收或分布式队列。
6. 浏览器 search/final 的证据协议已升级；P2 独立字幕接口和旧库/TUI 入口保留。旧图片链接遇到索引重建会明确过期，不能静默换成新图。
7. 历史仍只在 WS 连接内存中，重连丢失；这是 P4 工作，本阶段不宣称会话持久化。

## 6. GitHub 交付

代码、测试和说明直接上传 [main](https://github.com/xiaohuali280-star/EviReel/tree/main)，不创建分支/PR。更新前核对远程 main，使用普通提交与分支头校验，不强制覆盖其他改动。实际提交和云端检查链接在最终交付消息及结果清单记录。

## 7. 下一阶段（未执行）

P4 才增加持久会话、消息、上下文预算与跨轮指代处理。P5 再实现 React 界面。本次停在 P3 完成，不自动进入下一阶段。
