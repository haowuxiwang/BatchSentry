# 收尾计划（2026-10-21）—— 交付手工测试前的最后一轮

> 依据：`docs/AUDIT_2026-10-15_14Q.md`（14 问审计）、`docs/ADVERSARIAL_REVIEW_2026-10-19.md`（F1–F6）、
> `docs/TODO.md`（0-42…0-61）。
> **本文件只回答一件事**：从现在到"可以交给用户手工测试"，还差什么、按什么顺序做、每条怎么算做完。

## 0. 口径（先说清楚哪些是实测）

| 标记 | 含义 |
|---|---|
| **实测** | 本轮跑过命令，数字可复跑（命令/脚本已给出） |
| **推断** | 由代码或实测数据推出，但没有直接观测 |
| **未知** | 没验证过 —— 不许当结论用 |

**范围裁剪（用户 2026-10-21 明确）**：
- **#11 演示视频（0-59）本轮不做。**
- **最小标注集（0-56）不在本轮** —— 没有标注集就没有可信的 P/R，这是**后续**任务；
  本轮所有"判得准"相关的条目都只做到"判据就位、数字留空"。

## 1. `dist-electron` 是否最新 —— **实测：是**

| 判据 | 实测值 |
|---|---|
| 源码 HEAD | `ec75673`（含 0-42 修复） |
| 入包清单 | `dist/pbc-server/build_manifest.json`：`version=1.2.1 files=116 head=ec7567340098 dirty=False` |
| `bundle_manifest --check`（`dist/pbc-server`） | **OK** —— 清单与源码/产物逐字节一致 |
| `bundle_manifest --check`（`dist-electron/win-unpacked/resources/pbc-server`） | **OK** |
| 两份 `pbc-server.exe` 是否同一字节 | **是**：`20 498 538 B`，sha256 `cef4b153e57a7148…` |
| CSS 重建 | `static/app.css` 20 016 B，sha256 前后一致 ⇒ 本轮只改了 Python |
| `PROVENANCE.txt` | BOM + 纯 CRLF（12 行），`git_head: ec75673`，`version: 1.2.1` |

**结论**：`dist-electron/win-unpacked/` 是 `ec75673` 的产物，包含 0-42 修复。
⚠️ 但它**不含**本文件的后续改动（0-41 等）—— 见 §5 的"最后一次重建"。

### 1.1 全量测试基线（**实测**，本批 0-41 之后）

| 项 | 值 |
|---|---|
| 汇总行 | `4244 passed, 2 skipped` in **933.77 s**（0 failed / 0 error） |
| junit 交叉核对（独立于汇总行） | `tests=4246 / failures=0 / errors=0 / skipped=2` |
| 覆盖率 | **95%**（11 027 stmt / 533 miss） |
| 与上一批对比 | 4233 → **4244**（+11 = 新增的 `test_llm_fallback_batching.py` 11 条） |
| 数据文件 | `devlogs/_verify/.coverage.20261021_100806`（**时间戳命名** —— 复用同名文件会被 `coverage run` 先删后写，而垫片会拦住那次删除） |

> 判据纪律：`SUITE_EXIT=0` **不是**判据（垫片可能改写退出码）；判据是**汇总行 + junit
> 独立核对**两条同时成立。

### 1.2 发布门禁（**实测**，重建产物之后）

```
════════ v1.1 发布门禁（打包信号）════════
[PASS] worktree_clean   工作区干净
[PASS] no_build_outputs 422 个已跟踪文件中无构建产物
[PASS] dist_variants    1 份完整产物（dist-electron）
[PASS] artifact_freshness 2 份产物与源码逐字节一致
[PASS] packaging_files  5 个前置文件就位
[PASS] rules_wired      29 个 _check_* 规则函数（下限 14）
[PASS] kb_corpus        知识库 441 条（下限 200）
[PASS] kb_packaging     6 个 KB 源自动入包
[PASS] dependency_vulns 0 条公告
[PASS] runtime_eol      dist-electron: Electron/43.7.4
[PASS] tests_coverage   4244 passed, 0 failed, coverage=95.17% (门禁 95%)
OVERALL: pass  (pass=11 fail=0 warn=0 skip=0)
```

报告：`devlogs/gate_report_20261021_104302.json`。

## 2. 0-42（P1）：**已修**，以及"为什么这么修"

**缺陷**：写库去重键是 UNIQUE `idx_findings_dedup(job_id, source, page, type, description)`，
而 `llm_checks` 三个产出分支的描述模板**不编码 `time`/`step_no`** ⇒ 同一参数在一页里
有多个时间点、同一取值时，描述**逐字相同** ⇒ `INSERT OR IGNORE` **静默丢弃**后一条。
实测 7 个 job 丢 **64–77 条 / 13–17%**，其中含 `param_out_of_spec` —— 是**静默漏检**。

**修法（已落地）**：新增 `_where_tag(q)`，把 `步骤N` / `时间点` 编进 `第{p}页` 之后，
三个分支统一调用；**两个字段都没有时返回空串** ⇒ 旧文案**逐字节不变**
（有专门用例 `test_without_time_the_text_is_byte_identical_to_legacy` 钉住）。

**为什么不去动 UNIQUE 索引**（这是关键取舍）：改索引要迁移历史数据、且会让"同一测量
重复出现"这类**真重复**也进库（复核页噪音）。缺陷在**产出侧**，不在索引本身 ——
同仓对照：规则层走 `make_oos_finding` + `_where_phrase`，同输入产出 **2 条互异**描述，**不丢**。

**证据**：变异 **4/4**（关标签 / 只丢 `time` / 只丢 `step_no` / 无字段仍拼垃圾标签），
每条后 sha256 逐字节还原；`test_cross_page_llm` 96 passed。

**残留（必须说清）**：**已经丢掉的存量行不可恢复** —— 旧 job 的 findings 计数偏低是
**历史事实**，不是当前代码的缺陷。任何拿旧 job 做的召回评估都要先声明这一点。

## 3. 0-41（P2）：**本轮已修**，选的是"分批"，不是"把降级做得更显眼"

**缺陷**：`len(llm_queue) > 50` 时**不调 LLM**，把该 job **全部**待判定参数一次性降级。
真实样本 job `670672f3-d0f`（丝裂霉素，**51 页 / 400 个待判定参数**）因此**一条
`param_out_of_spec` 都没有**；用户标注页 page 6 的实测值 `6.88` 在全 job findings 中
命中 **0 次**。

