# 工程状态

更新：2026-10-28　阶段：**E1 两轮独立验收发现的 4 项缺陷均已修复，本机回归通过**；修复提交的 CI 结果见下文；内容秒传未做

## 当前提交
基线 `3ede061`；E1 实现 `2991d2b`；本机验证修复 `4632b4f`；第一次独立验收（目标 `be66e7d`）整改 `04122c8`；第二次独立验收（目标 `55d4271`）整改见其后的提交（`git log`）。

## 第二次独立验收整改（`55d4271` 之后）
复验确认前 3 项已修复，又发现 1 项 [P2]：会话尚未建立（正在计算指纹或等待创建响应）时点取消，前端立即显示“已取消”，但创建请求返回后 `run()` 没有检查取消意图，继续签名并上传（复现：签名 1、PUT 1、DELETE 0，状态 hashing → cancelled → uploading）。

| 修复 | 说明 |
| --- | --- |
| 幂等创建（迁移 `003_upload_request_id.sql`） | 前端为每次新上传生成随机 `client_request_id`；服务端在锁定空间行后，按（空间, request id）返回已有会话，不重复预占配额。响应丢失时可以重放同一个 id 查明结果 |
| 取消先确认，再宣布 | 创建在途时，`cancel()` 等它出结果：服务端返回错误说明没有创建；拿到会话就补发 `DELETE`；完全没有回音时重放创建，查到会话后再 `DELETE`；仍然无法确认则报告 `cancel_unconfirmed`，可以重试 |
| 每个 await 之后检查是否被取消 | `run()` 在指纹、创建、读取会话、核对分片之后以及启动分片之前，检查中止信号和代次；被取消或被新一轮替代就停止，不再签名、上传或改动界面。启动分片时显式检查信号是否已中止（给已中止的信号添加监听不会触发回调） |

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 测试替身 | 新增 4 个场景：指纹计算中取消（0 POST / 0 签名 / 0 PUT）；创建响应等待中取消（会话被 DELETE，0 签名 / 0 PUT，取消前不宣布“已取消”）；创建响应丢失（重放同一 id，只创建 1 个会话，随后取消）；结果无法确认（报告 unconfirmed，重试后取消成功）。`resumable-upload.test.ts` 共 15 个 | **15/15 通过** |
| 测试替身 | 用验收报告自带的复现脚本跑修复后的代码。新 `cancel()` 会等待在途的创建，所以只把“先 await 取消、再放行响应”的顺序改为“先发起取消、再放行、再 await” | 取消结果 cancelled；签名 0、PUT 0、DELETE 1；状态 hashing → cancelled |
| 真实中间件 | 新增：同一 request id 并发 4 次只生成 1 个会话、只预占 1 次；取消后重放返回 aborted 的原会话，不新建；新 id 新建；非法 id 返回 422；不同空间的同一 id 互不影响。`test_e1_unit` + `test_e1_platform` | **23 passed**（146 s） |
| 回归 | `e1-local.ps1 -Action test` 一次完整运行：全量 pytest（含真实 PG+MinIO）、前端 typecheck/vitest/build、P5 浏览器回归（21 个用例）、E1 浏览器验收（真实 PG+MinIO） | pytest **238 passed, 0 skipped**；typecheck/build 通过；vitest **34/34**；P5 **21/21**；E1 浏览器 **2/2** |
| 未运行 | 浏览器端到端没有专门构造“创建响应在途时取消”（时序由测试替身覆盖）；Linux compose 本机无 Docker | — |

## 第一次独立验收整改（`be66e7d` 之后）
验收报告结论是“不建议无条件验收通过”，共 3 项 Spec 缺陷和 1 项非阻塞的规范建议。处理如下：

| 发现 | 修复 | 回归测试 |
| --- | --- | --- |
| [P1] 重选文件只核对采样指纹，同大小文件在非采样区被修改时仍能续传，可能组装成“旧已传分片 + 新剩余分片” | 会话视图新增 `uploaded_part_md5`（存储确认分片的签名 MD5）。前端续传前用重选文件逐片计算已上传分片的 MD5，任一不符即 `file_mismatch`，不签名也不发送 | 测试替身：20 MiB 文件在 2 MiB 和 12.5 MiB 处修改，采样指纹相同但被拒绝，0 次 PUT、0 次签名；原文件仍只补传缺失片。真实 PG+MinIO：视图中的 MD5 等于实际存储分片的 MD5。浏览器（真实 PG+MinIO）：在已上传分片 1 内翻转一个字节、保持大小和全部采样，被拒绝且 0 次 PUT、0 次签名 |
| [P2] 对象删除失败只记日志，没有持久补偿（验证失败、视频永久删除） | 迁移 `002_object_deletions.sql` 加 `vam/platform/cleanup.py`：在使对象不可达的同一事务内登记待删除；提交后立即尝试，失败按退避由协调器重试，跨进程重启继续执行；确认删除后标 `done`；仍被引用的对象标 `skipped` 且绝不删除。配额在逻辑删除时释放，`migrate check` 报告积压 | 真实 PG+MinIO，在真实 store 外包一层注入 DeleteObject 故障：① 删除视频时存储不可用 → 204、归属与配额已释放、对象仍在、记录 pending；重启后仍不可用 → 重试，attempts=2；存储恢复后再启动 → 对象删除、记录 done，再次协调为幂等。② 验证失败（非视频）时删除失败 → pending，恢复后清理。③ 仍被归属引用的对象即使误入队也不会删除 |
| [P2] 取消请求失败（网络错误或 409 完成竞争）时，前端仍显示“已取消” | `cancel()` 返回 `cancelled / unconfirmed / continuing`。网络失败时进入 `cancel_unconfirmed`（提示会话可能仍占配额，可重试取消或继续上传）。409 `upload_not_cancellable` 时跟随服务端的真实阶段直到完成 | 测试替身：DELETE 网络失败 → `unconfirmed`，没有出现 `cancelled`，服务端仍是 uploading；重试 → `cancelled`。DELETE 时已完成 → `continuing`，`wait()` 得到 completed，没有出现 `cancelled` |
| [Standards，非阻塞] `UploadService` 职责较多 | 清理已拆到 `cleanup.py`；验证执行单元的拆分留给 E2（见 E1_DELIVERY 第 7 节） | — |

