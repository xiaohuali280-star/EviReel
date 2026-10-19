# P5 网页与学习接口

## 启动

仓库包含构建后的网页资源，Python wheel 也携带这些资源。安装 Python 3.10+、FFmpeg，配置 `.env` 后执行：

```bash
pip install -e ".[asr]"
uvicorn vam.server.app:app --host 127.0.0.1 --port 8000 --workers 1
```

访问 `http://127.0.0.1:8000/`。`/app/learning?video=v_...` 可直接进入学习页；`/api/...` 和 `/ws/agent` 不走网页回退。没有网页构建时，根路径仍提供 API 状态，`/app/...` 明确返回 503。可用 `FRONTEND_DIST` 指定其他完整构建目录。

模型密钥仅配置在服务器。上传、索引、语音识别、问答的模型和媒体要求见 P1–P4 文档；ASR 默认只加载本地模型，需先准备模型。示例图中的自动笔记采用独立生成任务，不改变 P3 的按需摘要策略。

当前是单工作区应用，没有账号权限系统。开发和本机使用按上例绑定本机；对外部署应由运营方配置认证入口、TLS 和支持 WebSocket/Range 的代理，上传体积及超时应与服务器限额一致。继续使用单 worker 和持久 DATA_ROOT，先备份再迁移。

## 前端开发与构建

项目验证环境使用 Node 24、pnpm 11.25.0；前端依赖固定于锁文件。

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
pnpm run typecheck
pnpm test
pnpm run build
```

开发服务器将 `/api` 和 `/ws` 代理到本机 8000。生产构建直接写入 `vam/server/static/`，提交前同时更新源文件与构建资源。`pip wheel .` 会将 HTML/CSS/JS 和 schema 4 迁移打入安装包，使用者无需再启动 Node 服务。

## 学习任务和导出

| 接口 | 用途 |
| --- | --- |
| `GET /api/ui-config` | 上传格式、字节/时长上限、问题字数、模型是否配置；不返回密钥 |
| `GET /api/videos?q=...&trash=false&offset=0&limit=20` | 全库文件名搜索、分页；搜索文字按字面匹配 |
| `POST /api/videos/{id}/trash` / `restore` | 移入回收站、恢复；任务/读取活动中返回冲突 |
| `DELETE /api/videos/{id}` | 原永久删除语义保留；网页只在回收站确认后调用 |
| `GET /api/videos/{id}/study` | 最新学习任务及最后成功产物，包含旧版标记 |
| `POST /api/videos/{id}/study` | JSON `{"request_id":"r_<32位小写hex>"}`；202 返回持久任务 |
| `GET /api/videos/{id}/study/jobs/{job_id}` | 独立任务状态与已完成批次 |
| `GET /api/videos/{id}/study/artifacts/{artifact_id}/export?format=md` | `md` / `pptx` / `pdf` 下载 |
| `GET /api/videos/{id}/transcripts/export?format=txt` | 语音原文 `txt` / `srt` 下载 |

请求 ID 在同一视频中幂等。每视频只允许一个学习任务；不同视频受 `STUDY_MAX_RUNNING` 限制。任务由服务器持有，切页/断线不取消。服务重启不自动重跑，状态变为 interrupted；显式生成使用新 ID。

默认 `STUDY_AUTO_GENERATE=true`：索引完成且配置了模型服务时生成完整笔记。关闭可避免自动新增此类模型调用；视觉索引和问答原有调用独立存在。单批最多 `STUDY_BATCH_CHARS` 文字和 40 个来源，完整视频最多 `STUDY_MAX_BATCHES` 批；总任务/单次模型超时分别为 `STUDY_TIMEOUT_S` / `STUDY_MODEL_TIMEOUT_S`。课件导出默认最多 `STUDY_MAX_EXPORT_PAGES=500` 页，超限明确返回错误，页面保留学习内容；可下载 Markdown 或调整上限。超预算明确失败，不发布截断笔记。模型内容只是学习辅助，覆盖检查不能证明模型总结无误。

任务优先使用可读语音片段；无语音时使用已有视觉事件文字。无内容时明确失败。模型生成标题和知识点，来源 ID 必须属于当前批且覆盖全部材料，定位时间由实际记录派生。产物最后一次事务发布，失败保留旧成品。重建索引后标为旧版，禁用旧来源导航并允许重新生成。

文稿保留识别原文和片段级时间，不包含人工校对、逐词对齐或说话人分离。课件使用已发布笔记的知识点和来源时间；长文本自动续页。PPTX 使用 Microsoft YaHei，由阅读端字体回退；PDF 使用标准中文 CID 字体。导出不包含原始视频或任意模型附件。

## 问答恢复与界面范围

页面为每视频选择独立会话，保存待处理请求的原始 ID。重连重放同一 ID，并通过 HTTP 查询最终结果；切视频/会话关闭旧订阅并过滤不匹配的事件。失败/重启后的重试使用新 ID。服务端保留原始问题与最终结果，浏览器历史不能代替服务器记录。

回答 Markdown 禁用 HTML 和图片链接；导航只取服务器验证的证据对象。旧版来源有提示，不能点击跳转。上传前与处理中右栏使用同一欢迎组件；上传字节进度不充当模型处理百分比。

搜索目前面向视频文件名；问答面向当前视频。头像为本地空间标识，附件按钮说明已有视频上传入口，没有额外附件、跨视频问答、联网搜索或多用户登录。

## 自动验证

```bash
python -m pytest tests -q
python tests/p5_browser_server.py --port 8875
# 另一个终端
cd frontend
pnpm exec playwright install chromium
pnpm e2e
```

浏览器测试服务器是显式测试程序，提供合成 MP4 和确定性 ASR/模型适配器，生产入口不会加载它。测试覆盖真实上传/SQLite/Range/播放/seek/导出、刷新与断网恢复、多视频隔离、旧版证据与回收站。CI 会自动构建网页、运行 Python/前端/浏览器测试并上传报告及 wheel。
