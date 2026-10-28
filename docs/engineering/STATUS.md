# 工程状态

更新：2026-10-28　阶段：**E1 进行中（验证未完成）**

## 当前提交
基线 `3ede061`；E1 提交见 `git log`，本文件随每次验证更新。

## 已完成（代码层面）
- PostgreSQL 平台 schema 与迁移器、身份/空间/归属/配额、上传会话状态机与协调器、S3 适配层。
- 上传 API（`/api/uploads*`）、登录 API、认证与授权中间件、视频列表按空间过滤、删除时释放配额。
- 前端：登录页、续传客户端（Worker 计算 MD5、并发、重试、重签、暂停/继续、刷新后恢复与文件身份校验）、三阶段状态。
- 迁移工具（dry-run / backfill / check / rollback）。
- Linux/WSL2 compose（固定镜像、健康检查）、Windows 原生运行脚本、CI 工作流 `e1-platform.yml`。

## 未完成 / 未验证
- 真实 PostgreSQL + 对象存储集成测试：**尚未运行**。
- 浏览器端到端测试（`upload-resume.spec.ts`）：**尚未运行**。
- 前端 typecheck / vitest / build、既有后端与 P5 浏览器回归：**尚未在完整依赖环境运行**。
- 内容秒传：未实现，列为下一步。

## 已运行的检查（受限环境，非验收）
| 检查 | 环境 | 结果 |
| --- | --- | --- |
| `tests/test_e1_unit.py`（纯逻辑） | 云端沙箱，以临时 import shim 代替缺失的 pydantic-settings（未提交） | 5 passed |
| 续传客户端逻辑（`resumable-upload.test.ts` 8 个场景） | 云端 Node 22 + 最小 vitest shim，**内存假存储 = 测试替身** | 8 passed |
| MD5 实现与 Node crypto 对照（含 8 MiB+3 边界） | 云端 Node 22 | 一致 |
| Python 语法编译 | 云端 | 通过 |

## 启动、迁移与回退
见 `E1_DELIVERY.md` 第 4 节。

## 已知限制
- 单 API 实例（`OwnerLock`）；验证与受理任务在进程内执行，依靠租约和协调器实现重入，不支持多 API 实例或任务重启续跑。
- 新上传在本地 `DATA_ROOT` 保留一份处理用副本（与对象存储中的原片重复）。E2 改为由 Worker 从对象存储读取。
- MinIO 社区版已停止发布二进制，开发环境固定使用最后一个公开版本。

## 下一阶段入口
E2：见 `E1_DELIVERY.md` 第 7 节。
