# E2 交付：可靠异步处理与阶段续跑

## 1. 改造前核验（基线 `12a16ee`）

| 检查 | 结果 |
| --- | --- |
| E1 实测 | 本机真实 PostgreSQL 16.4 + MinIO 重跑 `test_e1_unit` + `test_e1_platform`：**24 passed**。`2466121` 的 CI（E1 platform、CPU tests）均为 success |
| E1 启动配置 | `deploy/e1/compose.yml`（PG + MinIO，固定版本 + 健康检查）、`scripts/windows/e1-local.ps1`。E2 在其上叠加 RabbitMQ 与应用容器 |
| 处理链路现状 | `IndexingService.schedule` 为每个任务建一个进程内 `asyncio` 任务。`_run` 依次跑视觉（`index_video_async`）和转写（`index_transcript`）。进度、终态写在 SQLite `index_jobs`；API 重启时 `recover()` 把所有未完成任务标为 interrupted，不会续跑 |
| 阻塞 E2 的 E1 缺口 | 无。E1 的 `UploadService.accept_processing → IndexingService.schedule` 是 E2 的受理入口。已验证的原片在对象存储里（`video_ownership.object_key/sha256`），Worker 通过对象引用读取原片 |
| 本机环境 | Windows 11，没有 WSL/Docker，CPU 虚拟化关闭。conda-forge 没有 Windows 版 RabbitMQ/Erlang。所以 RabbitMQ + Celery Worker 只在 Linux（GitHub Actions）上验证（见第 9 节） |

## 2. 执行架构

```
浏览器 ──> API（鉴权/校验/持久受理） ──同一事务──> PostgreSQL: jobs + job_stages + outbox_events
                                                     │
                       python -m vam.pipeline.dispatcher（发布器 + 恢复扫描，独立进程）
                                                     │ publisher confirm
                                                  RabbitMQ（durable 队列 media / asr / embed / control）
                                                     │ acks_late, prefetch=1
                            Celery Worker（长：media,asr ｜ 短：embed,control），各自独立进程
                                │ 认领=租约+attempt_token；产物=不可变对象键；提交=带 token 的条件更新
                     对象存储（音频、ASR 结果、Embedding 批次）＋ 共享 DATA_ROOT（已发布的每视频索引、SQLite 读模型）
```

- **API 只做受理**：worker 模式（`PIPELINE_MODE=worker`，仅平台模式可用）下，`schedule()` 调用 `PipelineClient.submit`，在一个事务里写入任务、阶段和第一条 outbox，然后把任务镜像到 SQLite `index_jobs`（UI 读模型），随即返回。API 进程不跑任何媒体或模型计算。
- **消息正文只有 `run_stage(job_id, stage)`**：视频、音频、向量都以对象键 + sha256 传递，Worker 读取时校验。
- **单 API + 独立 Worker**：保留 `OwnerLock`（只守护 API）；Worker 不持有该锁，只写按 attempt 命名的路径，并通过条件更新发布结果。多 API 实例仍未开放（第 11 节）。

## 3. 数据边界（E2 生效）

| 事实 | 唯一写入方 | 说明 |
| --- | --- | --- |
| 任务、阶段、尝试、租约、outbox、检查点 | PostgreSQL（迁移 `004_pipeline.sql`） | 正确性的唯一依据 |
| 阶段产物（音频、ASR 片段、Embedding 批次） | 对象存储：`pipeline/<video>/<job>/<stage>/<attempt_token>/…` | 不可变；sha256 记录在阶段输出或检查点中 |
| 已发布的每视频索引（`memory/<generation>.sqlite3`、`cache/<generation>/`） | Worker，写在共享 DATA_ROOT | 视觉代次 = vision 阶段的 attempt_token；转写版本按内容寻址 |
| 视频元数据、`active_generation`、转写状态 | SQLite registry；worker 模式下只由 Worker 的 publish/stage 写 | E2 不迁移到 PG（见第 11 节） |
| `index_jobs` | worker 模式下是 **PG 任务的读模型**（`project_job`），不是事实来源 | `/api/jobs/{id}` 以 PG 终态为准，进度细节来自读模型 |
| 会话/问答、笔记、导出 | SQLite + API 进程（未迁移） | 见第 8 节 |

## 4. 状态与租约协议（`vam/pipeline/repository.py`）

