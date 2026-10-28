# 对抗性审查 · 2026-10-19（Round 88 后独立复核）

> **范围**：`api/` `core/` `db/` 共 **64** 个 `.py`（`find` 实测）+ 前端状态链抽检。
> **前提**：仓库已有 35 批对抗性审查（Round 34/40/48/54/58/59/66/87）。本文**只报新发现**；
> 凡已登记在 `docs/TODO.md` §0 的（含 **B2-14** retry 配额 TOCTOU、**0-18** 删 job 清审计、
> **0-42** 描述模板有损）一律**不计入本文结论**，仅在需要对照时引用。
>
> **纪律**：每条给「定位 → 影响 → 证据 → 建议 → 验收判据」，并显式标注**实测 / 未实测**。
> 未构造出可复现失败的，一律降级为「结构性」或「条件性」，**不按已发生缺陷计**。

---

## 结论摘要

| # | 标题 | 级别 | 状态 |
|---|---|---|---|
| **F1** | `retry` 从 `review` **全量删除** findings（含已裁决），审计不记条数 | **P2** | **已修（方案 B：归档后重来）+ 变异验证**（2026-10-15） |
| **F2** | 「所有 DB 写走 `db_lock`」在 **122 处**（F2 落地时 121，F1 +1）未落实，且**无机检** | **P3** | **已加机检 + 变异验证**（2026-10-15）；⚠️ **债务仍 122 处，未收敛** |
| **F3** | 页豁免 `exemption` 有**两个独立写者**，自愈重建 JSON 时**不搬运**该键 | **P3** | **已修 + 变异验证**（2026-10-20） |
| **F4** | `update_finding` 的 `old_status` **锁外读** ⇒ 并发下审计写成两条相同迁移 | **P3** | **已实测复现 + 已修 + 变异验证**（2026-10-20） |
| **F5** | **测试自身**的两条环境依赖：写死 PID 4242（本机存活 ⇒ 假红）、`.slidep/` 影子目录（外部工具落点） | **P3** | **已修 + 变异验证**（非产品缺陷） |
| **F6** | 4 条「真值复算」用例靠 `%APPDATA%` 定位真实产物，沙箱里该变量为空 ⇒ **静默跳过**（不红不报，计数不可比） | **P4** | **已修 + 变异验证**（测试侧） |

**没有发现 P1。** 已排除两条我一开始怀疑的路径（见 §5「已排除」），排除过程一并留证。

---

## F1 · `retry` 从 `review` 全量删除 findings，含已裁决项（P2 · 潜在）

### 定位

`api/jobs/actions.py:91-101`：

```python
if job["status"] == "review":
    await db.execute("DELETE FROM findings WHERE job_id = ?", (job_id,))      # ← 无 status 过滤
    await db.execute(
        "UPDATE page_cache SET structured_json = NULL, analyzed_at = NULL "
        "WHERE job_id = ? AND raw_html IS NOT NULL", (job_id,))
    await db.execute(
        "INSERT INTO audit_log (job_id, action, detail, created_at) "
        "VALUES (?, 'analysis_reset', ?, datetime('now','localtime'))",
        (job_id, "从 review 重试：已清空 findings 与结构化分析（全量重新分析，保留 OCR 缓存）"))
```

### 影响

1. **人工裁决被一并删除**：`status ∈ {confirmed, rejected, corrected}` 的行，连同
   `reviewer_note` / `reviewed_at` / `corrected_text` 一起消失。
2. **审计不可重建**：`analysis_reset` 的 `detail` 是**固定文案**，不含条数、不含状态分布
   ⇒ 事后无法回答「清空前有几条被确认过、驳回理由是什么」。这与 **0-18** 是同一类问题
   （审计可被删除），但**位点不同**：0-18 在 `delete_job`，此处是**常规工作流动作**。
3. **`audit_log.finding_id` 会悬挂**：`db/schema.sql` 的 `audit_log` 里 `finding_id INTEGER`
   **无外键**。清空 findings 后，此前的 `finding_update` 行仍指向已不存在的 id。
4. **规则调优信号丢失**：`api/review.py::_review_stats_from_rows` 的 `by_source.reject_rate`
   依赖 `rejected` 行；清空后该 job 的驳回率归零，读者会把「没驳回」误读成「规则很准」。

### 同仓反例（这条是本缺陷的核心）

`core/pipeline/stage3.py:166-170` 对**同一语义**采取**相反**做法：

```python
await db.execute(
    "DELETE FROM findings WHERE job_id = ? "
    "AND source IN ('llm_cross', 'llm_fallback', 'user_rule') AND status = 'pending'", ...)
```

它删的是**「LLM 生成 且 仍待审」**这一个交集 —— 注释给出的理由是：LLM 措辞
每次不同、无法指纹判重，所以只能先删后插；而**规则层是确定性生成**，靠
`idx_findings_dedup` 去重即可，**不必删**；**已裁决**的更是明确保留。

同一份内存库实测两条语句的差别（3 行：`rule/pending`、`llm_cross/confirmed`、`rule/rejected`）：

```
stage3 口径（source IN (...) AND status='pending'）剩余: ['pending', 'confirmed', 'rejected']
retry  口径（DELETE ... WHERE job_id = ?）          剩余: []
```

⇒ 同一个问题「重跑时旧 findings 怎么办」，两处给了两个答案，且无任何机检锁定口径。
**retry 那条是唯一没有 `status` 过滤的删除。**

### 可达性（用户可见，非隐藏路径）

`templates/review.html:72` 有 `data-full-retry="1"` 的按钮，`static/review.js:271-282`
弹出确认框，文案是：

