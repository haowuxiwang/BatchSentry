# 运行时看门狗：是否需要，以及做到什么程度

> 结论先行：**需要，但要分层**。当前最痛的缺陷不是"没有看门狗"，而是
> **一个无上限的等待点**（P0，已修）；运行期间的自动巡检（P1）经需求方确认后
> 也已实施（`core/watchdog.py`，schema v12）。P2（SSE 停滞可见性）仍未做。
> 本文保留完整的判定依据、实测数据与分层过程 —— 包括**实施中踩到并被用例
> 当场抓出的两个坑**，它们的价值不低于结论本身。
>
> **第二轮（实施后回审，§8，2026-09-16）**：P1 自己还有 5 项缺陷，其中三项
> 属严重 —— 收敛只改状态不终止 task（会让 retry 永久卡在 pending）、OCR 阈值
> 低于上游封顶、`cancelling` 阈值低于它要等的 CPU 封顶（会把"已正常取消"的
> job 改成 error）。均已修 + 配机检。第 5 项是被第 2 项的不变式**顺带揪出来的**。

## 1. 问题定义

评估"是否需要运行时看门狗"，等价于回答三个问题：

1. 是否存在**没有任何超时兜底**的等待点？（有 → 必然能永久卡死）
2. 卡死时**用户能看到什么**？（无声转圈 = 最差）
3. 兜底动作**能否自动执行**？（只在启动时恢复 = 不重启永不恢复）

## 2. 现状定位（每条带 file:line 或实测）

### 2.1 执行模型：pipeline 与 API 同进程、同事件循环

- `core/pipeline/engine.py:65` —— `task = asyncio.create_task(_run_pipeline(job_id, pdf_path))`
- `main.py:80` —— `recover_stuck_jobs` **只在启动 lifespan 调用一次**

推论：**不重启应用，就没有任何自动兜底**。一个卡死的 job 会永久停在非终态。

### 2.2 唯一的无限等待入口：`run_cpu`

超时覆盖面实测矩阵（`grep -rn timeout`）：

| 外部/内部调用 | 超时 | 依据 |
|---|---|---|
| LLM 适配器 | **180s** | `llm/adapters/{openai,anthropic}_adapter.py` |
| MinerU 上传/轮询/下载 | 300 / 30 / 300s | `core/mineru_client.py:124,151,184,304` |
| Paddle OCR 提交 | 动态 120s + 3s/MB | `core/ocr_client.py:79` |
| Paddle OCR 轮询 | 按页数计算，**封顶 3600s** | `core/ocr_client.py:42-47` |
| **`run_cpu`（进程池路径）** | **无** | `core/procpool.py`（修复前） |
| **`run_cpu`（线程回退）** | **无** | 同上 |

结论：**外部调用都有超时，唯独本地 CPU 重活（Stage 0 规范化）没有任何上限**。
所以"永久卡死"不是理论担忧，而是这个入口的必然结果。

### 2.3 放大效应：单 worker 进程池会被一个卡死任务占死

`core/procpool.py` 的池是 `max_workers=1`（设计如此：规范化是串行批处理，
多 worker 只增加内存峰值）。这意味着：

```
一个 job 的 Stage 0 挂死 → 唯一槽位被永久占用
                        → 后续所有 job 的 Stage 0 提交全部排队
```

表现从"某个 job 卡住"扩散成"整个应用不再处理新任务"。
这一点项目注释里已经预见（`core/pipeline/stage1.py:45-48`：
"procpool 单 worker 会连带阻塞后续任务的提交"），但没有对应的超时措施。

### 2.4 卡死时用户视角：SSE 无限等待

- `api/jobs/status.py:310` —— `while True:`，只有状态进入 `_TERMINAL_STATUSES`
  才 `return`（`:341-344`）
- 没有任何"停滞多久就提示/放弃"的逻辑

所以卡死的用户体验是：**进度条永远停在某个百分比，无提示、无错误、无出路**
（`cancelling` 若也卡住，连取消都不生效 —— 这正是 `_STUCK_STATUSES` 里包含
`cancelling` 的原因，`core/pipeline/state.py:135`）。

## 3. 判据缺失：当前无法区分"在跑"与"卡死"

要实现运行时看门狗，必须先能回答"这个 job 最后动过是什么时候"。

