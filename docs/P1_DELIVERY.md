# EviReel P1：阶段一交付与审查报告

日期：2026-10-17（Asia/Shanghai）
仓库：[xiaohuali280-star/EviReel](https://github.com/xiaohuali280-star/EviReel)
起始提交：`daef28828893879251309c93be20fe7fe6a00841`
本地分支：`codex/p1-video-management`
状态：**P1 实现与自动验证完成，代码仅在独立工作副本，尚未上传 GitHub。**

## 1. 本阶段目标

完成原视频持久化、多视频隔离、视频列表/详情/播放/删除、持久索引任务、重启恢复与合理重试，并让 HTTP 和 WebSocket 检索/摘要明确绑定 video_id。

按用户新的指令，开发仓库由 VAM 改为 EviReel。EviReel 起始 main 已包含 P0 报告和测试，无需等待原 VAM PR 合并。计划已单独上传为 [EviReel 计划草稿 PR #1](https://github.com/xiaohuali280-star/EviReel/pull/1)，该 PR 只有 plan.md，不包含 P1 实现。

本会话没有 Codex Cloud 交互式终端。为满足“阶段一待审核后上传代码”，本阶段在 agent 创建的独立工作副本和隔离 Python 依赖中实现与测试，没有要求用户自己下载仓库或安装依赖，也未修改项目 sources/ 参考材料。实际验证位置为本机 agent 工作环境，不能描述为已通过 Linux 云端 CI。

## 2. 修改文件

共 32 个文件（含本报告），没有实现 ASR、持久会话或 React 网页。

| 文件 | 改动与用途 |
| --- | --- |
| vam/config.py | 资产根目录、上传/时长/并发/超时限制、工具路径、旧 HTTP 开关、独立 Embedding 配置 |
| vam/vision/backends.py | 修复单条接口、统一向量校验、模型指纹 |
| vam/llm/openrouter.py | 独立 Embedding 端点/密钥/模型、响应数量/维度/索引顺序检查 |
| vam/retrieval/frame_store.py | re 导入、层级持久化、事件代表帧保护、schema=2/模型身份、关闭连接、独立片段缓存 |
| vam/retrieval/agent_search.py | 修复原有时间锚点回退中的未定义函数引用与类型引用 |
| vam/runtime.py | 取消任务时等待工作线程收尾，避免清理和写入竞争 |
| vam/video.py | 复用原视觉索引；可注入 store/持久进度、内联执行、受控缓存；全失败不报成功 |
| vam/agent.py | retrieve/summarize/路由编排接收明确 FrameStore，保留库/TUI 默认兼容入口 |
| vam/protocol.py | 新增必须绑定 video_id 的 WS text/tool 请求协议 |
| vam/server/ws/agent_chat.py | 验证视频、清除跨视频连接历史、传入独立库、返回带归属的事件/证据 |
| vam/server/app.py | 生命周期、持久服务、领域错误、HTTP 错误与新路由；旧接口默认禁用 |
| vam/server/api/videos.py | 上传、列表、详情、播放、删除、索引重试及任务查询 |
| vam/server/api/memory.py | 分页帧/事件/摘要、图片与现有视觉检索接口 |
| vam/server/upload_limit.py | Multipart 解析前限制请求体，含分块上传与超限清理 |
| vam/video_registry.py | 视频/任务 SQLite 操作、服务器路径校验、版本发布与恢复 |
| vam/storage/database.py | 短生命周期连接、事务、迁移和单 worker 文件锁 |
| vam/storage/migrations/001_registry.sql | videos / index_jobs 表、外键及每视频唯一运行任务索引 |
| vam/storage/migrations/__init__.py | 将迁移作为安装包资源分发 |
| vam/storage/__init__.py、vam/indexing/__init__.py | 包入口 |
| vam/indexing/media.py | 真实容器/音视频轨检查，浏览器兼容播放副本与时轴检查 |
| vam/indexing/service.py | 有界队列、持久进度、索引版本、重试、租用/删除协调及中断恢复 |
| vam/cli.py | EviReel 启动提示与 WS 消息体上限 |
| tests/test_p0_baseline.py | 移除临时 re 注入，按新 video_id 要求更新 WS 基线 |
| tests/test_p1_video_management.py | 48 项 P1 参数化/集成回归 |
| .github/workflows/p0-baseline.yml | 准备 Ubuntu/Python 3.11 CPU 回归；审核上传后才会实际运行 |
| pyproject.toml、setup.cfg | 同步 dev 测试依赖、SQL 迁移打包规则 |
| .env.example | P1 可配置项示例，不含真实密钥 |
| README.md | EviReel P1 入口说明，保留原 VAM/许可证与 TUI 说明 |
| docs/P1_VIDEO_API.md | HTTP/WS 协议、部署、数据生命周期和兼容性说明 |
| docs/P1_DELIVERY.md | 本报告 |

实施调整：未重写原 JobManager，也未直接改动 vam/jobs.py；新增 PersistentProgress 适配层，将既有索引管线进度写入业务 SQLite。这样保留库/TUI 的原调用方式，同时实现业务任务持久化。

## 3. 核心实现

### 3.1 上传与索引

```text
POST /api/videos
  -> 请求体上限（解析前）+ 流式暂存
  -> 扩展名/文件头 + FFprobe 视频轨、有限时长验证
  -> 服务端生成 video_id，原子保存原视频与 videos 记录
  -> 有界索引队列 + index_jobs 持久记录
  -> 准备 H264/AAC MP4（原片已兼容则复用）
  -> 为本次任务新建独立 FrameStore generation
  -> 原 VAM FFmpeg 抽帧/过滤/Embedding/切分/Event/Retention
  -> 检查有效帧与部分失败
  -> 同一业务事务发布 active_generation 与任务终态
```

原视频不会在索引 finally 中删除。全部向量失败、无可用帧或事件生成失败记录 failed；部分向量失败或最大帧数截断为 partial。旧可用索引仅在新版本成功时切换；新版本失败时保留旧索引。队列满时保留上传成功的视频并返回 job_id=null/index_error，后续可显式重试。

任务状态有 queued/running/succeeded/partial/failed/interrupted、phase、进度、attempt、结果与错误。重启将遗留 queued/running 标记 interrupted，显式重试，不自动无限重复付费调用。单 worker 锁避免另一进程误把正常任务标成中断。

### 3.2 隔离与读取

各视频原片、播放副本、generation 数据库和缓存位于自己的受控目录。客户端不传 store_path/video_path；旧 /api/frames 路由默认不挂载。

HTTP visual search 和 WS retrieve/summarize 经 video_id 解析到明确的库，响应证据也有 video_id。两个视频可使用相同原文件名和 v-000001 帧编号，不互相污染。切换 WS 视频时清空连接内旧历史。P4 的 conversation_id、持久会话和顶层指代解析尚未实现。

### 3.3 播放、删除与重建

播放通过受控 MP4 路由，支持 Range、206、后缀范围和 416。播放下载、问答与摘要持有资源租用，删除/冲突重建返回 409。删除只移除该视频的原片、副本、数据库/WAL/SHM、缓存和关联任务；失败进入 delete_failed，可重试。

读取固定索引版本；旧图片链接携带版本，重建后返回 stale_evidence，而不是错误显示相同帧编号的新图。旧成功版本暂保留到视频删除，以免破坏在途引用。

### 3.4 P0 缺陷和兼容修复

- 补齐 re 导入，Event/Summary 不再因此 NameError。
- 单文本/单图接口返回一维向量；数量、维度、有限值、FP16 范围和响应 index 校验。
- retention 仅改变 memory_tier 时也会写入 SQLite；两帧及以下也按视频时间计算。事件代表帧通过 Event 文档关联保护，不再借用 long 时间标签。
- 明确 document/query 输入用途，保存模型指纹；不同模型即使维度相同也拒绝混用。指纹不含 URL 密码/查询密钥。
- 旧非空、无身份的 SQLite/JSON 不自动猜测模型；提示备份重建，拒绝时保留旧 SQLite schema。
- 修复 SQLite with 块未关闭连接的问题，解决 Windows 删除时的文件占用。
- 使用新版 FFmpeg 的 fps_mode 参数，工具路径和子进程超时可配置。
- 修复另一个原有缺陷：时间锚点回退分支引用不存在的 _source_bucket。
- 默认旧库按需解析，避免未使用的遗留库阻止新视频管理 API 启动。

## 4. 测试结果

### 4.1 最终自动回归

**63 passed，0 failures，0 errors，0 skipped；终端总耗时 46.83 秒。**

- 原图片限制测试：3 项。
- P0 回归：12 项；P0 的 re 诊断注入已移除，真实修复路径执行。
- 新 P1 合约/集成回归：48 项（含参数化案例）。
- 唯一 warning 为测试库提示 httpx/TestClient 适配将来弃用；没有影响本次通过结果。

覆盖内容：同名视频/同帧编号隔离，原片保存，H264 复用与转码，Range/206/416，失败重建/重试和旧库保留，删除占用/失败恢复，坏文件/无视频轨/超限/时长/磁盘失败/分块上传，服务器路径注入防护，时间范围，向量/模型/schema 校验，任务重启/单 worker/有界队列，partial 状态，HTTP/WS 视频归属、跨视频历史清理和摘要隔离。

实际执行入口（包装器只装载隔离依赖、临时资产目录、FFmpeg PATH 和空模型密钥）：

```powershell
& '<bundled-python-3.12>/python.exe' work/run_p1_tests.py tests -q --disable-warnings --junitxml=../../outputs/P1_TEST_RESULTS.xml -o faulthandler_timeout=30
```

标准安装环境对应执行：

```bash
python -m pytest tests -q
python -m compileall -q vam tests
```

本次确实执行 compileall；全库未定义名/语法检查与新增代码完整 F 类静态检查通过，git diff --check 通过。构建 wheel 并从构建产物加载 VideoRegistry，确认 SQL 迁移资源实际包含在包中且能初始化全新业务库。

### 4.2 实际环境与浏览器

Windows agent 隔离环境：Python 3.12.14，SQLite 3.53.1，torch 2.14.1+cpu，CUDA=False；FastAPI 0.143.3，NumPy 1.26.4，OpenCV 4.10.0，pytest 9.1.1，FFmpeg/FFprobe 9.0.2。

本机异步事件循环需要回环连接，沙箱中最初卡在 socketpair 初始化；授权测试进程使用回环后完成。该环境问题没有被算作源码失败或测试通过。

真实浏览器通过本机临时验证页加载 P1 播放 API：

- 6 秒 H264 视频：duration=6，readyState=4，seekable=1，error=null。
- 点击时间按钮后 currentTime=3.2，画面实际定位到 00:00:03.200。
- 点击播放后推进到 currentTime=6，decoded frames=154，error=null。
- 临时验证页不在产品源码中，不属于 P5 前端；验证完成后已停止临时服务。

### 4.3 三种完成状态

| 状态 | 结论 |
| --- | --- |
| 代码已实现 | P1 视频资产、隔离、持久任务、播放、重试/删除、受控 HTTP/WS 及基础修复完成 |
| 自动测试通过 | 63 项；真实 SQLite/FFmpeg/HTTP/WS，模型为非零确定性 Mock/Mock HTTP/Mock VLM |
| 真实 API 已验证 | **没有**；未使用付费 Embedding/VLM/LLM 密钥，未验证真实课程问答质量 |
| 云端 Linux CI | 流程已准备，**尚未执行 P1**，因为用户要求审核前不上传代码 |
| 浏览器媒体验证 | 真实播放与时间跳转通过；不是完整 React 流程验收 |

此前 60/61 项的回归是中间结果；最终以 63 项和对应 JUnit 文件为准。

## 5. 当前问题、风险与限制

1. 真实模型 API、图文向量空间兼容性和课程视频问答仍待有凭据/样本时验证；本阶段未编造效果指标或费用。
2. P1 Linux CI 尚未运行。工作流已备好；审核上传后应首先检查云端回归再审查合并。
3. 原 FrameStore 仍使用全量快照持久化、内存候选检索；长课程的内存/延迟/磁盘占用尚未基准测试，不宣称大规模并发能力。
4. 成功旧索引版本暂不自动回收，保留到删除视频；高频重建会增加磁盘占用。
5. 没有登录系统、分布式任务队列、自动模型重试或跨进程 worker；同 DATA_ROOT 只支持一个服务进程。
6. 旧库不具备可靠模型身份时需备份并重建。原片/旧视频知识不因新索引失败而被删除。
7. 连接内历史仍不持久化；断线丢失、顶层历史指代属于 P4。ASR/Transcript、RRF/Rerank 和 React 分别属于 P2/P3/P5。
8. wheel 构建保留原项目的许可证 TOML 弃用提示与双配置依赖提示；构建和迁移资源验证通过，不影响本次安装产物。
9. 关闭服务时必须等待工作线程收尾；视频处理/模型调用的最坏关闭时间受已配置的超时/重试上限影响。

## 6. GitHub 与审查交付

- 唯一已上传的新变更：[EviReel 计划草稿 PR #1](https://github.com/xiaohuali280-star/EviReel/pull/1)，分支 codex/plan-p1-p5，仅包含 plan.md。
- P1 在本地独立分支 codex/p1-video-management 保存提交，供审查与恢复；**未 push、未创建 P1 GitHub PR、未合并 main**。
- 交付审查报告、完整源码 zip、差异 patch、JUnit 结果、浏览器截图；运行数据/模型依赖/真实密钥不进入源码包。
- 用户审核通过后才上传 P1 分支并创建草稿 PR。主分支保持原状，后续也只使用 EviReel，不再修改原 VAM 仓库。

## 7. 下一阶段计划（未执行）

P2 将新增 FFmpeg 音频提取、faster-whisper/Mock Transcriber、时间戳规范化、字幕 Chunk、持久化与幂等索引、字幕语义查询，依赖 P1 的原片、video_id、任务状态与隔离模型配置。

本阶段到此停止。审核 P1 不代表自动开始 P2；需要用户明确发出下一阶段指令。