**为什么选分批**（结合真实用户需求）：GMP 复核的价值就在"51 页也要逐项判定"。
把降级做得再显眼，用户拿到的仍然是"400 个参数请你自己看"—— 那不是降级，是**没做**。
而 50 这个上限的原始理由只是"单次 prompt 别太大"（449 参数实测会让上游重试并卡住
Stage 3 数分钟），**它约束的是单次调用，不是整份记录** ⇒ 正确解法是**切块**，
不是"放弃"。

**实现**：`_judge_one_batch()` 判一批；`_llm_fallback_check()` 负责切块与两道闸门：

| 闸门 | 常量 | 取值 | 作用 |
|---|---|---|---|
| 单批大小 | `_LLM_FALLBACK_BATCH_MAX` | 50 | 与改造前一致（已验证尺寸） |
| 批数上限 | `_LLM_FALLBACK_MAX_BATCHES` | **8** | 400 参数正好覆盖真实最大样本；更大 ⇒ **显式**降级 |
| 墙钟预算 | `_LLM_FALLBACK_BUDGET_S` | **600s** | 防"批数 × 单批最坏"相乘（8×1440s=3.2h）；超预算 ⇒ **显式**降级 |

- 失败隔离：单批异常/解析失败**只**降级该批，其余批照常采用 LLM 判定；
- 降级一律 `aggregate=True`（**每批一条**汇总，不会退回"407 参数 = 407 条噪音"）；
- **不变式**：每个入参恰好落在「被判定」或「被某条降级 finding 覆盖」之一，不重不漏。

**证据**：新增 `tests/unit/test_llm_fallback_batching.py`（**11 条**）+ 改写旧用例
`test_queue_over_limit_is_batched_not_abandoned`；变异 **4/4**（还原旧行为 / 摘批数闸门 /
摘预算闸门 / 取消切块）。

> ⚠️ 首版变异只抓到 3/4 —— M4（把批大小从 50 改成 1000）**漏网**。原因：所有用例都用
> `_queue(_LLM_FALLBACK_BATCH_MAX + 10)`，队列长度**跟着常量走**，常量放大用例也一起放大。
> 补了两条**绝对**断言（批大小 ≤ 50、**60 个参数必须切 ≥ 2 批**）才抓住。这是"用例存在 ≠
> 断言有效"的又一例，已记入 `docs/PROJECT_PITFALLS.md`。

**代价（明说）**：真实最大样本从"1 次降级、0 次调用"变成"8 次调用"。按单批 120s 超时、
600s 全局预算，最坏新增 **10 分钟**级耗时。这是"把该判的判掉"的价格，不是性能回归。

## 4. 收尾 todolist（排除 #11 与最小标注集）

> **P0** 交付前必须；**P1** 本轮尽量；**P2** 交付后第一迭代。
> 每条都有判据；**没有判据的不许开工**。

| ID | 优先级 | 事项（对应原编号） | 现状 | 判据 |
|---|---|---|---|---|
| **C1** | **P0** | **0-42 已修**（静默丢数据） | ✅ 本批 | 见 §2；变异 4/4 |
| **C2** | **P0** | **0-41 已修**（大记录覆盖缺口） | ✅ 本批 | 见 §3；变异 4/4 |
| **C3** | **P0** | **交付前的端到端矩阵**（用户明确要求"完善的 e2e"） | ⏳ 见 §5 | 多轮 e2e 全绿 + 产物级 `e2e_frozen` 全绿 + 每轮独立日志 |
| **C4** | **P0** | 0-48 轮换泄露 key | 🔵 用户处理 | `check_leaked_keys.py` ③ 为空 |
| **C5** | **P1** | 0-51 **`.github/dependabot.yml`**（`ci.yml` 已引用但文件不存在） | ✅ 本批 | YAML 可解析；`electron`/`electron-builder` 在 ignore 里（镜像上没有 43.7.5） |
| **C6** | **P1** | 0-51b **CD 工作流**（`release.yml`） | ❌ 未做 | **前置**：需要一个能真跑的 Windows runner 与一次真实构建。**不许先写一份没跑过的 YAML** —— 见 §6 |
| **C7** | **P1** | 0-50 真实浏览器 e2e（Playwright） | ✅ **本轮完成** | **19/19 判据、RC=0**（产物 `0371511`，`devlogs/_verify/browser_e2e/run_r94b.log`；首版 14/14 见 `run_20261021_133135.log`）；变异 **3/3 CAUGHT**；设计与三次真实失败见 §8 |
| **C8** | **P2** | 0-55 `retry_job` 6 处裸写进 `db_lock` | ✅ **本轮完成** | 扫描器 **122 → 116**、键 **106 → 100**、`retry_job` **6 → 0**；变异 1/1（摘锁 ⇒ 扫描器回到 122、棘轮变红）；产物级 retry e2e **RC=0**（§5.2） |
| **C9** | **P2** | 0-53 `findings_archive` 读取入口 | ✅ **本轮完成** | 后端**只有写入点**（`api/jobs/actions.py:121`、`core/pipeline/stage3.py`），**没有任何读接口**；前端 `archive` 全是 **job 级**归档（`upload-jobs.js`），与 `findings_archive` 无关 ⇒ 需要新端点 + 「已归档」抽屉；空态有文案 |
| **C10** | **P2** | 0-54 审计可视化 | ✅ **本轮完成** | 两个端点**都已存在**且返回 `{entries, count}`（`api/review.py:713` / `:729`），但 `grep -rn audit static/ templates/` = **0 命中** ⇒ 端点全黑、无人调用 |
| **C11** | **P2** | 0-52 CHANGELOG + `Round`/`R` 双体系漂移 | ✅ **本轮完成** | `test_todo_freshness` **18 passed**；新增不变式 I（`[Unreleased]` 内 `Round` 非递增）+ 3 条用例，真实文档变异 **1/1 CAUGHT**（插入逆序条目 ⇒ 红，还原后 sha256 逐字节一致）；顺带修复第三十四批的**孤立标题**（标题在上、正文在下，被第三十五批的插入顶开） |

| **C12** | **P3** | 0-57 漏检哨兵（规则层与 LLM 结果做差集） | ❌ 未做 | 差集非空的页在 e2e 里能被断言出来 |
| **C13** | **P3** | 0-58 上传队列（第 6 个不再直接 409） | ❌ 未做 | 第 6 个任务进 `queued`，有空位自动开跑 |
| **C14** | **P3** | 0-60 图标管线 / 0-61 DPAPI 密钥加密 | ❌ 未做 | 日志不再出现 `application icon is not set`；`config.json` 无明文 key |

**建议顺序**：`C3`（e2e 门槛）✅ → `C7`（浏览器 e2e）✅ → `C8` ✅ → `C11` ✅
→ **剩下**：`C9` / `C10`（P2，都已定位到具体缺口）→ `C12`~`C14`（P3）→ `C6`（需 runner）。