| 候选信号 | 粒度 | 结论 |
|---|---|---|
| `jobs.created_at` | 只有起点 | ❌ 无法反映进展 |
| `jobs.finished_at` | 只有终点 | ❌ 非终态时为空 |
| `jobs.ocr_progress`（JSON） | Stage 1 实时 | ⚠️ **只有值、没有时间戳** |
| `audit_log.created_at` | 阶段级事件 | ⚠️ 粒度粗：51 页分析期间可能数十分钟无写入 |

**没有任何一列记录"最后活动时间"** → 看门狗无法判定，除非改 schema。
这是本文最重要的实现约束。

> **已解决（v12）**：`jobs.last_activity_at` 就是为回答这个问题而加的列。
> 注意 `audit_log` 那条仍然成立 —— 它是**弱信号**，不能拿来当判据（见 §5
> "不推荐的做法"），现在的判据只认 `last_activity_at`。

## 4. 业界范式（对标）

主流任务系统的做法高度一致，四条可直接借鉴：

1. **心跳必须绑定"前进"，不能是纯计时器**
   —— "Beat on a timer and the pulse continues during a stall, so the watchdog
   never fires. Always tie the pulse to a forward advance."
   对应本项目：心跳应写在**进度推进点**（OCR 轮询每轮、每页分析完成），
   而不是独立的定时器。
2. **看门狗不应与被观察对象同进程**
   —— "putting the watchdog inside the same process as the agent... the built-in
   timer freezes too."
   对本项目的含义：同一事件循环内的定时器，只能捕获"单任务等待挂起"
   （loop 仍在转），**捕获不了"事件循环停摆"**（GIL 饿死那类，已由 procpool
   的进程隔离解决）。两类问题需要不同手段，不能指望一个看门狗全包。
3. **阈值必须按任务类型分级**
   —— 用同一个 `stall_sec` 衡量 30 秒的格式化任务和 5 分钟的大生成任务，
   必然误伤其一。对应本项目：`ocr_running` 的合理阈值应参考 Paddle 的
   页数相关轮询上限，`analyzing` 应参考 页数 × LLM 超时。
4. **max runtime + 两阶段终止**
   —— 先给优雅终止机会（清理临时产物、释放锁、写"aborted"标记），
   再强杀。对应本项目已有的 `recover_stuck_jobs` 模式（标记 error + 审计 +
   通知），可直接复用同一路径。

另可对照：Sidekiq 用"每 5-10s 写心跳、60s 过期"判定进程 dead；
SQS/Celery 用 visibility timeout 让"超时未 ACK"的任务重新可见。
两者都是"**心跳缺失即判定**"，而非"主动探测是否卡了"。

## 5. 结论与方案分层

### P0 —— 已实施（无争议，属缺陷修复）

`core/procpool.py`：给 `run_cpu` 加单任务超时 + **进程池回收**。

- 默认 1800s（`PBC_CPU_TASK_TIMEOUT_S` 可覆盖）—— 定位是"永久等待兜底"
  而非性能限制，故留足一个数量级余量（实测 138MB/51 页规范化在百秒量级）
- 超时后**必须回收池**：仅让调用方不再等待（`future.cancel`）不会释放
  单 worker 槽位；不回收就等于把"某个 job 卡住"变成"应用不再接活"
- 线程回退路径无法强杀线程，故只能止损（抛错让 job 走 error 路径）
- 护栏：`tests/unit/test_procpool.py::TestCpuTaskTimeout`
  + `::TestProcessPoolTimeoutRecycle`（含"回收后可重建"）

### P1 —— **已实施（2026-09-16，用户确认后开工）**

模块 `core/watchdog.py` + schema v12：

1. `jobs` 加 `last_activity_at`（v11 → v12；迁移体只加列，schema.sql 是 DDL 唯一声明处）
2. 在**进度推进点**写心跳：`state.touch_activity`（状态迁移 + OCR / 自愈 / 跨页
   三类进度更新）+ Stage 2 单页分析完成（含解析失败页）+ `upload.py` 建 job。
   **绑定前进，不是定时器**
3. 后台周期任务（默认 60s，`PBC_WATCHDOG_INTERVAL_S`）扫描非终态 job，按状态分级阈值判定
4. 判定超时 → 复用 `recover_stuck_jobs` 同款动作（条件 `UPDATE` + 审计
   `watchdog_stall_recovery` + 锁外通知）
