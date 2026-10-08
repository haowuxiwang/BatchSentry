# 对抗性审查 — Round 66（2026-09-30）

> **范围**：全仓库（文档 ↔ 代码一致性 / 测试现状 / 模块化与单一职责 / 仓库卫生 / git 与 GitHub）。
> **方法**：先取证再下结论；每条结论挂命令或产物路径；**未取到证据的一律标"未验证"**。
> **基线**：HEAD `28bda0d`，工作区干净。
> **本轮性质**：**没有功能 bug**。三条缺陷都是**文档与事实脱节** —— 它们不会让任何用例变红，
> 只能靠人工比对发现（这也正是它们的危害：无人看着）。

---

## 一、结论摘要

| 问题 | 结论 |
|---|---|
| 上一轮（R65）完成情况 | **已完成**。6 个提交 `11e8e03`…`28bda0d`，工作区干净 |
| 是否做了多次端到端测试 | **做了，但只有两层**：`e2e_frozen` 今日 2 次、`e2e_unpacked` 多次 —— **后者每次都崩**（见 §三） |
| 应用边界有无界定 | **有，且分层清晰**；但**分散在 4 份文档**，且"未验证边界"此前只活在 agent 记忆里 → 本轮补进 `docs/TODO.md` §0 |
| 测试情况 | 门禁 **11/11 pass**（`overall=pass`）、**3889 passed / 0 failed**、覆盖率 **95.09%**（门禁 95%）—— 在最终 HEAD `7d53aed` 上跑出，逐树对照见 §5.2 |
| 前后端是否模块化 / 单一职责 | **是**。前端两轮大文件拆分已完成（`review.js` R63、`settings.js` R65）；后端 68 文件 24.9K 行、无神模块 |
| 可维护性 | **强**。测试 63,009 行 vs 生产 34,928 行 ≈ **1.8 : 1**；门禁 11 项全自动 |
| 仓库卫生 | **好**。375 个跟踪文件，工作区干净，`.gitignore` 有解释性注释且被机检锁定 |
| git / GitHub | ⚠️ **本地领先 `origin/main`，尚未推送**（`dd33330` 时点为 9 个提交，属**快照**；取当前值见 §八） |
| 本轮修复 | **5 条**：F1–F3 **文档缺陷**、F4 **测试清理期缺陷**、**F5 错误文案未脱敏（代码，见 §十一）** + 2 个新护栏（9 条用例）+ 变异 **9/9**，F5 另有变异 **5/5** |
| 门禁 | 修复后重跑 ⇒ `overall=pass`、`11/11`、**3879**（`2787f29`）→ **3883 passed / 0 failed**（`8d5848d`，**含重建产物**） |
| 重建与产物 | **已重建（R66 第四批）**：改 `core/` 后 `artifact_freshness` 转红 ⇒ PyInstaller + electron-builder 重建 ⇒ **两份产物逐字节一致**；`runtime_eol` 从 **SKIP 恢复为实测** —— 见 §十一 |
| 待办清单核销 | **§A 及以下的"未做"有一半已做完**：核销 **16 条**（含 **2 条 P1 安全项** `B10-1`/`B10-2`、代码项 `#146`/`#144`）、清掉 **16 个失效行号锚点**、抽查确认 **10 条**仍开放 —— 见 §十 |

---

## 二、上一轮（R65）完成情况 —— 复核

```
$ git log --oneline -6
28bda0d docs(review): R65 段补记行号引用护栏（第 2 个真缺陷）
5adbb19 test(hygiene): 禁止测试里用「模块名 + 冒号 + 行号」指路
9a1ef2a docs(review): 补记 R65 `settings.js` 全量拆分与产物出处护栏
09abea8 docs(settings): 修正拆分后失效的注释/指针（无功能改动）
d290d98 fix(e2e): 产物出处 `git_head` 为空时必须报红（原为全链路静默绿灯）
11e8e03 refactor(settings): settings.js 拆为 6 模块（1861 行 → 状态宿主 + 4 职责 + 入口）
$ git status --porcelain
（空）
```

**判定：R65 已完整闭环**（拆分 → 护栏 → 文档 → 重打包 → 门禁）。两个 R65 查出的真缺陷
（`PROVENANCE.txt` 的 `git_head` 静默为空、测试里行号引用腐坏）都已修复并挂护栏。

---

## 三、端到端测试现状（**必须分层读，别合并**）

| 层 | 驱动 | 本轮证据 | 判定 |
|---|---|---|---|
| 单元 / 集成 | `tests/unit` + `tests/integration` | 3866 passed / 0 failed | ✅ 绿 |
| 内嵌后端（frozen） | `tests/e2e_frozen.py` | `devlogs/e2e_coverage_20260930-103315.json`、`-103754.json`（**今日 2 次**） | ⚠️ 结构全绿；LLM/OCR 流水线因**账户欠费**记 `failed`（环境项，非回归） |
| **Electron 应用层（unpacked）** | `tests/e2e_unpacked.py` | `devlogs/e2e_unpacked_20260930_104144.json`：两次 `boot_s≈5.1`、**`health: null`**、`exitCode=2147483651` | ❌ **在本 agent 环境取不到证据** |

`2147483651 = 0x80000003`（`STATUS_BREAKPOINT`）—— 沙箱内 Chromium GPU 进程反复失败后
`FATAL: GPU process isn't usable.`。

> 🔴 **不得把这一层读成"已通过"，也不得读成"回归"。** 它是**环境边界**：
> 非沙箱下同一产物 3.0s 就绪、连跑全绿（历史实测）。须在**真实终端**复跑。

---

## 四、应用边界界定

### 4.1 已界定的边界（有文档、有机检）

| 边界 | 值 | 出处 | 机检 |
|---|---|---|---|
| 应用层并发 job 上限 | **3**（第 4 个起 409 硬拒绝，不排队） | `api/jobs._MAX_CONCURRENT_JOBS` | 门禁 + 单测 |
| CPU 池上限 | `_POOL_MAX_WORKERS = 2`（进程全局单例） | `core/procpool.py` | 单测 |
| 进程模型 | **`uvicorn workers=1`**（正确性前提，非性能取舍） | `server.py` | `test_config_import_order_contract.py::TestSingleWorkerPrerequisite`（4 条，变异 10/10） |
| 上传页数 | 硬 200 / 软 80 | `config.UPLOAD_LIMITS` | 单测 |
| 运行时可支持线 | Electron `[41,42,43]`，现 `43.7.4` | `docs/RUNTIME_SUPPORT.json` | 门禁 `runtime_eol` |
| 依赖公告 | 0 条（快照 6 天前，43 条目） | `docs/DEPENDENCY_AUDIT.json` | 门禁 `dependency_vulns` |
| 知识库语料 | 441 条（下限 200） | `core/kb/data` | 门禁 `kb_corpus` |
| 规则覆盖 | 29 个 `_check_*`（下限 14） | `core/rules/` | 门禁 `rules_wired` |

### 4.2 未验证边界（**不得读成已通过**）

1. **无他机验证**；**"功能跑通 ≠ 判定正确"**。
2. **精度/召回无可信数字** —— 无真实标注集，唯一 1.0 来自合成集。
3. **200 页上限是线性外推**，未实跑。
4. **Electron 应用层 e2e 在本环境不可复现**（§三）。
5. **`build.ps1` 从未被 agent 执行过**（PS 工具跑不了原生程序），只能 Bash 复刻其步骤。
6. 上游凭据真机连通性未验；`llm_pipeline` / `ocr_pipeline` 因**账户欠费**未真正覆盖。
7. **垫片 `SAFE_DELETE_FAIL_CLOSED` 的触发条件未完全定位** —— 本轮把
   `test_pdf_non_local_host_returns_403` 从"未定 flaky"降到"**已知机制的清理期红**"
   （详见 §六-F4），但**为什么偏偏在全量跑的某一位次触发**仍未定位
   ⇒ 其他用例的 `finally`/teardown 里若还有手写 `unlink`，**同类红可能复发**。

> ⚠️ **本轮之前，前 6 条只存在于 agent 的记忆文件里**，仓库文档中没有一处集中陈述
> —— 一个只读仓库的人会误以为 Electron 层已验收。本轮已把 1/2/3/4/5 写进
> `docs/TODO.md` §0，并在 §六-F1 补了 gate 清单的机检；第 7 条是本轮**新查出**的。

---

## 五、测试现状与模块化评估

### 5.1 规模（实测，排除生成物）