### 整改后的验证（2026-10-28，本机 Windows 原生，同一环境）
| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 真实中间件 | `pytest tests/test_e1_unit.py tests/test_e1_platform.py`（PostgreSQL 16.4 + MinIO `RELEASE.2025-10-15T17-29-55Z`；17 个平台场景，含 3 个新增） | **22 passed**（122 s） |
| 真实中间件 | 全量 `pytest tests`（`e1-local.ps1 -Action test`） | **237 passed, 0 skipped**（327 s） |
| 真实中间件 + 浏览器 | `upload-resume.spec.ts`（Chromium，平台模式，新增“修改过的同名同大小文件”步骤） | **2/2 通过** |
| 测试替身 | vitest（`resumable-upload.test.ts` 11 个场景，含 3 个新增；共 5 个文件） | **30/30 通过** |
| 回归 | 前端 typecheck / build | 通过 |
| 回归 | 既有 P5 浏览器回归（单用户模式，21 个用例，mock provider） | 完整运行中 `panels.spec.ts:67` 失败 1 次：视口缩小后没有轮询，直接读取比例，读到了重新钳制前的 75。单独重复 3 次 9/9 通过。改为 `expect.poll`（只改测试）后完整复跑 **21/21 通过** |
| GitHub CI（Linux，`04122c8`） | `E1 platform` run 40695779090（compose 中的真实 PG + MinIO：后端验收 + 浏览器验收）；`EviReel CPU tests` run 40695779209 | 均 **success** |
| 未运行 | Linux compose 本机无 Docker（只在 CI 上运行）；真实模型调用 | — |

以下“验证记录”和“本轮修复”是 `4632b4f` 时的结果，保留作为历史。

## 已完成（代码层面）
- PostgreSQL 平台 schema 与迁移器、身份/空间/归属/配额、上传会话状态机与协调器、S3 适配层。
- 上传 API（`/api/uploads*`）、登录 API、认证与授权中间件、视频列表按空间过滤、删除时释放配额。
- 前端：登录页、续传客户端（Worker 计算 MD5、并发、重试、重签、暂停/继续、刷新后恢复与文件身份校验）、三阶段状态。
- 迁移工具（dry-run / backfill / check / rollback）。
- Linux/WSL2 compose（固定版本、健康检查）、Windows 原生运行脚本、CI 工作流 `e1-platform.yml`。

## 验证记录（2026-10-28，本机 Windows 11 原生，无 Docker/WSL2）

环境：PostgreSQL 16.4（EDB 二进制，端口 54329）、MinIO `RELEASE.2025-10-15T17-29-55Z`（conda-forge，端口 9100）、FFmpeg 7.1.1、Python 3.11.16（conda-forge）+ torch 2.14.1+cpu、Node + pnpm 11.25.0、Playwright Chromium 1248。入口：`scripts/windows/e1-local.ps1 -Action up|test`，日志在 `e1-local-logs/`（git 忽略）。

### A. 真实中间件（PostgreSQL + MinIO）
| 检查 | 结果 |
| --- | --- |
| `tests/test_e1_platform.py`（14 个场景：续传只补缺失分片且字节一致、进度登记丢失后与存储对账、并发/重复 complete 只生成一个视频和一个任务、合并成功但 DB 失败后恢复、验证中断后租约过期重领、损坏分片/MD5 签名不符、非视频内容、签名过期与重签、跨用户隔离、并发会话配额、取消与完成竞争、过期清理、旧数据副本迁移与回退、删除释放配额和对象） | 第 1 轮 14/14 通过；第 2 轮“签名过期与重签”失败（见“本轮修复” 2），修复后该用例连续 3 次通过 |
| 全量 `pytest tests`（含上述 E1 用例与既有后端测试） | 第 1 轮 **234 passed, 0 skipped**；第 2 轮 233 passed / 1 failed（同上） |
| E1 浏览器验收 `frontend/e2e/upload-resume.spec.ts`（Chromium，`tests/e1_browser_server.py`，平台模式） | **2/2 通过**：中断 → 刷新 → 重选同一文件 → 重签 → 只补缺失分片 → 字节一致；暂停/继续 + 浏览器内跨用户隔离 |

