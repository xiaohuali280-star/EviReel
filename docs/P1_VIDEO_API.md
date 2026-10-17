# EviReel — P1 视频管理 API

EviReel 在原 VAM 上增加多视频资产、独立视觉记忆、持久任务和视频播放。P1 尚不包含 ASR、持久聊天会话或 React 网页。

## 安装和运行

Python 3.10+，FFmpeg/FFprobe 6+；先安装 CPU 或合适平台的 PyTorch，再安装项目。

```bash
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install -e ".[dev]"
# 复制 .env.example 为 .env 并配置服务端模型密钥
vam-server
```

系统工具可放入 PATH，或配置 FFMPEG_PATH/FFPROBE_PATH。默认 DATA_ROOT=data/evireel。默认以单进程、单 worker 运行；不要给同一 DATA_ROOT 配置多个 Uvicorn workers。独占锁会拒绝第二个服务，避免其错误地恢复正在运行的任务。

SILICONFLOW/DASHSCOPE/OPENROUTER 为兼容的聊天/Embedding 设置。需要独立 Embedding 服务时使用 EMBEDDING_BASE_URL_OVERRIDE、EMBEDDING_API_KEY_OVERRIDE、EMBEDDING_MODEL_OVERRIDE；向量空间由所选模型负责兼容。不要把密钥放在 URL 查询参数或前端。ENABLE_RETRIEVAL=false 只启用库级零向量 Dummy，不会自动模拟真实 VLM；完整无密钥功能测试通过依赖注入的非零 Mock 与 Mock LLM 实现。

## HTTP

上传：

```bash
curl -F "video=@lesson.mp4" http://127.0.0.1:8000/api/videos
```

返回 202，包含 video_id、job_id 和视频信息。若队列已满，原视频仍保存，job_id=null 且 index_error 指明 queue_full；稍后显式发起索引即可，不需重复上传。

| 接口 | 用途 |
| --- | --- |
| GET /api/videos?offset=0&limit=50 | 分页视频列表 |
| GET /api/videos/{video_id} | 状态、时长、当前索引版本、可用能力 |
| GET /api/videos/{video_id}/content | MP4 播放；Range/206、后缀范围、416 |
| DELETE /api/videos/{video_id} | 删除全部所属资产、库和任务；占用中返回 409 |
| POST /api/videos/{video_id}/index | 重建/重试，返回新的 job_id |
| GET /api/jobs/{job_id} | phase、进度、attempt、终态、错误和过滤结果 |
| GET /api/videos/{video_id}/frames | 帧列表；分页、min_time/max_time |
| GET /api/videos/{video_id}/frames/{frame_id}/image | 关键帧；index_version 可防止重建后误取新帧 |
| GET /api/videos/{video_id}/memory-documents | Event/Summary 列表；kind、分页和时间过滤 |
| POST /api/videos/{video_id}/search | 原有视觉 Hybrid 检索；query/top_k/min_time/max_time |

search 仍复用原 Frame/Event/Summary 排序，没有 P3 的 Transcript、RRF 和 Rerank。图片通过受控资源 URL 读取，列表不传输全库 Base64。接口不接收 store_path 或 video_path。旧 /api/frames 路由默认不挂载；ENABLE_LEGACY_HTTP_API=true 仅供受信任的内部/TUI 兼容用途，不具备业务资源隔离约束。

未知资源 404；校验问题 422；占用/索引未准备好/模型不兼容 409；体积与时长超限 413；格式错误 415；队列满 429；磁盘不足 507。应用领域错误使用 error.code/message，输入字段校验保留 FastAPI 的 detail 数组。

## WebSocket

/ws/agent 仍发送执行阶段与最终回答，不是逐 token 协议。每个 text/tool 必须带服务端 video_id：

```json
{"type":"text","video_id":"v_<32 hex>","text":"这个视频讲了什么？"}
```

```json
{"type":"tool","video_id":"v_<32 hex>","name":"summarize","args":{"min_time":0,"max_time":120,"time_mode":"relative","granularity_seconds":60,"prompt":"概括课程内容"}}
```

返回事件有 video_id/index_version，检索证据也绑定视频。连接切换视频会清空该连接的旧历史；断线仍丢历史，P4 才实现持久会话。顶层历史指代解析同样留给 P4。reset 只重置连接内历史。

## 任务、索引和删除

- 状态：queued、running、succeeded、partial、failed、interrupted；分阶段进度含 playback、extract、embed、event_finalize。
- 任何未生成有效帧、全部 Embedding 失败或事件生成失败的任务为 failed；有部分向量失败/最大帧数截断时为 partial，明确可用能力。
- 每视频同一时刻只允许一个索引任务；INDEX_CONCURRENCY 和 MAX_QUEUED_JOBS 限制后台并发/队列。
- 重建写独立 generation SQLite，成功时在业务事务中切换活跃版本和任务终态。失败不改旧版本；原片和已准备好的播放副本仍可用。
- 查询固定索引版本。旧图片链接重建后返回 stale_evidence，要求刷新，不静默指向同编号的新帧。
- 为避免在途引用损坏，成功的旧版本暂留到删除视频时统一清理；频繁重建需考虑磁盘容量。MVP 没有版本垃圾回收服务。
- 删除与播放下载、聊天/摘要、索引协调；忙碌时 409。删除失败有 delete_failed 状态，可再发 DELETE。启动恢复中断的删除与任务记录，不自动无限重试模型。
- 进程关闭时先等待已进入工作线程的操作收尾再清理，避免写盘与删除竞争；系统/模型超时决定最坏等待时间。
- 业务库 schema=1，FrameStore schema=2。已有无模型指纹的非空库不能安全推断模型，需备份并明确重建。新模型即使维度相同也不能静默复用旧向量。
- 事件代表帧由 Event 文档关联保护；memory_tier 专门按视频时间年龄计算。Summary 保持按需生成，不自动创建章节。

目录：

```text
DATA_ROOT/
  registry.sqlite3
  videos/v_<uuid>/
    source.<extension>
    playback.mp4              # 仅需要转码时生成
    memory/<generation>.sqlite3
    cache/<generation>/
```

## 测试与限制

```bash
python -m compileall -q vam tests
python -m pytest tests -q
```

自动测试使用真实 SQLite、合成音视频与 FFmpeg；模型调用被 Mock。不能由这些结果推断付费模型输入兼容性、课程问答准确率、费用或 ASR 效果。P1 已按用户最新要求整合到默认 main 分支；GitHub Ubuntu/Python 3.11 CPU 回归的 63 项测试全部通过。后续开发直接在 main 进行，原 PR #2 作为历史记录。

保留原 MIT LICENSE、原 VAM 说明与 TUI。P2 再扩展字幕表与 ASR，P3 再扩展统一证据与检索融合，P4/P5 按 plan.md 执行。