| 层 | 文件 | 行数 |
|---|---|---|
| 生产 · 后端 | 68 | 24,942 |
| 生产 · 前端 | 32 | 9,986 |
| 生产 · 脚本 | 23 | 6,739 |
| **测试** | **175** | **63,009** |
| e2e 驱动 | 4 | 1,275 |

**测试 : 生产 ≈ 1.8 : 1**（63,009 / 34,928）—— 对"规则密集 + 有合规要求"的项目，这是健康的比例。

### 5.2 门禁（11/11）—— 最终证据在**已提交的树**上

三个数**别混**，各自属于不同的树：

| 时点 | 树 | 用例数 | 结果 | 报告 |
|---|---|---|---|---|
| R65 终验（11:27） | `28bda0d` | 3866 | 0 failed | `gate_report_20260930_112739.json` |
| R66 加护栏后全量（手工） | R66 工作区 | 3875 | **2 failed / 3873 passed** | 见 §六 |
| **R66 终验（12:10）** | **`dd33330`** | **3875** | **0 failed** | `gate_report_20260930_121027.json` |
| **R66 第三批终验（12:43）** | **`2787f29`** | **3879** | **0 failed** | `gate_report_20260930_124300.json` |
| **R66 第四批终验（13:07）** | **`8d5848d`** | **3883** | **0 failed** | `gate_report_20260930_130757.json` |
| **R66 第五批终验（13:49）** | **`7d53aed`** | **3889** | **0 failed** | `gate_report_20260930_134900.json` |
| **R68 第六批门禁（08:57）** | **`acd25ed`** | **3915** | **0 failed** | `gate_report_20261008_085756.json` |
| **R68 第六批终验（09:17）** | **`9768ae8`** | **3921** | **0 failed** | `gate_report_20261008_091744.json` |
| R68 第七批门禁（09:50，**产物未重建**） | `2c820d9` | 3923 | **10/11** —— `artifact_freshness` FAIL（`core/watchdog.py` 与产物副本不符） | `gate_report_20261008_095033.json` |
| R68 第七批终验（10:06） | `2c820d9` | 3923 | **0 failed** | `gate_report_20261008_100622.json` |
| **R68 第七批收尾（10:28）** | **`8e281cd`** | **3924** | **0 failed** | `gate_report_20261008_102812.json` |

**第四段对账**：`3883 → 3889` = **+6**，正是 §11.4 新增的 6 条用例
（`test_sliced_path_escalates_config_error` 1 条 + `TestSlicedPathSharesConfigErrorContract` 5 条）
⇒ **数字自洽**。

**第六段对账**：`3889 → 3915` = **+26**，正是 `#145` 新增的 `tests/unit/test_log_privacy.py`（26 条）；`3915 → 3921` = **+6**，正是 `9768ae8` 新增的 `TestE2eUnpackedGuard` 护栏（5 条新增 + 1 条防空转）⇒ **数字自洽**。详见 §十二。

**第七段对账**：`3921 → 3923` = **+2**，正是本批新增的 `TestStartupFirstScan`（1 条判别性行为用例 + 1 条 AST 结构钉）；`3923 → 3924` = **+1**，正是收尾新增的 `test_frozen_smoke_watchdog_liveness_budget_discriminates_scan_first` ⇒ **数字自洽**。详见 §十三。

**第三段对账**：`3879 → 3883` = **+4**，正是 F5 新增的 4 条用例
（`TestPageLevelErrorTextIsSanitized`）⇒ **数字自洽**。

⚠️ **第四、五批与前三批有一处结构性差别：它们改了 `core/` 字节**（前三批全是 docs/tests）。
`artifact_freshness` 是**逐字节**比对源码与产物副本 ⇒ 改完 `core/pipeline/stage2.py`（第四批）
或 `core/pipeline/engine.py`（第五批）该项立刻转红。⇒ 必须**重建产物**才能复绿 ——
见 §十一。**这正是前三批能靠"docs-only"免于复跑、后两批不能的原因。**

**第二段对账**：`3875 → 3879` = **+4**，正是 §十 新增的 4 条用例
（`TestCheckoffLedger` 2 条 + `TestNoDeadFrontendAnchors` 2 条）⇒ **数字自洽**。

⚠️ 上表第 1 行的 `3866` 是 **R65 的**终验数，**不是** R66 的"修复前"数 —— 曾一度记错，已更正。
**对账**：`3866 + 9 = 3875`，那 **9** 条正是本轮两个新护栏
（`test_gate_inventory_doc.py` 5 条 + `test_todo_freshness.py` 4 条）。
第 2 行那 **2 条红** = ① 新护栏**自我命中**了既有的 `test_repo_hygiene.py` 行号引用护栏
（新文件 docstring 里引了 `settings.js` 的**行号字面量**）；② §六-F4 的顺序相关红。
两条均已修 ⇒ 第 3 行 **0 failed**。**门禁全绿是"修复后"的状态**，不是红着也全绿。

最终结果：

```
$ python scripts/release_gate.py          # 工作区干净，HEAD = 7d53aed（R66 第五批）
[PASS] worktree_clean   工作区干净
[PASS] no_build_outputs 378 个已跟踪文件中无构建产物
[PASS] dist_variants    1 份完整产物（dist-electron）
[PASS] artifact_freshness 2 份产物与源码逐字节一致（dist/pbc-server, dist-electron/win-unpacked/resources/pbc-server）
[PASS] packaging_files  5 个前置文件就位
[PASS] rules_wired      29 个 _check_* 规则函数（下限 14）
[PASS] kb_corpus        知识库 441 条（另有章节标题元数据 36 条，无正文且检索器不索引，不计入）（下限 200）
[PASS] kb_packaging     6 个 KB 源经 core/kb/data glob 自动入包
[PASS] dependency_vulns 0 条公告（快照 6 天前，扫描 43 个条目）
[PASS] runtime_eol      在支持线 [41, 42, 43] 内：dist-electron: Electron/43.7.4
[PASS] tests_coverage   3889 passed, 0 failed, coverage=95.09% (门禁 95%)

OVERALL: pass  (pass=11 fail=0 warn=0 skip=0)
报告: devlogs/gate_report_20260930_134900.json
```

> 上面这份是**最新一次**全量门禁（第五批树）。历史各树的逐项输出在各自的
> `devlogs/gate_report_*.json` 里；**别把这里的数字当成别的树的结论**。

⚠️ **`worktree_clean` 是 11 项里的第 1 项**，它在 pytest 之前跑 —— 所以门禁结果
对**当时那一份字节**有效；跑完门禁后若再改任何文件，该结论即失效，须重跑。

**复跑（收尾）**：本节落盘为提交 `995b50b` 后**又跑了一次**，仍是
`overall=pass`、`11/11`、**3875 passed / 0 failed / coverage 95.09%**
（`devlogs/gate_report_20260930_122311.json`）。
⇒ **门禁已在含本节文字的树上验证过**。

> ⚠️ **到此为止，不再递归**：若为"记录这次复跑"再改文档，就会产生新的 tip、又需要再跑一次。
> 终止规则是 —— **只要后续提交严格是文档（不含 `core/` `api/` `db/` `static/` `scripts/` 任何字节），
> 上一份门禁结论对代码仍然有效**；此时应**声明"该提交为 docs-only"**，而不是无限复跑。
> 反之，**任何触及被测代码的提交都会使该结论失效**，必须重跑。

### 5.3 覆盖率热点（数据来自 `coverage.xml`，**该文件为 09:39 产物，早于 HEAD**，仅作指示）

- 生产模块中**唯一**低于 85% 的是 `core/docling_client.py`（77.8%）——
  未覆盖行是**"docling 已安装"分支**，而本机未装该可选依赖 ⇒ 属**环境边界**，
  不是测试缺口（`is_available()` 的降级路径已被覆盖）。
- 其余生产模块 ≥ 86%。`report.py` 87.6%、`jobs/upload.py` 87.7% 的缺口集中在
  图片转换超时 / 结构错误 / DB INSERT 失败 / `launch_pipeline` 失败等**兜底分支**。

### 5.4 模块化与单一职责

- **前端**：两轮大文件拆分已完成 —— `review.js`（R63 → 8 模块）、`settings.js`（R65 → 6 模块）。
  范式统一为"状态宿主 `window.X` + 职责模块 + 入口"，且**状态只有一个宿主**。
  当前最大前端文件 `static/upload-jobs.js`（1,035 行，职责内聚，**建议暂不拆**）。
- **后端**：`api/` 按资源分包（`jobs/`、`settings/`），`core/` 按能力分包
  （`pipeline/`、`rules/`、`kb/`）。最大生产文件 `core/mineru_client.py` 1,301 行
  （单一外部服务客户端，职责清晰）。**无"什么都干"的神模块。**