5. 阈值全部可配置（`PBC_WATCHDOG_SCALE` 整体缩放），默认保守

**实施中定下的两条硬约束**（比原方案更严格）：

- **`pending` 移出监视范围**。运行期间 `pending` 是**"已建单、pipeline 尚未
  推进"的过渡态**（上传与重试都是先写 `pending`、紧接着 `launch_pipeline`），
  把它当停滞会**误杀刚上传的任务**；崩溃残留的 pending 由启动恢复兜底。
  原方案把 `_STUCK_STATUSES` 整体当监视集，是错的。
  - ⚠️ 更正（§8.3，2026-09-16）：原文写"运行期间 pending 是合法排队态
    （等 `MAX_CONCURRENT_JOBS` 槽位）"—— 与代码不符。`MAX_CONCURRENT_JOBS`
    是**拒绝**（upload/retry 直接 409），没有排队机制。结论不变（仍不默认
    监视），但机制描述已按实际更正，并补了"已被 pipeline 接管"这一例外。
- **`last_activity_at IS NULL` / 不可解析 → 跳过**。不用 `created_at` 兜底：
  那会让"正常跑了很久的大文档"被判成停滞。宁缺勿错。

**实施中踩到并修掉的两个坑**（都被本轮新增用例当场抓出，不是事后发现的）：

| 坑 | 后果 | 修法 / 护栏 |
|---|---|---|
| `recover_stalled_jobs` 首版在 `db_lock` **内**调 `_audit_log`，而后者也取同一把锁（`asyncio.Lock` 不可重入） | **永久死锁**（测试跑成 5 分钟无输出） | 审计移到锁外；`test_marks_error_audits_and_notifies` |
| `_migrate_v12` 首版被贴在 `_migrate_v11` 的 `if current_version < 11:` 块内 | v11 库**跳过迁移**，但 `PRAGMA user_version` 仍无条件写 12 → **库标成 v12 却缺列**（静默 schema 漂移，看门狗永久失明且不报错） | 补同号守卫；通用护栏 `test_every_migration_version_has_its_own_guard`（每个版本号必须有同号守卫 + 同号调用） |

阈值分级（默认值，`PBC_WATCHDOG_SCALE` 可整体缩放）：

| 状态 | 基准 | 页数增量 | 封顶 | 实测参照 |
|---|---|---|---|---|
| `ocr_running` | 4200s | +120s/页 | 10800s | 51 页真实件 644s（10× 余量）；4 页旋转自愈轮 1031s |
| `analyzing` | 1800s | +180s/页 | 10800s | 51 页真实件 825–1209s（9× 余量）|
| `ocr_done` | 1800s | — | — | 纯过渡态，正常 <1s |
| `cancelling` | 2400s | — | — | 取消要等 Stage 0 收尾，封顶 = CPU 重活超时 1800s（§8.5）|
| `pending` | **不监视** | — | — | 已建单未推进的过渡态；**例外**：已被 pipeline 接管（§8.3）|

> ⚠️ `ocr_running` / `cancelling` 两个基准在 v1.1.3 首版分别写成 1800s / 900s，
> 都**低于**它们所覆盖的上游封顶 —— 属 §8.2 / §8.5 的缺陷，v1.1.4 已按
> "基准 ≥ 上游封顶 + 余量"的不变式修正。

**仍然不做的**：不动"事件循环整停"那一类（GIL 饿死）—— 同循环内的定时器
也停摆，捕获不了。那类问题由 `core/procpool.py` 的进程隔离解决（Round 15），
两者是不同手段，不能互相替代（对照本文件 §4 第 2 条）。

### P2 —— 可选（不依赖 schema）

SSE 停滞可见性：服务端记录"同一状态已持续多久"（内存计数即可），
超过阈值时推送一个**普通 message 帧**（注意不能用 `event: error` ——
SSE 规范中 error 是保留类型，浏览器会立即断连且不暴露 data，
见 `api/jobs/status.py:328-330` 的既有注释）。前端据此显示
"任务似乎停滞，可取消或重试"，把"无声转圈"变成"有信息的选择"。

### 不推荐的做法