> 「将清空现有分析结果并完整重新分析（OCR 结果复用，不重复消耗）。」

**这个警告存在，但不够**：它说「分析结果」，而复核员会把它理解成「机器产出的
findings」，不会理解成「我自己确认/驳回过的记录和理由」。破坏性最大的一部分
恰恰没有被告知。

### 证据（实测）

```
$ python -c "sqlite3 只读查询 data/pharma.db"
findings by status        : pending 5390 | confirmed 2
non-pending with note     : 0
corrected                 : 0
jobs with analysis_reset  : 2   (job 63fa17db-16b ×1, job b638ccc5-1c0 ×2)
  → 这两个 job 的 finding_update 审计行 : 0
audit_log 带 finding_id   : 2 条；悬挂 : 0 条
```

**必须如实说明**：路径**确实在跑**（3 次 reset 是实证），但那 2 个 job 在清空前
**没有** `finding_update` 记录 ⇒ **本次样本中未发生裁决丢失**。故定级 **P2 潜在**，
不是 P1，也**不得**表述为「已发生数据丢失」。

### 建议

- **口径统一**：把「重跑时旧 findings 怎么办」收敛为**一个函数**（单一真值），
  由 `stage3` 与 `retry` 共用；默认语义建议「保留已裁决、清 pending」。
- **要删就归档**：`review → pending` 前把将删的行写入 `findings_archive`
  （或 `audit_log` 的 `detail` 里落 JSON：条数 + 每条 `id/type/status/note` 摘要），
  满足 21 CFR 11.10(e)「不遮蔽先前记录」。
- **审计带计数**：`analysis_reset` 的 `detail` 至少含
  `deleted=N confirmed=a rejected=b corrected=c`。

### 验收判据（可机检）

1. 构造 `review` 态 job + 1 条 `confirmed` + 1 条 `rejected`（各带 note）⇒ `POST /retry`
   ⇒ 断言：已裁决行**仍在**（或已进归档表），且 `analysis_reset.detail` 含非零计数。
2. 变异：把 `DELETE` 改回无 `status` 过滤 ⇒ 用例必须**红**。
3. 反向用例：`pending` 行**必须**被清（否则重跑会产生重复）。

### 修复记录（2026-10-15）· 选 (B)：全量归档后重来

**为什么是 B**：A（保留已裁决在复核页）与 C（409 拒绝重跑）都会**改变用户可见行为** ——
而按钮承诺的是「完整重新分析」。B 是唯一**既不改 UX、又消灭数据丢失**的选项：
重跑语义不变，删之前把行搬进 `findings_archive`。

改动：`db/schema.sql`（新增 `findings_archive`）、`db/client.py`（`SCHEMA_VERSION 13 → 14`
+ `_migrate_v14`；**迁移体不含 DDL** —— 表结构只在 `schema.sql` 一处声明，见偏离 3）、
`api/jobs/actions.py`（统计分布 → 归档 → 删除 → 审计带条数与分布）、
`static/review.js`（确认框补「已确认/驳回的记录会先归档留存，复核页不再显示。」）、
`core/pipeline/stage3.py`（**仅注释**：与 retry 的删除口径互相指路）。

护栏 `tests/integration/test_retry_archives_findings.py`（8 条）分三层断言 ——
**判据不是"归档表有行"，而是"能重建"**：

1. **live 侧真的删了**（否则"归档"可能只是复制，用户以为重跑过其实没跑）；
2. **归档侧逐字段一致**（`status` / `reviewer_note` / `reviewed_at` / `corrected_text`）；
3. **审计 detail 含条数与分布**（只有一句固定文案等于没记）。

另有三条**建表/迁移**用例（新库有表 / 老库走真实启动序列有表 / 连跑两次幂等）
与一条**单一声明处**守卫（`CREATE TABLE … findings_archive` 只许出现在 `schema.sql`）。

变异 **6/6**（两个脚本：`mutate_f1.py` 管归档逻辑 M1–M4，`mutate_f1b.py` 管声明位置
M5–M6；每条后 sha256 逐字节还原）：

| # | 变异 | 实测 |
|---|---|---|
| M1 | 关掉归档（`if total:` → `if False:`） | 1 failed，`assert 0 == 3` |
| M2 | 审计退回固定文案 | 2 failed（审计 + 0 条反向对照） |
| M3 | 只归档不删（`AND 1 = 0`） | 1 failed |
| M4 | 摘掉 `migrate()` 里的 v14 注册 | 1 failed |
| M5 | `schema.sql` 里表名改掉（建表 + 索引一起） | 7 failed（含两条建表用例） |
| M6 | 把 `CREATE TABLE` 复制回迁移体 | 1 failed（单一声明处守卫） |

**四条偏离（原计划 vs 实际，逐条说明）**

1. **「先红后绿」没做**（F1-2.2）。先写实现再写用例，用变异证明判别力。
   效果上"用例不是空断言"，但**不等于**"先证明它在缺陷存在时会红"。
2. **没建 `findings_reset.py` 单一实现**（F1-4.1）。`stage3` 只删「LLM 生成且待审」，
   `retry` 全量删 —— 两者语义**本就不同**，合并等于顺手改掉 stage3 的口径，
   那是**第二个行为决策**。现状是**已知分叉**：两处都加了互相指路注释。