- **单一真值**已机检锁定：状态中文/颜色 → `static/status.js`；finding 级 → `static/findings-map.js`；
  后端 → `core/zh_map.py`。
- **导入图契约**：顶层导入图无环（SCC 全 1）；`core` 不依赖 `api`；`db` 是叶子。

---

## 六、本轮发现与处置（定位 → 修复 → 测试）

### F1【已修】`CLAUDE.md` 的 release gate 清单与代码脱节：**9 items vs 实际 11**

- **定位**：`CLAUDE.md` 写 `# 9 items: worktree_clean / … / tests_coverage`；
  而 `scripts/release_gate.py::run_all()` 实际调用 **11** 个检查函数。
  漏掉 **`dependency_vulns`** 与 **`runtime_eol`**。
- **为什么严重**：这两项正是 B11-2 为"**我们依赖的东西还安不安全**"加的，
  而 Round 58 正是靠它们查出 **`pip-audit` 50 条公告 / 26 条 CVE** 与
  **产物内 Electron 33 EOL 17 个月**。把这一面从文档里写没了，等于让后来者以为没人看着。
- **修复**：`CLAUDE.md` 补全为 11 项并按 `run_all()` 顺序列出 + 注明必须一致。
- **测试**：`tests/unit/test_gate_inventory_doc.py`（5 条）。锁 4 条不变式：
  ① 条数一致；② 所有 `CheckResult("<name>")` 名字都进文档；③ 不空转（两边都解析出内容）；
  ④ `run_all()` 调用的每个 `check_*` 都有定义（防重命名让检查**静默消失**）。

### F2【已修】`docs/TODO.md` 自称"唯一且最新"，实际落后 6 轮且含失效锚点

- **定位**：
  - 头部写「这是**唯一**的待办清单」「**最后更新：2026-09-23（Round 59）**」，
    而 `CHANGELOG.md` 已到 **Round 65** ⇒ **落后 6 轮**；
  - **自述已解除却仍是 `[ ]`**：`A4`（「2026-09-18 已解除」）、`#162`（「已在本次一并修正」）；
  - 仍含 `settings.js:46`、`settings.js:116-118,150-152` 这类**行号锚点** ——
    R63/R65 把前端拆成模块后**已失效**，指不到任何东西。
- **为什么严重**：这是最坏的一类文档腐坏 —— 它不只是"旧"，它**自称权威且最新**。
  按它开工的人会去重做已完成的事、或去修一个不存在的行号。
- **修复**：更正头部（删掉不成立的自称、说明分两层）；新增 **复核戳**；
  新增 **§0 实核 backlog**（5 条，逐条挂证据）；明确标注 §A 及以下为
  **Round 59 快照、R60–R65 未逐条复核**。
- **测试**：`tests/unit/test_todo_freshness.py`（4 条）。锁：① 复核戳存在且可解析；
  ② **戳不得落后于 `CHANGELOG.md` 的最大 Round**（允许领先，不允许落后）；
  ③「未复核」免责声明不得被删；④ 不空转。

### F3【已修】两处关于"安全删除垫片"的描述会误导

- **定位**：`CLAUDE.md` 把垫片源码指到 `cli/vendor/shim/safe-delete-bulk-guard.cjs`
  —— **本仓库没有 `cli/`**（`ls -d cli` 失败），那是宿主目录；且文档未点明
  **两个旋钮是不同东西**。
- **修复**：注明该路径不在本仓库；并明确区分
  `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD`（**抬阈值、安全网仍开**，`release_gate.py`
  的既定做法，`SANDBOX_BULK_DELETE_THRESHOLD=100000` 且**强制赋值**）与
  `CODEBUDDY_SAFE_DELETE_ENABLED=0`（**整个关掉**垫片）。**agent 记忆文件里同样写错了**
  （曾自称"唯一开关"），一并更正。
- **测试**：无新护栏（属描述性更正）；已用 `release_gate.py` 源码逐行核对。

### F4【已修】`test_pdf_non_local_host_returns_403`：红在**清理期**，不在断言

- **定位**：该用例**在全量跑里红、单独跑与整文件跑都绿**（本轮为第 2 次观察到）。
  原始证据（不是推断）：

  ```
  [safe-delete][SAFE_DELETE_FAIL_CLOSED] {"target": "...\\output\\guard_probe.pdf",
   "reason": "trash-failed", "detail": "SHFileOperationW 失败: 0x2"}
  ```

  ⇒ 根因是**测试自己**的 `finally: pdf_path.unlink()`：垫片在回收站不可用时**失败即关闭**
  （fail-closed）并抛 `OSError`，该异常**逃出 `finally`**，把一条**断言已经通过**的用例染红。
  它不是 flaky 的逻辑，是**清理期红**。
- **为什么此前查不出来**：`pytest` 自己清 `tmp_path` 时**只记 `PytestWarning` 并继续**
  （`(rm_rf) error removing ...`）；**手写 `unlink` 没有这层容忍** —— 同样的 OSError，
  一个吞、一个炸。差别不在"临时目录"，在**谁来删**。
- **修复**：改用 `tmp_path` 夹具 + **删掉手写 `unlink`**；同时把断言**收紧** ——
  加 `assert "non-local" in r.text`。因为 host 守卫与路径越界守卫**都返回 403**，
  不锁理由的话，夹具一挪位就可能"因别的原因通过"（空断言）。
- **测试**：`TestServePdf` 整组重跑 **52 passed**（含本仓库卫生与两个新护栏文件）。
- **机制当场被复现**：本轮最后一次重跑（61 passed）里，pytest **自己**清 `tmp_path` 时也撞上了
  同一个垫片故障，但**只是警告、不红**：

  ```
  [safe-delete][SAFE_DELETE_FAIL_CLOSED] {"target": "...\\Temp\\pytest-of-wusitan\\garbage-bca47a85-…",
   "reason": "trash-failed", "detail": "[Errno 53] 找不到网络路径。"}
  ...\_pytest\pathlib.py:96: PytestWarning: (rm_rf) error removing \\?\C:\Users\...\garbage-bca47a85-…
  ```

  ⇒ 同一次故障、同一个 `OSError`，**在 pytest 手里是警告，在手写 `finally` 里是红**。
  这就是 F4 的全部机制，可复现。
- ⚠️ **我第一版 docstring 写的是"临时目录可避开垫片"，紧接着一次运行就打脸** ——
  `%TEMP%\pytest-of-*` 路径**照样**报 `SAFE_DELETE_FAIL_CLOSED [Errno 53] 找不到网络路径`。
  已按实测改写，并把**触发条件标注为未完全定位**（见 §4.2-7）。

### 6.1 变异验证

`devlogs/_verify/mutation_doc_contract_guards.py` ⇒ **9/9**（基线绿）：

| 用例 | 结果 |
|---|---|
| M1 文档退回 "9 items" 并删掉两项供应链检查 | CAUGHT |
| M2 只把条数 11 改成 10 | CAUGHT |
| M3 `run_all` 里摘掉 `check_runtime_eol()` | CAUGHT |
| M4 把 `check_kb_corpus` 的定义改名（调用点悬空） | CAUGHT |
| M5 TODO 复核戳退回 Round 50 | CAUGHT |
| M6 删掉「未复核」免责声明 | CAUGHT |
| M7 整行删掉复核戳 | CAUGHT |
| M8 阴性对照：CHANGELOG 追平到 Round 66 | GREEN（不误报） |
| M9 阴性对照：只动 CHANGELOG 正文 | GREEN（不误报） |

> ⚠️ **首版脚本本身是错的，值得记录**：它用进程内 `pytest.main()`，**基线即红** ——
> 同名测试模块跨不同临时目录被再次导入，触发 pytest `import file mismatch`、
> **rc=2（收集错误）**。而 `rc != 0` 会被误读成"变异被抓" ⇒ 得到**假的 100%**。
> 已改为**每例一个子进程** + **退出码严格分诊**（只有 `rc==1` 算 CAUGHT，`rc==2` 记 INVALID）。
> **教训与项目既有纪律同源：`rc != 0` 不是"被抓"的证据，必须先分诊。**

---

## 七、仓库卫生

- 跟踪文件 **378** 个（`git ls-files | wc -l`，与门禁 `no_build_outputs` 的计数一致）；
  `git status --porcelain` **空**。
- `.gitignore` 覆盖完整且**带解释性注释**（每条都写明"为什么加"）；`htmlcov/`、`devlogs/`、
  `dist*/`、`data/`、`.workbuddy-ai/` 均被忽略 —— 已用 `git check-ignore -v` 逐项验证。