用 `audit_log` 最后时间 + `ocr_progress` 非空做**弱信号**推断卡死：
不用改 schema，但粒度不足以支撑判定 —— 51 页分析期间 audit_log 可能
数十分钟无写入，任何基于它的阈值都会误杀。省下的 schema 变更代价，
会以"误杀正常任务"的形式还回去。

## 6. 本机实测（P0 验证）

```
tests/unit/test_procpool.py  ................ 14 passed
日志：CPU process pool recycled (hang timed out after 0.5s) — a fresh pool will be created
```

## 7. 相关既有结论

- 其余对抗审查维度见 `docs/ADVERSARIAL_AUDIT.md`
- 降噪现状见 `docs/NOISE_REDUCTION_TODO.md`

## 8. 实施后第二轮回审（2026-09-16）

P1 落地并打包进 v1.1.3 之后，用**同一组三个问题**（§1）重新审视它 ——
但这次对象是 P1 自身：**它的收敛动作真的收敛吗？它的阈值真的不会误杀吗？**
结论是都不能：发现 5 项缺陷（三项严重），全部已修并配了机检。本节的证据
全部为实测。其中 8.5 是被 8.2 提炼出的**不变式**顺带揪出来的 —— 这条收益
说明"把判据写成可推导的规则"比"逐个调参"值钱。

### 8.1 【严重】只改状态、不终止 task → 看门狗自己造出"永久 pending"

链路是这样的：

```
run_pipeline 用 per-job 锁串行同一 job 的 pipeline
retry 端点: error → pending，然后 launch_pipeline(job_id, ...)
孤儿 task 仍持有那把锁 → retry 永远停在 `async with lock`（该等待点没有任何上限）
而 pending 按 §5 的硬约束**不被监视** → 永久无声
```

也就是说：**用户照着看门狗的提示（"已标记为失败供重试"）点了重试，换来的
是一个永远不动、也不会有任何提示的 pending。** 看门狗自己的恢复动作，
制造了它本要消灭的那种状态。这条只有把"恢复"放在运行期语境下才会暴露 ——
启动恢复（`recover_stuck_jobs`）不会有这个问题，因为那时进程里没有活 task。

旁证：`core/pipeline/engine.py` 的取消路径**早就承认**同类风险并做了处理
（"取消已排队的分析任务：否则孤儿协程继续跑 LLM 并写入已取消的 job
（对抗审查 — retry 后新旧 pipeline 对同一页写入互相竞争）"）——
所以这是"已知风险类"在新增的看门狗路径上被漏掉了，不是新问题。

附带损害：孤儿还会继续写 `ocr_progress` / `last_activity_at` / `page_cache`
（这些 UPDATE **不带状态条件**），于是**重试轮**的心跳会被孤儿的写入刷新成
"在动" —— 既污染数据，也可能掩盖重试轮真正的停滞。

**修法**：收敛后 `task.cancel()` + `asyncio.wait(timeout=10s)`
（`watchdog._terminate_pipeline_task`），处置结论写进审计
（`orphan_task=cancelled|no-task|not-exited`，GMP 可追溯 + 现场排障）。
两处顺序约束：

- 终止必须在 `db_lock` **之外** —— pipeline 的 `CancelledError` 分支自己要
  `transition_status`（取同一把锁），持锁等待就是死锁（同 8.1 的兄弟坑，
  模块首版已在 `_audit_log` 上踩过一次）；
- 状态 UPDATE 必须**先于**终止 —— 否则 pipeline 自己的
  `CancelledError → transition_status("error")` 会先落库，我们随后那条带
  `status = ?` 条件的 UPDATE 就影响 0 行，观众看到的会是通用的"流水线中断"
  而不是看门狗的判定与审计。

**护栏**：`test_watchdog.py::TestOrphanTaskTermination`，其中
`test_terminating_orphan_releases_per_job_lock` 用一个**真的按住 per-job 锁
且永不退出**的 task 冒充孤儿，断言收敛后锁已释放、`lock.acquire()` 能拿到
—— 这是死结被解开的正面证明，而不只是"调用了 cancel"。

### 8.2 【严重】OCR 阈值低于上游封顶 → 会把"上游还在正常等待"判成停滞

`POLL_TIMEOUT_MAX = 3600`（`core/ocr_client.py`，单任务轮询上限 1 小时），
而 P1 的 OCR 基准写的是 **1800s** —— 看门狗可以抢在**上游自己超时之前**动手。

