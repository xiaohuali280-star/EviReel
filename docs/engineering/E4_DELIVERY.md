# E4 交付：评测驱动的检索优化与面试交付

状态：**工具与合成验证交付已验收通过**（第三轮 `E4_THIRD_ACCEPTANCE_REVIEW_20261031.md`，`09a370b`，Standards 0、Spec 0）。E4 的验收范围是“评测工具、合成验证与工程/面试交付”。真实视频、人工标签、真实模型效果、实际费用 / 延迟和人工引用审核仍待补齐，不在这次通过范围内；合成结果不能作为真实准确率提升或成本降低的证明。

验收经过（首轮验收 `E4_ACCEPTANCE_REVIEW_20261031.md`：部分完成、暂不通过，P1×1、P2×2，另有两项建议和一处文档表述；第二轮验收 `E4_REACCEPTANCE_REVIEW_20261031.md`：原三项关闭，新增 P2×1，见第 0.1 节）。代码提交与 CI 见 `STATUS.md`；评测方法和结果见 `EVALUATION.md`；演示见 `DEMO.md`；面试材料见 `INTERVIEW.md`。

## 0. 首轮验收整改

| 问题 | 整改 | 验证 |
| --- | --- | --- |
| **P1** real 评测用新建的进程内限流 + 不持久的评测账本 + 本地租约，没有工作区归属和对象恢复；传入的模型配置与供应商实际读取的全局配置可能不同 | 把 API 启动时的平台装配抽成 `vam/server/assembly.py`，API 和评测共用：PostgreSQL 读租约、Redis 共享限流、PostgreSQL 用量账本（含工作区每日预算和未知结果对账）、查询向量缓存、视频所属工作区、对象存储恢复。评测账本改为**叠加**在平台账本上：先查本轮调用 / token 上限，再由平台账本预占，结算、失败、未知结果都先写平台账本，再记逐题归属。模型调用的工作区取视频所属工作区。没有 `DATABASE_URL` 时拒绝运行，除非显式 `--standalone`（只在没有其他进程使用该 DATA_ROOT 时）。real 模式把同一个 Settings 装为进程设置，供应商、网关、读取器和元数据用同一份配置；元数据记录配置的模型身份、网关实际看到的（操作, 供应商, 模型）及各自计价依据，以及两者是否一致 | `test_e3_platform.py::test_a_real_evaluation_run_uses_the_platform_leases_ledger_workspace_and_object_restore`（真实 PostgreSQL + MinIO：空 DATA_ROOT 从对象存储恢复、读取时 PG 中有租约、调用写入 `model_usage` 且归属视频工作区、本轮上限在平台预占前拒绝、工作区预算仍然生效）；`test_e3_redis.py::test_an_evaluation_run_shares_the_api_rate_window`（真实 Redis，CI）；`test_e4_eval.py` 中账本叠加、零 token、单一设置的单元测试 |
| **P2** 多轮 Agent 对照没有实现，real 模式也直接跳过 | 新增 `agent` 配置：在同一检索上下文（固定版本、同一账本）里运行产品现有的多轮工具调用循环（`vam/retrieval/agent_search.py`，规划 → 检索 → 看帧 → 有依据的回答）。轮次受 `RETRIEVAL_MAX_TURNS`、候选受每通道候选数和 `RETRIEVAL_MAX_EVIDENCE`、看帧受 `RETRIEVAL_INSPECT_FRAMES`、每题受 `CONVERSATION_REQUEST_TIMEOUT_S`、调用受账本上限约束。规划模型可替换（`SearchContext.planner_client`；模拟器为两轮的 `SimPlanner`）。超时、预算耗尽、规划异常、没有最终结果都记为该题的错误而不是回答；有错误的配置在元数据和对比表中标为**不完整**。不再有“跳过但算完成”的配置 | `test_agent_config_runs_the_tool_loop…`、`test_agent_turns_and_candidates_are_bounded`、`test_agent_timeout_budget_exhaustion_and_planner_failure_are_errors_not_answers`、`test_a_run_with_failed_agent_questions_is_marked_incomplete` |
| **P2** 视觉检查失败后仍可能标为 answered | 最终状态由规则决定，不只看模型自报：`answered` 需要回答通过校验、有引用、模型没报证据不足、升级后的评估为足够；需看画面的问题还要求本次请求真的读到了画面（检查返回非空），且至少一条引用指向读到的证据。检查失败、超时、没有可用帧、结果为空、引用的不是读过的证据，都标为 `uncertain`，原因写在 `route.status_reasons`，并加 `visual_evidence_unverified` 警告 | `test_a_visual_question_is_not_answered_when_the_frames_were_not_actually_read`（auto / deep × 失败、超时、空结果、无可用帧，回答替身都给合法引用）、`test_a_visual_answer_must_cite_evidence_whose_frames_were_read` |
| 建议：零 token 被估算替换 | 抽出 `billed_tokens`：只有供应商没报用量（None）时才用估算，预算和报告用同一逻辑 | `test_eval_ledger_counts_a_reported_zero_as_zero_not_as_the_estimate` |
| 建议：`cheap_answer_uncertain` 实际记录的是是否升级 | 分开记录：`cheap_answer_uncertain` 是模型是否报不确定；`escalated` 和 `escalation_reason`（`cheap_answer_uncertain`、`visual_question`、`strong_but_insufficient`、`not_allowed_in_cheap_mode`、`no_related_frames`）是路由决定 | `test_route_records_model_uncertainty_separately_from_the_escalation_decision` |
| 文档：`INTERVIEW.md` 写“每个阶段都经过独立验收”；简历数字缺少“模型模拟器”限定 | 改为“E1–E3 已验收、E4 整改后待复验”；E3 性能数字在介绍和简历条目里都注明“模型调用为模拟器，非真实供应商性能” | — |