3. **`_migrate_v14` 里没有 DDL（自撤销初稿）。** 初稿在迁移体里复制了一份
   `CREATE TABLE IF NOT EXISTS`，理由是"老库要补表"。**这个理由是错的**：
   `get_db()` 是 `migrate()` 的**唯一**调用方，且它**先**跑 `init_schema()`
   （`schema.sql` 全量执行）⇒ 走到 v14 时表**一定已存在**，那份 DDL 只会被
   `IF NOT EXISTS` 静默跳过 —— **死代码**。更要紧的是它把"建表"伪装成迁移的职责，
   还给两份 DDL 留了静默漂移的空间（`_migrate_v11` 的 docstring 明令禁止）。
   现改为：DDL 只在 `schema.sql`，迁移体只留一条日志，并由
   `test_ddl_is_declared_only_in_schema_sql` 机检（M6 变异可证）。
   ⇒ 连带**作废**了初稿的第 3 条"故意不套 try/except"（没有 DDL 就没有要 catch 的异常）。
4. **列名与索引与原计划不同**：`id`→`archive_id`、`orig_finding_id`→`finding_id`；
   索引 `(job_id)` 而非 `(job_id, archived_at)`。

**改了两条既有用例（必须声明）**：`test_llm_finish_reason.py::TestSchemaV13::
test_schema_version_is_13` 与 `test_watchdog.py::TestSchemaSingleSourceOfTruth::
test_schema_version_is_13` 都钉 `SCHEMA_VERSION == 13`，v14 落地后必然红 ⇒ 均改为
`>= 13` 并改名 `..._is_at_least_13`。**两条同名同断言、在两个文件里各一份** ——
第一轮只改了一条，全量跑才抓到（`1 failed, 4228 passed`）。
⚠️ 这是「参数变更清扫铁律」的一次失败：grep 的是 `user_version`，而它们钉的是
`SCHEMA_VERSION`。**改常量要按常量名扫。**
理由：精确等值会让人每次加迁移都改一次测试（变更探测器），而真正要防的
「加了迁移却忘了 bump 版本」由 `test_every_migration_version_has_its_own_guard` 机检。

**不得读成已通过的部分**

- **重跑语义没变**：已裁决行**不会**留在复核页。归档是「留存」，不是「保留在界面上」。
- **归档表没有读取入口**：全仓对 `findings_archive` 只有 `INSERT`（+ DDL），
  **没有**任何 API / 页面能查它 ⇒ 数据**留住了但用户取不到**，目前只能用 sqlite 工具。
  这是**未满足的需求**（要不要做、做成什么样是产品决定），**不得**读成"已闭环"。
- **归档与删除的原子性**只靠「同一隐式事务」（都在那一次 `commit()` 之前），无显式 `BEGIN`；
  `retry_job` 整体仍**没有**包进 `db_lock`（属 0-45 / F2-3.2，未修）。
- `findings_archive` **没有读取界面**。满足「可追溯」，**不满足**「复核员能自助查看」。

---

## F2 · 「所有 DB 写走 `db_lock`」在 121 处未落实（F1 后 122），且无机检（P3 · 结构性）

### 定位

不变式出处：`.workbuddy-ai/memory/MEMORY.md` §6「后端所有 DB 写必须走 `db_lock`」；
实现自陈出处：`api/review.py:597-599` 注释「必须在 db_lock 内原子执行……防止
aiosqlite 单连接上的事务边界被穿插」。

### 证据（实测，脚本已持久化）

`scripts/scan_unlocked_db_writes.py`（AST 判定「写语句是否词法上位于
`async with *db_lock` 体内」，只判写、SELECT 单列计数）。

> 刻意放 `scripts/`（受版本控制）而非 `devlogs/`（`.gitignore:163` 会吃掉）——
> 否则证据随磁盘消失，结论无法从一份 clone 复现（同 R81 的处置理由）。
> 已验证：换工作目录重跑，结果一致。

```
scanned 64 .py files under api/core/db
unlocked raw DB writes : 121   <- 违反「所有 DB 写走 db_lock」
unlocked unknown-SQL   : 3     <- 静态取不到首关键字
unlocked reads (SELECT): 78    <- 读，不需锁，仅计数
```

⚠️ **读计数是 78 而不是初稿的 80** —— 这不是扫描器变了，是 F3-b / F4 各把**一条**
`SELECT` 移进了 `db_lock` 体内（F4 的 `SELECT id, status FROM findings`、
F3-b 的豁免页读取）。**写计数 121 前后不变**，与「这两处只挪了读」一致。
⇒ 这条 delta 本身就是扫描器在测真东西的旁证（它精确数出了 -2 条读、0 条写）。

⚠️ **上块是 F2 落地时的口径。F1 之后复测：`writes=122 / unknown=3 / reads=79`。**
`+1 写` = `retry_job` 新增的 `INSERT INTO findings_archive`；`+1 读` = 归档前那条
`SELECT status, COUNT(*) … GROUP BY status`。两处都在 `retry_job` 内、都不在
`db_lock` 里，由 **F2-3.2** 整体收口。基线已按流程重生成（106 键 / 合计 122）。
⇒ **这条 +1 是棘轮自己抓到的**（不同批重生成基线就会报 `【新增】`），
不是扫描器抖动 —— 静态 AST 扫描不存在 0-15 那种计数不确定性。

⚠️ 行号会随编辑漂移（F3 给 `self_heal.py` 加了 15 行）。所以护栏的键**不含行号**，
用 `文件|函数|方法|SQL前缀`（见 §F2 修复记录）。下表行号是当前树实测值。

高密度位点（前几处）：

| 文件 | 函数 | 形态 |
|---|---|---|
| `api/jobs/actions.py:82,92,93,98,102` | `retry_job` | **4 条写 + commit 组成一个逻辑单元，全程无锁** |
| `core/pipeline/self_heal.py:509,527,542,553,574` | `_rotation_heal` | 循环内 UPDATE + 末尾 commit |
| `core/pipeline/engine.py` | `_run_pipeline_impl` / `_run_sliced_stage1_2` | 多组 UPDATE + commit |
| `api/settings/*` | 设置写入 | 单条 UPDATE + commit |

