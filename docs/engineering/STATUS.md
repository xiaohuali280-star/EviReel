# 工程状态

更新：2026-10-31　阶段：**E4 整改完成，待复验**（首轮验收：部分完成、暂不通过，P1×1、P2×2；第二轮验收：原三项关闭，新增 P2×1（并行调用突破本轮上限），已整改；CI 和自测通过不等于验收通过）。E1、E2、E3 已验收（E3：`53bbe0c`）。

## E4（当前）

- **代码**：`2943661`（暂停时的检查点）→ `0ed66d9`（评测工具修正、路由规则修订、补充测试）→ **`84a85c6`（首轮验收整改：real 评测复用平台装配、多轮 Agent 对照、视觉问题最终状态、零 token、路由字段）→ **`fb48b7c`（第二轮验收整改：本轮调用 / token 上限在并发下成立，平台预占拒绝、异常、取消时归还额度）**。整改明细见 `E4_DELIVERY.md` 第 0、0.1 节。设计、取舍、限制见 `E4_DELIVERY.md`；评测方法与结果见 `EVALUATION.md`；演示 `DEMO.md`；面试材料 `INTERVIEW.md`。
- **真实评测未做**：
  - 没有有授权的视频（本机唯一的视频来源不明，未使用）；
  - 没有人工标注；
  - 没有模型预算授权，重排序 key 为空；
  - 没有调用任何付费模型。补齐步骤见 `EVALUATION.md` 第 6 节。
  - 工具修好不等于真实效果已验证。