量化（这才是判定依据）：空页自愈是**逐页**写心跳的
（`self_heal._report_heal_progress`），所以心跳缺口的**上界 = 单页补救预算**：

```
候选旋转角 3 个（_ROTATION_CANDIDATES）× 单页探测封顶 630s = 1890s
+ 该页重分析的 ~180-240s
≈ 2100s
```

| | 旧阈值（-1800） | 合法缺口上界 | 余量 |
|---|---|---|---|
| 1 页文档 | 1920s | ~2100s | **−180s（必误杀）** |
| 4 页文档 | 2280s | ~2100s | +8.6% |

实测旁证：4 页旋转自愈轮整轮 **1031s**（且这一轮里还跑完了第二轮自愈）。

**修法**：基准改为 `POLL_TIMEOUT_MAX + 余量 600 = 4200s`，并把关系写成
**不变式**而不是注释 —— "基准阈值必须 ≥ 它覆盖的那次上游调用自己的封顶"。
护栏 `TestThresholdsAboveUpstreamCaps` **不重复字面数字**，而是从真值源推导：
`core.ocr_client.POLL_TIMEOUT_MAX`、`core.procpool._DEFAULT_TIMEOUT_S`、
`llm.adapters.base.LLMAdapter.chat` 的 `timeout` 默认值，以及
`len(_ROTATION_CANDIDATES) × poll_timeout_for(1 页)`。上游封顶改了，护栏自动
跟着走；看门狗基准被改小到封顶之下，护栏立刻红。

**代价与补偿**：真停滞的判定变慢（1 页文档 32 分钟 → 70 分钟）。按 §5 既定
策略（"晚判几分钟毫无代价，误杀代价极高"）这个交换是对的，但**用户可见的
"停滞提示"就因此从"可选"变成了"应当做"** —— 见 8.5。

### 8.3 【中】"被接管的 pending"不可见

即使 8.1 的终止在极端情况下失败（task 10s 内不退出），仍会留下
"`pending` + 活 task"这一组合，而它按 §5 是不判的。补一条**收窄**规则：

> `pending` 只在 `_pipeline_tasks` 里**有未完成 task** 时纳入判定。

合法排队永远没有注册 task（`launch_pipeline` 才登记），所以这条**不会**
误杀排队上传 —— 它判的正是"已被接管却没动静"的那一类。阈值 900s
（正常只在毫秒级停留，见 `stall_limit_seconds`）。

**护栏**：`TestPendingTakeover` 三个相反用例 —— 无 task 不判（哪怕心跳烂了
几十年）、有 task 判、心跳新鲜不判。

### 8.4 【中】e2e 轮次预算写死单值 → 两次把真实长跑判成失败

同一类缺陷**已经犯过两次**，两次都是"写死单值"：

| 时间 | 现象 | 真相 |
|---|---|---|
| 2026-09-04 | rot 轮 620s > 默认 600s，被误杀 20s | 旋转证据本身完整 |
| 2026-09-16 | pdf 轮 835s > 默认 600s，判超时 | 该 job 之后正常进 `review`，835s 是真实耗时 |

固定值的病根是它**同时**对小文档太紧、对大文档太松。修法是把预算表达成
"基线 + 每页 × 页数"（与产品侧 `core/watchdog.py` 同一思路），斜率按各轮
**实测每页成本**取且全部大于观测值：

| 轮次 | 基线 | 每页 | 6/4/51 页的预算 | 相对实测余量 |
|---|---|---|---|---|
| `pdf` | 900s | 180s | 1980s @6 页 | 2.4×（实测 835s）|
| `rot` | 1200s | 300s | 2400s @4 页 | 2.3×（实测 1031s）|
| `real` | 1800s | 120s | 7920s @51 页 | 4.0×（实测 ~1975s）|

页数读不出来时按实测最大真实件（51 页）保守回退；`E2E_*_TIMEOUT` 仍可覆盖。
**护栏**：`tests/unit/test_e2e_round_budget.py`，含"不得再出现写死的默认值"
的静态检查（正则匹配 `E2E_*_TIMEOUT", "<数字>"`）与"调用点不得引用已删除
常量"的作用域感知 AST 检查。

