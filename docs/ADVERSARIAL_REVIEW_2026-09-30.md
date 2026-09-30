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
| 测试情况 | 门禁 **11/11 pass**（`overall=pass`）、**3875 passed / 0 failed**、覆盖率 **95.09%**（门禁 95%）—— 在**已提交的树** `dd33330` 上跑出，见 §5.2 |
| 前后端是否模块化 / 单一职责 | **是**。前端两轮大文件拆分已完成（`review.js` R63、`settings.js` R65）；后端 68 文件 24.9K 行、无神模块 |
| 可维护性 | **强**。测试 63,009 行 vs 生产 34,928 行 ≈ **1.8 : 1**；门禁 11 项全自动 |
| 仓库卫生 | **好**。375 个跟踪文件，工作区干净，`.gitignore` 有解释性注释且被机检锁定 |
| git / GitHub | ⚠️ **本地领先 `origin/main` 9 个提交，尚未推送** |
| 本轮修复 | **4 条**（F1–F3 为**文档缺陷**，F4 为**测试清理期缺陷**）+ 2 个新护栏（9 条用例）+ 变异 **9/9** |
| 门禁 | 修复后在**已提交的树**上重跑 ⇒ `overall=pass`、`11/11`、**3875 passed / 0 failed** |

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

⚠️ 上表第 1 行的 `3866` 是 **R65 的**终验数，**不是** R66 的"修复前"数 —— 曾一度记错，已更正。
**对账**：`3866 + 9 = 3875`，那 **9** 条正是本轮两个新护栏
（`test_gate_inventory_doc.py` 5 条 + `test_todo_freshness.py` 4 条）。
第 2 行那 **2 条红** = ① 新护栏**自我命中**了既有的 `test_repo_hygiene.py` 行号引用护栏
（新文件 docstring 里引了 `settings.js` 的**行号字面量**）；② §六-F4 的顺序相关红。
两条均已修 ⇒ 第 3 行 **0 failed**。**门禁全绿是"修复后"的状态**，不是红着也全绿。

最终结果：

```
$ python scripts/release_gate.py          # 工作区干净，HEAD = dd33330
[PASS] worktree_clean   工作区干净
[PASS] no_build_outputs 378 个已跟踪文件中无构建产物
[PASS] dist_variants    1 份完整产物（dist-electron）
[PASS] artifact_freshness 2 份产物与源码逐字节一致
[PASS] packaging_files  5 个前置文件就位
[PASS] rules_wired      29 个 _check_* 规则函数（下限 14）
[PASS] kb_corpus        知识库 441 条（下限 200）
[PASS] kb_packaging     6 个 KB 源经 core/kb/data glob 自动入包
[PASS] dependency_vulns 0 条公告（快照 6 天前，扫描 43 个条目）
[PASS] runtime_eol      在支持线 [41, 42, 43] 内：dist-electron: Electron/43.7.4
[PASS] tests_coverage   3875 passed, 0 failed, coverage=95.09% (门禁 95%)

OVERALL: pass  (pass=11 fail=0 warn=0 skip=0)
报告: devlogs/gate_report_20260930_121027.json
```

⚠️ **`worktree_clean` 是 11 项里的第 1 项**，它在 pytest 之前跑 —— 所以门禁结果
对**当时那一份字节**有效；跑完门禁后若再改任何文件，该结论即失效，须重跑。

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

- 跟踪文件 **375** 个；`git status --porcelain` **空**。
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
* main dd33330 [origin/main: ahead 9]
$ git remote -v
origin  https://github.com/haowuxiwang/BatchSentry.git (fetch/push)
$ git rev-list --left-right --count origin/main...HEAD
0       9
```

⚠️ **本地 `main` 领先 `origin/main` 9 个提交**（`c8b092b` 之后的全部 R62–R66 工作），
**GitHub 上还是旧的**。推送是对外发布动作，**等用户确认**。

---

## 九、遗留 backlog（详见 `docs/TODO.md` §0）

1. 推送本地 9 个未推送提交到 `origin/main`（**用户动作**）。
2. Electron 应用层 e2e 在**真实终端**复跑（本环境不可复现）。
3. `docs/TODO.md` §A 及以下的 **86 条**未复核项逐条复核（自述已解除的 2 条优先）。
4. 跨页总览增强（后端 `status` 过滤 / 关键词搜索）—— 小，可选。
5. 上传批次并发提交 —— 小，可选，**不建议先做**。

### 本轮**未**做、但值得下一轮考虑的

- **`docs/` 体量治理**：`docs/` 下 20+ 个 `.md`，其中 `TODO.md`（152K 字）、
  `PROJECT_PITFALLS.md`（168KB）、`ADVERSARIAL_AUDIT.md`（114KB）体量巨大。
  ⚠️ 但**历史审查报告不得回改**（那是证据）；要做也只能**加归档头 + 目录**。
- **`docs/TODO.md` 里失效的行号锚点**：本轮只在头部标注了该问题，**未逐条清理** ——
  因为 §A 是历史快照，清理它需要先逐条复核（即 backlog 第 3 条）。