- **下一步**：等待 E4 复验。

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 模拟器评测（合成题集 32 题，整改后） | `docs/engineering/e4-eval/sim-all-v3/`，`84a85c6`，`dirty=false`，全部配置（含 `agent`）× 2 遍 | 通道失败 0，运行错误 0，没有不完整的配置，模型身份一致。结果只说明工具和路由规则在工作，不代表真实效果（`EVALUATION.md` 第 4、5 节） |
| 本机第二轮整改后（`fb48b7c`），真实 PG 16 + MinIO 与无中间件测试一起 | 全量 `pytest tests`（E1 本地测试环境） | **342 passed, 24 skipped, 0 failed**；新增并发上限测试 7 个（含真实 PG 上 4 线程争最后一次额度）；验收探针 `e4-second-cap-probe.py` 在新代码上 0 个调用被执行 |
| CI `fb48b7c` | E1、CPU、E2、E3 | 四项**全部通过**：[E3](https://github.com/xiaohuali280-star/EviReel/actions/runs/41196830336) 测试 49 passed，PG 读模型回归 98，E1/E2 回归 50；[E2](https://github.com/xiaohuali280-star/EviReel/actions/runs/41196830318)；[CPU](https://github.com/xiaohuali280-star/EviReel/actions/runs/41196830351) 280 passed、浏览器 21；[E1](https://github.com/xiaohuali280-star/EviReel/actions/runs/41196830339) |
| 本机首轮整改后，真实 PG 16 + MinIO 与无中间件测试一起 | 全量 `pytest tests`（E1 本地测试环境） | **335 passed, 24 skipped, 0 failed**（跳过的需要 Linux + RabbitMQ 或 Redis，由 CI 覆盖；另 1 个是 deep 模式没有单独升级时限的参数组合） |
| CI `84a85c6`（整改后） | E1、CPU、E2、E3 | 四项**全部通过**：[E3](https://github.com/xiaohuali280-star/EviReel/actions/runs/41193905838) 测试 49 passed（含两个新的评测装配测试：真实 PG + MinIO、真实 Redis），PG 读模型回归 98，E1/E2 回归 50；[E2](https://github.com/xiaohuali280-star/EviReel/actions/runs/41193906022)；[CPU](https://github.com/xiaohuali280-star/EviReel/actions/runs/41193905787) 273 passed、vitest 42、浏览器 21；[E1](https://github.com/xiaohuali280-star/EviReel/actions/runs/41193906067) |
| 首轮交付时（`0ed66d9`） | 模拟器 `sim-all-v2`；本机 257 passed / 83 skipped，真实 PG + MinIO 89，PG 读模型 145 | 保留作对照 |
| CI `0ed66d9` | E1、CPU、E2、E3 | 四项**全部通过**：E3（运行 41180288099）测试 47 passed、0 skipped，PG 读模型回归 98，E1/E2 回归 50，容器冒烟停掉一个 API 后 20/20 成功；E2（41180288091）26 + 14 + 24；CPU（41180288083）257 passed、vitest 42、浏览器 21；E1（41180288084）通过 |
| 前端 | 无改动（`/search` 的新字段可选，默认保持原行为） | 前端测试与浏览器回归见 CI 的 CPU tests |

## E3 验收结论

第三轮验收通过（`53bbe0c`）：Standards 0、Spec 0，第二轮的 P2 已关闭。通过范围限于单机共享 DATA_ROOT 的部署；没有两实例吞吐实测、没有调用真实模型、没有演练完整回退，这些仍是 E3 的边界。

## E3 第二轮验收整改（`5f58ac1` 之后，`aefb6fd`）

| 发现 | 修复 | 回归测试 |
| --- | --- | --- |
| [P2] 认领回收时就提交了 `removed_at`，之后才删除文件；文件删除失败或进程中断后，该代次永久退出候选，显式重试也会跳过 | “认领”与“清理完成”分开记录（schema 8，迁移 `008_generation_cleanup.sql`）。`removed_at` 只表示已认领：从此拒绝新租约。`cleaned_at` 在确认文件已无残留后才写入。已认领未清理的代次一直是候选，再次 `claim_removal` 返回“可继续”，读者在此期间仍被拒绝。删除是幂等的（已不存在的文件视为成功，其他错误上抛并记录次数与原因）。每批待重试项最多占一半，避免挤掉新候选。本地协调器语义相同；删除了未使用的 `mark_removed` | 真实 PG，`test_e3_platform`：① 首次 `unlink` 抛 `PermissionError` 后，文件仍在、已认领、未清理、仍是候选、新租约被拒、检索读新代次；恢复后再清扫成功，`cleaned_at` 已写入；② 认领后、删除前进程停止，重启后清扫完成；③ 两个线程（各自的 PG 连接）同时清理同一代次：无异常，文件被删除，之后清扫无事可做；④ 活跃读者仍阻止认领。①② 在修复前的代码上因行为本身失败；③④ 在修复前的代码上失败的原因是缺少新列，作为回归保护。单元测试覆盖本地协调器的同一协议 |

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 本机，无中间件 | 全量 `pytest tests` | **243 passed, 83 skipped** |
| 本机，真实 PG 16 + MinIO | E3 单元 + 平台测试，E1/E2 平台与流水线测试 | **89 passed**；最后一处改动（候选分批）后，E3 + P1 重跑 **87 passed** |
| 本机，真实 PG 读模型 | P1/P4/P5/聊天在 `REGISTRY_BACKEND=postgres` 下 | **98 passed** |
| CI `aefb6fd`：E3（运行 41060063121） | E3 测试（真实 Redis + PG + MinIO，禁止跳过）/ PG 读模型回归 / E1+E2 回归 / 容器冒烟 | **47 / 98 / 50 passed**；冒烟：停掉 api-1 后 20/20 成功 |
| CI `aefb6fd`：E2（41060062985）、CPU（41060062924）、E1（41060063057） | E2 流水线 + 故障 + E1 回归；CPU 全量 + vitest + 浏览器；E1 平台 | **26 + 14 + 24 passed**；**243 passed, 83 skipped**，vitest 42，浏览器 21；E1 通过 |

## E3 首轮验收整改（`c5bb411` 之后，`35776f8`、`9e5ba98`）

| 发现 | 修复 | 回归测试 |
| --- | --- | --- |
| [P1] 读取快照与登记租约之间有空窗：期间新版本发布、旧代次退役并被清扫，请求仍能登记指向已删除代次的租约；清扫器在查询候选与删除文件之间也没有互斥 | 先读视频行并取得租约，再从这些代次生成快照。协调器在每个视频的 advisory lock 下授予租约；所列代次已被认领删除时拒绝（`snapshot_stale`），调用方重新读取视频行再固定。清扫器删除代次前、回收已删除视频前，都在同一把锁下认领（`claim_removal` / `claim_reclaim`），提交后才删除文件。字幕、帧记忆、笔记语料和导出使用同样的取租约方式 | 真实 PG，`test_e3_platform`：在“读取视频行”与“写入租约”之间插入新版本发布和清扫，旧代次被删除，请求改为固定新代次，指向旧代次的租约被拒绝；先拿到候选列表、随后写入租约时，删除认领失败、文件保留，租约释放后才删除。两项在修复前的代码上都失败 |
| [P1] 租约达到最大寿命或续租失败后，读取继续运行而不再受保护 | 共享租约在本地记录截止时间（最近一次成功写入的开始时刻 + TTL − TTL/6，始终早于数据库行过期）。续租不复活已过期的行。达到最大寿命、超过本地截止时间或行已过期时，租约判定为丢失：受保护的检索、字幕、帧记忆和导出被取消并返回 `503 read_lease_lost`，播放流停止发送。丢失后的行不删除、自然过期，留出剩余 TTL。启动时要求 `READ_LEASE_MAX_S ≥ 问答截止 + TTL`。Worker 读取已发布代次时也持有续租的 PG 租约 | 真实 PG：三种丢失原因（最大寿命、行已过期且未被复活、未能续租）；检索中途租约丢失时，在 3 s 内返回 503 `read_lease_lost`（被打断的读取原本要 5 s）；租约寿命短于问答截止时间时拒绝启动。单元测试：受保护读取被取消，请求结束后的丢失不会误取消后续请求，播放流在第一块数据后停止。PG 用例在修复前的代码上失败 |
| [P2] 模型总超时不约束准入等待，每次重试还会重新获得完整的排队等待预算 | 总截止时间作为绝对时刻，同时约束准入等待、退避、预占和调用；过期后不再准入、不再调用。排队预算由一次请求的所有尝试共用。同步、异步两条路径共用同一套策略（同时处理了 Standards 建议 1） | 单元测试（同步与异步各一）：总超时 0.05 s、排队预算 0.5 s、限流器持续繁忙时，0.2 s 内返回 504，供应商未被调用；重试共用 0.2 s 排队预算 |
| [P2] 本地降级把小限额向上取整为 1，也没有覆盖 Worker | 本地限额不再向上取整（份额为 0 或限额为 0 都拒绝）。只要还有其他进程调用模型（多实例或 worker 模式），API 拒绝以 `local` 启动，Worker 忽略该设置并按 `reject` 处理 | 单元测试：限额 1、2 份时放行 0 个，限额 0 时放行 0 个；Redis 不可用时，API 与 Worker 两个网关都返回 503 且没有调用供应商。真实 Redis：两个实例共用在途上限 1 时只放行一个，上限 0 时不放行 |

Standards 建议 2（删除流程直接依赖会话和笔记表）：检查与标记移到 `VideoRegistry.mark_deleting`。

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 本机，无中间件 | 全量 `pytest tests` | **242 passed, 79 skipped** |
| 本机，真实 PG 16 + MinIO | E3 单元 + 平台测试，E1/E2 平台与流水线测试 | **84 passed** |
| 本机，真实 PG | P1–P5、聊天、笔记 HTTP 测试在 `REGISTRY_BACKEND=postgres` 下 | **200 passed** |
| CI `35776f8` | E3、E2、E1、CPU | E3、E2、E1 通过；CPU **失败**：新单元测试用了真实 redis 客户端，而 CPU 工作流没有安装可选依赖 `redis`。已在 `9e5ba98` 改为不依赖该包的替身客户端（真实客户端的同一场景在 `test_e3_redis` 中测试） |
| CI `9e5ba98`（E3 运行 41054536951） | E3 测试（真实 Redis + PG + MinIO，禁止跳过）/ PG 读模型回归 / E1+E2 回归 / 容器冒烟 | **42 / 98 / 50 passed**；冒烟：停掉 api-1 后 20/20 成功 |
| CI `9e5ba98`（运行 41054536747、41054536813、E1 platform） | E2 流水线 + 故障 + E1 回归；CPU 全量 + 浏览器；E1 平台 | **26 + 14 + 24 passed**；**242 passed, 79 skipped**，vitest 42，浏览器 21；E1 通过 |

## E3（首轮交付记录）

- **代码**：`2916817`（E3 实现与测试）→ `175dccb`（nginx 转移到另一实例、冒烟脚本计数修正）→ `cddcfe4`（两个实例同时启动时创建存储桶的竞争）。设计取舍、协议、迁移与回退、限制见 `E3_DELIVERY.md`。
- **阻塞**：无。真实模型验证没有授权预算，未执行（见 E3_DELIVERY 第 9 节）。
- **下一步**：等待 E3 验收。

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 本机，无中间件 | 全量 `pytest tests`（SQLite 读模型） | **234 passed, 73 skipped**（跳过的需要 PG/S3/Redis/RabbitMQ） |
| 本机，真实 PG 16 + MinIO | `test_e3_unit` + `test_e3_platform` + E1/E2 平台与流水线测试 | **71 passed** |
| 本机，真实 PG | P1–P5、聊天、笔记 HTTP 测试在 `REGISTRY_BACKEND=postgres` 下 | **200 passed** |
| 本机，前端 | tsc；vitest；构建 | 通过；**42/42**；已重建 `vam/server/static` |
| CI `2916817`：E3 concurrency | E3 测试（真实 Redis + PG + MinIO，两个 API 实例，禁止跳过） | **28 passed, 0 skipped** |
| | P1/P4/P5/聊天在 PG 读模型上 | **98 passed** |
| | E1/E2 在 schema 7 上回归 | **50 passed** |
| | 容器栈冒烟 | **失败**：脚本计数有误；停止 api-1 后 20 个请求中 4 个失败（nginx 未转移到另一实例）。已在 `175dccb` 修复，结果见下一行 |
| CI `175dccb`：E3 concurrency | 同上 | 测试 **28 / 98 / 50 passed**；容器栈**失败**：两个 API 同时启动，都发现存储桶不存在并去创建，后创建的那个收到 `BucketAlreadyOwnedByYou` 后退出。这是真实的多实例缺陷，已在 `cddcfe4` 修复，并补充真实 MinIO 回归测试（6 个并发实例；未修复时失败，修复后通过） |
| CI `175dccb`：E2 pipeline | 同上 | 通过 |
| CI `cddcfe4`（运行 41046551674）：E3 concurrency | E3 测试 / PG 读模型回归 / E1+E2 回归 / 容器栈冒烟 | **29 / 98 / 50 passed**，E3 测试 0 skipped。冒烟通过：两个实例都接受同一登录会话；停止 api-1 后 20/20 请求成功，全部由 api-2 处理；api-1 重启后重新加入 |
| CI `cddcfe4`：E1 platform、CPU tests、E2 pipeline | 同前 | 三项**全部通过** |
| CI `2916817`：E2 pipeline | 流水线测试 + 故障测试（kill -9、RabbitMQ 停启）+ E1 回归 | **26 + 14 + 24 passed** |
| CI `2916817`：E1 platform、CPU tests | E1 平台；全量 CPU 测试 + 浏览器 | 通过（CPU **234 passed**，浏览器 **21 passed**） |
| 压测（本机，模型模拟器） | 基线 `b39a564`，以及 `2916817` 的无缓存、热缓存、热缓存 + PG 读模型三组 | 见 E3_DELIVERY 第 8 节；JSON 在 `docs/engineering/e3-bench/` |

主要实测（同一台机器、同一模拟器）：

- 同视频 20 并发：P95 从 17.9 s 降到 2.4 s（无缓存）；
- 固定到达 5/s：基线只能处理 1.2/s，P99 达到 64 s；E3 跟得上 5/s，P99 为 0.32 s（无缓存）。

真实中间件与测试替身：

- **真实中间件**：PostgreSQL、MinIO、Redis、RabbitMQ、媒体处理（ffmpeg）。
- **测试替身**：模型（`e2_factory`、压测模拟器）。
- **只在 Linux CI 上验证**：真实 Redis 测试与容器栈（本机没有 Redis）。

---

# E2 记录（历史）

当前提交：E2 实现 `866e8bf` → 修复 `2d8309f` → CI 计数 `85a8278` → 验收整改 `afd21ee` → CI/文档 `8f8ce44` → 复验整改 `d662b61` → 第三轮整改 `7d5868f` → 聊天重复发送修复 `8047089`，状态记录见其后的提交。

## E2 第三轮验收修复（`e955785` 之后，`7d5868f`）

| 发现 | 修复 | 回归测试（真实 PG + MinIO，`tests/test_e2_third.py`） |
| --- | --- | --- |
| [P2] notes_publish 在 token 校验前写入公开产物，旧 attempt 能在接管者失败后把笔记改回成功 | notes_publish 只做决定（校验草稿、确定产物 id）；产物和 study_jobs 终态只由 `project` 根据已提交的决定写入。读取在两个方向上都以账本为准（镜像为成功但账本未提交成功时纠正），只有账本记为成功的笔记任务的产物才可见 | 草稿完成 → 旧 notes_publish 暂停 → 接管者永久失败 → project 写入失败 → 旧 attempt 恢复：其提交被拒，没有 study_artifacts 行；`/study` 返回 failed 且产物为 null，`/study/jobs/{id}` 返回 failed 且 artifact_id 为 null，按产物 id 导出返回 404 |
| [P2] 自动笔记可能先于视频 project 执行，因语料未发布而永久失败 | 自动笔记改为在视频任务 `project` 投影成功后、于其账本提交事务里创建 | publish 已决定、project 未执行时，不存在任何笔记任务；project 完成后才出现 `auto-notes:<job>` 笔记任务，执行成功，产物可见 |

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 真实中间件（本机） | `test_e2_pipeline` + `test_e2_remediation` + `test_e2_rereview` + `test_e2_third` | **26 passed** |
| 真实中间件（本机） | 全量 `pytest tests` | **265 passed, 14 skipped**（14 个故障测试需要 Linux 和 RabbitMQ，在 CI 上执行） |
| GitHub CI（`7d5868f`） | E2 pipeline、E1 platform、EviReel CPU tests | E2、E1 **通过**；CPU tests 浏览器步骤 `panels.spec.ts:81` 失败（见下一行） |
| 浏览器（本机，`8047089`） | 前端 vitest；P5 浏览器完整回归；`chat-actions` + `panels` 重复 2 次 | vitest **42/42**；**21 passed, 2 skipped**；**8/8** |
| GitHub CI（`8047089`） | E2 pipeline、E1 platform、EviReel CPU tests | **三项全部通过** |
| 根因与修复 | `afd21ee` 和 `7d5868f` 的 CPU tests 浏览器失败都是 `e2e/panels.spec.ts:81`：同一 `request_id` 被发送两次。原因是 `ChatChannel.send()` 发送后立即做 HTTP 对账，服务端尚未登记请求时返回 404，客户端据此重发。属于客户端重复请求缺陷，不是测试时序问题。修复：首次对账延后一个轮询间隔（`RECONCILE_MS`），确认缺失时仍以原 ID 重发；新增 `frontend/src/chat.test.ts`（测试替身） | 已修复 |

## E2 整改复验修复（`e001459` 之后，`d662b61`）

| 发现 | 修复 | 回归测试（真实 PG + MinIO，`tests/test_e2_rereview.py`） |
| --- | --- | --- |
| [P1] 孤儿清理会删除后来被合法检查点引用的对象 | 同一对象键的三件事都在该键的 PG advisory 事务锁下串行：检查点登记（键已入删除队列时拒绝）、清理入队（持锁重核无引用）、最终删除（持锁重核检查点、阶段输出、视频归属后，在同一事务内删除）。ASR 遇到拒绝时以新键重做 | 复现审查方顺序：上传 → 清理判为孤儿且删除失败（pending）→ 登记检查点被拒，检查点不存在 → 存储恢复后删除。反向顺序：先登记，清理不入队。已入队但随后被引用：删除时跳过（`still_referenced:checkpoint`），对象保留 |
| [P2] 导出人工重试丢失 artifact_id/format | retry 继承原任务的业务参数，kind/video_id 重新推导 | 注入永久失败的 pdf 导出 → 人工重试（幂等）→ 新任务参数完整 → 成功，下载内容以 `%PDF` 开头 |
| [P2] 同一任务的旧 attempt 能在接管者失败后写入成功 | 先决定、后投影：publish 只做决定（带 attempt_token 的账本提交，不写可见状态）；新增最终 `project` 阶段（迁移 006），从账本推导读模型并按 `jobs.seq` fencing。任何 attempt 写入的都相同，因此旧 attempt 无法违背账本 | 旧 publish 认领后暂停 → 接管者永久失败 → 可见状态为 failed → 旧 attempt 恢复：其决定提交被拒，任何 project 重放仍是 failed，PG 与 `/api/jobs` 一致为 failed |
| [P2] 笔记被扫描终结后前端一直显示运行中 | `project` 写入 study_jobs 终态；`StudyService.job/state` 在投影完成前以账本的失败阶段覆盖旧镜像 | 最后一次 attempt 失联 → 扫描判次数用尽 → 读取接口立刻返回 failed/attempts_exhausted → project 执行后 study_jobs 也为 failed |

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 真实中间件（本机） | `test_e2_pipeline` + `test_e2_remediation` + `test_e2_rereview` | **24 passed** |
| 真实中间件（本机） | 全量 `pytest tests` | **263 passed, 14 skipped**（14 个故障测试需要 Linux 和 RabbitMQ，在 CI 上执行） |
| GitHub CI（Linux，`d662b61`） | E2 pipeline run 40890260277：故障测试 tests=14 failures=0 skipped=0；账本/整改/复验测试 tests=24 failures=0 skipped=0；容器栈冒烟；E1 回归 | success |
| GitHub CI（`d662b61`） | E1 platform run 40890260175；EviReel CPU tests run 40890260227 | 均 success |
| 历史 | `afd21ee` 上 CPU tests 的浏览器步骤失败过一次（原因未查明，按偶发记录），`8f8ce44` 与 `d662b61` 上均通过 | — |
| 未运行 | 真实模型、真实网关故障、本机 Linux 容器 | — |
| 说明 | 跨 PG、SQLite registry、转写库的一致性依靠“单调序号 + 可重放投影”，**不是跨库原子提交** | — |

## E2 第二次独立验收整改（`a224201` 之后）

| 发现 | 整改 | 回归测试 |
| --- | --- | --- |
| [P1] publish 在 token 校验之前写可见状态，旧执行者能把 `active_generation` 回退；transcript_index 在校验前移动字幕 head | 每个任务有单调递增的 `jobs.seq`（PG 迁移 005）。可见状态只在 publish 中写，并按 seq fencing：SQLite `publish_fenced`（代次、播放路径、视频/转写状态、任务行，一个事务，条件 `published_seq ≤ seq`；registry 迁移 005），以及转写库 `publish_head`（`transcript_fence`）。transcript_index 只完成版本、不移动 head；失败投影在账本接受后才执行且带 fence；Worker 进度不会重新打开已终结的行 | 任务 1、任务 2 先后发布后，任务 1 的 publish 执行者恢复：`published=false`，head 未移动，可见代次仍为任务 2 的，其账本提交也被拒；当前任务重放发布幂等。转写 head：旧序号的 head 移动被拒，同序号可重复执行 |
| [P2] 音频故障让视觉分支被跳过 | 新增 `audio` 阶段（语音分支专属）；`prepare` 只校验原片和生成播放文件 | 只对音频抽取注入 `audio_timeout`：audio 失败（2 次尝试），asr/transcript 跳过，**vision 执行并成功**，任务 partial，可播放、视觉可用，转写状态为 failed |
| [P2] 扫描在锁外读候选，锁内只查 status，会终止已续租或已被接管的尝试 | 持锁后重核：同一 token、租约仍过期、未被再次投递；不满足就不动作 | 用测试钩子把“候选读出后续租”和“候选读出后接管”注入锁前：两者都不失败、不重投；健康尝试正常完成 |
| [P2] 笔记批次、导出未迁移，自动笔记回退 | `notes` 任务：`notes_draft`（按批调用模型，批次检查点）→ `notes_publish`（确定性 id，按任务创建时间排序）。`export` 任务：产物写入对象存储，按引用下载。自动笔记在 publish 的同一事务里创建。API 重启不会中断笔记任务 | 自动笔记随发布创建；首次输出无效时重试成功；手动再生成复用全部批次检查点（模型调用次数不变）；重复执行笔记发布不会产生重复产物；API 重启后笔记任务仍为 queued 并能完成；导出任务幂等，下载内容与直接导出一致。**Linux 独立 Worker**：在笔记批次检查点之后 kill -9，恢复后复用该批（computed=0，模型只调用 1 次） |
| [P2] 失败对象没有清理收敛 | `vam/pipeline/sweep.py`：`pipeline/` 下超过宽限期、未被检查点/阶段输出/运行中尝试引用的对象进入持久删除队列（`pipeline_orphan`），由发布器周期执行 | 真实 MinIO：宽限期内不删除；超过宽限期后只删除孤儿；被阶段输出引用的产物、检查点、运行中尝试的产物都保留；删除记录为 done |

### 整改后的验证
| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 真实中间件（本机，PG 16.4 + MinIO） | `tests/test_e2_pipeline.py` + `tests/test_e2_remediation.py`（新增 8 个） | **20 passed** |
| 真实中间件（本机） | 全量 `pytest tests` | **259 passed, 14 skipped**（14 个故障测试需要 Linux 和 RabbitMQ，在 CI 上执行）。registry 迁移测试改为期望版本 5（数据保留断言不变） |
| 回归（本机） | P5 浏览器回归 21 个用例 | **21/21 通过** |
| GitHub CI（Linux，`afd21ee`） | E2 pipeline：故障测试 tests=14 failures=0 skipped=0（含新增的笔记批次 kill -9 恢复）；容器栈冒烟；E1 回归 | success；E1 platform 工作流 success |
| GitHub CI（Linux，`8f8ce44`） | E2 pipeline：账本/阶段/整改测试 tests=20 failures=0 skipped=0；故障测试 tests=14 failures=0 skipped=0 | success |
| GitHub CI | EviReel CPU tests | `afd21ee` 上浏览器回归步骤失败一次（当时没有具体输出，本机同样 21 个用例全部通过）；之后为该步骤增加 JUnit 注释输出，`8f8ce44` 上 **success**。该次失败的原因未查明，按偶发记录 |
| 未运行 | 真实模型调用；真实网关故障；本机 Linux 容器（无 Docker） | — |

## E2：可靠异步处理（`12a16ee` 之后）
设计、启动、迁移与回退见 `E2_DELIVERY.md`。

### 完成项（代码）
- PG 迁移 `004_pipeline.sql`：`jobs`、`job_stages`、`stage_attempts`（失败台账）、`outbox_events`、`checkpoints`，schema 版本 4。
- `vam/pipeline/`：
  - `repository.py`：受理、认领、心跳、条件提交、汇合、退避重试、恢复扫描、outbox 发布、检查点。
  - `dispatcher.py`：发布器 + 恢复扫描，独立进程，publisher confirm。
  - `celery_app.py`：acks_late、reject_on_worker_lost、prefetch=1、软/硬时限、4 个 durable 队列。
  - `runner.py`：认领 → 心跳 → 执行 → 带 token 提交；错误分类。
  - `stages.py`：prepare / asr / transcript_index / vision / publish；ASR 检查点、Embedding 批次检查点。
  - `api.py`：持久受理、人工重试、任务描述、失败台账。
- API：
  - worker 模式下 `schedule` 只做持久受理，API 重启不再中断任务。
  - `Idempotency-Key` 头（同一 key 参数不同返回 409）。
  - 上传受理使用固定 request id。
  - `GET /api/jobs/{id}` 附带 `pipeline.stages/attempts`；新增 `POST /api/jobs/{id}/retry`、`GET /api/pipeline/failures`。
  - 删除/回收前检查 PG 中的活动任务。
- 部署：
  - `deploy/e2/compose.yml`：包含 E1 的 PG/MinIO，加上 RabbitMQ 4.1.4（健康检查、`consumer_timeout`）、一次性 `migrate`、`api`、`dispatcher`、`worker-long`、`worker-short`。
  - `deploy/e2/app.Dockerfile`，`.env.example`，`pyproject` 新增 `pipeline` extra（Celery 5.5–5.6）。
- CI：`.github/workflows/e2-pipeline.yml`，真实 PG + MinIO + RabbitMQ + 独立 Worker 进程 + 容器栈冒烟。

### 未完成 / 未迁移（如实记录；已按第二次验收更新）
- 问答没有迁移到 Worker（需求允许自行决定），保留 P4 的幂等与终态持久化。笔记批次、导出、自动笔记已在 `afd21ee` 迁入流水线。
- 视觉阶段只有阶段级恢复。
- 仍是单 API + 共享 DATA_ROOT；SQLite 中的视频元数据和发布指针没有迁入 PG（发布已按任务序号 fencing）。
- 已成功任务的阶段产物会一直保留，保留期策略交给 E3；孤儿产物由清理扫描回收。
- 以下“验证记录”是 `85a8278` 时的结果，整改后的结果见上一节。

### 验证记录
**E2-A 真实中间件（本机 Windows，PostgreSQL 16.4 + MinIO；没有 RabbitMQ，outbox 行在测试进程内交给真实 runner）**

| 检查 | 结果 |
| --- | --- |
| E1 回归基线（改造前，`12a16ee`）`test_e1_unit` + `test_e1_platform` | 24 passed |
| `tests/test_e2_pipeline.py`，12 个场景（包括 CI 发现缺陷后补的“ASR 失败时任务为 partial、保留视觉”）：<br>• 持久受理（API 不执行任何计算）<br>• request id 幂等、参数冲突返回 409、并发重复提交只建一个任务、每个视频只有一个活动任务<br>• 8 线程并发认领只有一个赢家；租约过期后接管，旧 token 的完成/失败/心跳全部被拒<br>• 两分支并发完成，汇合只激活一次（publish 的 outbox 只有 1 条）<br>• 退避窗口内不能认领；次数用尽后失败，依赖阶段被跳过，失败台账有记录<br>• 扫描能重新投递“已投递未消费”的阶段和租约过期的阶段<br>• broker 报错时 outbox 保留并退避；“已确认、未标记”后重发，重复投递只执行一次<br>• 两条分支端到端发布（播放/Range、字幕、视觉索引）；仅转写任务复用 ASR 检查点与全部 Embedding 批次（转写只调用 1 次）<br>• Embedding 第 3 批失败后重试，复用前 2 批（共 4 次调用）<br>• 视觉失败时任务为 partial，旧 `active_generation` 和字幕保留，失败代次被删除；人工重试幂等且成功；失败台账可查<br>• API 重启后任务仍为 queued，可继续完成；活动任务期间不能删除视频 | **12 passed**（75 s） |
| 全量 `pytest tests`（最终代码） | **251 passed, 13 skipped**（13 个是 E2-C 的故障测试：本机不是 Linux，也没有 RabbitMQ，所以跳过；这些测试在 CI 上执行） |

**E2-B 测试替身**：模型由 `tests/e2_factory.py` 代替（颜色/关键词向量、固定 ASR 片段），只证明流水线逻辑，不代表真实模型的效果或成本。

**E2-C 真实中间件 + 独立进程（GitHub Actions Linux：RabbitMQ 4.1.4 + PG + MinIO + 独立 dispatcher / Celery Worker，kill -9）**

| 检查 | 结果 |
| --- | --- |
| `tests/test_e2_faults.py`，13 个场景：<br>1. 任务提交后、投递前发布器崩溃 → 重启后完成<br>2. broker 已确认但 outbox 未标记时发布器崩溃 → 重复消息被 Worker 记为 `not_runnable`，每个阶段只执行一次<br>3. ASR 提交后、ACK 前 pool 子进程被杀（日志有 WorkerLostError，消息重投）→ 转写只调用 1 次<br>4. ASR 检查点已登记、阶段未提交时被杀 → 复用检查点，转写只调用 1 次<br>5. 对象已上传、检查点未写入时被杀 → 重新转写 1 次（共 2 次）<br>6. 视觉产物写出后、提交前被杀，以及 prepare 提交后、ACK 前被杀 → 收敛；被杀尝试的代次被删除<br>7. 整个 Worker 在阶段中途被 SIGKILL → 未确认的消息重投，最终完成<br>8. 两个 Worker 并发消费 6 条重复的 prepare 消息 → 只执行 1 次<br>9. 挂起的 Worker 被接管 → 旧结果被拒（`stage_result_discarded`），发布的是新代次<br>10. RabbitMQ 停机 → outbox 退避重试；broker 恢复后完成<br>11. 重试次数用尽 → ASR 记为 retry→failed，任务 partial，视觉可用，失败台账可查<br>12. 重新索引时视觉分支失败 → 保留旧索引与能力，进度可查<br>13. 软时限超时 → 记为 `stage_timeout` 并重试成功 | `85a8278`：E2 工作流 run 40853028005 **success**；JUnit 计数公开注释：**tests=13 failures=0 errors=0 skipped=0** |
| `tests/test_e2_pipeline.py`（同一 Linux 作业，真实 PG + MinIO） | tests=12 failures=0 errors=0 skipped=0 |
| 容器栈冒烟（构建 `deploy/e2/app.Dockerfile`；`migrate`、API 健康检查、dispatcher；`worker-long` 响应 `inspect ping`，`worker-short` 列出 active_queues） | success |
| 同一中间件上的 E1 回归 | success |
| 当前代码 `2d8309f` 的三个工作流（E1 platform、EviReel CPU tests、E2 pipeline） | 均 success（`85a8278` 只改了 CI 计数步骤） |

**CI 抓到的真实缺陷**：ASR 重试用尽后，`publish` 只看 `transcript_index`（状态为 skipped），把只有视觉的结果报成 succeeded。已在 `2d8309f` 修复：语音分支按 prepare → asr → transcript_index 取第一个失败，任务为 partial。另补了进程内回归测试。

**本轮修正的测试本身问题**：
- 故障测试参数冲突。
- broker 重启后 vhost 未就绪时清空队列失败：清空改为重试，健康检查增加 `check_running`、`check_virtual_hosts`。
- ASR 检查点故障点放错了位置：现在分别测试“检查点已登记”和“对象已上传、检查点未写入”两个窗口。

---

# E1 记录（历史）

E1 更新：2026-10-28　阶段：**E1 三轮独立验收发现的 5 项缺陷均已修复；本机回归与 `2466121` 的 GitHub CI 均通过**；内容秒传未做

## 当前提交
基线 `3ede061`；E1 实现 `2991d2b`；本机验证修复 `4632b4f`；第一次独立验收（目标 `be66e7d`）整改 `04122c8`；第二次独立验收（目标 `55d4271`）整改 `1515c1e`；第三次独立验收（目标 `952247f`）整改 `2466121`。

## 第三次独立验收整改（`952247f` 之后）
发现 [P2]：创建会话请求失败时，前端把所有 `UploadError` 都当成“服务端没有创建会话”，随即报告取消成功。可是 `App.tsx` 把 HTTP 错误转成 `UploadError` 时丢掉了状态码，而服务端先提交会话行和配额预占，之后才创建 multipart。之后某一步失败返回 500，或网关在创建成功后返回 502，会话其实都已经存在。复现：会话已创建、收到 HTTP 502，结果为 cancelled，签名 0、PUT 0、DELETE 0。

| 修复 | 说明 |
| --- | --- |
| 保留 HTTP 状态 | `UploadError` 新增 `status` 字段；App 的传输层透传 `ApiError.status` |
| 只有确定没创建时才认定“未创建” | `certainlyNotCreated()` 只把本 API 返回的 4xx（排除 408/499）视为“确定未创建”，依据是 create 的所有 4xx 都在插入会话行之前抛出。重放请求的 401/403 不能说明原请求的结果，不算。5xx、网关错误、没有状态码的错误、没有响应，一律视为结果未知 |
| 结果未知时 | 用同一个 `client_request_id` 重放；找到会话就 `DELETE`，确认后才报告 cancelled；重放仍然得不到确定结果，就报告 `cancel_unconfirmed`，可以重试 |

| 类别 | 检查 | 结果 |
| --- | --- | --- |
| 测试替身 | 新增 6 个场景：持久创建后 500、创建后网关 502、无状态码的 HTTP 错误（都应重放、找到会话并 DELETE，只创建 1 次）；响应丢失后重放得到 503、500 后重放得到 401（都应先报告 unconfirmed，不出现 cancelled，服务恢复后取消成功，只创建 1 次、只删除 1 次）；API 返回 4xx（直接 cancelled，不重放、不 DELETE）。原有的指纹期间取消、创建等待中取消、响应丢失后重放等场景保持通过。`resumable-upload.test.ts` 共 21 个 | **21/21 通过** |
| 测试替身 | 用验收报告自带的 502 复现脚本跑修复后的代码。该替身对每次创建都返回同一个错误，所以正确结果是“未确认” | 结果从 cancelled 变为 **unconfirmed**；签名 0、PUT 0；没有虚报成功 |
| 真实中间件 | 新增：在真实 store 外包一层，让 CreateMultipartUpload 崩溃，得到 HTTP 500，而会话行和配额预占已经提交。验证：用同一 id 重放能找到这个会话，预占仍只有一份；DELETE 后变为 aborted，预占归零；之后再协调和重复取消都不会再次释放。`test_e1_unit` + `test_e1_platform` | **24 passed**（132 s） |
| 回归 | `e1-local.ps1 -Action test` 完整运行：全量 pytest（含真实 PG+MinIO）、前端 typecheck/vitest/build、P5 浏览器回归、E1 浏览器验收 | pytest **239 passed, 0 skipped**；typecheck/build 通过；vitest **40/40**；P5 **21/21**；E1 浏览器 **2/2** |
| GitHub CI（Linux，`2466121`） | `E1 platform` run 40812241691（compose 中的真实 PG + MinIO：后端验收 + 浏览器验收）；`EviReel CPU tests` run 40812241671 | 均 **success** |
| 未运行 | 没有真实网关注入 502 的端到端实验（由测试替身覆盖）；Linux compose 本机无 Docker（只在 CI 上运行） | — |

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
| GitHub CI（Linux，`1515c1e`） | `E1 platform` run 40805852343（compose 中的真实 PG + MinIO：后端验收 + 浏览器验收）；`EviReel CPU tests` run 40805852382 | 均 **success** |
| 未运行 | 浏览器端到端没有专门构造“创建响应在途时取消”（时序由测试替身覆盖）；Linux compose 本机无 Docker（只在 CI 上运行） | — |

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