**任务**：`queued → running → succeeded | partial | failed`。
- `jobs_one_active` 唯一索引：每个视频最多一个未完成任务。
- `(workspace, request_id)` 唯一：同一请求 ID 返回同一个任务；参数摘要不同则返回 409 `request_conflict`。
- 上传受理固定使用 `upload:<upload_id>`，HTTP 请求用 `Idempotency-Key` 头，人工重试用 `retry:<job_id>`。

**阶段**：`waiting → pending → running → (retry_wait → running)* → succeeded | failed | skipped`。

| 转换 | 条件（数据库判定） | 同一事务内 |
| --- | --- | --- |
| 受理 | 无活动任务；request_id 未用或参数相同 | jobs + 全部阶段 + 无依赖阶段的 outbox |
| 认领 `claim` | 锁 job 行再锁阶段行；`pending`，或到期的 `retry_wait`，或 `running` 但租约已过期；且 `attempts < max` | 新 attempt_token、`lease_until`、心跳；写一条 `stage_attempts`；接管时把上一次尝试标为 `lease_lost` |
| 心跳 | `status='running' AND attempt_token=<mine>` | 延长租约；返回 0 行说明被接管 |
| 完成 `complete` | 同上（token 必须仍是当前值），否则视为 `lease_lost` 并丢弃结果 | 阶段输出 + 后继激活（`waiting→pending` 条件更新）+ 后继的 outbox；join 在 job 行锁下判定，只激活一次 |
| 失败 `fail` | 同上 | 临时错误：`retry_wait` + 延迟 outbox（指数退避 + 抖动）。永久错误或次数用尽：`failed`，依赖它的阶段 `skipped`，join 照常运行 |
| 扫描 `scan` | 租约过期且距上次投递超过一个租约；`pending` 但没有待发 outbox 且超过 `PIPELINE_REDISPATCH_AFTER_S` | 新的 outbox（`dedupe_key = job:stage:序号`）；次数用尽则失败 |

- **加锁顺序统一为“先 job 行、后阶段行”**，避免同一任务的并行阶段在汇合时死锁。
- **旧执行者回写**：完成或失败都要求 token 仍是当前值。已被接管的执行者得到 `lost`，它按 attempt 命名的产物会被删除（视觉代次），不会覆盖新结果。

## 5. 阶段与检查点（`vam/pipeline/stages.py`）

图（第二次验收后）：
- `video`：`prepare → {audio → asr → transcript_index, vision} → publish`
- `transcript`：同上，但没有 vision
- `notes`：`notes_draft → notes_publish`
- `export`：只有 `export` 一个阶段
- 没有 join 阶段的图（notes、export）在全部阶段到终态时结束。

| 阶段 | 队列 | 内容 | 恢复粒度 |
| --- | --- | --- | --- |
| `prepare` | media | 从对象存储取已验证原片、探测时长；视频任务生成播放文件（只写文件，`playback_path` 由 publish 发布）。原片无效则两个分支都失败（合理） | 阶段级 |
| `audio` | media | **语音分支专属**：抽取 16 kHz 音频并上传为产物；无音轨或 ASR 关闭时跳过 asr/transcript_index。音频故障只影响语音分支，视觉照常执行，任务为 partial | 阶段级 |
| `asr` | asr | 语音识别，输出原始片段 JSON | **检查点**：`asr:` + sha256(转写键 = 源 sha256 + ASR 身份 + 分块规则, 代码版本)。上传并校验后先登记检查点、再提交阶段。所以“检查点已登记、阶段未提交”时被杀，可以复用。更窄的窗口“对象已上传、检查点行未写入”时被杀会重新识别一次（有测试覆盖），未被引用的对象成为待清理的负债 |
| `transcript_index` | embed | 片段规范化（时间轴偏移、裁剪规则不变）→ 分块 → 按批 Embedding → 按内容寻址的转写版本发布 | **批次检查点**：`emb:` + sha256(Embedding 身份, 指令, 本批文本, 代码版本)。中断后只算缺的批次。最后一次尝试仍然失败时，与原行为一致：只发布文本，状态 partial |
| `vision` | media | 抽帧、筛选、图像 Embedding、事件整理（沿用 `index_video_async`），写入 `memory/<attempt_token>.sqlite3` | 阶段级（不可变代次） |
| `publish` | control | 汇合：两条分支都到终态后，**唯一一次**写入用户可见状态，并由任务序号 fencing（见下）；清理失败尝试的代次；需要时在同一提交事务里创建自动笔记任务 | 幂等重放（同值写入） |
| `notes_draft` | embed | 读取已发布的字幕/事件语料，按批调用笔记模型 | **批次检查点**：`notes:` + sha256(模型身份, 代码版本, 本批 evidence_id 与文本)。无效输出按可重试处理 |
| `notes_publish` | control | 写入 study_artifacts（确定性 id `a_<job_id>`，`created_at` 取任务创建时间），并更新 study_jobs 读模型 | 幂等 |
| `export` | control | 把笔记产物渲染为 md/pdf/pptx 并上传对象存储；`GET …/study/exports/{job_id}/download` 按引用下载，读取时校验 sha256 | 阶段级 |