## 5. 手工测试前的 e2e 门槛（C3 的展开）

**为什么不能只跑一次**：流水线计数**非确定**（0-15：同一输入 findings 数会变），
单次全绿不能证明稳定；而且上一轮多轮 e2e **卡死过**（35 分 48 秒停在 RUN 1、零输出）。

| 层 | 驱动 | 判据 | 现状 |
|---|---|---|---|
| 单元/集成 | `pytest`（全量） | 全绿 + 覆盖率 ≥ 95% + junit 交叉核对 | ✅ 见 §1.1 |
| 产物级（内嵌 exe） | `tests/e2e_frozen.py` | `pipeline_terminal=review`、`llm_audit` 真实调用、报告与页图落盘 | ✅ **本批全绿**：`covered=4 skipped=0 failed=0`、`llm_audit 3/3`、`findings=3`、`report_md 5477B`、`page_image 24241B` |
| **retry 专项（0-55）** | `devlogs/_verify/r93_055_retry_e2e.py` | 在产物上真走 `review → retry`，从产物自己的 `data.db` 核对归档/清空/审计 | ✅ **RC=0**（见 §5.2） |
| **多轮产物级** | `devlogs/_verify/run_e2e_rounds.py` | 每轮独立日志；任一轮超时/非 0 即未通过 | ✅ **2/2 全绿**（见 §5.1） |
| 真实浏览器 | Playwright（C7） | **19 判据全绿** | ✅ **19/19 OK，RC=0**（`run_r94b.log`，产物 `0371511`） |
| Electron 双击层 | `tests/e2e_unpacked.py` | 由**用户本机**跑 | ❌ 沙箱不可复现（不是产品缺陷） |

### 5.1 多轮结果（**实测**）

命令：

```bash
python devlogs/_verify/run_e2e_rounds.py --runs 2 --rounds pdf,img,cancel,robust --timeout-s 1500
```

| 轮 | 结果 | 耗时 | 轮内断言 | LLM 审计 | 终态 |
|---|---|---|---|---|---|
| RUN 1 | **PASS** | 1434.3 s | `-> OK` × 8，`-> FAIL` × 0 | `7/7` + `2/2` × 3，失败 0 次 | `review` × 4 + `cancelled` × 1 |
| RUN 2 | **PASS** | 1040.0 s | `-> OK` × 8，`-> FAIL` × 0 | `7/7` + `2/2` × 3，失败 0 次 | `review` × 4 + `cancelled` × 1 |

日志：`devlogs/_verify/e2e_rounds/run{1,2}_20261021_104444.log`（逐行带时间戳）。

**两轮断言计数完全一致**（8 OK / 0 FAIL、审计 7/7 + 3×2/2）—— 这是**稳定性**的正面证据，
但**不是**"流水线计数确定"的证据（findings 数仍按 0-15 属非确定项，本轮未拿它作判据）。

**关于上一轮"35 分钟停在 RUN 1"的更正（口径收紧）**：本轮在同一位置（`upload e2e_test.pdf`
之后）能**持续**输出进度，且单是 `pdf` 轮就要 **约 900 s**（6 页，其中 page 2 是 0-38 的
长尾页）。⇒ 上次"零输出"**更可能**是**块缓冲**造成的可观测性缺失（旧驱动把 stdout 重定向
到文件，4–8 KB 才落盘），而不是进程卡死。**但那只对上次那一次运行的结论是"无法判定"**——
本轮修的是**可观测性**（`PYTHONUNBUFFERED=1` + 逐行落盘），不是"证明上次没卡"。

⚠️ **未复跑**：`mineru` / `dual` / `rot` 三轮（`dual`/`mineru` 需要额外 token，`rot` 单轮
预算 1200+300 s）。它们**不是**"已通过"，只是本轮没跑。

**多轮驱动本轮的改进**（因为上次卡死拿不到证据）：
- `PYTHONUNBUFFERED=1` + 逐行落盘 ⇒ 卡死时能看到**最后一行**（旧版块缓冲，最后几行根本没写出去）；
- 单轮 `--timeout-s`（默认 1800s）⇒ 超时**先取证再杀**（进程表 / 监听端口 / 日志尾部）；
- 日志落在 `devlogs/_verify/e2e_rounds/run<N>_<ts>.log`，可直接贴进问题单。

### 5.2 retry 专项（0-55）—— 为什么必须单独一条

`e2e_frozen` 与多轮 `e2e_run` **都不碰 retry**，而 0-55 改的正是 `retry_job` 的锁范围。
集成用例只能证明"逻辑对"，只有走**打包产物**才能证明"真实进程里锁没把事件循环锁死、
归档与删除确实按顺序落盘"。

驱动：`devlogs/_verify/r93_055_retry_e2e.py`（**RC=0**，日志 `devlogs/retry_e2e_055.log`）。
它**故意不等 OCR/LLM 跑完**：要验的三件事全部发生在 `launch_pipeline()` 之前，
而 pipeline 产物是**非确定**的（findings 0–15；首次实测同一 1 页样本这次 0 条、
下一次整条 job `error`）⇒ 拿它当前置条件会让这条 e2e 随机红。

流程：上传（拿一个带 `pdf_path` 的真实 job）→ 立即 cancel → DB 里置回 `review`
并种一条 `confirmed` finding（夹具）→ `POST /retry` → **立刻**读产物自己的 `data.db`。

| 判据 | 实测输出 |
|---|---|
| 归档行存在且字段逐字保留 | `OK 归档：status=confirmed note='e2e-0-55 归档核对' reason=review_retry_full_reanalysis` |
| live findings 已清空 | `retry 前 findings=1 archive=0` → `retry 后 findings=0 archive=1`；`OK 清空` |
| 审计可重建 | `从 review 重试：归档并清空 findings 1 条（confirmed=1）与结构化分析（全量重新分析，保留 OCR 缓存）` |

> 过程中踩到的两处**真实**契约（已写进脚本注释）：设置接口的字段是
> `llm_provider` + `<provider>_api_key`（不是 `provider`/`model`），且**OCR 前置门**
> 也要配（否则上传 400）；`findings_archive` **没有 `id` 列**（主键是 `finding_id`）。

## 6. 本轮**故意没做**的一件事（以及为什么）

1. **`release.yml`（C6）**：写一份**从未跑过**的 CD 工作流，等于往仓库里放一条"看起来
   能发版"的声明。本仓库的门禁口径依赖 Windows + 精确依赖版本 + `ELECTRON_MIRROR`
   镜像，这些在 GH runner 上**未验证**。先补 `dependabot.yml`（纯声明、无执行），
   把 `ci.yml` 里那句"见 release.yml"改成**当前事实**，等有 runner 再补。