### 影响（已做反证，勿夸大）

最坏路径是「A 的 `rollback()` 把 B 的裸写一起回滚」。我**逐一核对**了全仓 3 处
`rollback()`：

| 位点（rollback 行） | 包围它的 `db_lock` | 结论 |
|---|---|---|
| `core/pipeline/stage2.py:102` | `:96` `async with db_lock:` | **在锁内** |
| `api/jobs/upload.py:524` | `:460` `async with db_lock:`（B2-7 同临界区） | **在锁内** |
| `api/jobs/actions.py:282` | `:264` `async with db_lock:` | **在锁内** |

⇒ **「回滚掉别人的写」这条最坏路径不成立**。残余风险仅为「事务边界被提前提交 /
别人的写混入本事务」，**我未能构造出可复现的失败**。

因此本条**不是**「已知会出错」，而是：
**一个被写成绝对律的不变式，在实现上有 121 处例外，且没有任何机检**。
这本身是治理缺陷 —— 下一个人无从判断「哪些是刻意豁免、哪些是漏了」。

### 建议

1. **别急着补锁**：`db_lock` **非重入**（`state.py` 注释），盲目加锁会与
   `transition_status` / `_audit_log`（内部自取锁）死锁。参考 **B2-14** 的结论：
   需要 `_transition_status_unlocked` 这类「锁内版本」配套。
2. **先分类再收敛**：把 121 处按「单语句 + commit」与「多语句逻辑单元」分开。
   前者风险低，可登记豁免；**后者**（`retry_job`、`self_heal` 循环、`engine`）优先。
3. **加机检**：把本脚本收为 `tests/unit/test_db_write_lock_discipline.py`，
   维护**显式豁免清单**（file + 行唯一子串 + 理由），新增裸写即红 ——
   与本仓 `test_innerhtml_escaping.py` 的 `_AUDITED` 范式完全同构。

### 验收判据

- 豁免清单外的裸写数 == 0；✅（2026-10-15 落地，判据改为「与基线**逐键相等**」）
- 变异：在任一受管文件加一条裸 `UPDATE` ⇒ 用例红；✅ M1
- 反空转：扫描到的写语句总数设下限（当前 122，防止正则/解析退化成 0 命中而假绿）。
  ✅ 但**下限取 50 而不是 121** —— 见下方修复记录「与建议的差异」。

### 修复记录（2026-10-15）· 护栏已落地，**债务未收敛**

产物（3 个文件，全部 LF）：

| 文件 | 字节 | 说明 |
|---|---|---|
| `scripts/scan_unlocked_db_writes.py` | 8 602 → 14 020 | 抽出 `collect()` 供护栏导入；新增 `key_of` / `baseline_from` / `BASELINE_PATH`；`--baseline` / `--write-baseline`；`collect()` 回传 `parse_errors`；每条 hit 增 `sql` 前缀 |
| `tests/unit/db_write_lock_baseline.json` | 新增 8 631 | 105 个写键（合计 **121**）+ 3 个 unknown 键 |
| `tests/unit/test_db_write_lock_discipline.py` | 新增 12 161 | 15 条用例（判定谓词 9 / 反空转 2 / 棘轮 1 / 基线形态 3） |

**与建议的差异（两处，都有理由）**

1. **不是 `_AUDITED`（file + 行唯一子串 + 理由），而是「基线快照 + 棘轮」。**
   建议里说这与 `test_innerhtml_escaping.py` 的 `_AUDITED` 范式同构 —— 那条范式
   的前提是**条目少且能给出理由**。这里 121 处散在 35 个函数里
   （`engine._run_pipeline_impl` 一处就 10 条），逐条写「为什么可以不加锁」
   需要先做 F2-3 的时序分析；**现在写就是编**。而"编出来的理由"比"没有理由"更坏：
   它会让下一个人以为这处被审查过。
   ⇒ 基线只声明「当前欠账长这样」，**不**声明安全（`_note` 里明写，
   并有 `test_note_still_says_debt_not_approval` 盯着它别被改成"已审计安全"）。
2. **键不含行号**，用 `文件|函数|方法|SQL前缀`。行号/行内容随无关编辑漂移 ⇒
   每次无关编辑都要重生成基线 ⇒ diff 噪声淹没真变更。实测 105 个唯一键，
   其中 12 个键重复（最多 5 处）⇒ 值必须存**计数**，否则"少一处"发现不了。

**「减少」也判红**（刻意）：只拦新增挡不住"A 处删一处、B 处加一处不同形态"的抵消。
副作用是"修好了也得重生成基线"，但那正好让债务下降在 diff 里留痕 ——
否则基线永远虚高，以后涨回 121 也没人发现。

**变异台账**（6/6 按预期；每条变异后文件 sha256 逐字节还原）

| # | 变异 | 期望 | 实测 |
|---|---|---|---|
| — | 未变异 | 15 passed | 15 passed |
| M1 | `api/report.py` 加一个裸 `UPDATE` 函数 | 棘轮红 + 文本【新增】 | 1 failed / 14 passed |
| M2 | `SCAN_DIRS = ()`（扫描器失明） | 棘轮红 + 反空转红 | 2 failed，报【消失】 |
| M3 | `_is_db_lock_with` 恒 `True`（谓词退化） | 谓词用例红 + 棘轮红 | 2 failed |
| M4 | 真把 `_audit_report_export` 的写包进 `db_lock` | 棘轮红 + 文本【消失】 | 1 failed |
| M5 | 基线 JSON 删掉一个键 | 棘轮红 + 文本【新增】 | 1 failed |
| M6 | 去掉函数边界的锁作用域重置 | 嵌套 def 用例红 | 1 failed |