**可见发布的 fencing（第二次验收 P1）**：
- **序号来源**：每个任务有单调递增的 `jobs.seq`（迁移 005）。
- **可见写入只在 publish 里发生**，分两处，各自带 fence：
  - SQLite registry：`publish_fenced` 在一个事务里写 `active_generation`、`playback_path`、视频状态、转写状态、任务读模型行，条件是 `videos.published_seq ≤ seq`。
  - 转写库：`publish_head` 移动 head，条件是 `transcript_fence.seq ≤ seq`。
- **效果**：旧任务的执行者晚于新任务恢复时，写入被拒（`published=false`），可见状态不会回退；同一任务的重放写入相同的值，从而修复 PG/SQLite 提交窗口。
- **其他阶段不写可见状态**：`transcript_index` 只完成版本，不移动 head；失败投影在账本接受失败之后才执行，也带 fence；Worker 的进度更新不会重新打开已终结的任务行。

- **检查点只指向已验证的完整产物**：上传后核对大小。读取时按 sha256 校验，不一致就删除检查点、重新计算。
- **partial 语义保留**：语音、视觉分支互不影响；失败分支不覆盖旧的已发布索引（`active_generation`、转写 head 都只在成功时移动）。
- **ASR 时间轴**：`asr` 只存原始片段和音频偏移。规范化、裁剪、分块仍走 `normalize_segments`/`make_chunks`，规则版本是复用键的一部分。没有对音频做二次切片，所以不存在分片重叠的问题。

## 6. 队列语义与实测配置（`vam/pipeline/celery_app.py`）

| 配置 | 作用 | 验证 |
| --- | --- | --- |
| `task_acks_late=True` | 在 `execute()` 返回（账本已提交）之后才 ACK | 杀掉整个 Worker 后，未确认的消息会重投（`test_killing_the_whole_worker_mid_stage…`） |
| `task_reject_on_worker_lost=True` | pool 子进程被杀时消息重新入队，而不是被 ACK | “提交后、ACK 前退出”能重投，且重投是 no-op（`test_asr_result_is_kept…`，日志中有 WorkerLostError） |
| `worker_prefetch_multiplier=1`，长短队列分开 Worker | 长任务不囤积消息 | compose 中分为 `worker-long`（media,asr）和 `worker-short`（embed,control） |
| `task_soft_time_limit` = 硬限制 − min(60, 10%) | 软超时在任务内抛出，记为可重试的 `stage_timeout`；硬超时作兜底 | `test_long_stage_hits_the_soft_time_limit…` |
| RabbitMQ `consumer_timeout = 4 h` | 必须大于最长阶段（硬限制默认 3 h），否则长投递会被 broker 关闭 | CI 启动时读取生效值并断言 |
| `confirm_publish=True`，`task_publish_retry=False` | 发布器只在 broker 确认后才把 outbox 标为 sent；失败则按退避重试 | 发布器崩溃窗口的测试 |
| 不使用 Celery 重试 | 重试次数、退避、失败台账全部在 PG | — |

**结论（不夸大）**：队列提供的是至少一次投递。重复投递靠认领、条件提交和唯一约束吸收，保证“效果只发布一次”。队列本身不提供业务严格一次执行。

**外部模型计费边界**：模型已经返回、但检查点或阶段未提交时被杀，重试会再调用一次（再计费）。
- 缓解措施：检查点粒度（ASR 整段、Embedding 批次）、`PIPELINE_STAGE_MAX_ATTEMPTS` 上限、失败台账。
- 没有供应商级幂等键，无法承诺计费严格一次。