> 原本这里的第 2 条是"`retry_job` 进 `db_lock` 留到单独一批"——**本轮已做**（C8）。
> 做法与当时预告的一致：先 `transition_status`（它自取锁）→ 再 `async with db_lock`
> 包住 6 处写 → 审计用裸 `INSERT`（`_audit_log` 自取锁，锁内不可调用）。
> 关键取舍：**不能**把 `transition_status` 包进锁里 —— `db_lock` 非重入，包了是
> **永久挂住**，不是超时。

## 7. 未验边界（不许读成"已通过"）

- **精度/召回无数字**：没有最小标注集（0-56 后续）⇒ 只能说"不丢、不漏降级"，
  **不能**说"判得准"。
- **多轮 e2e 已跑 2 轮全绿**（`pdf,img,cancel,robust`），但 `mineru`/`dual`/`rot` 三轮
  **本轮未复跑** ⇒ 不能说"七轮全绿"。
- **Electron 双击层**在本沙箱不可复现，必须在用户机器上跑。
- **0-41 的真实上游行为未观测**：8 次调用在真实 provider 上的耗时/限流**未实测**，
  600s 预算与 8 批上限是**按最坏情况设的闸门**，不是实测出来的最优值。

- **浏览器层没覆盖真实 happy path**（OCR/LLM 成功 ⇒ `review` 且有 finding）：两份合成样本
  `o6_small_font_low_dpi.pdf` / `o1_small_box.pdf` 的 OCR 结果是**空页**（自愈重试后仍空），
  实测终态**稳定为 `error`**（连跑 5 次）。该路径由 `e2e_frozen` 与多轮 e2e 覆盖（**非浏览器层**）。
- **`B6_report_contract` 的 200 分支没在真实 job 上跑到**：真实 job 终态是 `error` ⇒ 走 303 分支。
  200 分支由**夹具 job**（`status=review`）在 `B6_report_renders` 里覆盖，但那条验的是"正文渲染"，
  不是"真实 job 的报告契约"。

## 8. C7 浏览器 e2e（**实测**）

驱动 `tests/e2e_browser.py`：Playwright + 真 chromium，跑**冻结产物**
（`dist-electron/win-unpacked/resources/pbc-server/pbc-server.exe`），**不是** `uvicorn` 开发进程。
最近一次：**14/14 OK，RC=0**（`devlogs/_verify/browser_e2e/run_20261021_133135.log`，3m12s）。

| 用例 | 判据 |
|---|---|
| B1 | 上传**3 页**文档 → 跳 `/jobs/{id}/review`，且 `#page-total` 真是 3 |
| B2 | `#btn-next-page` ⇒ `#page-num` 1→2；缩放 100%→125%→100% |
| B3 | 点「首页」→ URL 回 `/`、`#upload-area` 可见 → **再上传第二份**，job_id 不同 |
| B4 | 两个任务的 `jobs.status` 都落到 `TERMINAL_STATUSES`（**从源码取**，不 import） |
| B5 | 首屏（**SSR**）与第 2 页（**AJAX**）各点一次「确认」，两条都落库 `confirmed` |
| B6 | 真实 job 的报告路由契约按终态分支；夹具 job 正文含文件名 / Job ID / GMP；设置页改 `ocr.backend` 后 GET 读得回 |
| B7 | 无 `pageerror`；除 `/api/jobs/{id}/pages/N` 的 404 外无 4xx/5xx |

### 8.1 三次真实失败换来的设计（每一条都对应一次红，不是"预防性"设计）

| 现象 | 根因（定位） | 修法 |
|---|---|---|
| 旧的 `B3_page_nav` 第一次就"绿" | `devlogs/ocr_samples/*` **全是单页**（实测 6 份均 1 页）⇒ 走"按钮禁用（符合预期）"的**跳过分支** | 运行时用 pypdf 生成 **3 页**样本 |
| `page.goto(report)` 30s 超时 | 上传接口**立即 303**，任务还在 Stage 1；报告页对**非终态**再 `303 → 复核页` | 先 `_wait_terminal` 再开报告页（顺带成了一条真断言） |
| `wait_for_selector("#ocr_slices")` 30s 超时 | 设置页是**分区切换**（`<section data-section="ocr" class="hidden">`）；且 `#ocr_slices` 在 `data-backend-form="mineru"` 子表单里，后端为 paddle 时**永不显示** | 深链 `/settings#ocr`；断言载体换成始终可见的 `#ocr-backend-seg` |
| `no_js_errors` 红（2 条） | Chromium 的 "Failed to load resource" **不带 URL**；而那条 404 是**我们主动翻到未分析页**造成的（`/pages/2`） | 改判 `pageerror` + `response` 状态码（结构化真值），不再解析文案 |
| 变异 M1 **MISSED**（12/12 全绿） | 首屏 findings 是 **SSR** 渲染（`templates/review.html`），`static/review-findings.js` 只管**第 2 页起** ⇒ 摘掉 AJAX 侧 onclick 抓不到 | B5 拆成 SSR / AJAX 两条，各点一次 |
| 变异 M2 判 `NOT_REACHED` | 等不到 AJAX 按钮时 `wait_for_selector` **抛异常**，把整轮打断（后面的 B6/B7 全跳过） | 收敛成 `fail(...)` 断言失败，不再中断 |

> 最后两条值得单独记：**"变异没抓住"有两种**，一种是断言真的没判别力，另一种是
> **测试自己崩了**（NOT_REACHED）。前者要改断言，后者要改测试的健壮性 —— 混为一谈
> 会去改一条本来正确的断言。

### 8.2 变异（**实测 3/3 CAUGHT**）

`devlogs/_verify/mutate_browser_e2e.py` —— 只改 `devlogs/_verify/mutation/<M>/` 下的
**产物副本**，交付物一个字节不动（事后 `bundle_manifest --check` 仍 OK）。

| 变异 | 目标断言 | 结果 | 旁证 |
|---|---|---|---|
| 摘掉**首屏（SSR）**「确认」onclick | `B5_confirm_ssr` | **CAUGHT** | 同一次运行里 `B5_confirm_ajax` **仍绿** |
| 摘掉**第 2 页（AJAX）**「确认」onclick | `B5_confirm_ajax` | **CAUGHT** | 同一次运行里 `B5_confirm_ssr` **仍绿** |
| 摘掉「下一页」onclick | `B2_page_nav` | **CAUGHT** | 页码停在 1 |

两条「确认」变异互不干扰 ⇒ 不是同一个断言被数了两次。

