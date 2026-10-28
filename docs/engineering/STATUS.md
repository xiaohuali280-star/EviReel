# 工程状态

更新：2026-10-28　阶段：**E1 进行中（本机验收通过；CI `E1 platform` 待修复后的提交重跑）**

## 当前提交
基线 `3ede061`；E1 实现 `2991d2b`；本机验证修复见其后的提交（`git log`）。

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
- Linux compose（`deploy/e1/compose.yml`）：本机没有 Docker，**未在本机运行**。依赖修复后提交的 CI `E1 platform` 重跑结果来确认。
- `2991d2b` 的 CI `E1 platform` 失败（run 40674376201，第 4 步 compose 启动）：`minio/minio` 与 `minio/mc` 已从 Docker Hub 下架（仓库 404，`latest` 同样 404），镜像无法拉取。已改为基于 sha256 固定的 conda-forge 包构建本地镜像，**尚未在 CI 上验证**。
- 真实模型调用：没有预算，未运行。
- 内容秒传：未实现，列为下一步。

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
- MinIO 社区版已停止发布二进制和 Docker 镜像。开发与 CI 固定使用 conda-forge 打包的最后一个版本，生产前需重新评估 S3 实现。

## 下一阶段入口
E2：见 `E1_DELIVERY.md` 第 7 节。
