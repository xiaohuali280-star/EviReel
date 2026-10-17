# EviReel P2：字幕转写、存储与独立语义查询

P2 在 P1 的视频管理上增加字幕通道。上传或完整重建会执行视觉和字幕阶段；字幕也可单独重试。HTTP/WS 的联合检索规划、RRF/Rerank、持久会话和网页仍分别属于 P3–P5。

## 安装与模型

```bash
python -m pip install -e ".[dev,asr]"
vam-server
```

FFmpeg/FFprobe 仍是必需系统工具。ASR 为可选依赖，默认 `tiny`、CPU、int8、2 线程。`ASR_ENABLED=true`；没有音轨直接记录 `skipped/no_audio`，不加载模型。缺少可选依赖返回 `asr_dependency_missing`，模型不完整、未缓存或设备不兼容返回 `asr_model_unavailable`，不会生成假的字幕。

默认 `ASR_LOCAL_FILES_ONLY=true`，不会自动下载模型。管理员可提前将完整 faster-whisper 模型放入 `ASR_MODEL` 指定的本地目录，或在允许联网的服务环境显式设为 false、配置 `ASR_MODEL_CACHE` 后执行一次任务。模型必须包含 tokenizer.json，避免 tokenizer 的隐式联网回退。远程模型用 `ASR_MODEL_REVISION` 固定版本；更换模型或配置会产生新转写版本，本地模型目录按实际文件校验值识别。

可配置项见 `.env.example`：模型/修订、语言（空为自动检测）、设备、compute_type、线程、beam_size、VAD、超时、最大输出段数/字符数和 Chunk 的长度/时长/停顿阈值。模型运行在独立进程；`ASR_TIMEOUT_S` 超时终止该进程。音频提取受 `MEDIA_TIMEOUT_S` 限制。测试仅通过显式依赖注入使用 MockTranscriber，生产配置不会静默选 Mock。