整改过程中顺带发现并修正：多轮循环的轮次上限读的是进程全局设置，而不是本次检索上下文的设置（与 P1 的配置不一致是同一类问题），现改为读上下文的设置。

### 0.1 第二轮验收整改：并行调用突破本轮上限

| 问题 | 整改 | 验证 |
| --- | --- | --- |
| **P2** `EvalLedger.reserve` 先检查上限、再做可能阻塞的平台预占、最后才登记，中间没有互斥。两个并行调用会看到同样的剩余额度，平台预占返回后都被发送（验收探针：上限 1 次 / 10 token，实际 2 次 / 20 token） | **检查和占用合为一步**：在锁内检查调用次数和 token 上限，并立即写入一条 `reserving` 行，按估算占住额度；之后才在锁外做平台预占（可能阻塞，不持锁，不会把并行调用串行化）。平台拒绝（工作区预算）、抛异常或被中断时，在锁内删除这条行、归还额度，调用不会发送。结算、失败、未知结果和所有读取都用同一把锁；平台账本写入失败时本地行仍按网关观察到的结果关闭。额度口径：预占中、已预占、未知结果按估算计，已结算按实际计费 token，失败为 0 token，每行都计 1 次调用（失败的尝试也算）。<br>**取消**：调用方在平台预占进行时被取消，预占仍会在线程里完成（`run_sync` 会等它结束）。此前网关不会关闭这笔预占：PG 行要等过期后由对账程序标为未知结果，本地行一直按估算占着额度。现在网关在预占已完成的情况下把它记为失败（`cancelled_before_send`）并释放限流许可，调用不会发送。这是 `vam/gateway/__init__.py` 中异步入口的局部改动，同步入口没有这个时间窗。E3 的共享限流、持久账本和工作区预算不变 | `test_e4_eval.py`：<br>· `test_parallel_calls_cannot_both_take_the_last_share_of_the_run_budget`（最后一次调用 / 最后一份 token，平台预占阻塞时第二个被拒）；<br>· `test_many_threads_never_exceed_the_run_caps`（20 个线程争 5 次额度）；<br>· `test_a_platform_refusal_or_error_after_blocking_gives_the_run_budget_back`（平台阻塞后拒绝 / 抛异常）；<br>· `test_a_call_cancelled_during_the_platform_reservation_is_never_sent_and_is_closed`；<br>· `test_settled_failed_and_unknown_outcomes_count_against_the_run_budget_as_recorded`；<br>每个用例都断言没拿到额度的 `attempt_fn` 没有执行。<br>`test_e3_platform.py`（真实 PostgreSQL）：4 个线程争最后一次额度，只有 1 个在 PG 中预占并发送，逐题归属和工作区预算仍然有效。<br>第二轮的屏障探针 `e4-second-cap-probe.py` 在新代码上 0 个 attempt 执行，但这是因为屏障等不到第二方而超时，不能单独证明修复；第三轮验收用允许获胜者正常完成的正向探针复测（8 个请求争 1 次调用或 10 token，均只有 1 个执行，其余在执行前被拒） |