## 7. 重试、失败台账、人工重试、恢复扫描

- **分类**（`runner.classify`）：
  - 可重试：对象存储/网络/超时/连接错误、HTTP 408/409/425/429/5xx、软超时。
  - 永久：媒体无效等 4xx 类 `DomainError`、产物校验失败、空索引。
  - 未知异常：按可重试处理，但受次数上限约束。
- **退避**：`min(cap, base·2^(n−1))`，取一半固定、一半随机（equal jitter）。延迟写在 outbox 的 `next_attempt_at` 上。
- **失败台账**：`stage_attempts`（每次尝试一行：结果为 succeeded/retry/failed/lease_lost，附错误码）。`GET /api/pipeline/failures` 按调用者空间过滤。
- **人工重试**：`POST /api/jobs/{id}/retry`，只允许 failed/partial 任务，`retry:<id>` 保证幂等。新任务会复用已验证的检查点。
- **恢复扫描**：发布器进程每 5 次轮询扫描一次（见第 4 节）。消息只是提示，扫描才是保证进度的机制。
  - 无锁读出的候选只是线索。持锁后会重新核对：仍在运行、attempt_token 相同、租约仍已过期、未被再次投递，全部满足才动作。所以候选读出之后发生的续租或接管不会被扫描否决（第二次验收 P2）。
- **孤儿对象清理**（`vam/pipeline/sweep.py`，发布器每 `PIPELINE_SWEEP_INTERVAL_S` 执行一次）：`pipeline/` 前缀下的对象满足以下全部条件才会进入持久删除队列（`object_deletions`，原因 `pipeline_orphan`）：
  - 早于 `PIPELINE_ORPHAN_GRACE_S`（默认 1 h）；
  - 不被检查点引用；
  - 不被任何阶段输出引用；
  - 不属于仍在运行的尝试（attempt_token 是对象键的一部分）。

  覆盖的情形：上传后、登记检查点前崩溃；检查点竞争的落败者。

## 8. 兼容与未迁移项（如实记录）

- **单用户模式与 `PIPELINE_MODE=inline`**：行为与 P5/E1 相同，原有测试不变。
- **播放/Range、字幕、带来源问答、检索**：读取的仍是 SQLite 读模型和不可变索引代次；发布只切换指针，读者持有的旧代次不会被删除。
- **问答（会话/消息）**：**本轮未迁移**，属需求允许的选择。仍在 API 进程内执行，并保留 P4 的 `conversation_requests` 幂等与终态持久化（SQLite）。浏览器关闭不取消已受理的请求（P4 行为）。
- **笔记批次、导出（第二次验收后已迁移）**：
  - 笔记：worker 模式下 `POST /api/videos/{id}/study` 持久受理为 `notes` 任务（请求 id `notes:<request_id>`，与原接口一样去重），由 Worker 按批执行。
  - 自动笔记：在 publish 的提交事务里创建（`auto-notes:<job_id>`），条件是 `STUDY_AUTO_GENERATE` 打开且已配置模型。
  - 导出：`POST /api/videos/{id}/study/artifacts/{aid}/exports?format=` 创建导出任务；原有的同步导出接口保留。
  - 读模型：study_jobs/study_artifacts 是 PG 笔记任务的读模型，API 重启不再中断笔记任务。
- **视觉阶段**：仍是一个整体阶段（阶段级恢复）。帧级批次检查点没有做。

## 9. 启动与演示

**Linux / WSL2（推荐，完整栈）**：
```bash
cp deploy/e1/dev.env.example deploy/e1/dev.env && cat deploy/e2/dev.env.example >> deploy/e1/dev.env   # 改成自己的开发口令
docker compose --env-file deploy/e1/dev.env -f deploy/e2/compose.yml up -d --build --wait
# 浏览器 http://127.0.0.1:8000/app/ ；创建用户：
docker compose --env-file deploy/e1/dev.env -f deploy/e2/compose.yml exec api \
  sh -c 'EVIREEL_NEW_PASSWORD=... python -m vam.platform.migrate create-user --username demo'
```
服务包括：`migrate`（一次性 schema）→ `api`、`dispatcher`、`worker-long`（media,asr）、`worker-short`（embed,control）、`rabbitmq`（4.1.4，management 在 127.0.0.1:15672）。