### 8.5 【严重】`cancelling` 阈值（900s）低于它要等的那个封顶（1800s）

这条是**被 8.2 的不变式顺带揪出来的** —— 把"基准必须 ≥ 它所覆盖的那次上游
调用自己的封顶"当成普遍规则重新过一遍，`cancelling` 立刻不成立：

```
取消检查点只在 run_cpu 的**前后**（core/pipeline/stage1.py:49 与 :54）
→ 一个正在 Stage 0 规范化的大文档，用户点取消后会合法地停在 cancelling 上
   直到 run_cpu 收尾，而它的封顶是 PBC_CPU_TASK_TIMEOUT_S（默认 1800s）
→ 旧阈值 900s 会让看门狗把"**已请求取消、正在正常收尾**"的 job 判成停滞
```

危害不是"多等一会儿"，而是**语义被改坏**：job 会从本该到达的 `cancelled`
变成 `error`。`engine.py` 的终态语义注释明确禁止这件事 ——
"终态语义不得覆盖：cancelled 被改成 error 会破坏取消审计链，通知也会重发"
（该注释写于 e2e cancel 轮的一次实证之后）。

**修法**：基准 = CPU 重活封顶 + 余量 600 = **2400s**，同样写成不变式并配
推导式护栏（`TestThresholdsAboveUpstreamCaps::test_cancel_base_covers_cpu_task_cap`，
从 `procpool.cpu_task_timeout_seconds()` 取值，不写字面数字）。

**同时暴露了一个固有上限**（记录而非"修掉"）：取消的**响应性**受限于本地
CPU 重活的封顶 —— 进程池隔离决定了 `run_cpu` 跑到一半无法被打断，所以最坏
情况下"点取消到真正 cancelled"最长就是这个封顶。这是 GIL 隔离的代价，
不是缺陷；看门狗必须容纳它，而不是把它误判成停滞。

### 8.6 新增：看门狗自身的可观测性

`GET /api/health/watchdog` —— 看门狗**自己挂掉**比 job 卡死更糟（用户会以为
"有兜底"而不再手动重试，见 §5 与模块注释）。端点暴露：存活
（`running`/`started_at`/`last_scan_at`/`last_scan_error`）、判定口径
（`stall_limits_s`/`per_page_s`/`cap_s`/`ocr_upstream_cap_s`）、最近一轮结论
（`last_stalled_found`/`last_recovered`）。`last_scan_at` 停滞或
`last_scan_error` 持续非空 = 巡检失效的证据。

**不并入 `/health`**：后者的返回体是 Electron 启动与 e2e harness 依赖的稳定
探针契约，不为可观测性增删字段。守卫口径与同家族的
`/api/health/downstream` 对齐（`is_local_request`）。

### 8.7 本轮之后仍不做的

**P2（SSE 停滞可见性）** —— 8.2 把阈值抬高之后，它从"可选"升级为"应当做"：
在杀掉任务之前，UI 应当先给出"任务似乎停滞，可取消或重试"的信号，否则用户
要多等几十分钟才看到任何反馈。方案见 §5 P2（注意不能用 `event: error`，
SSE 规范里 error 是保留类型）。

### 8.8 把"三问测试"用到看门狗**自己**身上（本轮调研的收尾动作）

§1 的三问测试原本是拿来问**被监视对象**的。§8.1/8.2/8.5 三个严重缺陷都属于
"看门狗自己的判定或动作不对"，于是本轮最后一件事是把这三问反过来问**看门狗
本身**：它自己有没有无限等待？它坏掉时看得出来吗？它的兜底会不会自己失效？

**Q1：看门狗里有几个等待点，各自的上界是什么？**

逐条枚举（这是"彻底"的全部证据，不是抽样）：

| 等待点 | 上界 | 依据 |
|---|---|---|
| `db.execute` / `fetchall`（`find_stalled_jobs`）| 无显式超时 | 与全仓所有 DB 访问同款；aiosqlite 单连接串行 |
| `async with db_lock`（`recover_stalled_jobs`）| **无显式超时** | 见下"刻意保留" |
| `await asyncio.wait({task}, timeout=10)`（`_terminate_pipeline_task`）| **10s** | `_TERMINATE_TIMEOUT_S` |
| `await _audit_log(...)` | 内部再取 `db_lock` | 同上 |
| `await notify_job(...)` | **~330s（间接）** | 见下"量化后判定非缺陷" |
| `await asyncio.wait_for(stop_event.wait(), timeout=interval)` | **interval（≥5s）** | 显式 |
| `await asyncio.sleep(interval)` | **interval** | 显式 |

