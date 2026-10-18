# EviReel P3：统一多模态检索与证据回答

P3 使用同一协议返回 frame、event、summary、transcript。HTTP 检索、WebSocket 最终回答以及显式摘要工具均能返回带来源的 evidence；P4 持久会话、P5 网页与真实课程效果评估尚未实施。

## 检索接口

```http
POST /api/videos/{video_id}/search
Content-Type: application/json
```

```json
{
  "query": "老师讲的注意力公式在 PPT 上如何表示？",
  "queries": ["attention softmax Q K V"],
  "sources": ["transcript", "frame", "event"],
  "top_k": 5,
  "min_time": 0,
  "max_time": 120,
  "fusion": "rrf",
  "rerank": false,
  "answer": true
}
```

query 必填，最长 4000 字符；queries 最多 3 个额外查询，每项非空且不超过 4000 字符。sources 可省略，允许四种来源的非空子集。时间为有限非负秒数，max_time>=min_time；帧按时间点匹配，文档/字幕按闭区间相交匹配，不裁剪来源记录的原始时间。top_k=1..50，另受 RETRIEVAL_MAX_EVIDENCE 上限约束。

answer 默认 false，仅执行召回与可选文本重排，不调用生成模型/VLM。answer=true 时进行有界视觉检查与结构化证据回答。客户端只能选开关和既定策略，不能提供服务端路径、API 端点、密钥或模型名称。输入校验错误为 422，视频不存在为 404，尚无兼容可检索通道为 409；单个通道失败会保留其他通道并返回 warning，不伪造结果。

普通检索返回 evidence、warnings、execution，并暂保留 results 作为 evidence 的别名。answer=true 返回 answer、found、evidence、citations、warnings、execution。WebSocket `/ws/agent` 的 final.result 使用同一回答协议；执行过程仍发送阶段事件，并非逐 token 流式。视频相关请求仍必须携带 video_id。

## Evidence 与引用

| 字段 | 含义 |
| --- | --- |
| evidence_id | 根据视频、来源、实际版本、记录 ID、内容和时间生成的稳定标识 |
| video_id / index_version | 所属视频和实际视觉或字幕搜索版本 |
| source | frame、event、summary、transcript |
| doc_id / frame_id | 至少有一种真实记录定位 ID |
| start_t / end_t | 视频相对时间；帧为同一点 |
| text | 原始来源文字；没有描述的帧为空字符串 |
| image_url | 从已存在的本视频帧构造的受控链接；代表帧缺失时为空 |
| related_frame_ids / related_evidence_ids | 时间关联，不能当成语义一致或互相替代 |
| score / score_details | 当前排序及来源排名、相似度、RRF 与重排审计，不是概率 |

同一帧重新编号或重建视觉索引不会复用旧 evidence_id。字幕重建与视觉重建有独立版本；视觉重建不改变未变更的字幕证据身份。查询固定读取到的视觉库和字幕 search_version，不能在中途静默切成新版本。旧图片链接在视觉重建后仍遵循 P1 的 stale_evidence/409 规则，需要刷新，不能显示同编号的新帧冒充旧证据。

生成模型提交的是 claims/text/evidence_ids，而非自由生成的时间或资源链接。程序逐条校验 ID 是否属于本视频本轮实际召回的证据，并从记录生成 citations 的时间、版本及资源链接。模型在 claim 中夹带 URL、常见时间标签或自造 ID，或返回不合规 JSON 时，回答会被拒绝并返回 answer_validation_failed；原始证据仍可核对。该校验保证结构化引用有效，不证明陈述语义正确，也不能替代真实数据评估。

## 四路召回、RRF 与比较基线