**故障演示**（与测试相同的注入点）：
- 给 Worker 设置 `VAM_PIPELINE_FAULT=after_asr_upload`（或 `before_commit:vision`、`after_commit:asr`、`stall:vision`）后重启，提交一个视频，观察 `/api/jobs/{id}` 中 `pipeline.attempts` 的 `lease_lost → succeeded`。
- 给发布器设置 `dispatcher_before_publish` 或 `dispatcher_after_publish`。
- 用 `docker compose stop rabbitmq` 模拟 broker 中断。

**Windows 本机**：只能运行 API（inline 或 worker 受理）和 PG/MinIO（`scripts/windows/e1-local.ps1`）。Celery Worker 不支持原生 Windows，本机不作为可靠性依据。

## 10. 迁移与回退

- **迁移**：`python -m vam.platform.migrate schema` 应用两份迁移，schema 版本为 5：
  - `004_pipeline.sql`：只新建 5 张表；
  - `005_pipeline_fencing.sql`：新增 `jobs.seq`，扩展任务种类、阶段、删除原因的约束，导出任务不计入“每个视频只能有一个活动任务”。

  SQLite registry 自动升级到版本 5（`videos.published_seq`，默认 0）；转写库新增 `transcript_fence` 表（`IF NOT EXISTS`）。
- **启用**：设置 `PIPELINE_MODE=worker` 和 `BROKER_URL`，启动 dispatcher 与 Worker。已有视频、已发布的索引和 SQLite 历史任务不受影响。
- **回退**：
  1. 停止受理，等待 `jobs` 中没有 queued/running 的任务（或把它们置为 failed）。
  2. 设置 `PIPELINE_MODE=inline`，重启 API（回到进程内执行）。
  3. 若要回退到 v3 代码，再删除 `checkpoints`、`outbox_events`、`stage_attempts`、`job_stages`、`jobs` 表；把 `object_deletions_reason_check` 改回原值（删除 `pipeline_orphan` 记录后）；移除 `schema_migrations` 中版本 4、5 的记录（旧代码会拒绝比自己新的 schema）。
  4. SQLite registry 版本 5 只多一列，旧代码会拒绝版本 5。回退前需要先备份，再在 SQLite 中删除该列（`ALTER TABLE videos DROP COLUMN published_seq`），并把 `user_version` 设回 4。
- 对象存储中 `pipeline/` 前缀下的产物可以整体删除；检查点删除后会重新计算。

## 11. 已知限制（交给 E3）

- **仍是单 API**（`OwnerLock`）。进程内状态仍有：`IndexingService.leases/memory_locks`（读者租约与检索锁）、`UploadService.tasks`（上传验证）、`ConversationService.tasks`、`StudyService.tasks`。这些都没有跨进程化，不能直接开多 API。
- **Worker 与 API 必须共享 DATA_ROOT**（单机卷）。SQLite registry 仍保存视频元数据和已发布指针。多机部署需要先把读模型和发布指针迁入 PG，并把索引代次放进对象存储。
- **视觉阶段不能细粒度续跑**；问答未迁移（第 8 节）。
- **旧索引代次没有自动回收**（与 P 系列行为相同，保守保留）。失败尝试的代次会在 publish 时清理，对象存储中的孤儿产物由清理扫描回收。已成功任务的阶段产物（音频、ASR、Embedding 批次）被阶段输出引用，会一直保留；保留期策略交给 E3。
- **首次写入 `transcript_generation`**（只在该值为空时写一次，指向的文件此时还没有可读的 head）发生在 transcript_index 中，不受 fence 约束。它不会改变已发布的内容。

## 12. 交接给 E3

- **状态转换与租约协议**：第 4 节；代码 `vam/pipeline/repository.py`，测试 `tests/test_e2_pipeline.py`。
- **队列配置**：第 6 节；`vam/pipeline/celery_app.py`、`deploy/e2/rabbitmq.conf`、`deploy/e2/compose.yml`。
- **索引版本**：视觉代次 = vision 的 attempt_token；转写版本 = sha256(转写键, Embedding 身份)；检查点键见第 5 节。
- **清理规则**：失败或被接管的视觉代次在 runner 或 publish 时删除；已发布代次不删；检查点只在校验失败时删除。
- **尚存的进程内状态**：第 11 节。
- **真实故障测试结果**：见 `STATUS.md`（GitHub Actions：`e2-pipeline.yml`）。