### 8.3 与 `e2e_frozen` / 多轮 `e2e_run` 的分工

| 层 | 证明什么 | 不证明什么 |
|---|---|---|
| `pytest`（4244 条） | 逻辑、契约、单一真值、跨语言常量 | 真实浏览器里的 DOM 行为 |
| `e2e_frozen` | 冻结产物能跑通全链路（OCR→LLM→报告）、审计可追溯 | 用户点按钮之后发生什么 |
| 多轮 `e2e_run` | 稳定性（8 轮、并发、取消、鲁棒） | 同上 |
| **`e2e_browser`（本文件）** | **上传→复核→翻页→确认→报告→设置** 这条**用户路径**真的通 | 精度/召回；真实 happy path（样本 OCR 出空页） |

## 9. 交给你的手工测试清单

> 前置：用 `dist-electron/win-unpacked/` 里的入口启动（**这是唯一交付物**）。
> 每一步都写了"应该看到"；**看不到就是缺陷**，别当"可能就这样"。

0. **先配凭据**：设置页 → LLM 卡片内点「保存」→ OCR 分区（点左侧 `OCR`）→ 填 token 后保存。
   ⚠️ 设置页是**分区切换**的，默认只显示 LLM 区 —— 这是设计，不是加载失败。
1. **上传一份真实批记录**（`试用批记录.pdf` / `丝裂霉素提取批记录.pdf`，别用 `devlogs/ocr_samples/`
   里的合成小样 —— 那 6 份 OCR 出来是空页，终态会是 `error`）。
   应该看到：**立刻**跳到复核页（不等分析完成），页内有进度。
2. **翻页 / 缩放**：`下一页` 改页码；缩放 100%→125%，`重置` 回 100%。
   应该看到：页码与缩放标签同步变化。
3. **点「首页」→ 再上传第二份**。应该看到：回到上传区、能再传、拿到**新的 job**（不是旧的）。
4. **等任务到终态**（`review` / `partial_review` / `error`）。
5. **确认 / 拒绝 finding**：首屏点一次；再**翻到第 2 页**点一次。
   应该看到：两次都真的改状态（刷新后仍是新状态）。首屏与第 2 页是**两条不同的渲染路径**。
6. **报告页**：`review`/`partial_review` 才出正文；`error`/`cancelled` 会**跳回复核页**（设计如此）。
7. **归档 / 重试**：重试会把当前 findings **归档并清空**再全量重跑 —— 这是 0-44 的设计，
   不是数据丢失（归档行保留 `reviewer_note` 原文）。
8. **已知缺口**（不必当缺陷报）：界面上**没有**"已归档 findings"查看入口（C9）、
   没有审计/模型调用可视化（C10，端点已存在但无人调用）。

### 9.1 你机器上**必须**补跑的两条（本沙箱做不到）

```bash
# ① Electron 双击层（本沙箱内 chromium 必崩，非产品缺陷）
"C:/Users/WuSiTan/AppData/Local/Programs/Python/Python311/python.exe" tests/e2e_unpacked.py

# ② 真实 happy path 的浏览器层（需要一份 OCR 能出文本的样本）
#    把 tests/e2e_browser.py 的 PDF1/PDF2 换成真实批记录，再跑一次；
#    预期 B4 的终态变成 review（而不是 error）。
```

## 10. C9 / C10 实测（Round 88 第三十七批）

**问题形态是同一个**：数据在库里、端点在线上、**界面没有出口**。

| 项 | C9（0-53 归档读取） | C10（0-54 审计可视化） |
|---|---|---|
| 定位证据 | `grep -rn findings_archive api/ core/ db/` 只出 INSERT + schema；写入点 = `api/jobs/actions.py`、`core/pipeline/stage3.py` | 端点 `api/review.py::get_audit_log` / `get_llm_audit_log` 存在且返回 `{entries, count}`；`grep -rn audit static/ templates/` = **0 命中** |
| 改动 | 新增 `GET /api/jobs/{id}/archived_findings`（只读，`limit` clamp 1..500，`total` 独立于 `count`） | 新增 `static/review-audit.js` + 模板入口按钮 + `review.html` 引脚本（入口之前） |
| 陷阱 | 路径名若为 `/findings_archive` 会被 `GET /findings/{finding_id}`（`int`）抢走 ⇒ **422** | 审计内容含 OCR 原文/上游 error ⇒ 必须 DOM API，不许 `innerHTML` |
| 判据 | 8 条集成用例（含 total/limit/offset/403/路由不被抢） | 15 条单测（7 接线 + 8 行渲染，node 实跑）+ 浏览器 B8（4 判据） |
| 变异 | M1 顺序倒置 / M2 total 恒 0 / M6 路径名冲突 ⇒ **CAUGHT** | M3 列错位 / M4 漏引脚本 / M5 空态丢理由 ⇒ **CAUGHT** |

**合计变异 6/6 CAUGHT**，每条跑完按 sha256 逐字节还原（`devlogs/_verify/r94_mutate_c9c10.py`）。

**为什么 `action` 不翻译**（这是本批唯一"反直觉"的取舍）：`audit_log.action` 是
GMP 追溯用的**机器标识**（审计员按 code 对表），`detail` 已经是中文。再加一张
action→中文 表就是继 `status.js` / `findings-map.js` / `zh_map.py` 之后的**第四份**
中文真值 —— 新增一个 action 就要改两处，漏一处就漂移。少一张表，少一类缺陷。

## 11. 剩余 P3 的入口（下一迭代开工点，**本批不做**）

三条都**不改**本轮交付物，但开工前不必再重新定位一遍。判据按"能被机检"写。

| ID | 事项 | 定位（已核实） | 判据 | 为什么本批不做 |
|---|---|---|---|---|
| **C12** | 0-57 漏检哨兵（规则层 vs LLM 层做差集） | 规则层产出在 `core/rules/`（`make_oos_finding` 等），LLM 层判定在 `core/pipeline/stage3.py` 的 `llm_checks` 分支 —— 两者对同一 `(page, 参数)` 的结论目前**没有任何比对** | 差集非空的页能在 e2e 里被断言出来（一条 `audit_log` 或独立表 + 端点） | 需要先有**真实标注**才能判断"哪一边对"；无标注时差集只能证明"两边不一致"，不能证明"漏检"。与 0-56 同批做才有意义 |
| **C13** | 0-58 上传队列（第 6 个不再直接 409） | `api/jobs/upload.py`（`active_count >= _MAX_CONCURRENT_JOBS` ⇒ 409）、`_MAX_CONCURRENT_JOBS` 默认 5（`api/jobs/__init__.py`） | 第 6 个任务进 `queued` 且有空位时自动开跑；`test_upload_...` 现有 409 用例改为队列用例 | 引入 `queued` 状态要同步 `TERMINAL_STATUSES`/SSE/看门狗/前端状态映射四处，属于**状态机扩张**，不是收尾期的低风险改动 |
| **C14** | 0-60 图标管线 / 0-61 DPAPI 密钥加密 | `electron/main.js`（`electron/icon.ico`）、`build/icon.ico`（190 B / 16×16 占位，< builder 要求的 256×256）；`config.json` 明文 key | 日志不再出现 `application icon is not set`；`config.json` 无明文 key 且旧配置能迁移 | 图标需要**真实资产**（不是代码问题）；DPAPI 要处理"旧配置迁移 + 非 Windows 回退"，两者都会改变用户现有安装的行为 |