**量化后判定非缺陷**：`await notify_job` 看似是"无超时的外部调用"，但
`notify_job` 链路的**自身上限**是可算的 —— `_MAX_RETRIES=3` ×
（HTTP `timeout=5.0` + `_ratelimit_sleep` 的 30s 硬钳制）+ 三个阶段
（取 token / 解析 open_id / 投递）≈ **330s**。它**不会永久**堵住扫描循环。

关键的反证：`grep -rn "notify_job"` 显示**全仓 5 处调用点全部是 `await` 且都不带超时**
（`engine.py` ×2、`stage3.py`、`state.py`、`watchdog.py`）—— 也就是说这是**项目级既定的
取舍**，不是看门狗引入的问题。只在看门狗这一处加 `wait_for` 会制造一处不一致的
特例，且按 §8.2 的同一条不变式，"超时值必须 ≥ 被覆盖调用的自身上限 + 余量"，
推导出来会是 ~930s —— 既长得没有实际意义，又凭空多一个需要维护的魔数。
**结论：不改代码，改为在此登记为"有界等待"**（有界性由 `notify.py` 的重试常量
保证；若将来有人调大 `_MAX_RETRIES` 或去掉 30s 钳制，应回来复核本行）。

**刻意保留的 `db_lock` 无超时获取**：三种处置都不如保留。
① 加超时后超时即"跳过本轮恢复" → 看门狗变成**静默 no-op**，比慢更糟；
② 换非阻塞 `acquire` + 放弃 → 同样静默；③ 保留无超时 → 若持锁方卡死，
看门狗会卡在这里，但 `last_scan_at` **停止推进**就是可见证据（§8.6 的端点正是
为此而建），且此时全应用已无法写库，问题不在看门狗。**这是"可观测的等待"
优于"不可见的静默"的一次应用。**

**Q2：它坏掉时，用户/运维看得出来吗？**

改之前：**看不出来**。`watchdog_loop` 把每轮异常 `except Exception` 吞掉只写日志，
`_STATE` 不存在，没有任何对外信号 —— "看门狗在跑但一直失败" 与 "一切正常"
在外部完全不可区分。这正是 §8.6 新增端点的动机，也是本轮把它列为**必做**而非
可选的原因。现在 `last_scan_at` 停滞 / `last_scan_error` 非空 / `running=false`
三者任一都是失效证据。

**Q3：它的兜底动作会不会自己失效？**

会，而且是本轮最严重的一条（§8.1）：只改状态不终止孤儿 task。已经把
"终止孤儿"变成收敛动作的一部分，并加了
`test_terminating_orphan_releases_per_job_lock` —— 用**真的持锁 + 永不退出的 task**
证明锁被释放，而不是断言"函数被调用了"。

**顺带产出的护栏缺陷（非产品缺陷，但同类问题）**：`build.ps1` Step 2.5 配套的
冒烟脚本首版把 `/api/health/watchdog` 的**序列化文本**当断言对象
（`'"enabled": true' in body.replace(" ", " ")`），而实际响应是紧凑 JSON
（`"enabled":true`）→ **产物完全正确却报 "watchdog 未启用"**，把构建挡在 Step 2.5。
`.replace(" ", " ")` 更是"归一化"了等于没归一化。已改为 `json.loads` 后断言结构，
并顺势把 §8.2/8.5 的**阈值不变式搬到产物上复核**：`/api/health/watchdog` 同时回传
`ocr_upstream_cap_s`/`cpu_task_cap_s` 与 `stall_limits_s`，于是冒烟可以在**不硬编码
任何常量**的前提下断言 `阈值 ≥ 对应上游封顶`。实测输出：

```
invariant ok: ocr_running=4200.0 >= ocr_upstream_cap_s=3600.0
invariant ok: cancelling=2400.0 >= cpu_task_cap_s=1800.0
```

**教训（已登记到 `docs/PROJECT_PITFALLS.md`）**：护栏必须断言**解析后的结构**，
永远不要断言**序列化后的字符**。