整改**没有**改变的事实：真实视频、人工标注、真实模型的质量 / 延迟 / 成本、人工引用复核仍然没有做。工具修好不等于真实效果已经验证。

## 1. 一句话结论

E4 建成了可重跑的评测工具，并加了关键词通道和“低成本 / 深度 / 自动”三种回答路径。在合成题集上，用确定性模拟器验证了工具、指标和路由规则；路由规则还根据评测结果改过一次。**真实效果评测没有做**：没有有授权的视频、人工标注和模型预算。所以这里的任何数字都不能当作真实回答质量或成本。

## 2. 条件核对（开始时）

| 条件 | 情况 | 处理 |
| --- | --- | --- |
| 有授权的视频 | 本机唯一的视频是来源不明的第三方课程录像 | 不用于评测，也不上传给模型 |
| 人工标注 | 无 | 不编造。题集全部标为 `synthetic`，与人工标注分开统计 |
| 模型凭据 | 有一个对话/嵌入供应商 key；重排序 key 为空 | 不调用：没有预算授权 |
| 付费预算 | 未授权 | 不做付费批量调用；真实运行需要 `--allow-paid` 并给出调用次数和 token 上限 |

## 3. 实现

### 3.1 评测工具 `vam/evaluation/`

- **题集格式**（`dataset.py`，JSON Lines）：
  - 字段：问题、所属视频、类别（字幕可答 / 需看画面 / 跨片段 / 无答案 / 连续追问）、调参集或保留集、参考证据的时间段与来源（字幕 / 画面 / 事件 / 不限）、自动代理用的关键词；
  - 标签来源 `human | synthetic | model` 必填，统计时分开，模型生成的标签不会算作人工标注；
  - 加载时校验一致性：可答题必须有参考，无答案题不能有，追问必须带历史。
- **指标**（`metrics.py`）：定义和分母写在模块说明里，见 `EVALUATION.md` 第 2 节。无法计算的项给出 `None` 和原因，不记为 0。
- **运行器**（`runner.py`）：
  - 每个配置一次只改一件事（来源、关键词、重排、回答路径），配置表在 `CONFIGS`；
  - 每题写一行原始结果：证据、回答、路由、警告、固定的版本快照、用量；
  - 每次运行写 `metadata.json`：代码版本和是否有未提交改动、环境、题集与语料的 sha256、配置、模型身份、阈值、上限；
  - 另写 `summary.json` 与 `comparison*.md`（全量 / 调参集 / 保留集）；
  - 每个配置先跑一次不计时的预热查询；
  - 可用 `--repeat` 重复测量，用 `--set` 覆盖设置（扫描阈值时用）。
- **用量记账**：运行期间评测账本（`EvalLedger`）占用 ModelGateway 的账本位置，每次模型尝试记一行，按（配置, 重复轮次, 题目）归属。
  - 真实运行时它包着平台的 PostgreSQL 账本：先查本轮调用次数和 token 上限（超限就不发送，也不预占），再由平台账本按工作区预算预占；结算、失败、未知结果先写平台账本。见第 0 节；
  - token 统计只在供应商没报用量时用估算（报 0 就是 0）；
  - 费用只在配置了 `MODEL_PRICES` 时计算，否则报 token 并说明原因；
  - 复用 E3 的网关、账本和计费口径，没有另建计费系统。