## 12. 交付状态（2026-10-21，产物 = `0371511`）

| 项 | 值 |
|---|---|
| 交付入口 | `dist-electron/win-unpacked/`（`package.json` 的 `win.target=dir`） |
| 入包清单 | `dist/pbc-server/build_manifest.json`：`version=1.2.1 files=117 head=0371511 dirty=False` |
| 新鲜度 | `bundle_manifest --check` 对 `dist/pbc-server` 与 `dist-electron/.../pbc-server` **均 OK（逐字节一致）** |
| PROVENANCE | BOM + CRLF，`git_head=0371511` |
| CSS | `static/app.css` 20 046 B（新增 `align-top` / `border-collapse`，改了 `static/*.js` 必须重建） |
| 浏览器 e2e | **19/19 OK，RC=0**（`devlogs/_verify/browser_e2e/run_r94b.log`） |
| 产物级 e2e | **RC=1**：`covered=2 failed=2`，根因**上游 402 欠费**（非产品缺陷）⇒ **LLM 链路本轮未验** |
| 发布门禁 | 10/11 pass（唯一未过项 = tests_coverage：4269 passed / **1 ERROR**，coverage **95.17%** ≥ 95%。ERROR 发生在 test_watchdog.py::test_fresh_job_not_reported 的**夹具装配期**，junit 里是 `PermissionError: [WinError 10013]`（**沙箱禁止套接字绑定**），该用例单独跑 **3/3 passed** ⇒ **环境限制，不是回归**；但门禁因此是 10/11，不得读成 11/11。另：首跑 10/11 的 tests_coverage 是**沙箱拒绝仓外 TEMP**（571 红），已用仓内 basetemp 复跑；第二次因 coverage 下全量超 1800s 门禁默认超时，已提到 5400s） |
| 全量单测 | `4270 passed / 0 failed / 2 skipped`（1474.35 s） |

**2026-10-21 16:52 复核（新事实，覆盖上面那条 402）**：同一 `config.json` 的
`SILICONFLOW_API_KEY` 现在对**两个**允许模型（`DeepSeek-V3.2` / `DeepSeek-V3`）都返
**HTTP 401 `code=30014 Token is invalid`**（15:05 时还是 402 欠费）⇒ **key 已被轮换、
配置文件仍是旧的**。把新 key 写回 `config.json` 后再复跑 frozen；探测脚本（已持久化，只打印状态码、不打印 key）：
`python devlogs/_verify/probe_siliconflow_models.py` —— 200 / 401 / 402 三档直接给结论。

⚠️ **手工测试前请先充值或换 key，再复跑 `frozen`** —— 否则拿到的终态仍是 `error`，
而 `error` 在报告路由上是 303 跳转（设计如此），会被误读成"报告页坏了"。

---

## 13. 手工标注 10 条 → 5 组修复（Round 88 第三十八批，R95）

用户拿 `dist-electron/win-unpacked` 对 `试用批记录.pdf`（真实 job `4cffb358-3b3`，10 页，
`paddle`，47 条 findings）逐页人工标注 10 处。**每条都先定位到具体 finding id / 结构化字段，
再改代码** —— 不是按描述猜。

| # | 页 | 用户原话（要点） | 定位到的东西 | 判定 | 修法 |
|---|---|---|---|---|---|
| **A1** | p1 | 时间一致没问题；但 QA 批准时间早于执行/检查日期**不可以** | `1614` `user_rule/critical`：「…签名日期（2025.06.03）早于 QA 批准人…**但三者日期相同**」 | **误报**：文案自相矛盾（断言"早于"又自述"相同"） | `llm_finding_guard._check_same_day_contradiction` 抑制 |
| **A2** | p1 | 批号一致，**为什么标题是参数超标** | `1624` `param_out_of_spec/info`：「第1页批号…与第2页批号…不一致」 | **类型错配**（标题与内容不符） | `finding_quality.type_from_description` → `batch_inconsistency` |
| **A3** | p2 | 检查人与复核人日期同一天，**没问题** | `1615` `user_rule/critical`：「均为 2025.06.03，但…不应早于…」 | **误报**：同日不构成先后倒置 | `_check_declared_order` 同日抑制（原先 `a == b` 时返回 `None` 即放行） |
| **A4** | p3 | 「这里有问题」→ 同一栏签名被读成两个名字 | `1593` `llm_page` + `1623` `llm_cross`：「王小雨 vs 王涛雨」 | **同一人 OCR 变体** + 同缺陷两条 | 名字变体降级（1 字之差）+ `_TYPE_FAMILY` 跨源去重 |
| **A5** | p3 | 执行人都是王小雨，检查人都是沈雅晨 | p4 step 8/9/10：`operator=reviewer=王小雨`（**真自核自检**） | 漏聚合：逐行 3 条，用户读成"有点多" | `_check_self_review` 按人聚合为 1 条（`工序8、9、10`） |
| **A6** | p4 | 湿度规格 **24–65**、实测 **35** ⇒ 没问题；自核自检/参数规则有**误报** | `1617` `param_out_of_spec/info`：「湿度 35%RH%RH，单位重复」；单元格 `in_spec=true` ⇒ R3 **从未触发** | **类型错配**（真缺陷是"单位重复"，不是"超差"） | `type_from_description` → `completeness`；同时补 `is_spec_verdict` 防**反向**洗（真有区间违规时不许改成别的类型） |
| **A7** | p6 | 执行人和检查人**都有签字**，是 OCR 漏识别？ | `1592/1629/1633/1635` 均写「签名缺失」 | **归因错误**：把"没识别到"说成"缺失" | 文案改为「未识别到…签名（如原页已签，属 OCR 漏识别）」；`_has_signed` 只在**姓名非空**时算已签 |
| **A8** | p8 | 复核人及日期**没写**，但系统**没提示** | `signatures` 里有 `{"role":"reviewer","name":""}` ⇒ 旧代码 `has_reviewer=True` 直接跳过 | **漏报** | `_blank_slots` 新增「有 N 处复核人签名栏为空」条目（仅在同类另有已签署人时加，避免与"整类缺失"重复） |
| **A9** | p3/p4/p8/p9 | 「这里问题有点多」 | 同一缺陷在 `rule` / `llm_page` / `llm_cross` / `user_rule` 各出一份 | **噪声** | stage3 跨源去重：`rule` 已覆盖 ⇒ 压 `llm_*`；`llm_page` 已覆盖 ⇒ 压 `llm_cross`（`_TYPE_FAMILY` 把两个签名类归一族） |
| **A10** | p8 | 「他们有签名和日期」 | `transferor` / `acceptor` 已签却被漏 | **漏识别** | 与 A7 同一条归因修法 |