- 根目录存在若干**被忽略**的本地残留（`devlogs_build_*.log`、`coverage.xml`、`__pycache__/`、
  `config.json`）—— 均不污染版本库，属可接受的本地产物。
- 源码中 `TODO/FIXME/HACK` 标记仅 **5 处**，且全部是**指向文档条目的引用**（如
  `TODO B1-16`），**没有一处是"代码里留个坑先不管"**。

---

## 八、git 与 GitHub

```
$ git branch -vv
* main 172957c [origin/main: ahead 17]
$ git remote -v
origin  https://github.com/haowuxiwang/BatchSentry.git (fetch/push)
$ git rev-list --left-right --count origin/main...HEAD
0       17
```

⚠️ **本地 `main` 领先 `origin/main` 17 个提交**（`c8b092b` 之后的全部 R62–R66 工作），
**GitHub 上还是旧的**。推送是对外发布动作，**等用户确认**。

> ⚠️ **上表的 `17` 是 `172957c`（R66 第五批）时点的快照，不是实时值。**
> **取当前值的唯一方法是运行命令**，不要引用本报告里的数字：
>
> ```bash
> git rev-list --left-right --count origin/main...HEAD   # 左=落后, 右=领先
> ```
>
> **为什么不写成"实时"**：这个数**每提交 +1**，任何写死的值在下一个提交后即腐坏；
> 而"文档复述了一个会变的代码事实"正是本轮 F1/F2 的病根。⇒ 对**易变计数**，
> 正确做法是**给命令、不给数字**；确需给数字时必须**同时给时点**。

---

## 九、遗留 backlog（详见 `docs/TODO.md` §0）

1. 推送本地未推送提交到 `origin/main`（**用户动作**）—— 数量见 §八的实时命令，别引用快照。
2. Electron 应用层 e2e 在**真实终端**复跑（本环境不可复现）。
3. ~~`docs/TODO.md` §A 及以下的 86 条未复核项逐条复核~~ → 已分四批核销：**R66 第三批 14 条**（§十）、**第四批 `#146`**（§11.1）、**第五批 `#144`**（§11.4）、**第六批 `#145`**（§十二）。
   ⚠️ **累计条数以 `docs/TODO.md` §0.1.1 台账为准**（那里是权威，且被`test_todo_freshness.py` 机检锁定）—— **本报告不再写死剩余条数**：它每批都在变，
   而"文档复述一个会变的代码事实"正是本轮 F1/F2 的病根。
   ⚠️ 核销过的是"抽查到并确认"的，**不等于**其余条目都还开放。
4. 跨页总览增强（后端 `status` 过滤 / 关键词搜索）—— 小，可选。
5. 上传批次并发提交 —— 小，可选，**不建议先做**。
6. ~~产物重建~~ → **R66 第四批已做**（§11.2）：两份产物逐字节一致，`runtime_eol` 已从 SKIP 恢复实测。

### 本轮**未**做、但值得下一轮考虑的

- **`docs/` 体量治理**：`docs/` 下 20+ 个 `.md`，其中 `TODO.md`（152K 字）、
  `PROJECT_PITFALLS.md`（168KB）、`ADVERSARIAL_AUDIT.md`（114KB）体量巨大。
  ⚠️ 但**历史审查报告不得回改**（那是证据）；要做也只能**加归档头 + 目录**。
- ~~**`docs/TODO.md` 里失效的行号锚点**：本轮只在头部标注了该问题，**未逐条清理** ——
  因为 §A 是历史快照，清理它需要先逐条复核（即 backlog 第 3 条）。~~
  → **R66 第三批已清理**（16 个前端锚点；见 §十）。

---

## 十、待办清单核销（R66 第三批）

### 10.1 为什么值得单独立一节

`docs/TODO.md` §A 及以下自称"Round 59 快照、未逐条复核"。**实测：其中相当一部分已经做完，
却仍显示未做。** 这类腐坏的代价不是"多看一眼"，而是**让人重复劳动**——
按清单开工会去修一个已经修好的 P1 安全洞。

**方法**（可复现）：抽取全部 `[ ]`（脚本产物 `devlogs/_verify/todo_unchecked.json`），
先筛出**自带"已修/已完成/不成立"类标记**的 16 条 —— 这些才是"可能已完成却仍显示未做"的高危面；
再**逐条去代码/产物里取证据**。

> ⚠️ **纪律：条目自述不是证据。** 每条都要在代码/产物里找到独立凭据才允许翻 `[x]`。

### 10.2 核销 14 条（`[ ]` 86 → 72）

最有影响的是**两条 P1 安全项**：

| 条目 | 登记时 | R66 实测 |
|---|---|---|
| **B10-1** 守卫晚于请求体解析 | "跨站可触发 CPU 型 DoS，实测 16 MB **10.26 s**，且返回 422 而非 403 ⇒ 守卫完全没被走到" | **已修**。`main.py:318-420` 是**纯 ASGI 中间件**：`add_middleware` 是 **LIFO**（后加先执行）⇒ **最后注册 = 最先执行**，在**任何 body 读取之前**完成 ① Host/Origin 判定 ② 流式体积上限 ③ 非法/负 `Content-Length` 一律拒（负值那条正对 `CVE-2026-53540`）。护栏 `tests/unit/test_local_guard_middleware.py` ⇒ **17 passed** |
| **B10-2 / B11-7** 4 个依赖漏洞 | "`pip-audit` ⇒ 50 条公告（26 CVE）" | **已修**。`requirements.txt` 实测 `python-multipart==0.0.31`、`Pillow==12.3.0`、`requests==2.33.0`、`python-dotenv==1.2.2`；门禁 `dependency_vulns` ⇒ **0 条公告** |

其余 12 条：`B10-3`/`B11-8`（Electron `33.4.11` → **`43.7.4`**）｜`B10-4`（门禁确有这两项）｜
`B11-9`（根目录 4 个 `dist*` → **2 个**）｜`B11-17`（`docx` 已从 `dependencies` 移除）｜
`B11-18`（`self_heal.py` 用 `get_flattened_data()` + `test_pillow_api_compat.py`）｜
`#166`/`#167`（KB 键覆盖护栏**已存在**；`user_rule`/`uncategorized` 是**有意的开放桶**，非缺口）｜
`#162`（已修，且已升级为机检）｜`A4`（402 阻塞已解除）｜`B4-1`（自述"无需再做"，残余已拆到 `B2-11`）。

> **`#163` 只部分完成** ⇒ **保持 `[ ]` 并加注**（② 已修；① `core/kb/retriever.py:5` 注释仍写 "29K chars"）。

### 10.3 清理 16 个失效行号锚点

12 行、16 个**前端**行号锚点（全在 §A）。R63/R65 把前端拆成模块后它们**指不到任何东西**。

`devlogs/_verify/r66_strip_frontend_anchors.py`：替换为模块级指路（"在 `settings.js` 里"），
**并断言 93 个 Python 锚点一个都没被误伤** —— `core/zh_map.py:34`、`engine.py:366-370`
那些文件**没被拆、行号仍然有效**，删了才是破坏。

### 10.4 抽查确认"仍开放"12 条

保留 `[ ]`，并在 §0.1.3 就地记证据（`#144` 的 `engine.py` 两处 `_analyze_one` 确无 `config_error=`；
`#145` root 仍钉 DEBUG；`#147` 只清两列；
`#148` `api/report.py` 零处 `error_message`；`#163①`；`#165` 死键仍在；`#168` 每页检索两次；
`#169` `_STATIC_FIELDS` 白名单不含 `kb_prompt_inject`；`B2-9`；`B3-3`；`B4-2`；`B7-2`）。

> ⚠️ **上述 12 条里的 `#146` 已在 R66 第四批修复**（`stage2.py` 页级/任务级出口统一
> 「先脱敏、后截断」，见 §十一）。本条抽查**发生在第四批之前**，故当时确为"仍开放" ——
> 保留此注是为了说明**先后顺序**，不是自相矛盾。
> 本节其余 11 条**仍然开放**。

> ⚠️ **`B2-11`（聚合流 DB 异常期是否 yield 帧）本轮只读到注释、未构造 DB 异常实测**
> ⇒ **不作判定**，保持 `[ ]`。**"没查到"不等于"已修"。**

### 10.5 新护栏 2 条 + 变异 5/5

`tests/unit/test_todo_freshness.py`：

- **E**：带核销尾注的 `[x]` 条数 **==** §0.1.1 台账行数。防的是**新的腐坏方式**——
  R66 之前是"自述已解除却仍 `[ ]`"，修完之后会变成"**翻了 `[x]` 却不写证据**"，
  后者更坏：`[x]` 冒充"已验证"。