**M6 是自查出来的工具缺陷，不是被测代码缺陷**：`Scanner.visit_FunctionDef` 原先
**不重置**锁作用域 ⇒ 写在 `async with db_lock:` 体内的嵌套 `async def g()` 会被当成
「已保护」（**静默漏报** —— 这是扫描器唯一的漏报方向）。修前后计数都是 121
（本仓没有这种写法）⇒ 这个洞**不改数字、也不会自己暴露**，只能靠用例钉住。

⚠️ 脚本在 `devlogs/_verify/mutate_f2.py`，而 `devlogs/` 被 `.gitignore:163` 吃掉 ⇒
**脚本本身不入版本控制**。可复核的持久证据是上表 + 用例本身（用例受版本控制）。

**全量（当前基准，2026-10-15）**：**4 221 passed / 0 failed / 2 skipped / 2 xfailed**，
1466.10 s。junit 独立核对 `tests=4225 / failures=0 / errors=0 / skipped=4`。
⚠️ 这一轮的**覆盖率没有数字**（垫片在 teardown `SystemExit(1)` ⇒ `.coverage` 没落盘，
`coverage report` 报 `No data to report`）；上一轮同规模是 10 989 stmt / 533 miss / 95%，
但那是**少一条用例、且产品源码相同**的上一轮 —— **不得当成本轮结果**。

**不得读成已通过的部分**

- 121 处**仍然是 121 处**，一处都没加锁。F2-3（收敛）完全没动。
- M4 那条变异只加了 `async with db_lock:` 而**没加 import** —— 它验证的是 AST
  形状，不是运行期。不要读成"那处修好了"。
- 护栏证明的是"没有新的裸写悄悄出现"，**不**证明任何一处安全。

---

## F3 · 页豁免 `exemption` 有两个独立写者，自愈重建 JSON 时不搬运（P3 · 条件性）

### 定位

`page_cache.ocr_diagnostics` 是**一个 JSON blob**，有两条写路径：

| 写者 | 位点 | 写法 |
|---|---|---|
| 人工豁免 | `api/review.py:651-681` | 读全量 JSON → 改 `exemption` 键 → **整块写回** |
| 自愈诊断 | `core/pipeline/self_heal.py::_self_heal_diag` | **重建**一个新 dict，只含 `self_healed / recovered / source / prior_diagnostics / …` |

`_self_heal_diag()` 的构造里**没有** `exemption`；旧值只被塞进
`diag["prior_diagnostics"]` 这个**嵌套**位置。

### 影响

两个消费方都**只读顶层**：

- `api/report.py:67` → `diag.get("exemption")`（报告「豁免」章节）
- `static/review-pageinfo.js:308` → `diag.exemption`（复核页豁免区）

⇒ 一旦自愈在该页跑过，豁免在**报告与 UI 上双双消失**，而 `prior_diagnostics.exemption`
没有任何读取方。用户在报告里看到「本页无豁免」，与审计里 `ocr_exemption` 记录矛盾。

另有一处**读改写竞态**（同一函数）：`review.py:651` 的 `SELECT` 在 `db_lock` **之外**，
`:677` 才进锁写回整块 JSON ⇒ 读与写之间若被自愈写入，后写者覆盖前者（丢豁免或丢诊断）。

### 可达性（如实标注）

- `retry` 从 `review` 走 `raw_html IS NOT NULL` 复用（`stage1.py:66-94`），
  **跳过 stage1** ⇒ 自愈不跑 ⇒ 主路径**不可达**。
- 可达条件：`partial_review` / `error` 重试，且该页进入自愈目标集（空页/稀疏页），
  且用户**已**先设过豁免。三者同时成立才丢。

**已实测（函数级）**：直接调 `_self_heal_diag(prior_with_exemption, recovered=True)`：

```
top-level keys : ['content_length', 'prior_diagnostics', 'recovered', 'self_healed', 'source']
has exemption  : False
nested at      : True
```

⇒ 顶层丢键**是实测事实**，不是推断。但「端到端丢豁免」仍**未实测复现**（受上面三条可达条件约束）。定 P3。

### 建议

- `_self_heal_diag()` 显式**搬运** `exemption`（若 `prior` 有）到顶层，
  或改为「合并」语义（`diag = dict(prior or {}); diag.update({...})`）而不是重建。
- 把「读改写」整体放进 `db_lock`（读也在锁内），或改用 SQLite 的
  `json_set()` 单语句更新 `$.exemption`，从结构上消灭整块覆盖。

### 验收判据

1. 单测：`prior = {"exemption": {...}, "integrity": "degraded"}` ⇒ `_self_heal_diag(prior, recovered=True)`
   解析后**顶层**仍含 `exemption`。
2. 集成：设豁免 → 强制该页走自愈 → `GET /report` 与 `/pages/{n}` 仍能读到豁免。
3. 变异：从 `_self_heal_diag` 删掉搬运行 ⇒ 用例红。

### 修复记录（2026-10-20）

**两处，都属"读-改-写跨越临界区"，但机理不同：**

| 子项 | 缺陷 | 修法 |
|---|---|---|
| **F3-a** | `_self_heal_diag` **重建** dict，只搬 `source`、把整份 prior 塞进 `prior_diagnostics` ⇒ 顶层 `exemption` 消失 | 显式搬运 `prior["exemption"]` 到新 dict 顶层（**只搬一层**） |
| **F3-b** | `set_page_exemption` 的 `SELECT` 在锁外、`UPDATE` 在锁内 ⇒ 写回**锁外读到的副本**，覆盖并发写者（`_rotation_heal` 且不拿锁） | 把读-改-写整体收进 `db_lock`；审计仍用裸 `INSERT`（`_audit_log` 自取锁，非重入） |