### 13.1 离线重放的净效果（**实测，但不是产物级**）

`devlogs/_verify/r95_replay_sy.py`：读真实 `_pbc_app_copy.db`，LLM 两分支**打桩**、
把 DB 里真实的 `llm_cross/llm_fallback/user_rule` 行经新的 ingest 逻辑**注入**，再跑
`apply_review → reduce_self_referential_noise → reduce_completeness_noise → 跨源去重 → normalize_finding`。
跨源去重**直接 import `stage3._overlap_covered_by`**（不在脚本里复制判定 —— 早先复制版漏了
`rule` 覆盖，可见数会偏高 2 条，已改）。

| 项 | 改前 | 改后 |
|---|---|---|
| 可见 findings | **47**（DB 实际） | **34** |
| 规则层（降噪前） | 22 | **49**（A7/A8/A10 的槽位条目被正确产出：`未识别到…（如原页已签，属 OCR 漏识别）`） |
| 自指降噪 / completeness 降噪 | — | **17 / 13** |
| 跨源去重抑制 | — | **10 条**（1 条 `llm_page` 覆盖、9 条 `rule` 覆盖） |
| 同日抑制（guard 层） | — | **2 条**（A1、A3，理由均引「同一天不构成倒置」） |
| 自核自检 | 3 条 | **1 条**（`工序8、9、10` 合并 + 低置信度提示） |
| A8 空槽 | 无 | `第8页 工序 有 1 处复核人签名栏为空（同页另有已签署的复核人，属逐行核对）` |
| 严重度分布 | — | `{info 19, warning 13, critical 2}` |
| 类型分布 | — | `{completeness 25, signature_time_anomaly 4, batch_inconsistency 2, step_gap 1, self_review 1, handwritten 1}` |

**抑制明细可核**（脚本原样打印）：
`(3, signature_mismatch, llm_cross ← llm_page)` —— 即 A4 那条"王小雨 vs 王涛雨"；
`(7/8, signature_time_anomaly, llm_cross ← llm_page)`；其余 7 条 `completeness, llm_cross ← rule`。
⚠️ **没有一条 `signature_mismatch` 被 `rule` 的 `signature_time_anomaly` 吞掉** —— 这正是
`_overlap_covered_by` 两侧口径不对称的意义（规则层只按**精确类型**覆盖），护栏
`tests/unit/test_r95_manual_labels.py::TestCrossSourceDedup`（5 条）钉住。

⚠️ **不得读成"产物已修好"**：这是**离线重放**（LLM 打桩 + 注入历史行），不是重新跑流水线；
且流水线**非确定**（0-15：同一输入两次 41 vs 51）⇒ **单次计数不作回归判据**。
产物级证据要等重建后跑 `e2e_frozen`（§14 之后）。

### 13.2 A7 / A10 的取证：确实是 OCR 漏识别（不是"记录没签"）

用户当时问的是「我们是 OCR 漏识别了？」。这句话不能靠推测回答 —— 三份材料都在，逐页对：

| 页 | OCR 原文（`page_cache.raw_html`） | 页图（`devlogs/_verify/_sy_pages/`） | 判定 |
|---|---|---|---|
| **p6** | `执行人/日期：`、`检查人/日期：` **两格为空**，只有 `中控QA批准：孙文莉 2025.05.20` | `p06_sign.png`：两格都写着 `…2025.05.20`（连笔） | **OCR 漏识别** |
| **p8** | `转移人/日期：___ 2025.06.03 接受人/日期：___ 2025.06.03` —— **日期读到了，名字成了三个下划线** | `p08_bottom.png`：两个名字都清晰可见 | **OCR 漏识别**（只漏姓名） |
| **p8 复核人** | 表格行 `… 检查人/日期=张珂 ‖ 复核人/日期=2015.6.03`（**名字与日期串列**） | 复核人栏确实空白（用户 A8 原话） | **真空白** ⇒ `_blank_slots` 报得对 |

⇒ 结论分两层：
1. **日期、印刷体识别正常**，漏的是**连笔手写姓名** —— 全 10 页"标签在、值空/`___`"的槽位共
   **5 处**（p1×1、p6×2、p8×2）。这是**后端能力问题**，本批只能把归因说对（已登记 `0-63`，P2）。
2. **p8 的复核人空白是真空白**，`_blank_slots` 报的「有 1 处复核人签名栏为空」是对的 ——
   这正是 A8 要的提示。

⚠️ 所以 A7/A10 的条目**仍然会出现**（只是文案从"缺少"变成"未识别到…（如原页已签，属 OCR 漏识别）"）。
手工复测时看到它们**不等于没修好** —— 修好的是"不再把 OCR 问题写成记录缺陷"。

### 13.3 顺带查出来的一条（**未修**，登记 `0-64`）

对 10 页 `raw_html` 全量 `grep 生产日期` = **0/10** ⇒「缺少生产日期」是**真缺口**；
但它被 p5 / p10 各报一条（`1590` / `1595`），同一条文档级缺陷在复核页出现两次。
`reduce_completeness_noise` 的 `min_group=8` 打不到 2 条 ⇒ 原样保留。
属 A9 同族（重复），但发生在**文档级**而不是页级 —— 修法要先定义"文档级字段"的判定域，
不是调阈值就能对。

## 14. 首次打开 30 秒：定位到秒级，且不全是"缺陷"

用户原话：「首次打开等待了 30S」。**先用用户自己那次启动的台账分段**（
`%APPDATA%/PBC/logs/backend-boot.log`），不是猜：

| 时刻（UTC） | 事件 | 段耗时 |
|---|---|---|
| `00:07:21.013` | `[spawn] pbc-server.exe` | — |
| `00:07:35.139` | `[boot] server.py entering` | **14.1s** 进程创建 + Windows 加载器 + 杀软扫描 |
| `00:07:37.050` | `[PBC] Starting server on 127.0.0.1:58765` | **1.9s** uvicorn 自身 |
| `00:07:56.437` | `INFO: Started server process` | **19.4s** `import main`（最重） |
| `00:07:56.935` | `[ready] after 70 failed checks` | **0.5s** lifespan（建库 + KB 装载） |
| | | **合计 35.9s** |