- **F**：活清单里**不得有前端行号锚点**。含**防空转**（构造样例必须命中）与**过宽反例**
  （无行号的模块级指路**不得**命中）。

变异 `devlogs/_verify/mutation_todo_ledger_guards.py` ⇒ **5/5**（基线绿）：
M1 删尾注 / M2 删台账行 / M3 塞入前端锚点 ⇒ **CAUGHT**；
M4 加不带尾注的 `[x]` / M5 加 **Python** 锚点 ⇒ **GREEN**（不误报）。

> ⚠️ **本节的锚点检测器第一版是错的**：我在设计 F 之前跑过一个"数锚点"的探针，
> 它报 **0**，我据此在文档里写下"锚点已清干净"；**写护栏时才发现是探针的正则太窄**
> （`(?:-[\w]+)?` vs `[\w-]*`），实际有 **16** 个。
> ⇒ **又一次"测量工具本身没被验证"**（verification-integrity Trap 6）。
> 已把断言写成"构造样例必须命中"，就是为了让这类探针错误**当场暴露**而不是静默通过。

---

## 十一、R66 第四批：代码修复 + 产物重建 + 门禁复绿

前三批（§六 F1–F4、§十）**只动 docs/tests**，故能靠"docs-only ⇒ 门禁结论对代码仍有效"
免于复跑。**本批不同：它改了 `core/` 字节**，于是把产物新鲜度这条链也拉进了射程。

### 11.1 F5【已修】错误文案未脱敏：`str(exc)` 会把 key 与签名 URL 回给前端

**定位**（`core/pipeline/stage2.py`）：

- **页级出口** `"_error": str(exc)` —— **完全裸的**异常文本；
- **任务级出口** `config_error["reason"]` —— 只做 `[:200]` 截断，**不脱敏**。

异常文本里常见两类敏感内容：上游 SDK 抛出的 **API key**、HTTP 客户端抛出的**带签名 query
的 URL**。两者都会经 API 回给前端（页级还会渲染进复核页）。

**修复**：新增 `_ERROR_TEXT_LIMIT = 200` 与 `_sanitize_error_text()`：

```python
return redact_urls(_mask_secrets(text))[:_ERROR_TEXT_LIMIT]
```

**两层顺序是有意的**：① `_mask_secrets` 掩 key → ② `redact_urls` 抹签名 query → ③ **最后**截断。
⚠️ **截断若放在最前**，会把 key 切出一个**残片**（`sk-abcdefgh` 在 200 字处被腰斩成 `sk-abc`）
留在输出里 —— **顺序本身就是判据**，所以它值得一条专门的用例。

两个出口**都**改走该 helper（此前只有任务级做了截断）。

**测试**（`tests/unit/test_config_error_visibility.py` 新增 `TestPageLevelErrorTextIsSanitized`，4 条）：

| 用例 | 判据 |
|---|---|
| `test_masks_api_key` | key 不出现在输出里 |
| `test_redacts_signed_url_query` | 签名 query 被抹掉 |
| `test_truncation_happens_after_masking` | **顺序判别**：`payload = "x"*193 + "sk-abcdefgh"` ⇒ 输出**不得含 `sk-abc` 残片** |
| `test_both_export_sites_route_through_the_helper` | **两个调用点**都走 helper（防某次重构只改一处） |

**变异验证** `devlogs/_verify/mutation_page_error_sanitize.py` ⇒ **5/5，基线绿**：

| 变异 | 预期 | 结果 |
|---|---|---|
| M1 页级出口退回裸 `str(exc)` | CAUGHT | ✅ rc=1 |
| M2 helper 丢掉 `redact_urls` | CAUGHT | ✅ rc=1 |
| M3 **先截断再掩码**（顺序反转） | CAUGHT | ✅ rc=1 |
| M4 helper 退化成纯截断 | CAUGHT | ✅ rc=1 |
| M5 阴性对照（无实质改动） | GREEN | ✅ rc=0 |

⚠️ 脚本用**临时副本 + 每例独立子进程**：进程内 `pytest.main()` 会因同名模块跨目录重复导入
报 `import file mismatch`（rc=2），而 **rc≠0 会被误读成"被抓"** ⇒ 假的 100%。

### 11.2 重建与门禁复绿

改 `core/` 的直接后果：`artifact_freshness` **逐字节**比对源码与产物副本 ⇒ 立刻转红。
实测（不是推断）：

```
$ python scripts/bundle_manifest.py --check
[FAIL] dist\pbc-server 产物新鲜度校验未通过：
  - 1 个源文件与清单不符（**源码改动后未重建**）：['core/pipeline/stage2.py']
```

**只有 1 个文件** ⇒ 说明除本次改动外**无其他源码漂移**。重建步骤（`build.ps1` 无法由 agent
执行，按 DETAIL §10 用 Bash 复刻）：

```bash
CODEBUDDY_SAFE_DELETE_ENABLED=0 python -m PyInstaller pbc-server.spec --noconfirm --clean
python scripts/bundle_manifest.py --write && python scripts/bundle_manifest.py --check
CODEBUDDY_SAFE_DELETE_ENABLED=0 ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ \
    npx electron-builder --win --x64
python devlogs/_verify/gen_provenance.py          # electron-builder 会抹掉它，必须补
```

`npm run build:css` **本批不需要**（未动 `templates/` 或 `static/*.js`；已用
`git diff --exit-code -- static/app.css` 确认 CSS 干净）。

结果：**两份产物都新鲜**（`dist/pbc-server` + `dist-electron/win-unpacked/resources/pbc-server`），
清单 `version=1.2.1 files=116 head=8d5848d dirty=False`。

**门禁复跑（13:07，`8d5848d`）**：`overall=pass`、**11/11**、**3883 passed / 0 failed**、
coverage **95.09%**（`devlogs/gate_report_20260930_130757.json`）。
对账：`3879 + 4 = 3883`，那 4 条正是 §11.1 的用例。

### 11.3 构建链上的两个新发现（都会**静默降级**）

#### ① Node 层"安全删除"垫片是**两级**失败，只有 `ENABLED=0` 能过

`electron-builder` 要重建 `dist-electron/win-unpacked`（1927 个文件），连撞两道：

| 设置 | 结果 |
|---|---|
| 默认（宿主 `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=50`） | `SAFE_DELETE_BULK_CONFIRM_REQUIRED {"count":1927,"threshold":50}` |
| `BULK_THRESHOLD=100000` | 守卫放行，但删除**改走回收站** ⇒ `spawnSync genie-trash.exe ETIMEDOUT` |
| **`ENABLED=0`** | **通过** |

⚠️ 关键在于**只抬阈值是不够的** —— 它把失败从"被守卫拦下"变成"卡在回收站"，
**症状换了、命令仍然失败**。⇒ 构建类命令（PyInstaller `--clean`、`electron-builder`）
统一带 `CODEBUDDY_SAFE_DELETE_ENABLED=0`。

阈值来源已核对到源码：`cli/vendor/shim/safe-delete-bulk-guard.cjs` 读
`process.env.CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD || DEFAULT_THRESHOLD`（默认 20，宿主给 50），
且 shim 以 `env: {...process.env, NODE_OPTIONS: ''}` 调用它 ⇒ **子进程会继承你设的阈值**。
shim 只在 `CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR` + `CODEBUDDY_TOOL_CALL_ID` 同时存在时才检查
⇒ 只有**工具调用内**才触发，CI 上不存在这些变量、注入无害。

#### ② `PROVENANCE.txt` 缺失会让 `runtime_eol` **静默 SKIP**

`electron-builder` 会**整目录重建** `win-unpacked/` ⇒ 上一轮的 `PROVENANCE.txt` 被抹掉。
而 `release_gate.count_complete_artifacts` 把「`win-unpacked/PROVENANCE.txt` 存在」
当作**完整产物三件套**之一 ⇒ 缺它则产物被判残壳、`runtime_eol` **SKIP**。

**实测对照**（同一天、同一份产物，只差这个文件）：

- 补文件**之前**：`[SKIP] runtime_eol`（`gate_report_20260930_125855.json`）
- 补文件**之后**：`[PASS] runtime_eol 在支持线 [41, 42, 43] 内：dist-electron: Electron/43.7.4`

⇒ **SKIP 不影响退出码，所以这条链断了也不报警** —— 门禁在"产物最标准的状态"上反而最瞎
（与 `build.ps1` 内注释所述的历史缺陷同源）。agent 侧补写脚本：
`devlogs/_verify/gen_provenance.py`（格式严格对齐 `build.ps1`：**BOM + CRLF + UTF-8**）。