**为什么"只搬一层、不递归"**：撤销走 `diag.pop("exemption")`，只 pop 顶层。若从
`prior_diagnostics` 深处递归搬运，**已撤销的豁免会在下次自愈时复活** —— 比丢失更危险。
护栏里有一条专门钉这个的反向对照（`test_revoked_exemption_is_not_resurrected_from_history`）。

**护栏**：`tests/unit/test_self_heal_preserves_exemption.py`（7 条，含 3 条反向对照）+
`tests/integration/test_page_exemption_atomicity.py`（1 条，**确定性闸门**）。

**先红后绿**（实测，非推断）：

```
修复前：test_self_heal_preserves_exemption.py  → 3 failed, 4 passed
        test_page_exemption_atomicity.py       → 1 failed
        （红的原因：`assert None == {...exemption...}` / `self_healed` 被抹掉）
修复后：7 passed / 1 passed
```

**变异（3/3 被抓，`devlogs/_verify/mutate_f3f4.py`）**：

| 变异 | 期望 | 实测 |
|---|---|---|
| M1 不搬运 exemption | 3 红 | **3 failed** |
| M2 把 `set_page_exemption` 的 SELECT 抬回锁外 | 1 红 | **1 failed** |
| 还原 | 回绿 + `sha256` 一致 | **OK** |

---

## F4 · `update_finding` 的 `old_status` 锁外读，审计可能重复同一迁移（P3 · 结构性）

### 定位

`api/review.py:560-563` 读 `old_status`（**锁外**）→ `:608` 才 `async with db_lock` 写
`UPDATE` + `INSERT audit_log`。审计文案在 `:600-606` 用锁外读到的 `old_status` 拼装。

### 影响

两个并发请求对同一条 finding 提交 `confirmed`：

- 都读到 `old_status='pending'`
- 都写 `status='confirmed'`
- 审计出现**两条** `pending → confirmed`

结果状态正确，但**审计轨迹不精确**（GMP 场景下「谁先改的」不可判定）。
与 **B2-14** 同族（都是「检查/读取与写入之间释放了锁」），但 B2-14 讲的是配额，
此处讲的是**审计文案的正确性**。

### 建议

把 `SELECT id, status` 移进 `db_lock` 内，与 `UPDATE` + `INSERT` 同一临界区。

⚠️ **不要**用 `UPDATE ... RETURNING status`（本文初稿曾这么建议，**已实测推翻**）：
本机 sqlite 3.45.1 实测 `UPDATE ... RETURNING status` 返回的是**更新后**的值
（`('confirmed',)`），拿不到 `old_status` ⇒ 解决不了本条。

### 验收判据

- 并发两请求（闸门卡在 SELECT 与 UPDATE 之间）⇒ 断言审计里**只有一条** `pending → confirmed`，
  第二条的 `old_status` 应为 `confirmed`。
- 变异：把 SELECT 移回锁外 ⇒ 用例红。

### 修复记录（2026-10-20）· **已从"未实测"升级为实测复现**

**复现（确定性闸门，不靠 sleep 碰运气）**：先占住锁 ⇒ 两个请求都完成"读"后卡在锁上
⇒ 放锁。修复前实测审计为：

```
['status: pending → confirmed', 'status: pending → confirmed']   ← 两条同源迁移
```

即本文 §影响 的推演**逐字成立**。修复后：

```
['status: pending → confirmed', 'status: confirmed → confirmed']
```

**修法**：把 `SELECT id, status` 移进 `async with db_lock`，与 `UPDATE` + `INSERT audit_log`
同一临界区（`api/review.py`）。`params`/`sets` 仍在锁外拼装（只依赖请求参数，不依赖旧值）。

⚠️ **实测推翻本文初稿的建议**：`UPDATE ... RETURNING status` **不能**用 ——
本机 sqlite 3.45.1 实测 `RETURNING` 返回**更新后**的值（`('confirmed',)`），拿不到 `old_status`。

**护栏**：`tests/integration/test_finding_update_audit_race.py`（1 条，含反空断言：
两个任务必须都还在等锁，否则判"窗口未复现"直接红 —— 防止用例靠"什么都没发生"假绿）。

**变异**：把 SELECT 抬回锁外 ⇒ **1 failed**；还原 `sha256` 一致 ⇒ 回绿。

**踩到的坑（写进用例 docstring 了）**：闸门**不能**去 acquire 模块全局 `db_lock` ——
`asyncio.Lock` 一旦有等待者就绑定当时的 event loop，而 pytest-asyncio 每例一个新 loop
⇒ 会把全局锁"绑死"，后续用例进 `async with db_lock` 抛
`is bound to a different event loop`（单跑绿、组合跑红）。改用**本用例私有的锁** +
`patch("api.review.db_lock", lock)`。

---

## F5 · 全量测试阶段新发现：两条**测试自身**的环境依赖（P3 · 已修）

> 来源：2026-10-19 全量基线跑（`coverage run -m pytest`，**639.76 s**）：
> **`2 failed, 4194 passed, 2 skipped, 2 xfailed`**，覆盖率 **95%**（10988 stmt / 537 miss）。
> 两条红**都不是产品缺陷**，但都会让「全绿」这个信号失真 —— 所以必须修，且必须证明修完仍有判别力。

### F5-a · `test_cli_json_is_machine_readable` 写死 PID 4242

