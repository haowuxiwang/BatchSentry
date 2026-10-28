# win-unpacked 端到端验收标准（Electron 应用层）

> 对象：`dist-electron-out-<ts>/win-unpacked/` —— **用户双击 `BatchSentry.exe` 时
> 实际运行的那一份**（含 Electron 主进程、`app.asar`、内嵌后端）。
> 驱动：`tests/e2e_unpacked.py`｜证据：`devlogs/e2e_unpacked_<ts>.json`
> 覆盖记账：`tests/e2e_coverage.py`（与 `e2e_frozen` 同一口径）

## 0. 分层：为什么必须有这一层

| 驱动 | 测什么 | 能发现的缺陷 | 发现不了的 |
|---|---|---|---|
| `tests/unit/*` | 纯逻辑 / 静态契约 | 逻辑错误、契约漂移 | 产物是否真能跑 |
| `tests/e2e_frozen.py` | **内嵌后端 exe**（直接跑 `pbc-server.exe`） | 后端 HTTP 行为、优雅关闭（后端侧） | Electron 主进程、窗口、渲染层 |
| **本驱动** | **win-unpacked 整包** | 主进程启动/退出、拉起的是不是内嵌后端、渲染层是否加载 `app.asar`、端到端优雅关闭 | LLM/OCR 成功路径（归 `e2e_frozen`） |

⚠️ 三层不可互相替代：`e2e_frozen` 全绿**不蕴含**"用户双击能用"——
它连 Electron 都没启动过。

## 1. 三条前置硬约束（违反则整轮无效）

### 1.1 必须删除 `ELECTRON_RUN_AS_NODE`

宿主（WorkBuddy 自身是 `--stdio` 的 Electron daemon）**以 `ELECTRON_RUN_AS_NODE=1`
运行**，该变量**继承给所有子进程**。Electron 只要看到它就以**纯 Node 模式**启动：

| 观测 | 值 | 说明 |
|---|---|---|
| 正常启动 | `rc=0`、0.2~0.7 s、**零日志** | 不加载 app、不建窗口 |
| `--version` | `v20.18.3` | **Node** 版本，不是 Electron 版本 |
| `--user-data-dir=X` | `bad option: ...`、`rc=9` | Node CLI 对未知参数的报错 |
| 事件日志 / 崩溃记录 | **空** | 不是崩溃，是"正常"退出 |

⇒ 驱动**必须** `env.pop("ELECTRON_RUN_AS_NODE")`。
这也解释了 2026-09-23 之前"产物起不来、但也查不出原因"的全部现象。

### 1.2 隔离要**两把钥匙**（各管一半）

| 目标 | 机制 | 钥匙 |
|---|---|---|
| Electron/Chromium userData（Cache、GPUCache、lockfile） | `app.getPath('userData')` 走 **SHGetFolderPath**，**不读 `APPDATA`** | `--user-data-dir=<sandbox>` |
| 后端数据根（`data.db`/`output`） | `config.py` 读 `os.environ["APPDATA"]` | `APPDATA=<sandbox>` |

⚠️ 只设其中一个都会"看起来隔离了"而实际污染：v1 只设 `APPDATA` ⇒ Electron 写真实 userData。

### 1.3 关闭请求必须发给**主窗口**，且要等 splash 收敛

`createSplashWindow()` 与 `createWindow()` 产生的**都是可见的 `Chrome_WidgetWin_1`**，
且 **splash 先出现**（title = app name `batchsentry`；主窗口 title =
`BatchSentry — 批记录辅助审查`）。

若把 `WM_CLOSE` 发给 splash：
- 只是关掉 splash；`window-all-closed` 不触发 ⇒ **`app.quit()` 不发生** ⇒ 后端继续跑

⇒ 排障时曾据此误判"**关闭后进程残留**"（实测：端口 120 s 不释放、进程不退），
**实为探针缺陷**。驱动判据：**等可见窗口收敛为 1（连续 3 次采样）后再关**。

## 2. 评价维度（D1–D8）

| # | 维度 | 判据（可证伪） | 实测方法 | 通过条件 |
|---|---|---|---|---|
| D1 | 启动可达 | `/health` 返回 `status=ok` | 轮询 ≤90 s | 就绪（记录耗时） |
| D2 | 版本自证 | `/health.version` == `PROVENANCE.txt` 的 `version` | 比对两者（**不写死字面量**） | 完全一致 |
| D3 | 拉起的是**内嵌**后端 | 子进程 `pbc-server.exe` 命令行含 `<win-unpacked>/resources/pbc-server/` | CIM 枚举子进程 | 路径命中内嵌路径 |
| D4 | Electron 层真起来 | 存在 `--type=renderer` 且命令行含 `app.asar` | 同上 | 命中 |
| D5 | 渲染层就位 | 可见 `Chrome_WidgetWin*` **收敛为 1** 且 title 非空 | `EnumWindows` 轮询 | 收敛（记录耗时与 title） |
| D6 | 优雅关闭全链路 | ① `WM_CLOSE` 到主窗口 ⇒ ② 端口释放 ⇒ ③ 进程退出且**退出码 0** | `PostMessage` + 内核判据 + `netstat` | 三条件全中（记录各时刻） |
| D7 | `close_db()` 生效 | 退出后 `-wal`/`-shm` 消失（只剩 `data.db`） | 数据目录递归快照 | 无 `-wal`/`-shm` |
| D8 | 隔离性 | 真实 `%APPDATA%\PBC` 运行前后**无变化** | 递归快照（`size:mtime`） | 无变化（见 §4 已知例外） |