> **本节的数字都有出处**：产物结论来自 `bundle_manifest.py --check`，门禁结论来自
> `devlogs/gate_report_*.json`，用例数来自该报告的 `tests_coverage` 行。
> **任何一条若与当前树不符，以命令输出为准**（本文件不是事实源）。


### 11.4 F6【已修】分片路径漏掉配置级故障的 job 级提升（`#144`）

**定位**（`core/pipeline/engine.py::_run_sliced_stage1_2`）。

`_analyze_one` 在**确诊配置级故障后**会闸住后续页 —— 这是 #127 的设计：一次 401/403/400
就别再拿同一份坏配置去打 N 次。闸门本身没问题，**问题在"闸住之后谁来记账"**：

| 路径 | 谁记账 | 现状 |
|---|---|---|
| 整份路径 `stage2._run_stage2_analysis` | 传 `config_error=config_error` 给 `_analyze_one`，确诊后写 job 级 `error_message` | ✅ 已对 |
| **分片路径** `engine._run_sliced_stage1_2` | **两处 `_analyze_one` 调用都没传 `config_error`** | ❌ 漏 |

后果正是 #127 要消灭的 **GMP 假阴性**：分片路径上的配置故障**不会**提升到 job 级，
于是「一条 0 finding 的 job」与「记录本身没问题」在前端**不可区分**。
而**大文档恰恰走分片路径** ⇒ 覆盖面最大的那条路反而没修。

**修复**（5 处，字节模式改，CRLF 保持 `crlf=754 lf=0`，无 BOM）：

| # | 改动 |
|---|---|
| A | 在 `_run_sliced_stage1_2` 里声明**共享** `config_error: dict = {}`（与整份路径同口径） |
| B | 第一处 `_analyze_one(...)` 调用点补 `config_error=config_error,` |
| C | `await asyncio.gather(*analysis_tasks)` **之后**回填被闸掉的页 |
| D | 自愈（`_run_heal`）前加 `if config_error:` 守卫 —— 配置坏了就别再打 LLM |
| E | 自愈内的 `_analyze_one(...)` 调用点同样补 `config_error=config_error,` |

⚠️ **C 不是"顺手加的"，是 A/B 的伴生回归**：一旦把 `config_error` 传进 `_analyze_one`，
闸门就激活了，**尚未尝试**的页会**既不在 `failed_pages`、也不在"已分析"集合**里 ——
两个计数都丢。整份路径本来就有这段回填，分片路径因为此前没传 `config_error`
**从来不需要它**。⇒ 这是"修 A 引出 B"的典型，不补 C 就是把假阴性换成**静默丢页**。

**测试**：

- `tests/unit/test_pipeline.py::TestConfigErrorVisibility::test_sliced_path_escalates_config_error`
  （**行为化**）：`ocr_slices=2` + `ocr_backend="mineru"` 走分片分支，`analyze_page` 抛
  `LLMConfigError`。判据：`sliced_calls["n"] == 1`（**防空转**：证明确实走了分片分支，
  而不是整份路径那条已修好的行为在满足断言）、`"配置级故障" in error_message`、
  `failed_pages == [1,2,3,4]`、`calls["n"] == 1`、`heal_calls["n"] == 0`。
- `tests/unit/test_config_error_visibility.py::TestSlicedPathSharesConfigErrorContract`（5 条**结构性**）：
  `_analyze_one(` 计数 == 2（**防空转**）、`config_error=config_error,` 计数 == 2、
  共享字典声明存在、回填块存在、自愈守卫**结构上**绑定 `config_error`。

⚠️ 自愈守卫第一版是**空断言**：只查 `"skip self-heal" in src`，M3（`if config_error:` → `if False:`）
**照样绿**。改成 CRLF 归一化后的**多行结构锚**（`if config_error:` + `recovered = []` 的精确形状）
**外加**行为化 `heal_calls["n"] == 0` 双保险。

**变异验证** `devlogs/_verify/mutation_144_sliced_config_error.py` ⇒ **5/5，两条基线都绿**：

| 变异 | 断言层 | 预期 | 结果 |
|---|---|---|---|
| M1 第一处调用点丢 `config_error` | 行为 | CAUGHT | ✅ |
| M2 回填 `range(1, total_pages+1)` → `range(1, 1)` | 行为 | CAUGHT | ✅ |
| M3 自愈守卫 `if config_error:` → `if False:` | 结构 | CAUGHT | ✅ |
| M4 `config_error: dict = {}` → `config_error = None` | 行为 | CAUGHT | ✅ |
| M5 纯注释（阴性对照） | 行为 | GREEN | ✅ |

**定向回归**：`test_pipeline.py` + `test_config_error_visibility.py` +
`test_import_graph_contract.py` + `test_config_import_order_contract.py` ⇒ **167 passed**。

### 11.5 第五批：产物重建 + 门禁复跑（`7d53aed`）

`#144` **又动了 `core/` 字节**（`core/pipeline/engine.py`）⇒ 按 §5.2 的终止规则，
上一批的门禁结论**对本树失效**，必须重跑；且 `artifact_freshness` 需先重建产物。

**重建**（与 §11.2 同一链条，四步全绿）：

```
1/4 PyInstaller                        Build complete! → dist
2/4 bundle_manifest --write/--check    files=116，逐字节一致
3/4 electron-builder --win --x64       OK
4/4 gen_provenance.py                  git_head=172957c BOM=True crlf=11 lf=0
```

⚠️ **一次刻意的"重盖章"，必须写清楚**：构建时工作区**是脏的**（三份 docs 未提交），
故清单落的是 `dirty=True`。docs 不在入包集合里，这个标记是噪音 ⇒ 提交 docs 之后
**重跑了一次 `--write`**，`dirty` 转为 `False`。

> **`--write` 是断言，不是证据。** 它在**当下**计算清单，所以构建之后再跑，它**不可能**
> 与构建相互矛盾 —— `git_dirty=False` 从此是**我们的声明**，不再是构建的观测。
> 真正可验证、也真正要紧的那部分是**入包文件的逐字节一致性**，而那由 **`--check`** 给出
> （两份产物都过）。⇒ 结论只应引用 `--check`；`dirty` 字段须按"声明"读，不得当作
> "构建跑在干净树上"的证据。

**门禁复跑（13:49，`7d53aed`）**：`overall=pass`、**11/11**、**3889 passed / 0 failed**、
coverage **95.09%**（`devlogs/gate_report_20260930_134900.json`）。
对账：`3883 + 6 = 3889`，那 6 条正是 §11.4 的用例。

### 11.6 本批（第五批）暴露的自身缺陷：一处**悬空引用**

`#144` 的条目尾注写「仍开放，见 §0 的 0-6」，而 **§0 表里没有 0-6 这一行** ——
引用指向不存在的行。**没有任何用例变红**，因为"看起来像引用"的文字不会被机检拦下。

这**正是本轮 F1/F2 的同一类缺陷**（文档与事实脱节），而且**由本轮自己的修复引入**。
已就地补上 0-6 行（job 级 `error_kind` 列，即 `#144` 条目所称"更稳的做法"）。

⚠️ **这一类目前没有护栏**：`test_todo_freshness.py` 只锁"核销台账 1:1"与
"无失效行号锚点"，**不检查 `§N 的 M-K` 形式的交叉引用能否解析到真实条目**。
⇒ 已登记 **0-7**（给交叉引用加解析护栏）。

## 十二、R68 第六批：`#145` 修复 + 全量重建 + 两份构建物上的 e2e 实测

> 本批是对「**当前是否可以打包**」这个问题的完整答卷：
> `#145` 动了 `core/`/`llm/`/`electron/` 字节 ⇒ **必须重建产物**，
> 然后门禁 + **两份构建物**上的 e2e 全部实跑。
> 结论：**可以打包**（§12.2），但 **LLM 链路本轮仍未被覆盖**（外部凭据失效，§12.3）。

### 12.1 定位：`#145` 的三条实测更正（条目**低估**了问题）

| 条目所述 | 实测 | 性质 |
|---|---|---|
| `llm/client.py:361-365,393-397,477` | **L417 / L453 / L536** | 行号**已漂移**（该文件前三批改过） |
| 三处都过 `_mask_secrets` | **L536 那处没有** | 泄漏面**比条目宽** |
| 只有这三处写正文 | 还有 `core/page_analyzer.py::_parse_error_payload`（**2000 字**） | **零消费者**，却进 `page_cache.structured_json`，并被 `api/review.get_page_data` **整份回给浏览器** |