- 口述问题优先选择 Transcript，PPT/公式优先 Frame+Transcript，事件问题优先 Event，概览优先已有 Summary/Event。HTTP 使用轻量规则选择来源；WebSocket Planner 可明确选来源，嵌套单锚点、起止双锚点、候选来源组也支持 transcript。
- 每条通道使用自己的模型指纹/维度校验，先过滤视频、版本和时间，再做语义召回。不兼容时显式警告，禁止混用向量。字幕沿用 P2 的独立向量空间标识。
- 默认按 `Σ 1/(k+rank)` 融合，rank 从 1 开始、k 默认 60。每条来源/查询/时间窗口形成独立排名；重复查询的同一列表不重复计票，同一列表内同记录只计一次。全候选池先融合，再截取全局 top_k，避免遗漏多个查询都命中的中等排名记录。
- 原相似度仅作诊断，不跨来源直接相加；RRF 同分时按问题的来源优先级、时间与稳定 ID 排序。直接按引用检查的帧没有伪造相似度，审计中 similarity=null、selection=direct_inspection。
- 相同来源记录跨查询去重；相同时段的字幕、事件和帧分别保留。字幕可关联邻近帧/事件；这些补充证据可使 evidence 数量超过主结果 top_k，但总量不会超过配置上限。会为关联证据留出有限空间。
- `fusion=legacy, rerank=false` 为 Frame+Event 原分数排序的比较路径；明确排除 Summary/Transcript。不要把默认四路 RRF 或原库默认三路召回标成论文基线。保留库/TUI 无 video_id 的旧检索入口，不能将其当成业务多视频接口。
- 缺少 Summary 时回退现有来源并警告；检索 Planner 的自动 summarize 动作被拒绝，只有用户明确调用摘要工具才创建新 Summary。

## 可选文本重排

默认关闭。实际适配器依据 [Cohere v2 Rerank 官方接口](https://docs.cohere.com/v2/reference/rerank)：服务器向 `https://api.cohere.com/v2/rerank` 发送 model、query、documents、top_n，使用 Bearer 密钥，读取 results 中的 index 与 relevance_score。模型、候选数量、文本长度和超时均由服务器配置；已实现可注入 Mock 与 HTTP 合约测试，未用真实凭据验证付费服务。

重排只覆盖候选池前 N 条中具有实际文本的记录。空文本帧保持融合位置，不宣称完成视觉 Rerank；未覆盖数量与策略写入 execution。服务错误、无凭据、超时、缺项/重复索引/非法分数均回退 RRF，返回 rerank_failed_fallback_rrf。execution 区分 requested_fusion 与实际 fusion，并记录开关、模型、覆盖范围、耗时和状态。重排启用后 score 表示最终排名的倒数，原 RRF/相似度在 score_details 中保留。

## 联合检查与不确定性

字幕命中按时间重叠或邻近关联本视频的帧和 Event，复用 inspect_frames，保留检查实际使用的 frame_ids/evidence_ids。受控多视频检查不会附带全局 latest-video 文件。VLM 检查与 Reranker 是不同步骤，不能互相冒充。

字幕保留 ASR 不确定性、时间裁剪与适用质量提示。Transcript 时间锚点是文本锚点，不被当成视觉验证。关键帧缺失、通道不可用、语音/画面冲突或证据不足都有可观察 warning；无法核对画面时回答说明限制。没有命中时返回空证据和无匹配回答。检索文字均作为不可信引用资料传给模型。

## 配置与验证范围

`.env.example` 包含 RETRIEVAL_FUSION、RETRIEVAL_RRF_K、每来源候选数、最低相似度、证据总上限、邻近时间、检查帧数、Planner 回合上限及答案上下文长度。RERANK_ENABLED 默认 false；RERANK_API_KEY、MODEL、TIMEOUT_S、CANDIDATES、TEXT_CHARS 均为服务端配置。HTTP 单次最多 4 条查询；托管 Planner 默认最多 4 回合，每步受协议查询数量限制。

自动验证使用真实 SQLite、合成视频、公式幻灯片、HTTP/WS 和明确标记的 Mock Embedding/LLM/VLM/Reranker。真实课程、多语言问答准确率、真实模型之间的图文兼容性、Cohere 凭据与费用、长课程资源消耗仍未验证。
