# EviReel 工程升级路线（E1–E4）

产品能力 P1–P5（上传、播放/Range、字幕、带来源问答、会话恢复、笔记/课件、回收站、导出）已实现，见 `docs/P*_DELIVERY.md`。E 系列只做工程深化，不重复列出已有功能。方向依据：`outputs/evireel-backend-improvement-plan-20261028.md`；实现依据：当前代码与实测。

| 阶段 | 主题 | 状态 | 交付记录 |
| --- | --- | --- | --- |
| E1 | 分片直传与断点续传；PostgreSQL 平台元数据；对象存储适配层；用户/空间归属与最小授权；配额 | 已验收（本机 + CI，见 STATUS）；内容秒传留作后续 | `E1_DELIVERY.md` |
| E2 | 可靠异步流水线：任务表 + Outbox、独立 Worker（Celery + RabbitMQ）、阶段检查点、租约/attempt token、重启续跑 | 已验收（`b39a564`，单 API + 独立 Worker + 共享 DATA_ROOT） | `E2_DELIVERY.md` |
| E3 | 并发与隔离：版本快照读、去掉检索锁、读模型迁入 PG、跨实例租约与删除协议、ModelGateway（Redis 全局限流 + PG 用量账本）、版本化缓存、跨实例通知、两 API 实例 | **已验收**（`53bbe0c`，单机共享 DATA_ROOT、双 API + 独立 Worker；两实例吞吐未压测） | `E3_DELIVERY.md` |
| E4 | AI 评测与检索演进：可重跑评测工具与指标、关键词通道、低成本/深度/自动回答路径、演示与面试材料 | 实现与自测完成，**待验收**；真实评测未做（缺有授权的视频、人工标注、模型预算） | `E4_DELIVERY.md`、`EVALUATION.md` |

## 阶段边界

- E1 保持单 API 进程（`OwnerLock` 仍生效）。上传验证与受理在该进程内以 asyncio 任务执行，靠数据库状态、租约和 attempt token 保证可重入，不宣称多实例或任务重启续跑。
- E2（实际范围）：`PIPELINE_MODE=worker` 时，`IndexingService.schedule` / `UploadService.accept_processing` 改为在 PostgreSQL 中持久受理（jobs + stages + outbox），由独立的发布器和 Celery Worker 执行，支持阶段级续跑和 ASR/Embedding 批次检查点。仍为单 API + 共享 DATA_ROOT 的单机部署。
  - 笔记批次与导出已作为流水线任务执行（`study_jobs`/`study_artifacts` 降为读模型）。
  - 没有迁移到 PG：SQLite 中的 `videos` 与已发布指针（由任务序号 fencing；`index_jobs` 在 worker 模式下降为读模型）、会话/问答。
- E3（实际范围）：读模型与发布指针在 `REGISTRY_BACKEND=postgres` 下迁入 PG，已发布文件同时存入对象存储并可重建；读者租约、删除标记、问答归属跨实例化，检索锁删除。满足前置条件后可运行多个 API 实例。部署仍共享一个 DATA_ROOT（单机），见 `E3_DELIVERY.md` 第 5 节。
- E4（实际范围）：可重跑的评测工具与合成题集、关键词通道、低成本/深度/自动三种回答路径（默认仍为深度）、演示与面试材料。真实评测、多轮 Agent 评测、向量索引（按实测判断不需要）、跨视频检索未做，见 `E4_DELIVERY.md` 第 5、8 节。
- 不在路线内：Kubernetes、多套同类中间件、改写语言。