### B. 测试替身（只证明对应逻辑，不代表中间件行为）
| 检查 | 结果 |
| --- | --- |
| `frontend/src/resumable-upload.test.ts`（内存假存储，8 个场景）及其余前端单测 | vitest 5 个文件 / 27 个用例全部通过 |
| `tests/test_e1_unit.py`（纯逻辑：分片规划、复合 ETag、MD5 头、口令哈希） | 5 passed（已包含在 A 的全量 pytest 中） |
| 既有后端测试中的 mock 模型 provider | 包含在 A 的全量 pytest 中；没有调用真实模型 |

### C. 既有功能回归
| 检查 | 结果 |
| --- | --- |
| 前端 `typecheck` / `build` | 通过 |
| 既有 P5 浏览器回归（单用户模式，21 个用例，mock provider） | 完整运行 20 passed / 1 failed：`learning toggles preserve playback, draft and a running conversation` 在 `ingest` 阶段卡了约 29 分钟后，轮询学习产物超时。该文件单独复跑 3/3 通过（该用例 9.2 s）。完整复跑 **21/21 通过**（3.2 min）。那次卡顿没有复现，原 trace 已被单独复跑覆盖，根因未查明，记为未解释的偶发问题 |

### D. 未运行 / 未验证
- Linux compose（`deploy/e1/compose.yml`）：本机没有 Docker，**未在本机运行**；只在 CI（ubuntu-latest）上运行过，见 E。
- `2991d2b` 的 CI `E1 platform` 失败（run 40674376201，第 4 步 compose 启动）：`minio/minio` 与 `minio/mc` 已从 Docker Hub 下架（仓库 404，`latest` 同样 404），镜像无法拉取。已改为基于 sha256 固定的 conda-forge 包构建本地镜像，在 `4632b4f` 的 CI 上验证通过（见 E）。
- 真实模型调用：没有预算，未运行。
- 内容秒传：未实现，列为下一步。

### E. GitHub CI（`4632b4f`，Linux）
| 工作流 | 结果 |
| --- | --- |
| `E1 platform` run 40684790364：compose 构建并通过健康检查 → schema dry-run → `test_e1_unit` + `test_e1_platform`（真实 PG + MinIO 容器）→ 前端 typecheck/vitest/build → E1 浏览器验收 | **success** |
| `EviReel CPU tests` run 40684790380：既有后端、前端与 P5 浏览器回归 | **success** |

## 本轮修复（`2991d2b` 之后）
1. `e1-local.ps1 -Action up` 卡住：`pg_ctl start | Out-Null` 时，postgres 子进程继承了 PowerShell 管道句柄，管道一直不关闭。改为通过 `cmd /c ... >nul 2>&1` 调用。
2. `test_expired_signature_is_rejected_and_resigning_works` 偶发失败：`X-Amz-Date` 按整秒截断，TTL=1 s 时重签 URL 的实际有效期可能接近 0。TTL 改为 3 s，等待 4.5 s。
3. 前端检查全部只打印 pnpm 帮助：Windows PowerShell 5.1 经 `npm.ps1` 转发时，函数里展开的 `-- @args` 参数会丢失。改用 `npm.cmd`。
4. CI compose：MinIO 镜像改为 `deploy/e1/minio/Dockerfile`（`debian:12.12-slim` + conda-forge `minio-server-2025.10.15.17.29.55`，sha256 校验）。健康检查改为 `curl -fsS http://127.0.0.1:9000/minio/health/ready`。现在 compose 与 Windows 原生使用同一 MinIO 版本。
5. `2991d2b` 提交的内置前端 `vam/server/static` 不是用 E1 前端构建的（缺少 `md5.worker`），`vam-server` 直接提供的页面没有登录和续传界面。本次用 `pnpm run build` 重新生成并提交。

## 启动、迁移与回退
见 `E1_DELIVERY.md` 第 4 节。

## 已知限制
- 单 API 实例（`OwnerLock`）；验证与受理任务在进程内执行，依靠租约和协调器实现重入，不支持多 API 实例或任务重启续跑。
- 新上传在本地 `DATA_ROOT` 保留一份处理用副本（与对象存储中的原片重复）。E2 改为由 Worker 从对象存储读取。
- 续传前要在浏览器中读取并哈希已上传部分（Worker 中进行，大文件续传会多花一些时间）；内容一致性检查依赖默认的 `UPLOAD_VERIFY_PART_ETAG=true`。
- 对象物理删除是最终一致的：存储长期不可用时，`object_deletions` 会积压（`migrate check` 报告 `pending_object_deletions`）；配额已在逻辑删除时释放。
- MinIO 社区版已停止发布二进制和 Docker 镜像。开发与 CI 固定使用 conda-forge 打包的最后一个版本，生产前需重新评估 S3 实现。

## 下一阶段入口
E2：见 `E1_DELIVERY.md` 第 7 节。