适配器接口依据 [faster-whisper 官方用法](https://github.com/SYSTRAN/faster-whisper#usage)及[WhisperModel 官方源码](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py)，消费完整 segment 迭代器，关闭词级时间戳；这里保存的是 segment 时间。

## HTTP 接口

| 接口 | 行为 |
| --- | --- |
| GET /api/videos/{video_id}/transcripts | 分页读取字幕；kind=chunks（默认）或 segments |
| POST /api/videos/{video_id}/transcripts/index | 创建独立字幕任务，返回 202 与 job_id |
| POST /api/videos/{video_id}/transcripts/search | 独立 Transcript 向量查询，不混入视觉证据 |
| GET /api/jobs/{job_id} | kind、总状态、当前阶段及独立通道结果 |
| GET /api/videos/{video_id} | 字幕最新尝试状态、可读/可检索能力、当前文本/搜索版本 |

```bash
curl 'http://127.0.0.1:8000/api/videos/VIDEO_ID/transcripts?kind=segments&offset=0&limit=50&min_time=10&max_time=60'
curl -X POST 'http://127.0.0.1:8000/api/videos/VIDEO_ID/transcripts/index'
curl -X POST 'http://127.0.0.1:8000/api/videos/VIDEO_ID/transcripts/search' \
  -H 'Content-Type: application/json' \
  -d '{"query":"老师如何解释蓝色向量？","top_k":5,"min_time":0,"max_time":120}'
```

读取参数 limit=1..200，offset>=0；查询 top_k=1..50、query=1..4000 字符。时间为有限非负秒数，max_time>=min_time。时间筛选使用区间相交：`end_t >= min_time && start_t <= max_time`，不会把命中字幕裁剪成虚假的新时间。

Chunk 含 video_id、doc_id、transcript_version、start_t/end_t、text、segment_ids、timing_precision；语义命中另外有 score。原始段含 segment_id、raw_text（ASR 原文）、规范化 text、语言和可用质量字段；raw_start_t/raw_end_t 保留 ASR 音频原始时间，time_clipped 标记视频边界裁剪。普通读取不返回向量；语言为转写器报告值，不声称逐句语言识别。

返回同时包含 text_version、search_version、readable、searchable、search_uses_older_text。`capabilities.transcript` 表示有非空可读字幕，新增 `capabilities.transcript_search` 表示存在已发布向量索引；模型配置改变时查询还会验证指纹，必要时返回 409 并要求重建。

未知视频 404；非法字段、时间、分页或 kind 为 422；未发布字幕/搜索索引、模型不兼容和视频忙碌为 409；查询 Embedding 服务失败为 502。读取字幕不调用模型，缺少 ASR 依赖不影响已发布文本的读取。

## 状态与失败恢复

- 业务任务 kind 为 video 或 transcript；同视频任何一种索引运行中都阻止重复索引和删除，队列与并发沿用 P1 上限。
- 完整任务的 result.stages 分别报告 vision 和 transcript；阶段包括 asr_audio、asr_transcribe、asr_chunk、transcript_embed。视觉成功但 ASR 失败，总任务 partial；视觉失败而字幕成功也为 partial。
- 字幕状态 ready、partial、failed、empty、skipped、interrupted 分别表示搜索可用、部分完成、失败、空转写、明确跳过和中断。skipped 的 reason 区分 no_audio 与 disabled，empty 的 reason 为 empty_transcription。
- 原片 SHA256、ASR 身份/配置、Chunk 规则版本组成 transcription_key；再加入 Embedding 指纹组成发布版本。重复相同任务复用结果，不重复插入段或 Chunk；更换 Embedding 模型可复用已有转写文本。
- 先事务保存暂存文本，再生成并完整校验向量；成功时事务切换文本与语义指针。ASR/规范化失败不覆盖旧文本或旧索引。
- Embedding 失败时新文本仍可读，旧搜索版本继续保留。两者不同时 `search_uses_older_text=true`，命中明确携带旧版本，不能把它当成新字幕结果。显式重试复用已保存的文本，不重新识别音频。
- 没有音轨和真正空转写有不同结果；空转写可返回空列表，不能作为语义检索成功。零向量、维度/数量错误、非有限值均不能发布为可检索索引。
- 重启把遗留 queued/running 任务标记 interrupted，保留已发布文本/向量和可重试暂存文本，不自动重试模型调用。临时 WAV 在任务退出时清理；重启清理中断留下的临时音频。

## 时间和存储约定

原音轨按 16 kHz、单声道 PCM16 提取；保存音轨起点相对首视频轨起点的偏移。ASR 时间加此偏移后裁剪到原视频区间；非法或非有限段时间拒绝发布。VAD 的结果仍按转写器返回的音频时间映射，重叠段保留，不通过顺移伪造精度。

Chunk 优先句子边界，并受文本长度、最大时长和停顿控制。无标点长文本确定性按字符拆分，保留原始 segment_ids。若单个 segment 本身长于时长阈值，它的文本片段仍保留该 segment 的完整时间区间，metadata 记录长段数量；不会把 segment 时间假装为精确词级时间。小数中的小数点不作为句子结束。

业务库增量迁移至 schema=2。字幕适配器在每视频的 FrameStore SQLite 文件中增加独立 transcript_schema=1、transcript_versions、transcript_segments、transcript_chunks 和 transcript_heads，保持视觉 PRAGMA user_version=2。视觉的全量快照写入只操作自身表，不会清空字幕。

registry 的 transcript_generation 固定承载字幕表的记忆文件，独立于后续 active_generation 的视觉切换；有视觉库时复用该文件，没有可用视觉库也能单独保存和检索字幕。字幕版本在同一库内以 text_version/search_version 原子切换。旧记忆文件按 P1 约定保留至视频删除；字幕版本暂不自动回收。删除视频一并删除所有字幕版本与临时文件。

## 验证边界

自动回归使用合成媒体、真实 FFmpeg/SQLite/HTTP、确定性非零 Mock 向量及 Mock Transcriber，不下载模型。真实小模型结果单独记录在 P2 交付报告，不把 Mock 命中率等同于语音准确率，也不代表真实模型 Embedding 或课程问答效果。
