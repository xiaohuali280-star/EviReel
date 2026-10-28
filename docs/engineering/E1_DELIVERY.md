# E1 交付：分片直传、断点续传与平台元数据

## 1. 改造前核验（基线 `3ede061`）

| 位置 | 现状 | 对 E1 的影响 |
| --- | --- | --- |
| `IndexingService.ingest` | 单请求读完整 multipart，写 `DATA_ROOT/.uploads/*.part`，`inspect_upload`（文件头 + ffprobe）后移入 `videos/<id>/source.ext`，注册并 `schedule` | 无会话、无分片、断开即重传；保留为单用户模式兼容路径 |
| `UploadLimitMiddleware` | 只限制 `POST /api/videos` 的请求体 | 新协议的字节不经过 API，此中间件不涉及 |
| `VideoRegistry`（SQLite `registry.sqlite3`，schema v4） | videos、index_jobs、conversations、messages、study_* 共用一个库；`video_id` 由服务端生成 | 本轮不迁移；用 `video_id` 与 PG 关联 |
| 播放 `/api/videos/{id}/content` | 本地文件 `FileResponse`（Range 由 Starlette 处理），租约计数防并发删除 | 新上传在验证阶段落地本地工作副本，播放链路不变 |
| 前端 `upload()` | 单个 XHR 上传整个文件 | 新增续传客户端，按 `ui-config.upload_mode` 切换 |
| 进程模型 | `OwnerLock` 独占 `DATA_ROOT`；索引任务为进程内 asyncio 任务，重启时标记 interrupted | E1 保留单实例约束 |

## 2. 数据存储边界（E1 生效）

| 事实 | 唯一写入方 | 说明 |
| --- | --- | --- |
| 用户、学习空间、成员、登录会话 | PostgreSQL | 新增 |
| 资源归属 `video_ownership`（video_id → workspace） | PostgreSQL | 新增；平台模式下所有视频访问的授权依据 |
| 存储配额（quota/used/reserved） | PostgreSQL | 新增 |
| 上传会话与分片账本 | PostgreSQL | 新增 |
| 视频元数据、索引任务、会话/消息、笔记产物 | SQLite registry | **本轮不迁移**，E2 迁移 |
| 每视频帧/字幕索引（`videos/<id>/memory/*.sqlite3`） | 本地文件 | 暂留，不改向量检索 |
| 续传上传的已验证原片 | 对象存储 | 新增；本地 `source.ext` 是处理用工作副本 |

同一类事实只有一个写入方。两库之间唯一的跨库步骤是“验证通过 → 先写 SQLite `videos` 行 → 再在 PG 一个事务内完成会话、配额结算和归属”。中间崩溃时，SQLite 行存在但没有归属，因此对任何用户不可见；协调器按会话状态幂等补齐。删除视频是相反顺序：先删 SQLite，再释放归属并删除对象；残留的归属由协调器释放。

## 3. 关键设计

### 3.1 对象存储适配层 `vam/platform/object_store.py`
- 接口：`ObjectStore`（create/presign/list_parts/complete/abort/list_multipart_uploads/head/iter_object/delete）。
- 实现：`S3ObjectStore`（boto3，SigV4，path-style）。签名客户端使用 `S3_PUBLIC_ENDPOINT`（浏览器可达的地址），服务端客户端使用 `S3_ENDPOINT`。
- 预签名 `UploadPart` 把 `Content-MD5` 纳入签名：传输中字节损坏时存储返回 `BadDigest`；篡改 MD5 头会导致签名不匹配（403）。

### 3.2 上传状态机 `vam/platform/uploads.py`
`initiated → uploading → completing → verifying → completed`；终止态 `failed / aborted / expired`。
- 所有转换都是带当前状态条件的 `UPDATE`；验证阶段另带 `state_version`（attempt token）。
- **进度以存储为准**：`GET /api/uploads/{id}` 调用 ListParts 回写 `upload_parts`。客户端的 `/parts/{n}/done` 只是可丢失的提示。
- **complete 幂等**：只有把 `uploading→completing` 改成功的请求执行合并；其余请求返回当前会话视图（`completed` 时返回 200 和同一个 `result`）。`video_id` 在建会话时就固定，所以不会产生第二个逻辑视频。
- **合并与验证分离**：合并（completing）后进入 verifying，由后台任务流式下载对象：服务端计算 SHA-256 与大小，与声明大小比对；用各分片 MD5 计算 S3 复合 ETag 并比对（这个值不是整文件 MD5，只用于确认组装的是签过名的分片）；最后跑 `inspect_upload`。全部通过才注册视频并进入现有处理链路。失败时状态带错误码（`invalid_media`、`size_mismatch`、`integrity_mismatch`、`duration_limit`…），同时删除对象、释放配额。
- **合并成功、DB 失败**：会话停在 completing。租约过期后，`reconcile()` 再次调用 ListParts（返回 NoSuchUpload），转而 `head_object` 确认对象已组装，然后进入 verifying，无需重传。验证中断（进程退出）同理：租约过期后重新认领，attempt token 阻止旧执行者发布结果。
- **一次处理受理**：完成后 `accept_processing` 先查 SQLite 是否已有该视频的索引任务，没有才 `schedule`，再以 `job_id IS NULL` 条件写回 PG。同一事件循环内检查与调度之间没有 await，SQLite 的单活动任务唯一索引兜底。
- **配额**：建会话时对 workspace 行加 `FOR UPDATE`，原子检查 `used+reserved+size ≤ quota` 并预占；结算（completed）或释放（aborted/expired/failed）与状态转换在同一事务中，只发生一次。活动会话数也有上限。
- **取消/过期与完成竞争**：三者都要求当前状态为 `uploading`（行锁 + 条件更新），数据库决定唯一赢家。过期转换先提交，提交后再中止 multipart 上传。
- **清理**：协调器周期执行，处理过期会话、卡住的 initiated、无会话的孤儿 multipart（超过租约宽限）、以及 SQLite 已删除的孤儿归属。