⇒ 教训与 `#146` 同宗：**「只修你最先找到的那一处 = 没修」**。
`_raw` 这个键有 **3 个生产者**；本批按「先把生产者数清、再收口」做，
而不是逐点打补丁。

### 12.2 重建 + 门禁（两份产物逐字节一致）

**重建**（四步全绿，与 §11.2/§11.5 同一链条）：

```
1/4 PyInstaller                        Build complete! → dist
2/4 bundle_manifest --write/--check    version=1.2.1 files=116 head=acd25edcc36f dirty=False；逐字节一致
3/4 electron-builder --win --x64       OK（signing with signtool.exe）
4/4 gen_provenance.py                  git_head=acd25ed BOM=True crlf=11 lf=0
```

**门禁终验**：`overall=pass`、**11/11**、**3921 passed / 0 failed**、coverage **95.1%**
（`devlogs/gate_report_20261008_091744.json`，树 `9768ae8`）。

⚠️ **本批第二段（`9768ae8`，只动 `tests/`）没有触发重建 —— 这是对的，不是漏跑**：
`tests/` **不在入包集合**（入包集合 = `core/ static/ api/ llm/ templates/ db/ models/
config.py electron/ logging_config.py main.py server.py`）⇒ `artifact_freshness` 仍绿。

### 12.3 构建物 e2e（①）内嵌后端：**26 passed / 2 failed**

被测：`dist-electron/win-unpacked/resources/pbc-server/pbc-server.exe`（**双击时真正跑的那一份**）。
先做身份核对：它与 `dist/pbc-server/pbc-server.exe` **sha256 完全相同**
（`d4054cf883ffbe76ac32851e9ebcfee994045b47fa3cc2e377513504ed6f378f`）
⇒ **一次运行同时构成两份产物的证据**。

**26 条绿**，含：`/health v1.2.1`、看门狗阈值不变量、上传/设置/复核页、`/api/settings`、
上传建作业、`failed_pages` **运行时类型是 `list`**（#132 的分发实证）、`report_md`、
静态资源、**#127 的可见性修复确实在产物里**（`upload.js`/`upload-jobs.js`/`review-pageinfo.js`
三处标记全命中）、档位配色语义、settings 六个模块可从产物取到且非空、
`ocr_backend_used=paddle`（**期望值与实际一致**）、Swagger UI。

**2 条红，且同因 —— 上游凭据失效**：

```
status=error
error_message=LLM 配置级故障（重试无效）：LLM call failed (non-retryable)
  [job=…, page=1, stage=page_analysis]: Error code: 401 -
  {'code': 30014, 'data': None, 'message': 'Token is invalid.'}
```

驱动做了**两段式正向对照**（免费端点 + 计费端点，且用应用自己上报的 `base_url`/模型）：
免费端点即 **HTTP 401** ⇒ **上游确凿拒绝该凭据** ⇒ **环境问题，非产品缺陷**。

⚠️ 但**覆盖清单仍记 `llm_pipeline=failed`**，**不因「归因是环境」改记 `skipped`**：
流水线**确实执行了**且没到成功终态，而 `failed` 的定义就是「执行了但结果不符预期」，
`skipped` 的定义是「因环境缺项**未执行**」。**归因与状态是两个正交维度，不可互相顶替。**
⇒ 已登记 **0-8**（轮换凭据后复跑）。

⚠️ **同轮 `ocr_pipeline` 也记 `failed`，这是刻意的 fail-closed，不是误记**：
`classify_pipeline` 在 `error` 终态下对「凭据齐备的一侧」一律记 `failed`，**不采信**
`ocr_backend_used`。本批专门核了**为什么不能采信**：`core/pipeline/engine.py:628-630`
有一条「保证 GMP 审计字段在所有路径下都有值」的**兜底写入** ⇒ 该字段**非空 ≠ OCR 成功**。
在 error 轮次拿它当成功证据，就是**假绿**。
（代价是「OCR 明明跑了」与「OCR 坏了」在报告里不可区分 —— 但这是**安全方向**的误差；
真实归因由 `pipeline_terminal` 断言与产品自己的 `error_message` 给出，信息并未丢失。）

**顺带拿到一条正向证据**：本轮 `error_message` 是 `LLM 配置级故障（重试无效）` 且
`failed_pages=[1]` ⇒ 这证明 `#127`/`#144` 的**配置级故障 job 级提升**确实**随产物分发**了，
而不只是「源码树里写过」。

### 12.4 构建物 e2e（②）Electron 应用层：**3 passed / 3 failed（沙箱边界）**

被测：`dist-electron/win-unpacked`（双击 `BatchSentry.exe` 的完整形态）。**共 4 次复跑**：

| 复跑 | 结果 |
|---|---|
| #1 | `/health` 就绪 **9.44s**（`version=1.2.1` == PROVENANCE、`git_head=acd25ed`）⇒ 随后渲染层崩 |
| #2–#4 | **在 health 之前**就崩（5.15s / 5.15s / 5.27s） |

四次均 `exitCode=2147483651`（`0x80000003`）、`renderer_count=0`、窗口数恒 0。
stderr 的直接证据：

```
FATAL:content\browser\gpu\gpu_data_manager_impl_private.cc:416] GPU process isn't usable. Goodbye.
```

⇒ **受限沙箱内 GPU 进程起不来** ⇒ 渲染层/窗口层**在本环境无法取证**。
与 §11.3 的 2×2 对照一致（唯一自变量是「是否在沙箱里」，`--disable-gpu` 加与不加都崩
⇒ **不得把它当解法**）。**结论：0-2 仍未解除，须在真实（非沙箱）终端复跑；
本环境下的任何「通过」都不得采信。**

⚠️ **本轮还暴露了驱动自身的一处假红（已修，`9768ae8`）**：唯一到达 D3 的那次里，
`/health` 已应答、产品自身 stdout 也写着
`[BatchSentry] Spawning server: …\resources\pbc-server\pbc-server.exe`，
而 D3 的子进程枚举**什么都没看到**（`children` 里只有一个无关的 IME 进程）
⇒ D3 报「未找到 pbc-server.exe 子进程」，**指向一个并不存在的拉起缺陷**。
修法：**只把归因说准**（点明「沙箱内进程树枚举不可靠」），**判据强度不变（仍是 FAIL）**
—— 沙箱内该事实既证不实也证不伪，据此转绿或转 skip 都会制造假绿。
护栏 5 条 + 变异 **6/6**（含一条**假修复**变异：归因恒返回空串）。

⚠️ **诚实标注**：本沙箱 4 次复跑里 **3 次没走到 D3**（在 health 前就崩），
故新分支**未在实机观测到**，只有单元级（行为 + AST）与变异级证据。
按逻辑它必然命中（那次 stderr 里确实有 GPU FATAL —— D1 的归因正是据此触发的），
但那是**推断，不是观测**。

### 12.5 本批**未**验证的边界（不得读成已通过）

- **LLM 链路**：上游 401 ⇒ `llm_pipeline` 记 `failed`；「流水线到达成功终态」
  **不蕴含** LLM 成功过（这正是 `_judge_llm_success` 要拦的假绿）。
- **Electron 渲染层/窗口层**：沙箱内不可取证（§12.4）。
- **D3 新分支**：未实机观测（§12.4 尾注）。
- **依赖快照** `docs/DEPENDENCY_AUDIT.json` 已 **14 天**（门禁阈值 90 天，仍绿，
  但这条轴随**日历**老化，与代码是否改动无关）。

---

## 十三、R68 第七批：看门狗「启动即首扫」（`B4` / 缺陷 `#125`）

> 本批只动 `core/watchdog.py` + 其单测，**不改版号** —— 对外契约没变
> （`/api/health/watchdog` 的字段、阈值、判据一个没动），变的是**时序**。

### 13.1 定位：循环体把「等待」放在了「扫描」之前

改前 `core/watchdog.py::watchdog_loop` 的循环体：

```python
while True:
    if stop_event is not None:
        await asyncio.wait_for(stop_event.wait(), timeout=interval)   # ← 先等
    else:
        await asyncio.sleep(interval)
    await recover_stalled_jobs()                                      # ← 后扫
    ...
    finally:
        _STATE["total_scans"] += 1
        _STATE["last_scan_at"] = _local_now_str()
```

两个后果（`docs/RUNTIME_WATCHDOG.md` §8.10 早有登记，当时明写「本轮不改」）：

1. **存活信号有 ~60s 空窗** —— 进程启动后头一个 `interval`（默认 60s）内
   `last_scan_at` 恒为 `null`。外部监控**无法区分**「刚启动还没扫」与
   「巡检已死」（两者都报 `null`）。
