# EviReel P2：阶段二交付报告

日期：2026-10-17（Asia/Shanghai）
仓库：xiaohuali280-star/EviReel；开发与上传均使用默认 main。
起始提交：128ffef294ee1c3b7a51624d6185fd1724f61262。

## 1. 阶段目标与完成范围

完成 P2-A 至 P2-E：音轨探测与固定格式提取、可替换的 faster-whisper/Mock 转写器、字幕规范化与 Chunk、每视频记忆库内的持久化与向量索引、幂等与独立状态、分页读取及独立 Transcript 语义检索。未进入 P3–P5。

按用户持续有效的要求，所有修改直接在 main 进行，不创建新分支或 PR。Synced sources/ 未改动；模型、音视频测试样本、隔离依赖与运行数据库不上传源码仓库。

## 2. 修改文件

| 文件 | 改动 |
| --- | --- |
| vam/asr/audio.py | FFprobe 音轨探测，PCM16/16kHz/单声道提取，音视频偏移与超时 |
| vam/asr/transcriber.py、worker.py、__init__.py | 可注入协议、显式 Mock、可选 CPU faster-whisper、独立模型进程与输出限制 |
| vam/asr/chunker.py | 原始/规范化段时间与文本、质量字段、句界/停顿/长度切块、长段精度说明 |
| vam/asr/indexer.py | 校验值/模型/规则身份、复用文本、分批 document Embedding、完整向量校验、事务发布 |
| vam/memory/transcript.py、migrations/001_transcripts.sql 及包入口 | 同库字幕 schema、版本/原始段/Chunk、文本与搜索双指针、独立向量查询 |
| vam/retrieval/frame_store.py | 提供同一持久记忆文件的字幕适配器入口 |
| vam/storage/database.py、migrations/002_transcripts.sql | registry 从 schema=1 增量迁移至 2，保留原视频/任务记录 |
| vam/video_registry.py | 原片校验值、字幕承载库/状态、job kind、独立能力、事务终态 |
| vam/indexing/service.py | 保留既有视觉流程，加入 ASR 与独立重试、partial/恢复、同视频冲突控制和临时音频清理 |
| vam/runtime.py | 已有线程取消收尾包装器支持关键字参数 |
| vam/server/api/transcripts.py、server/app.py | 三个受控字幕接口及依赖注入入口 |
| vam/config.py、.env.example | 模型/语言/设备/超时/输出与 Chunk 限制 |
| pyproject.toml、setup.cfg | 一致的可选 asr extra，字幕 SQL 资源打包 |
| tests/test_p2_transcripts.py | P2 集成、错误恢复、媒体偏移、可选适配器及幂等回归 |
| tests/test_p1_video_management.py | 原 P1 能力断言增加 transcript_search 字段，保留原有行为回归 |
| README.md、plan.md、docs/P1_VIDEO_API.md | P2 入口与阶段状态；保留历史 P1 说明并指向新协议 |
| docs/P2_TRANSCRIPT_API.md、P2_DELIVERY.md、P2_REAL_ASR_RESULT.json | 接口/配置/状态、交付报告和真实小模型原始结果 |

没有修改原 JobManager/vam/jobs.py；继续使用 P1 的 PersistentProgress 适配层，把视觉和字幕阶段写入业务 SQLite。P2 不引入新任务队列或外部服务。

## 3. 核心流程与实现决定

```text
上传/完整重建 → 既有视觉管线 → 视觉结果（成功或失败）
                         ↓
                独立字幕阶段 / 单独字幕重试
                         ↓
      FFprobe → 16kHz mono PCM16 + 音视频起点偏移
                         ↓
      原片校验值 + ASR 配置 + Chunk 规则 → 幂等键
                         ↓
      复用已有文本 / 隔离进程 faster-whisper → 规范化与切块
                         ↓
      暂存完整文本 → document Embedding → 校验完整向量
                         ↓
      同库事务发布 text_version / search_version
                         ↓
      分页字幕 / query Embedding + 时间过滤语义查询
```

字幕表与视觉表位于每视频的同一 FrameStore SQLite 文件，独立 transcript_schema，不复用视觉的全量持久化操作。registry.transcript_generation 固定承载该字幕库；后续视觉重建切换自己的 active_generation 时不会搬走或删除字幕。视觉失败时，也能在该视频的记忆文件内独立保存字幕。

原始 ASR 文本/音频时间与归一化视频时间同时保留；time_clipped 明确标记边界裁剪。Chunk 只声明 segment 级精度。原片 SHA256、模型/配置与 Chunk 规则决定转写身份，Embedding 指纹另决定向量空间；相同任务不重复插入，单独重做向量复用已持久化文本。

ASR 失败不覆盖旧字幕；Embedding 失败发布新可读文本但保留旧语义索引。接口明确报告两个版本及 search_uses_older_text，查询命中绑定实际搜索版本。视觉与字幕任一通道失败不会让另一个通道失效。

## 4. 实际验证

### 自动回归与构建