### 3.3 身份与授权 `vam/platform/identity.py`、`vam/server/platform_auth.py`
- 用户名 + scrypt 密码哈希；登录后签发随机令牌。数据库只存 SHA-256；令牌放在 HttpOnly、SameSite=Lax Cookie 中，也支持 Bearer。请求体不接受 user_id/workspace_id（StrictSchemaModel 会拒绝多余字段）。
- 一个 ASGI 中间件先认证所有 `/api/*`（`/api/auth/login`、`/api/ui-config`、`/api/health` 除外），并在进入路由前，按路径把 video、job、conversation 解析到所属视频，再做归属检查。他人资源与不存在的资源同样返回 404。按请求体寻址的资源（新建会话、WebSocket 请求）显式调用 `authorize_video`。WebSocket 握手未认证时直接拒绝。
- 平台模式下禁用旧的 `POST /api/videos`（410 `use_resumable_upload`）和 `ENABLE_LEGACY_HTTP_API`，因为后者没有按空间授权。

### 3.4 前端 `frontend/src/resumable-upload.ts`
- MD5 在 Web Worker 中计算（Web Crypto 不支持 MD5，库内实现，有测试向量）。默认 3 路并发；网络错误按指数退避加抖动重试；403 时重签；`BadDigest` 时重新读取分片后重传。暂停用 AbortController 中止进行中的请求。
- 文件身份指纹 `fp1` = SHA-256(大小 + 首尾各 1 MiB + 3 处 64 KiB 采样)。只用于判断“重选的是不是同一个文件”，可信校验在服务端完成。
- 刷新或重开浏览器后，从 `GET /api/uploads` 列出未完成会话，提示重新选择文件；名称、大小、指纹不符时拒绝，且不发送任何字节。
- 三阶段分开显示：上传分片 → 服务器校验 → 视频处理（沿用现有处理中界面）。

### 3.5 迁移工具 `python -m vam.platform.migrate`
`schema [--dry-run]`、`create-user`、`set-quota`、`backfill-legacy [--dry-run]`、`check`、`rollback-legacy [--dry-run]`。SQLite 以只读 URI 打开，工具不移动、不修改现有视频文件和行。回填只新增 `origin='legacy'` 的归属，回退只删除这些行。

## 4. 启动方式

### 4.1 单用户模式（默认，与 P5 相同）
不设置 `DATABASE_URL`；`vam-server`。

### 4.2 平台模式（Linux / WSL2 容器）
```bash
cp deploy/e1/dev.env.example deploy/e1/dev.env   # 修改为自己的开发口令
docker compose --env-file deploy/e1/dev.env -f deploy/e1/compose.yml up -d --wait   # 首次会构建 MinIO 镜像
pip install -e ".[dev,platform]"
export DATABASE_URL=postgresql://evireel:<pw>@127.0.0.1:5432/evireel S3_ENDPOINT=http://127.0.0.1:9000 \
       S3_PUBLIC_ENDPOINT=http://127.0.0.1:9000 S3_ACCESS_KEY=<key> S3_SECRET_KEY=<secret> S3_BUCKET=evireel
python -m vam.platform.migrate schema
python -m vam.platform.migrate create-user --username alice     # 口令从 EVIREEL_NEW_PASSWORD 或交互输入
vam-server   # 浏览器打开 http://127.0.0.1:8000/app/
```
`minio/minio` 已从 Docker Hub 下架，因此 compose 用 `deploy/e1/minio/Dockerfile` 在本地构建镜像：`debian:12.12-slim`，加上 conda-forge 的 `minio-server-2025.10.15.17.29.55-hbcca054_0`（按 sha256 校验）。这个版本与 Windows 原生环境相同。健康检查请求 `/minio/health/ready`。