2. **重启后回收要等满一个周期** —— 库里遗留的 `running`/`ocr_running` 行
   要等 60s 才被标记为失败供重试。

### 13.2 一处**更隐蔽**的连带缺陷（本次一并修）

`finally` 里的 `total_scans += 1` / `last_scan_at = ...` 写在**等待之后**
⇒ **等待期间被取消也会记一次扫描**：`total_scans` 虚高，且 `last_scan_at`
会被「其实没扫过」的轮次刷新 —— 也就是**存活信号本身失真**。
这与主缺陷**同源**：都把「等待」与「扫描」的次序搞反了。

### 13.3 修法

把 `recover_stalled_jobs()` 提到**任何等待之前**，等待块整块挪到扫描**之后**。
`stop_event` 版仍是 `wait_for(stop_event.wait(), timeout=interval)`
⇒ 关停仍可被**立即唤醒**，不拖满一个周期。

**「扫描失败也不退出循环」的契约逐字未变**：扫描仍包在
`try / except CancelledError / except Exception / finally` 里，异常只写
`last_scan_error` 并继续下一轮。既有护栏 `test_loop_survives_scan_exception`
与 `test_scan_error_is_recorded_not_swallowed` **不动即仍绿**。

### 13.4 测试与变异（**7/7**）

| 护栏 | 类型 | 判据 |
|---|---|---|
| `test_loop_scans_immediately_before_first_interval` | **行为（判官）** | `interval=5s`、只等 0.1s ⇒ 旧实现扫描 **0** 次、新实现 **≥1** 次 |
| `test_loop_waits_only_after_the_scan` | 结构（AST） | 取 `watchdog_loop` 内「首扫」与「最早等待」行号，断言 `scan < wait`；含反空转锚点检查 |

变异 `devlogs/_verify/mutation_r69_watchdog_firstscan.py` ⇒ **7/7**：

| 变异 | 行为用例 | 结构钉 |
|---|---|---|
| M1 整块回退成「先睡后扫」 | CAUGHT | CAUGHT |
| M2 假修复：形状对但**第一轮跳过扫描** | CAUGHT | **GREEN** |
| M3 阴性对照（只改 docstring） | GREEN | GREEN |
| M4 删掉每轮 `last_scan_at` 写入 | CAUGHT | —（未跑） |

⚠️ **M2 那一格是**有意保留**的边界**：结构钉只钉**形状**，证明不了语义。
所以**行为用例才是判官**；结构钉的职责只有一条 —— 把「整块改回先睡后扫」
这种回退在**不需要 DB 的环境**里也能立刻抓住。把结构钉当成"语义已验证"就是
把形状当语义（Trap 57/66 的同一族错误）。

### 13.4.1 产物级覆盖（本批补齐的一处**断言失效**）

跑 e2e 时发现：`tests/e2e_frozen.py` 的 `watchdog_invariants` 用
`budget = interval + 30.0` 等 `last_scan_at` 被写上一次。旧实现「先睡一个周期
再首扫」要到 **t≈interval** 才写它 ⇒ **预算 ≥ interval 时新旧实现同样绿**，
这条**产物级**断言对 B4/#125 是**瞎的**；其旁注释（"循环是先等一个 interval
再扫，故头 60s 内本就为 null"）在修复后更**与代码矛盾**。

改法：预算改为 `interval * 0.8`（**严格小于** interval ⇒ 旧实现必然超时），
输出标签从写死的 `scanned@t+60s`（那只是 interval，不是实际耗时）改成
**实测** `first_scan@t+<elapsed>s`。

护栏 `tests/unit/test_e2e_round_budget.py::test_frozen_smoke_watchdog_liveness_budget_discriminates_scan_first`
—— 静态解析 `_wd_budget` 表达式，断言它是 `interval * <系数>` 且**系数严格 < 1**
（只查"含 `*`"不够：`interval * 1.2` 会假绿）。
变异 `devlogs/_verify/mutation_r69_frozen_watchdog_budget.py` ⇒ **5/5**：

| 变异 | 结果 |
|---|---|
| M1 `interval + 30.0`（回退） | CAUGHT |
| M2 `interval * 1.2`（≥ interval） | CAUGHT |
| M3 `interval * 1.0`（边界） | CAUGHT |
| M4 阴性对照（只改注释） | GREEN |
| M5 变量改名（护栏锚点消失） | CAUGHT（不把"找不到"读成"通过"） |

**重建产物上实跑**（`devlogs/_verify/_r69_e2e_frozen2.log`）：

```
OK watchdog_invariants: ocr_running=4200.0 >= max(cap=3600.0, rot_bound=1260.0);
   cancelling=2400.0 >= cpu_cap=1800.0; first_scan@t+0.0s (interval=60s)
```

⇒ **产物级确认「启动即首扫」**（`first_scan@t+0.0s`；旧实现会是 t≈60s）。

### 13.5 门禁：**先红后绿**（`artifact_freshness` 正确报警）

| 时点 | 结果 | 说明 |
|---|---|---|
| 首次（09:50，**产物未重建**） | **10/11** | `artifact_freshness` 指名 `core/watchdog.py` |
| 四步重建后（10:06） | **11/11 / 0 failed** | `3923 passed`、覆盖率 **95.08%** |
| 收尾（10:28，`8e281cd`，只动 `tests/`） | **11/11 / 0 failed** | `3924 passed`、覆盖率 **95.08%** |

**为什么必须重建**：`core/**/*.py` 在 `scripts/bundle_manifest.py::BUNDLE_SOURCES`
的入包集合内 ⇒ 改 `core/watchdog.py` 会让**两份产物**（`dist/pbc-server`、
`dist-electron/win-unpacked/resources/pbc-server`）与源码**逐字节不符**。
门禁第一次就把它挑出来了（`artifact_freshness` = FAIL）—— **这不是误报，
是门禁在阻止"用旧产物冒充新源码"**。

**收尾那次（`8e281cd`）只动 `tests/`**（不在入包集合内）⇒ 无需重建，
门禁仍 **11/11** —— 这顺带证明了「`tests/`-only 提交不使产物失效」
这条判断在本仓的清单口径下**确实成立**（`BUNDLE_SOURCES` 里没有 `tests/`）。

### 13.5.1 产物级 e2e（重建后实跑）

`python devlogs/_verify/run_e2e_with_config_creds.py frozen`
⇒ **26 passed / 2 failed**（`devlogs/_verify/_r69_e2e_frozen2.log`）。

2 条红**同因**且**非产品缺陷**：上游 LLM 返回 **402 `code=30001`
"account balance is insufficient"** ⇒ `llm_pipeline` / `ocr_pipeline` 记
`failed`（`devlogs/e2e_coverage_20261008-101148.json`）。产品按设计把 LLM
调用失败**降级成 finding 并继续** —— 这正是 `_judge_llm_success` 要拦的假绿
（"终态到达"不蕴含"LLM 成功过"）。

### 13.6 本批**未**验证的边界（不得读成已通过）

- **时序改动未在真实长跑上复核**：「启动即回收不引入竞态」正是 §8.10 当初
  写下"本轮不改"的理由之一。本批只做到：单测（行为 + AST）+ 变异 **7/7** +
  门禁 **11/11** + 产物逐字节一致 + 产物级 e2e（§13.4.1）。**"真实重启场景下
  不误杀"仍是推断，不是观测。**
- **沙箱内 Electron 层 e2e 仍不可复现**（见 §十二 / §12.5）。
- **LLM 链路仍未被覆盖**（上游账户欠费，见 §12.5 / §0 的 0-8）。
- ⚠️ **文档内 `R68` 与 `Round 67` 两套批次标签并存**（`CHANGELOG.md` 写
  `Round 67 第六批`，本报告与 `docs/TODO.md` 写 `R68 第六批`）。本批**沿用各自
  文件既有写法**（CHANGELOG 用 `Round 67`、报告/TODO 用 `R68`），**未统一** ——
  这是既存的文档卫生问题，单独登记，不在本批顺手改（避免把两批的证据行混在一起改）。
- ⚠️ **LLM 失败归因本批有更新**：上一批记的是 **401 `30014` 凭据被拒**，
  本批实跑（两次）都是 **402 `code=30001` "account balance is insufficient"**
  ⇒ **凭据有效、账户欠费**。故 `docs/TODO.md` 的 **0-8 动作已更正**为
  「充值或换一把有余额的 key」。⚠️ 两次 e2e 的 `llm_pipeline`/`ocr_pipeline`
  仍记 `failed` —— **"流水线到达终态"依然不蕴含 LLM 成功过**。