自动测试最终结果以本次 JUnit 与 GitHub Actions 日志为准。当前覆盖原 63 项和新增 39 项，共 102 项：真实 SQLite、FFmpeg 音视频、HTTP/WS；自动回归中的模型调用为显式 Mock，不隐式下载模型。唯一测试 warning 为 Starlette/httpx 的依赖弃用提示。

覆盖：中文/英文/混合文本、空转写、无音轨、长句、无标点、小数句界、重叠、非法时间/非有限值、偏移/裁剪、原始文本保留、分页与时间过滤、同名视频隔离、向量异常、document/query 用途、同维不同模型拒绝、重复索引与视觉重建、旧字幕/搜索保留、嵌入重试复用文本、独立通道 partial、读取旧版本、删除/重复任务冲突、重启恢复、schema=1 迁移，以及可选依赖/模型错误、子进程超时和无隐式联网的适配器合约。

全库 compileall、未定义名/语法检查及新增模块完整 F 类静态检查通过。构建 wheel 后确认两套 SQL 迁移和 ASR worker 随包分发，再从 wheel 解包导入并实际初始化 registry schema=2 与字幕 schema=1。

本机测试环境为 Windows、Python 3.12.14、CPU PyTorch、FFmpeg 9.0.2；异步 HTTP/WS 测试按 P1 已确认的环境要求使用本机回环。GitHub main 的 Linux CPU 流程不下载 ASR 模型，上传后检查对应 Actions 运行结果。

### 真实 CPU ASR

已实际安装 faster-whisper 1.2.1、CTranslate2 4.8.3，以 tiny.en、CPU/int8、2 线程、VAD=true 运行生成的短视频。台词由 Windows Microsoft Zira Desktop 合成：`The red triangle is above the blue square.`。识别原文与已知台词一致，关键词全部匹配；缓存模型的该次完整转写子进程耗时 3.704 秒，不作为性能基准。

必须同时记录时间限制：源音频长 3.06898 秒，封装视频长 3.04 秒；该模型原始 segment 为 0–7 秒，末端与音频文件边界相差约 3.931 秒。归一化后为 0–3.04 秒，time_clipped=true。裁剪后的边界不能被算作零误差，也不是词级对齐真值。原始时间与文本完整保存于 [P2_REAL_ASR_RESULT.json](P2_REAL_ASR_RESULT.json)。这验证了真实模型链路和归一化，而非精确时间定位或真实课程识别质量。

真实运行还发现 faster-whisper 的文件解码路径与当前 PyAV 的接口差异。已提取的 WAV 现在由 worker 验证后直接转为 16kHz PCM 浮点数组传入模型，保持同一音频时间轴并避免重复解码；真实运行验证了该修复。

| 完成状态 | 结论 |
| --- | --- |
| 代码实现 | P2-A 至 P2-E 完成 |
| 自动验证 | 102 项覆盖；模型为显式 Mock，实际结果见 JUnit/Actions |
| 真实 ASR | tiny.en 英文合成短视频实际运行成功；时间误差如上 |
| 真实 Embedding/课程问答 | 未验证，未使用付费模型密钥 |
| Linux 云端 CI | 直接上传 main 后核对 Actions；不把本机测试等同于云端运行 |

## 5. 未解决问题与限制

1. tiny.en 示例只证明一条英文合成语音可识别，且原始时间明显偏粗；中文真实课程、噪声、多说话人、术语和逐词对齐未验收。不宣称 WER 或时间精度达标。
2. 本阶段不集成字幕与视觉的联合规划、RRF、Rerank 或顶层问答路由；独立字幕查询按余弦相似度排序。
3. ASR 是可选依赖；模型缺失时含音轨的视频任务可 partial，视觉仍可用。模型下载默认关闭，管理员需显式准备缓存或授权联网配置。
4. 模型超时覆盖子进程的加载与推理；关闭时等待已经进入线程的提取/模型调用结束或超时，再清理临时文件。长课程资源和并发吞吐未基准测试。
5. 字幕语义查询仍扫描该视频活跃版本的候选向量；旧字幕版本/旧视觉库保留至删除视频，没有自动版本回收服务。
6. 字幕读取与搜索可以指向不同版本；调用方必须使用明确的版本/旧搜索提示，不把旧命中当作新文本。
7. 当前沿用 P1 单进程、单 worker；没有登录/分布式队列，真实 Embedding/VLM/LLM 兼容性与费用仍未验证。

## 6. GitHub 交付

所有实现、配置、测试和报告直接提交至 [main](https://github.com/xiaohuali280-star/EviReel/tree/main)，未创建分支或 PR。上传前核对远程 main 未被其他提交移动；普通更新保留历史，不强制覆盖。提交号和实际云端测试链接在本次交付消息/结果清单中记录，避免报告自引用提交号。

## 7. 下一阶段（未执行）

P3 才接入统一 Evidence、视觉与 Transcript 等通道的 Planner、RRF/Rerank、按证据生成回答与完整追溯。本次仅完成 P2，等待用户下一阶段指令。
