# EviReel 演示脚本（约 10 分钟）

顺序：弱网续传 → Worker 中断恢复 → 热门视频并发 → 带证据问答与成本/效果对比。每一段都写明“看什么”和“证据在哪”。演示只说已验证的事；标为“未实测”的不要讲成结果。

## 0. 准备（演示前完成，不计时）

Linux / WSL2，完整栈（两个 API + nginx + Redis + Worker）：

```bash
cp deploy/e1/dev.env.example deploy/e1/dev.env && cat deploy/e2/dev.env.example >> deploy/e1/dev.env   # 改成自己的开发口令
docker compose --env-file deploy/e1/dev.env -f deploy/e3/compose.yml up -d --build --wait
docker compose --env-file deploy/e1/dev.env -f deploy/e3/compose.yml exec api-1 \
  sh -c 'EVIREEL_NEW_PASSWORD=<口令> python -m vam.platform.migrate create-user --username demo'
```

- 浏览器打开 `http://127.0.0.1:8000/app/`，用 demo 登录。
- 准备一个**有授权**的课程视频（几十 MB 以上，续传才看得出效果）。不要用来源不明的录像。
- 问答需要配置模型凭据（`.env` 中的供应商 key）；没有凭据时第 4 段只演示检索和评测工具。

## 1. 弱网续传（约 2.5 分钟）

1. 上传视频，进度到一半左右时用浏览器开发者工具把网络切成 Offline（或直接拔网线）。
2. 刷新页面，恢复网络，重新选择同一个文件。
3. **看什么**：页面提示“发现未完成的上传”；只补传缺失分片，已传分片不再发送（开发者工具 Network 里只有剩余分片的 PUT）。
4. 再试一次：选一个不同的文件续传，会被拒绝（`file_mismatch`），不发送任何字节。

讲解要点：分片直传对象存储、PostgreSQL 记账、ListParts 对账、续传前前端逐片核对已传分片的 MD5。

证据：`docs/engineering/E1_DELIVERY.md` 第 3、6 节；`tests/test_e1_platform.py::test_resume_sends_only_missing_parts_and_new_video_is_playable`；浏览器用例 `frontend/e2e/upload-resume.spec.ts`。

## 2. Worker 中断恢复（约 2.5 分钟）

1. 上传完成后视频进入处理。在 ASR 阶段执行：
   ```bash
   docker compose --env-file deploy/e1/dev.env -f deploy/e3/compose.yml kill worker-media
   docker compose --env-file deploy/e1/dev.env -f deploy/e3/compose.yml start worker-media
   ```
2. 打开 `GET /api/jobs/{job_id}`，看 `pipeline.attempts`：被杀的那次是 `lease_lost`，之后新的 attempt 完成。
3. **看什么**：任务最终完成；已完成的检查点（ASR 结果、Embedding 批次）被复用，没有从头算。

讲解要点：任务账本 + Outbox、阶段租约和 attempt token（旧执行者不能覆盖新结果）、批次检查点。

证据：`docs/engineering/E2_DELIVERY.md` 第 4–7、9 节；Linux CI 中的 14 个故障测试（`tests/test_e2_faults.py`，含 kill -9 和 RabbitMQ 停启）。

## 3. 热门视频并发（约 2 分钟）

1. 展示压测对照（不现场跑，结果在仓库里）：`docs/engineering/e3-bench/`。
2. 现场：`docker compose ... stop api-1`，页面继续可用（nginx 转到 api-2），再 `start api-1`。

**说的数字**（本机单实例、模型模拟器、关缓存；不是线上容量）：同一视频 20 并发，P95 从 17.9 秒降到 2.4 秒；固定每秒 5 个请求时，改造前只能处理 1.2 个/秒、P99 64 秒，改造后跟得上、P99 0.32 秒。

讲解要点：去掉每视频一把锁、版本快照 + 读租约、删除协议、ModelGateway 全局限流。

证据：`docs/engineering/E3_DELIVERY.md` 第 8 节；E3 CI 容器冒烟（停掉一个 API 后 20/20 请求成功）。

## 4. 带证据问答与成本/效果对比（约 3 分钟）

1. 在页面提问一个字幕里能回答的问题，展示引用的时间点，点击跳转到视频对应位置。
2. 用 API 演示三种回答路径（低成本 / 深度 / 自动）：
   ```bash
   curl -s -X POST http://127.0.0.1:8000/api/videos/<video_id>/search -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" -d '{"query":"<问题>","answer":true,"answer_mode":"auto","keyword":true}'
   ```
   看返回的 `status`（answered / uncertain / refused）和 `route`（走了哪条路径、是否升级到看画面）。问一个视频里没有的问题，应得到 `refused`，且没有模型调用。
3. 展示评测工具（离线、可重跑）：
   ```bash
   python -m vam.evaluation run --dataset eval/synthetic/questions.jsonl --fixture eval/synthetic/fixture.json \
       --models simulator --configs all --out /tmp/eval-demo
   ```
   打开生成的 `comparison.md`。

**必须说明**：这里的数字来自合成题集 + 确定性模拟器，只证明评测工具和路由逻辑能正确工作，**不代表真实回答效果**。真实评测需要有授权的视频、人工标注和模型预算，目前未完成（缺口与补齐步骤见 `docs/engineering/EVALUATION.md`）。

证据：`docs/engineering/EVALUATION.md`、`docs/engineering/E4_DELIVERY.md`、`docs/engineering/e4-eval/`。

## 演示中可能被问到

- “为什么不用 Kubernetes？” 见 `INTERVIEW.md` 追问部分。
- “模拟器的数字有什么用？” 证明指标计算、分母、版本固定、路由规则和用量记账是对的；真实效果要等真实评测。