**定位**：`tests/unit/test_build_status.py`（原 `:183-189`）用 `_start(d, RUN_ID_NEW, 4242)`
造「有 start 无 finish」的台账，再断言 `state == interrupted`。该用例走 `bs.main(...)`，
**没有** `alive=` 注入口 ⇒ 用的是真实的 `process_alive(4242)`。

**证据（实测，非推断）**：

```
python -c "from scripts import build_status as bs; print(bs.process_alive(4242))"
  → True
OpenProcess(0x1000, False, 4242)   → handle=1096
GetExitCodeProcess                 → ok=1, code=259  (STILL_ACTIVE)
QueryFullProcessImageNameW         → D:\dsh\DeepSeek Harness.exe
```

即：**本机 PID 4242 真被占用**（本 agent 的宿主进程）。于是 `read_state` 判 `running`
是**正确**的，用例断言 `interrupted` 是**错**的 —— 基线日志里那行
`assert 'running' == 'interrupted'` 是**假红**。

**为什么值得单独立条**：同文件另外 6 处 `4242` 都注入了 `alive=lambda pid: False`
（确定性），只有这一处漏了 ⇒ 缺陷不在「pid 写死」这个写法本身，而在
**同一条判据里混用了注入式与真实式**。这类用例在 CI 上绿、在开发机上红，
最容易把「环境噪声」记成「代码坏了」。

**解决（已实施，测试侧，零产品改动）**：
新增 `_dead_pid()` —— 从 65535 递减找一个 `process_alive()` 为假的 pid，
调用方**当场再断言一次它确实已死**；断言本体（`state == interrupted`、`advice` 非空）**一字未改**。
这不是「把判据放松到绿」，而是把判据的**前提**从「宿主机碰巧」换成「运行时自证」。

**测试（变异 3/3 红，还原后逐字节一致）**：

| 变异 | 期望 | 实测 |
|---|---|---|
| M1 产品侧：`read_state` 把「无 finish + pid 已死」判成 `completed` | 两用例红 | **2/2 红** |
| M2 判据侧：`_dead_pid()` 返回 `os.getpid()`（活 pid） | 自证断言触发 ⇒ 红 | **红** |
| 还原 | 回绿 | **2/2 绿**，`sha256` 与改动前一致 |

脚本：`devlogs/_verify/mutate_f5a.py`（gitignore 内，属一次性证据）。

### F5-b · `.slidep/` 影子目录（**护栏工作正常**，不是护栏缺陷）

**定位**：`tests/unit/test_repo_hygiene.py::TestNoShadowDirectories` 报
`影子目录（既无被跟踪文件、又未被忽略）：['.slidep']`。

**归因（读 `slidep.log` 首行，非猜测）**：`@tencent/slidep` 的 `upsert-dsl`
以 **cwd** 为日志落点，而 cwd 恰好是本仓库根 ⇒ 生成 `.slidep/slidep.log`。

**为什么护栏是对的**（这里容易误判成误报）：

```
git check-ignore -v .slidep              → rc=1（目录**本身**没被忽略）
git check-ignore -v .slidep/slidep.log   → .gitignore:81:*.log
git status --short --ignored             → !! .slidep/
```

目录能「看起来干净」，只是因为里面**恰好**只有一个 `.log`（被 `*.log` 兜住）。
工具一旦写非 `.log` 文件，`?? .slidep/` 立刻出现在 `git status` —— 正是该护栏
docstring 描述的场景。**护栏比 `git status` 更严，是有意的。**

**解决（已实施）**：`.gitignore` 追加根锚定的 `/.slidep/`
（`.gitignore:174`）。**必须锚定**：未锚定的 `.slidep/` 会连子目录同名目录一起吃掉
（同 `/build/` 吞掉 `electron/build/`、`/data/` 吞掉 `core/kb/data/` 的教训）。

**未做**：没有删除 `.slidep/`。它是外部工具的可再生落点，删了下次跑 PPT 还会回来；
忽略才是收口方式。

### F5 的边界

- 两条都不改产品行为 ⇒ **不影响** F1–F4 的定级与优先级。
- 基线从此变成 **`0 failed`** 需要**下一次全量重跑**来确认；本次只验证了这两个文件
  （58 passed），**不是**「全量已全绿」。全量复跑在 P9 统一验收时做。

---

## F6 · `%APPDATA%` 缺失 ⇒ 4 条真值复算用例**静默跳过**（P4 · 已修）

### 定位

`tests/unit/test_regions_anchor.py`（原 `:647-650`）：

```python
_REAL_PADDLE = os.path.join(
    os.environ.get("APPDATA", ""), "PBC", "output", "0c5cfb00-897",
    "paddle_original.jsonl",
)
```

`APPDATA` 缺失时 `os.path.join("", ...)` 得到**相对路径** ⇒ `os.path.exists` 为假 ⇒
4 条 `skipif` 用例（`test_real_paddle_artifact_replays` 等）全部 skip。

### 证据（实测）

```
$ echo "APPDATA=$APPDATA"
APPDATA=                       # ← 本仓库 Bash 沙箱里是空串

pytest tests/unit/test_regions_anchor.py
  → 103 passed, 4 skipped
APPDATA=C:/Users/<user>/AppData/Roaming pytest tests/unit/test_regions_anchor.py
  → 107 passed
```

而产物**一直存在**：`%APPDATA%/PBC/output/0c5cfb00-897/paddle_original.jsonl`
（881 729 B，2026-08-26）。⇒ 不是"没有产物"，是**找不到产物**。

### 影响（两条，第二条更隐蔽）

1. **覆盖静默消失**：4 条用真实 51 页产物复算 `extract_regions` 的用例不再执行，
   报告里只多 4 个 `skip`，**不红不报**。