- **模拟器**（`simulator.py`）：
  - 组成：词袋哈希“向量”、重叠度“重排”、读取帧标题的“VLM”、按重叠度引用证据的“LLM”；
  - 所有调用都经过 ModelGateway，与真实供应商的调用点相同，带固定的模拟延迟；
  - 模拟器运行关闭限流（它没有需要保护的供应商配额），用临时 DATA_ROOT、进程内限流和内存账本，与中间件隔离；真实运行使用平台装配，保留共享限流，并在汇总中报告通道失败次数；
  - 另有两轮的 `SimPlanner`，驱动多轮 Agent 循环。
- **合成语料**（`fixture.py`、`eval/synthetic/fixture.json`）：两门为评测编写的合成课程文本（中文算法课、Python 代码课），按索引器的表结构写入真实的帧库和字幕库；帧图片的 PNG 文本块里存着标题。

### 3.2 关键词通道 `vam/retrieval/keywords.py`

- **分词**：代码符号保留完整写法并拆成各部分（`uf.find` → `uf.find` / `uf` / `find`，`std::vector` 同理）；中文按两字切分；另有英文词和数字。
- **打分与融合**：对固定版本的字幕块做 BM25，得分除以本次最高分；与其他通道一起做 RRF 融合。每条命中记录“覆盖了问题里多少词”，供路由判断。
- **开关**：默认关闭（`RETRIEVAL_KEYWORD`，请求体 `keyword`）。是否开启取决于真实评测，见第 5 节。
- **不持久化索引**：每个视频最多几千个字幕块，按请求现算。没有表结构变更，回退只需关掉开关。

### 3.3 回答路径 `vam/retrieval/routing.py`、`SearchContext.answer(mode=...)`

| 模式 | 行为 | 模型调用 |
| --- | --- | --- |
| `deep`（默认，E4 前的行为） | 先看证据帧，再生成有依据的回答 | VLM + LLM |
| `cheap` | 只用检索到的证据回答；没有强证据或问题涉及画面时直接拒答 | 最多 1 次 LLM |
| `auto` | 先按规则评估证据：<br>· 足够 → 走低成本路径；<br>· 低成本回答被模型标为证据不足且有相关帧 → 升级一次；<br>· 问题涉及画面，或有强证据但不足 → 升级一次（看帧 + 回答）；<br>· 什么都不强且不涉及画面 → 直接拒答，不调用模型 | 0 次（拒答）到 VLM + 2 次 LLM |

- **强证据**：语义相似度 ≥ `ROUTE_MIN_SIMILARITY`，或关键词覆盖率 ≥ `ROUTE_MIN_KEYWORD_COVERAGE`。
- **涉及画面的问题**：必须看过帧才算足够。这一条是评测后改的：第一版允许“事件摘要或帧相似度高”就算足够，结果在 `sim-all-v1` 中，这类题在低成本路径上全部弃答。
- **有界**：最多升级一轮；看帧数受 `RETRIEVAL_INSPECT_FRAMES` 限制；升级有超时（`ROUTE_ESCALATION_TIMEOUT_S`）；每次模型调用仍受 ModelGateway 的总截止时间、限流和预算约束。
- **最终状态**：`answered` 需要回答通过校验、有引用、模型没报证据不足、升级后的评估为足够；需看画面的问题还要求画面真的被读到，且至少一条引用指向读到的证据。否则为 `uncertain`，原因在 `route.status_reasons`（见第 0 节）。
- **结果字段**：每个回答带 `status`（answered / uncertain / refused）和 `route`（路径、`cheap_answer_uncertain`、`escalated` 与 `escalation_reason`、两次评估、`status_reasons`）。拒答时仍返回检索到的原始证据，可以核对和跳转。
- **API**：`/search` 请求体新增 `answer_mode`、`keyword`，默认值保持 E4 前的行为。前端没有改动。

## 4. 不变的部分（复用 E3）

检索仍在 E3 的版本快照和读租约下进行；每条证据带所属视频和固定版本；回答的引用必须属于本次检索，引用旧版本证据会被拒绝（有测试）。平台模式下的空间权限沿用 E1/E3 的中间件和授权检查（E3 平台测试中两个用户互相 404）。模型调用都经过 ModelGateway。