同一份产物**缓存热**时全程 **2.7s**（`devlogs/_verify/r95_startup_probe.py`：`t_boot 0.58 / t_bind 0.73 /
t_health 2.76 / t_index 2.79`）⇒ **35.9s 不是逻辑缺陷，是冷缓存下按文件计费**。

⚠️ **上一版探针测错了端口**（设 `APP_PORT=8099`，而 `server.py:37` 只读 `PORT`）⇒ 两次"超时"
是**测量错误**，不是产品缺陷。这条记进 `PROJECT_PITFALLS`：**改端口必须改 `PORT`**。

### 14.1 修法：把启动期不用的 SDK 移出导入链

`import openai` 独占 **1007 个模块**，而 `import main` 全链 **1340 个** —— 一个 SDK 占 **75%**，
可全树只用 `AsyncOpenAI` 一个符号（`llm/adapters/openai_adapter.py`，唯一导入点）。

| 指标 | 改前 | 改后 |
|---|---|---|
| 启动期导入事件 | **1340** | **665**（‑50.4%） |
| 累计导入耗时（同机、缓存热） | **2.91s** | **1.13s**（‑61%） |
| `'openai' in sys.modules`（`import main` 后） | True | **False** |

护栏 `tests/unit/test_startup_import_cost.py`（6 条）：子进程实测 `sys.modules`、
模块数预算（**900**，严格小于改前 1340 ⇒ 有判别力）、AST 禁模块级 openai 导入、
正对照（延迟导入必须**真能**导入且客户端类型正确）、"建客户端后才进 `sys.modules`"。
变异 **4/4 CAUGHT**（把导入挪回模块顶部 ⇒ 4 条红；还原后 sha256 逐字节一致）。

⚠️ **未验**：**冻结产物**的冷启动没有重测 —— 冷缓存需要重启或清 standby list，本沙箱做不到。
⇒ 「35.9s → ?」**没有数字**，只能说**启动期少碰 675 个模块文件**（结构性事实），
下次全新安装/重建后的首次启动才是有意义的观测点。

### 14.2 没做的那一半（诚实边界）

14.1s 的**进程创建 + 杀软扫描**段没有动：那取决于 117 MB / ~1900 文件的 one-dir 载荷与
用户机器的实时防护策略，改代码消不掉。可选方向（**本批不做**）：减小载荷（裁 `pymupdf` 全量 DLL、
PIL 插件）、或给安装包加 Defender 排除项说明 —— 两者都会改变分发形态，属下一迭代。

---

## 15. 交付状态（2026-10-22，产物 = `0b4972f`）—— 覆盖 §12

| 项 | 值 |
|---|---|
| 交付入口 | `dist-electron/win-unpacked/`（`package.json` `win.target=dir`） |
| 入包清单 | `build_manifest.json`：`version=1.2.1 files=117 head=0b4972ff3ea0 dirty=False` |
| 新鲜度 | `bundle_manifest --check` 对 `dist/pbc-server` 与 `dist-electron/.../pbc-server` **均 OK** |
| 内嵌一致性 | 两份 `pbc-server.exe` sha256 同为 `1eedf6dd…`；`test_distribution_parity` + `test_bundle_manifest` **63 passed / 2 skipped** |
| PROVENANCE | BOM + CRLF（11 行），`git_head=0b4972f` |
| CSS | `static/app.css` 20 046 B（**本批未改** `static/` 与 `templates/`，故未跑 `build:css`） |
| **产物级 e2e** | **29/29 passed，RC=0**（`devlogs/e2e_coverage_20261022-111754.json`）：`pipeline_terminal=review`、**`llm_audit 3/3`**、`covered=4 skipped=0 failed=0`、`findings=4`、`report_md 7161B`、`page_image 24241B`、`ocr_backend_used=paddle` |
| **浏览器 e2e** | **19/19 OK，RC=0**（`devlogs/_verify/browser_e2e/run_r95.log`，3 分 01 秒，含 B8 五条） |
| 全量单测 | **4313 passed / 0 failed / 2 skipped**（516.87 s） |
| 启动（产物实测） | `/health` 就绪 **热 2.72s → 1.08s**（`devlogs/_verify/_r95_startup.json`）；**冷启动无数字**（见 §14.1） |
| **发布门禁** | **11/11 PASS，0 fail / 0 warn / 0 skip**（`devlogs/gate_report_20261022_112856.json`）：`tests_coverage` **4313 passed / 0 failed / coverage 95.2%**（门槛 95%，`fact=junitxml`）、`artifact_freshness` 两份产物逐字节一致、`runtime_eol` Electron/43.7.4、`worktree_clean` 干净 |

### 15.1 这一轮与 §12 的关键差别

1. **LLM 链路首次被真实验证**：§12 那次 `e2e_frozen` 是 RC=1（上游 402 欠费）⇒ `llm_audit`
   与 `pipeline_terminal` 两项**未验**。本轮换 key 后 **`llm_audit 3/3`、`pipeline_terminal=review`、
   `covered=4 failed=0`** ⇒ 这两项第一次有产物级证据。
2. **`findings` 计数仍不可作回归判据**：本轮的 4 条是**这一次运行**的观测值，不是基线
   （0-15：同一输入两次 41 vs 51）。
3. **产物确实包含本批五组修复**：`core/` 与 `llm/` 都改了 ⇒ 重建是必须的；判据是
   `build_manifest` 的 `head=0b4972f dirty=False` 与两份 exe 的 sha256 一致。

### 15.2 手工复测建议（拿到新产物后）

- 首开：**不要**期待 3 秒 —— 冷缓存下仍有十几秒的进程创建段（§14.2）；但 `import` 段已从
  19.4s 量级降下来，**同一次会话里再开应该明显更快**。
- 重点看 **A1/A3（p1/p2 不应再出现"同日却报倒置"）**、**A2/A6（标题应是批号不一致 / 单位重复，
  不再是"参数超标"）**、**A8（p8 应出现"有 1 处复核人签名栏为空"）**、
  **A5（p4 的自核自检应是 1 条并合并工序 8、9、10）**。
- A7/A10 的"签名缺失"文案应变成"未识别到…（如原页已签，属 OCR 漏识别）"—— 这只是**归因**变了，
  条目还在。若原页确实有签名，那是 OCR 问题，属另一条线（不在本批范围）。