### 4.3 Windows 原生（无 Docker/WSL2，本机采用）
二进制只放在 `.local-infra/`（git 忽略）：PostgreSQL 16.4（EDB zip，只用 bin/lib/share）、MinIO `RELEASE.2025-10-15T17-29-55Z`、FFmpeg 7.1.1（均来自 conda-forge）、Python 3.11（conda-forge，`.venv` 基于它创建）。入口是 `scripts/windows/e1-local.ps1 -Action up|test|serve|down`，PG 端口 54329，MinIO 端口 9100。开发口令自动生成在 `.local-infra/dev-credentials.env`，脚本不会打印它。`-Action test` 依次运行全量 pytest、前端 typecheck/vitest/build、既有 P5 浏览器回归和 E1 浏览器验收，日志写入 `e1-local-logs/`，汇总写入 `summary.txt`。

### 4.4 迁移已有数据（先在副本上预演）
```bash
cp -r data/evireel /tmp/evireel-copy          # 不要直接对真实 DATA_ROOT 预演
python -m vam.platform.migrate backfill-legacy --data-root /tmp/evireel-copy --username alice --dry-run
python -m vam.platform.migrate backfill-legacy --data-root /tmp/evireel-copy --username alice
python -m vam.platform.migrate check --data-root /tmp/evireel-copy
python -m vam.platform.migrate rollback-legacy --workspace <ws_id> [--dry-run]
```
回退条件：`check` 报告 drift/孤儿，或平台模式下旧视频不可用。回退方式：先执行 `rollback-legacy`，再去掉 `DATABASE_URL` 回到单用户模式。SQLite 和视频文件始终未被修改。

## 5. 生产要求（与开发配置的区别）
托管或加固的 PostgreSQL 与 S3（备份、TLS、最小权限访问密钥、桶生命周期规则清理未完成分片）；`AUTH_COOKIE_SECURE=true` 并全站 HTTPS；密钥由密钥管理服务注入，不使用 `.env`；按真实前端域名配置存储 CORS。MinIO 社区版已于 2025 年停止发布二进制和 Docker 镜像（`minio/minio` 已从 Docker Hub 下架），生产前需重新评估所用的 S3 实现。

## 6. 验证记录
完整记录见 `STATUS.md`。那里只记录实际运行的结果，并把真实中间件、测试替身和浏览器测试分开。2026-10-28 本机（Windows 原生、真实 PostgreSQL 16.4 + MinIO）的结果摘要：

| 类别 | 内容 | 结果 |
| --- | --- | --- |
| 真实中间件 | `tests/test_e1_platform.py` 14 个场景，覆盖第二节验收 1–5 和第一节的迁移/回退 | 14/14 通过（签名过期用例修复了测试中 TTL 过短导致的偶发失败） |
| 真实中间件 + 浏览器 | `frontend/e2e/upload-resume.spec.ts`（Chromium，平台模式） | 2/2 通过 |
| 测试替身 | `resumable-upload.test.ts`（内存假存储）、`test_e1_unit.py` | 通过 |
| 回归（验收 6） | 全量 pytest；P5 浏览器回归 21 个用例（单用户模式，mock provider） | pytest 234 passed / 0 skipped；P5 复跑 21/21 通过（首轮有 1 次未复现的卡顿，见 STATUS） |
| CI（Linux） | `4632b4f`：`E1 platform`（compose 中的真实 PG + MinIO，后端验收 + 浏览器验收）与 `EviReel CPU tests` | 均 success |
| 未运行 | Linux compose 只在 CI 上运行过，本机无 Docker；真实模型调用 | — |

## 7. 交接给 E2
- **接口**：`UploadService`（create/get/sign/complete/cancel/reconcile/accept_processing），`ObjectStore`（见 3.1）。
- **任务受理入口**：`UploadService.accept_processing → IndexingService.schedule`。E2 应改为同一事务写入任务行和 outbox。
- **原片验证产物**：对象存储 `workspaces/<ws>/uploads/<upload_id>/source.ext`、`upload_sessions.verified_sha256/verified_size`，以及本地工作副本 `videos/<id>/source.ext`。E2 的 Worker 应直接从对象存储读取，并以 sha256 作为阶段输入摘要。
- **仍在进程内的部分**：`IndexingService.tasks/leases/memory_locks/semaphore`、`UploadService.tasks`（验证）、`ConversationService.tasks`、`StudyService`、`OwnerLock`。
- **需要替换的 SQLite 写入**：`VideoRegistry.register/update_video/create_job/finish/publish/recover`、`ConversationStore`、`StudyService` 的作业与产物写入。
