# 运行时看门狗：是否需要，以及做到什么程度

> 结论先行：**需要，但要分层**。当前最痛的缺陷不是"没有看门狗"，而是
> **一个无上限的等待点**（P0，已修）；运行期间的自动巡检（P1）经需求方确认后
> 也已实施（`core/watchdog.py`，schema v12）。P2（SSE 停滞可见性）仍未做。
> 本文保留完整的判定依据、实测数据与分层过程 —— 包括**实施中踩到并被用例
> 当场抓出的两个坑**，它们的价值不低于结论本身。

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

- **`pending` 移出监视范围**。运行期间 `pending` 是**合法排队态**（等
  `MAX_CONCURRENT_JOBS` 槽位），把"排队久"当停滞会**误杀用户排队的上传**；
  而崩溃残留的 pending 由启动恢复兜底。原方案把 `_STUCK_STATUSES` 整体当监视集，
  是错的。
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
| `ocr_running` | 1800s | +120s/页 | 10800s | 51 页真实件 644s（10× 余量）；4 页旋转自愈轮 1031s |
| `analyzing` | 1800s | +180s/页 | 10800s | 51 页真实件 825–1209s（9× 余量）|
| `ocr_done` | 1800s | — | — | 纯过渡态，正常 <1s |
| `cancelling` | 900s | — | — | 需等一次在途 OCR 轮询收尾 |
| `pending` | **不监视** | — | — | 合法排队态 |

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
