# EviReel 工程升级路线（E1–E4）

产品能力 P1–P5（上传、播放/Range、字幕、带来源问答、会话恢复、笔记/课件、回收站、导出）已实现，见 `docs/P*_DELIVERY.md`。E 系列只做工程深化，不重复列出已有功能。方向依据：`outputs/evireel-backend-improvement-plan-20261028.md`；实现依据：当前代码与实测。

| 阶段 | 主题 | 状态 | 交付记录 |
| --- | --- | --- | --- |
| E1 | 分片直传与断点续传；PostgreSQL 平台元数据；对象存储适配层；用户/空间归属与最小授权；配额 | 进行中（见 STATUS） | `E1_DELIVERY.md` |
| E2 | 可靠异步流水线：任务表 + Outbox、独立 Worker（Celery + RabbitMQ）、阶段检查点、租约/attempt token、重启续跑 | 未开始 | — |
| E3 | 并发与隔离：版本快照读、缩短检索锁、跨实例状态、全局限流与模型预算、多 API 实例 | 未开始 | — |
| E4 | AI 评测与检索演进：证据评测集、检索方案对照、成本/延迟取舍 | 未开始 | — |

## 阶段边界

- E1 保持单 API 进程（`OwnerLock` 仍生效）。上传验证与受理在该进程内以 asyncio 任务执行，靠数据库状态、租约和 attempt token 保证可重入，不宣称多实例或任务重启续跑。
- E2 接手：把 `IndexingService.schedule` / `UploadService.accept_processing` 的进程内任务换成持久任务表 + Outbox + Worker；把 SQLite 中的 `videos/index_jobs/conversations/study_*` 迁入 PostgreSQL。
- 不在路线内：Kubernetes、多套同类中间件、改写语言。