## 5. 是否引入向量索引：不引入

依据 E3 的压测记录（`docs/engineering/e3-bench/e3-final-nocache/`，每个视频 300 帧、60 个事件、200 个字幕块，向量 1024 维）：

| 阶段 | 并发 1，均值 | 并发 20，均值 |
| --- | --- | --- |
| 帧向量比对（SQLite 读 + 计算） | 0.05 ms | 0.04 ms |
| 事件文档检索 | 3.0 ms | 57 ms |
| 字幕检索 | 15 ms | 365 ms |
| 一次查询 Embedding（模拟器固定值） | 41 ms × 每请求约 4 次 | 同左 |
| 请求 P50 | 251 ms | 1913 ms |

- 无并发时，单个视频的暴力比对只要几毫秒到十几毫秒，大头是 Embedding 调用。
- 高并发时字幕检索变慢，原因是 CPU 争用（每次请求从 SQLite 读出并解码向量），不是相似度计算量。近似索引省掉的只是本来就很小的相似度计算，读取和解码照旧，还要处理索引构建、维度和模型版本匹配、召回下降。
- 结论：在“按视频检索、每视频几百到几千条”的规模下不需要向量索引。
- **重新评估的条件**：做跨视频检索、单个检索范围达到数万条，或真实测量显示相似度计算本身成为瓶颈。更直接的优化是把固定版本的字幕向量矩阵缓存在只读句柄里，这一项没有做，记为候选。

跨视频检索是可选项，本阶段没有做。

## 6. 迁移、部署与回退

- **迁移**：无表结构变更，无新服务。新增设置（`RETRIEVAL_KEYWORD`、`ANSWER_ROUTE`、`ROUTE_*`）默认保持 E4 前的行为。
- **部署**：同 E3（`deploy/e3/compose.yml`）。评测工具是离线命令，不进入线上服务。
- **回退**：把设置改回默认（或请求不传新字段）即可回到 E4 前的行为；代码层面直接回退提交，没有数据要处理。

## 7. 验证证据

见 `STATUS.md` 的 E4 部分。测试分层：

- `tests/test_e4_eval.py`：
  - 分词与 BM25；
  - 路由规则；
  - 指标定义与分母；
  - 题集一致性；
  - 付费运行必须给出上限；
  - 合成语料上的检索不变量：证据都属于请求的视频和固定版本，过滤后为空时拒答且不调用模型，新字幕版本发布后引用旧证据被拒绝，关键词通道召回中文术语和代码符号；
  - `auto` 会升级涉及画面的问题，`cheap` 从不调用 VLM；
  - `/search` 的新参数；
  - 模拟器端到端运行。
- 回归：P3 检索测试与 E1–E3 测试（见 STATUS）。
- 模拟器评测结果：`docs/engineering/e4-eval/`。

## 8. 剩余限制与补齐步骤

- **真实评测未做**。最短补齐步骤：
  1. 准备 3–5 个有授权的课程视频，用平台导入并完成索引；
  2. 按 `eval/synthetic/questions.jsonl` 的格式人工标注 30–50 题，`label.source=human`，写明核对的时间段，分调参集和保留集；
  3. 确认预算后运行 `python -m vam.evaluation run --models real --data-root <DATA_ROOT> --dataset <题集> --allow-paid --max-calls <上限> --max-tokens <上限> --configs all --repeat 2 --out <目录>`，配置 `MODEL_PRICES` 才能算费用；
  4. 在调参集上重新选路由阈值（真实模型的相似度尺度和模拟器不同），在保留集上报告；
  5. 人工复核引用支持率（另列一栏），再决定是否默认开启关键词通道和 `auto`。
- **重排序**：key 为空，真实重排序没有测过。
- **多轮 Agent**：评测入口已实现（第 0 节），只在模拟器和可控替身上跑过；真实规划模型上的效果、轮次和成本没有测过。
- **合成题集的局限**：题目由我按合成语料编写，覆盖面窄；模拟器按词重叠打分，天然偏向关键词通道。它的数字只说明工具和规则在工作。