2. **两次全量计数不可比**：同一棵树，有 `APPDATA` 时 `4194 passed / 2 skipped`，
   没有时 `4192 passed / 6 skipped`。若把「`passed` 不低于基线」当回归判据，
   这条差异会被误读成"退化 2 条"—— 实际是**环境漂移**。
   （本轮就踩了：先看到 4194→4192，再对 junit 逐条比对才定位到这 4 条。）

### 解决（已实施，测试侧，零产品改动）

新增 `_roaming_dir()`：`APPDATA` 为空且 `os.name == "nt"` 时回落到
`~/AppData/Roaming`。真实终端里 `APPDATA` 必然存在 ⇒ 行为等价；沙箱里则把
"本该跑"的用例跑起来。产物仍不存在时照旧 skip（不制造假绿）。

### 测试

| 验证 | 期望 | 实测 |
|---|---|---|
| `APPDATA` 为空（模拟沙箱） | 107 passed / 0 skipped | **107 passed** |
| 变异：删掉回落分支 | 4 条重新 skip（103 passed / 4 skipped） | **复现** |
| 还原 | `sha256` 与改动前一致 | **一致** |

### 附带：一次**护栏正常工作**的记录（不是我改坏了）

修完 F6 后首次全量出现 1 条红：

```
tests/unit/test_e2e_proc_helper.py::test_no_hardcoded_paths_pointing_at_this_repo
  → tests/unit/test_regions_anchor.py:656
```

原因：我在新 docstring 里写了 `C:/Users/<user>/AppData/Roaming/...` 这种**家目录形态
的字面量**。该护栏扫描**全部行、不区分 docstring**，所以它是对的。已把文案改成
`%APPDATA%` + 相对片段，并把这个坑写进 docstring 本身（防止下一个人再踩）。

> 教训：**"文档里的路径"也会被路径护栏判红**。写示例路径时用 `%VAR%` / 相对片段。

---

## 5. 已排除（留证，避免下一轮重复怀疑）

| 怀疑 | 排除依据 |
|---|---|
| `stage2.py:102` 的 `rollback()` 会回滚其他 job 的裸写 | **在 `db_lock` 内**（`:96` `async with db_lock:` 包住 rollback+UPDATE+commit） |
| 3 处 `rollback()` 至少有一处裸奔 | 全部在锁内：`stage2:96-118`、`upload:524`（B2-7 同临界区）、`actions:282` |
| `retry` 的配额 TOCTOU 是新发现 | **已知未修**：`docs/TODO.md` **B2-14**（`:1966-1975`），本文不重复计 |
| `delete_job` 删审计是新发现 | **已知**：**0-18**（P1），本文不重复计 |
| 审计记录大面积悬挂 | 实测 `audit_log` 带 `finding_id` 仅 **2** 条，悬挂 **0** 条 |
| `llm_checks` 描述模板丢数据是新发现 | **已知**：**0-42**（P1），本文不重复计 |

---

## 6. 未验证边界（不得读成已通过）

- ~~本文**未运行**全量测试~~ ⇒ **已补，且已覆盖 F1 之后的树**：全量
  **4 233 passed / 0 failed / 2 skipped / 0 xfailed**（743.95 s，
  junit 独立核对 `tests=4235 / failures=0 / errors=0 / skipped=2`），覆盖率 **95%**
  （533 miss / 11 008 stmt）。e2e 两轮 **7/7** 全通过。
  ⚠️ `xfailed` 由 2 变 0 是 **0-42 修好**的结果（2 条 `strict=True` 转绿），不是放宽。
  ⚠️ 但 e2e 的 `findings` 计数**非确定**（已知 0-15），**单次计数不作回归判据**，
  只作"跑通了"的判据。
- 「并发」部分：**F4 已从推演升级为实测复现**（并发审计真的写成了两条相同的
  `pending → confirmed`）；**F2 仍是推演** —— 未在运行中的服务上做并发注入，
  121 处里没有一处被证明"实际出过错"。
- `data/pharma.db` 是**开发库快照**，其计数（5390 pending / 2 confirmed / 3 次 reset）
  **不代表生产**，只用于说明「路径是否在跑」。
- 扫描脚本判据是**语法层**：它证明「哪些写不在 `db_lock` 词法作用域内」，
  **不**证明这些写一定出错；也不覆盖通过封装（`_audit_log` / `transition_status`）间接取锁的路径。
- 前端只做了 `exemption` 一处读取点核对，未做全量前端审查。
- **Electron 层 e2e 在受限沙箱内不可复现**（见 `MEMORY.md` §12）⇒ 本轮所有
  "e2e 通过"都是**后端/服务层**的，不含 Electron 壳。

---

## 7. 建议的登记方式

按 `docs/TODO.md` 惯例，本文四条可登记为 **0-44（F1）/ 0-45（F2）/ 0-46（F3）/ 0-47（F4）**。
**本文未改动 `TODO.md`** —— 该文件有 `test_todo_freshness` 等机检（复核戳、不变式 H），
新增条目应与跑一遍相关护栏同批进行，避免把绿树改红。

**进展（2026-10-15）**：F3/F4/F5/F6 已修并各自带变异验证；F2 已加机检（棘轮，
债务未收敛，122 处）。**`TODO.md` 已登记 0-44…0-47**（0-44/0-46/0-47 标 ✅、
0-45 标「护栏已落地 / 债务未收敛」），与 `test_todo_freshness` 15 条同批跑绿；
`_declared_items()` 135 → 139。
⚠️ 只改了 §0 待办表里的状态，**没有**在 §0.1.1 台账加「已核销」行 ——
不变式 E 只统计 `[x] … 已核销`，所以两者目前一致；若以后补核销行，必须同时补台账行。