### 判据的硬性要求

- **存活判据用内核级**：`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` +
  `GetExitCodeProcess == STILL_ACTIVE`。文本解析（`tasklist` + 字符串匹配）
  在本地化/编码下会产生自相矛盾的结论（本驱动开发中实际踩到）。
- **失败归因要有正向对照**，不能只看"没成功"。
- **`-wal` 残留**只能证明"没跑 checkpoint"，**不能**证明"没崩"——两者要分开记。

## 3. 基线（2026-09-23 实测，**5 次连跑**）

目标：`dist-electron-out-20260921-160111/win-unpacked`（`version=1.2.0`）

| 维度 | run1 | run2 | run3 | run4 | run5 | 判读 |
|---|---|---|---|---|---|---|
| D1 启动就绪 | 5.19 s | 5.67 s | 5.14 s | 5.70 s | 5.09 s | 冷启 ~5–6 s（含后端 migration + KB 播种）|
| D5 主窗口收敛 | 1.02 s | 1.01 s | 1.01 s | 1.01 s | 1.01 s | 极稳定 |
| D6 端口释放 | 0.50 s | 0.50 s | 0.50 s | 0.50 s | 0.50 s | 后端优雅退出 |
| D6 进程退出 | 1.37 s | 1.37 s | 2.25 s | 2.24 s | 2.26 s | **双峰**（~1.4 / ~2.3 s），均 `exitCode=0` |
| D7 checkpoint | ✓ | ✓ | ✓ | ✓ | ✓ | 仅剩 `data.db` + 后端自有 `logs/` |
| D8 隔离 | SKIP | SKIP | SKIP | SKIP | SKIP | 见 §4 |

**结论：35 passed / 0 failed / 5 skipped（5 次 × 8 维 = 40 项），零 flaky。**

⚠️ **D6 进程退出时间是双峰的**（1.37 s 与 2.25 s 两档），但**两档都远小于 60 s 预算**、
且 `exitCode` 恒为 0 ⇒ 属正常抖动（Chromium 回收子进程的时机差异），
**不是 flaky**。判据因此**只断言"最终退出且退出码为 0"**，不设"必须在 N 秒内"的
硬阈值（否则会把抖动误判成缺陷）。

## 4. 已知盲区（**必须如实声明，不得冒充已覆盖**）

1. **`%APPDATA%\PBC\logs\backend-boot.log` 无法从外部隔离**：
   `main.js::bootLog()` 用 `app.getPath("appData")`（= `%APPDATA%`，Chromium 走
   `SHGetFolderPath`），`--user-data-dir` 与 `APPDATA` 都改不到它。
   ⇒ D8 记 **SKIP** 并具名该文件，**不记 PASS**。
2. **不含 LLM / OCR 的"成功路径"**：本驱动只验应用层起停；该两项在覆盖清单里
   如实记 `skipped`，由 `tests/e2e_frozen.py` 负责（并且需要真实凭据）。
3. **不含人工观感项**（窗口渲染是否正确、中文是否 tofu 等）：需人看截图，
   机器判据只能到"窗口存在且 title 非空"。
4. **不含"启动过程中关闭"**：splash 阶段关窗属另一条路径（见 §1.3），
   当前驱动不覆盖，**也不据此宣称已覆盖**。

## 5. 跑法

```bash
# 默认：自动取**最新**的 dist-electron*/win-unpacked（绝不写死 dist-electron/）
python tests/e2e_unpacked.py --runs 3

# 指定目标（发布验收建议显式指定，避免歧义）
PBC_E2E_UNPACKED=dist-electron-out-20260921-160111/win-unpacked \
  python tests/e2e_unpacked.py --runs 3
```

环境变量：
| 变量 | 含义 |
|---|---|
| `PBC_E2E_UNPACKED` | 被测 win-unpacked 目录（最高优先级） |
| `PBC_E2E_EXE` | 兼容：指向内嵌 `pbc-server.exe`，驱动反推 win-unpacked |
| `PBC_E2E_UNPACKED_RUNS` | 重复次数（等价 `--runs N`） |
| `PBC_E2E_COVERAGE_JSON` | 覆盖清单落盘路径 |

退出码：`0` = 全过；`1` = 有 FAIL；`2` = 找不到目标。

⚠️ 本驱动会**独占端口 58765**，不得与会占用该端口的测试并行跑。
