# TODO —— 活的待办清单（单一入口）

> **怎么用**：这是**唯一**的待办清单。开工先读本文件；完成一项就地把 `[ ]` 改 `[x]`
> 并填上**证据**（提交号 / 日志路径 / 数字），**不要**另开 TODO 文档。
> 历史计划（`PLAN.md`、`docs/PLAN_v1.1_EXECUTION.md`、`docs/ROADMAP_v1.1.md`）是**存档**，不再更新。
>
> 纪律（见 `CLAUDE.md`「Repo hygiene & release discipline」）：**先定位 → 再解决 → 最后测试**；
> 结论必须挂证据；**不升版号的边界** = 改动是否进入 PyInstaller 产物。
>
> 最后更新：2026-09-20（Round 45：**B1-9 规则层时序假阳性修复** —— 视觉复核 p48 定位到
> `_check_time_reversal_cross_page` 把**区间重叠**当成**倒序**并按 `time_reversal` 给
> `critical`；原图上是**不同设备的两道并行清洗**（D2101/D2102 箱体 03:12→03:36 与
> 烘盘/盘罩/勺子 03:34→04:12），且两处时刻都是**手写**填写。已改为：真倒序
> （`curr.start < prev.start`）⇒ critical、仅重叠 ⇒ warning；**手写或日期靠
> `production_date` 推定 ⇒ 一律封顶 warning**（与 B1-6 的 L4 同源）。真实 51 页回放
> p48 `critical → warning`；新增护栏 33 例 + 变异验证 4 方向全红。同时**更正上一轮的一处
> 误读**（原写"两处手写日期 15日 vs 25日"，实测两者都是 25 日）。新增 **B3-3**
> （`RuleSpec.severity` 声明与实现漂移且无人消费）。
> Round 43：**第三轮对抗性审查** —— 五路只读审查（前端 / 后端 /
> 配置与协议 / 流式与规则 / 文档与知识库）+ **视觉直读原页交叉比对**（核 5 页坐实 4 条假
> critical）+ 产物实测。**判定：无 P0，v1.1.9 可作基线**；新增 **P1×1 / P2×7 / P3×9**，
> 其中 **P1 = provider 被启动逻辑自动改写并持久化**（上一轮列为"未复核"，本轮确证）。
> 同时**纠正 2 条已过时的登记**（`B4-1` 的 `id:` 帧其实早已存在；`#155` 对复核页不适用）。
> 报告 → `docs/ADVERSARIAL_AUDIT.md` **§15**）·
> Round 42：**产物级端到端测试（#149 闭环）** ——
> 用 `tests/e2e_run.py` 驱动 **Electron 内嵌的那份 exe**（`win-unpacked/resources/
> pbc-server/pbc-server.exe`），跑 **7 个轮次全部 PASS**（`pdf` / `img` / `cancel` /
> **`real` 51 页真实批记录** / `rot` / `robust`×2），driver 报 `ALL ROUNDS PASSED` + `exit=0`。
> **独立核验**（不采信自述）：findings **直查库**与 driver 逐条一致（real **293 条** /
> 14 类 / `gmp_basis` **293/293**）、SSE 帧数吻合（real 507）、`ocr_backend_used` 全为
> `paddle`（**无 failover 掩盖**）、真实 `%APPDATA%/PBC` **未被污染**；
> 且 `win-unpacked` 内嵌 exe 与 `dist/` 那份 **sha256 完全相同** ⇒ 不存在「测 A 发 B」。
> **判定：功能面可分发**。同时用**多模态直读原图**核实出 **3 类 LLM 层假阳性**
> （日期判据方向反 / 串位幻觉造出图上不存在的值 / 非数值与日期形态输入无防线）
> + 1 条日志口径缺陷（`measurements=0` 恒为 0）⇒ 已登记 B1-4 / B1-5 / B2-6）·
> Round 41：**基线冻结 + 密钥卫生实测复核** ——
> 判定 v1.1.9 可分发；**全历史密钥扫描**得 3 把 `sk-` 值，逐把对两家厂商**实测**，
> **全部 `401 invalid`**（K1/`5d855143`、K2/`838c00a7`、K3/`2608ecd2`）⇒ 泄露面已关闭；
> 当前生效 key（`%APPDATA%` 配置，`c11aa513`）**从未入库**；**产物与 `app.asar`
> 二进制实测 0 命中**；打 tag **v1.1.9**；修正 `CHANGELOG` 顶部过期的
> 「可分发版本 = 1.1.8」；补 `DEPLOYMENT.md` 的「暴露面登记」+「证据边界」）·
> Round 40：第二轮对抗性审查（基线 = v1.1.9） —— 复核上一轮 8 项指控；多模态直读第 8 页
> 确认「OCR 把 `4.6` 读成 `46`」并验证当前版本已有一等对策；新增 6 条发现；
> **判定：无 P0 阻塞**；产出 **v1.2.0 迭代清单**（见文末）·
> 审查报告 → `docs/ADVERSARIAL_AUDIT.md`）·
> 可分发版本 **v1.1.9**（11 条缺陷已修、产物已重建、**已打 tag**）·
> 远端 `1ee2c17`（Round 39/40 均已推送）·
> **A1 已解除**（用户已手工收敛产物目录）

---

## A. 需要**用户**动作（我做不到，已在等）

- [x] ~~**A1（阻塞收敛）完全退出 WorkBuddy 宿主，然后在普通终端跑收敛脚本。**~~
      ✅ **2026-09-18 已解除**（用户手工完成）：全部 `dist-electron*` 已清空，
      现存仅 `dist/`（backend，107 MB）。`python scripts/clean_dist.py` dry-run 输出
      **「待清理: (无)」**，目录数 == 1 ⇒ 仓库卫生回到约定状态。
      ⚠️ **新约定（用户明确要求）**：仓库根**只能有一个 `dist-electron*`** ——
      下次构建应从零一次成型，直接写**标准路径 `dist-electron/`**；若自愈到时间戳目录
      **必须当轮归位**，不得留着过夜；每次构建后跑一次 dry-run 自证。
      ⚠️ 注意反面影响：**Electron 产物已不存在** ⇒ 重建时 `tests_coverage` 的
      `test_distribution_parity` 在 Electron 打包完成**之前**必然 skip/红，
      这属预期（先后端产物 → 再 Electron 打包 → 再跑门禁）。
- [x] ~~**A2 轮换已泄漏的 LLM 凭据（必须厂商侧操作）。**~~
      ✅ **2026-09-18（Round 41）实测复核：泄露面已关闭，无需再动作。**
      全历史扫描（`git grep -o -E "sk-[A-Za-z0-9]{32,}"` 覆盖全部 249 个提交）得**唯一 3 把**，
      逐把对 **SiliconFlow 与 DeepSeek 两家**实测 `POST /v1/chat/completions`：

      | 代号 | 历史出处 | sha8 | 实测 |
      |---|---|---|---|
      | K1 | `tests/e2e_frozen.py`·`e2e_manual.py`·`e2e_quick.py`（自 `81964a3`） | `5d855143` | **401** `{"code":30014,"message":"Token is invalid."}` |
      | K2 | `tests/unit/test_config.py`（早期 5 个提交） | `838c00a7` | **401**（两家中均拒） |
      | K3 | `spike_test.py`（`8651e3e` 初始骨架） | `2608ecd2` | **401**（两家中均拒） |

      ⇒ 已公开暴露的 3 把**全部被上游吊销**（"主动吊销"与"自然过期"之别未验证，
      但**可利用性已归零**）。⚠️ 前提事实：本仓库是 **public**，所以这 3 把等同于已公开。
      ✅ **当前生效 key 从未入库**：`%APPDATA%/PBC/config.json` 的 `SILICONFLOW_API_KEY`
      （`c11aa513`，实测 **HTTP 200 可用**）**不在**上述 3 把中。
      ⚠️ **遗留但无安全影响**：仓库根 `config.json` 仍存 K1（它是**开发模式的生效配置**，
      见 `config.py:_config_path()`）⇒ 开发模式 / 走仓库配置的 e2e 会拿到一把**死 key**，
      表现为 `30014` 假红。已登记为 **B4-3**。
      `config.json.bak_pre_key_rotation` 保留不动（被 `%TEMP%/pbc_127_accept.py` 用来复现 #127 触发条件）。
- [ ] **A3 提供第 2 份真实批记录 PDF 用于泛化验证（缺陷 #126）。**
      目前**只有一份**真实批记录（丝裂霉素提取批记录，51 页）⇒ 规则/OCR 的泛化性
      **未经检验**。缺它就无法回答"换一份记录还准不准"。
- [ ] **A4（阻塞"需真实 findings 的多轮 e2e"）硅基流动账户余额不足（402）—— 2026-09-18 已解除。**
      > **✅ 2026-09-18 复核：key 已恢复可用。** 实测 `POST /v1/chat/completions`
      > `model=deepseek-ai/DeepSeek-V3.2` → **HTTP 200**，正常返回内容（usage 18 tokens）。
      > ⚠️ 但**仓库内的 `config.json` 里那把 key 仍是失效的旧 key**
      > （`sk-vprn...` → `30014 Token is invalid`）。**真正生效的是
      > `%APPDATA%/PBC/config.json` 里的另一把**（`sk-vhxq...`，2026-09-17 10:55 更新）。
      > ⇒ 用仓库 `config.json` 做任何自检都会**假红**；自检脚本应显式读 `%APPDATA%/PBC/config.json`。
      > **本条阻塞已解除，多轮 e2e 现在可以跑**（尚未跑，见 F11 开工顺序）。
      > ① 旧证据（2026-09-17，保留以说明"驱动拒绝假绿是正确的"）：
      实测（v1.1.8 产物，`e2e_run.py --rounds pdf,img`）——驱动**自检**就报出来了：
      ```
      test_provider -> 200 {"ok":false,"provider":"siliconflow",
        "reason":"...APIStatusError: Error code: 402 -
         {'code': 30001, 'message': 'Sorry, your account balance is insufficient'}"}
      ```
      时间线（同一天，同一 key）：
      `14:36–14:42 冻结冒烟 findings=3`（**LLM 当时可用**）→ `14:43–14:44 #127 验收`
      （用**失效凭据**故意触发失败路径，消耗 0）→ `14:56–15:02 多轮 e2e` **402**。
      **后果**：两轮 OCR 均成功（`backend=paddle`，6 页 / 1 页），但 Stage 2 无法调用
      LLM ⇒ 0 findings ⇒ 驱动的**内容断言 FAIL**。
      ⚠️ **这两条 FAIL 是正确行为**：驱动不能把"0 findings"记作成功（否则就是"假绿"）；
      同时**产品侧行为正确** —— 终态如实报 `error`（SSE `ocr → analyze → done:error`），
      未伪装成绿点或 `partial_review`，这正是 #127/#131 的**实战确认**。
      **处置**：充值，或在 `%APPDATA%/PBC/config.json` 换成有额度的提供方/密钥，然后重跑：
      ```
      python %TEMP%/pbc_run_artifact_e2e.py <产物>\win-unpacked\resources\pbc-server\pbc-server.exe \
             e2e_run.py --rounds pdf,img
      ```
      注：本地目前**只配了 `siliconflow` 一家**（`DEEPSEEK_API_KEY` 等键不存在）⇒ 没有可直接
      切换的备选供应方。

## B. 高优先（可做，且不依赖 A）

- [ ] **B1 让 e2e 驱动也覆盖"LLM 路径坏了"（本轮发现的口子）。**
      冻结冒烟已能报出"凭据失效"（Round 31 修好），但**多轮驱动**（`e2e_run.py` 的
      `pdf/img/real/rot…`）仍没有"断言 LLM 真的被调用过且成功"的判别。做法参考
      `tests/e2e_frozen.py::settings_llm_provider_matches_key`：
      配置后先断言"活动提供方 == 密钥所属且 `configured=True`"，再断言产物里
      `pages_analyzed > 0`。**验收**：故意用失效 key 跑一轮，必须 **FAIL** 且原因指向 LLM。
- [ ] **B2 CI 增加"冻结产物" job，让 A 组护栏真的跑起来（缺陷 #106）。**
      现在 CI 只在源码上跑；`tests/unit/test_distribution_parity.py` 这类"产物内一致性"
      在 CI 上被跳过。**验收**：CI 日志里能看到该 job 的 junit，且**不是 0 用例**。
- [ ] **B3 视觉交叉对比做成常规检查（缺陷 #110）。**
      已有 `docs/VISUAL_CROSSCHECK.md` 与 M8 结论（独立复现"46 → 4.6"丢小数点），
      但仍是**一次性**脚本。**验收**：有单测/脚本能对指定 job 输出"模型视觉 vs OCR 抽取"
      的一致性数字，并写进 job 报告。⚠️ 记住**多模态不能当降噪手段**（第 9 页三方
      72/72 逐格一致，替代净损坐标/互证/成本 ≈4.2×）⇒ 只宜作**第四读**。
- [ ] **B4 巡检器"启动即回收 + 存活信号无空窗"（缺陷 #125）。**
      现状：看门狗**先睡一个周期再首扫** ⇒ 重启后有一段无保护窗口。
      **验收**：进程启动后立刻完成一次巡检，且 `GET /api/health/watchdog` 的
      `last_scan_at` 从第一秒起就持续推进（不留空窗）。

## C. 技术债 / 已知缺口（明确记录，不假装已解决）

- [ ] **C1 无真实标注集 ⇒ 无可信 P/R。**
      `docs/FINDING_GROUND_TRUTH.json` 是**合成件**，结果**不可外推**。
      任何"精确率 0.9x"的说法都必须标注其出处是合成集。
- [ ] **C2 上传页数上限 200 是**线性外推未实跑**（`config.UPLOAD_LIMITS` 硬 200 / 软 80）。
      51 页已实跑；200 页的耗时/内存是推算值。**验收**：真跑一次 ≥120 页的样本并记录。
- [ ] **C3 知识库有 2 个死键 + 2 个类型无词表**（KB 与 finding 类型的覆盖面不一致）。
      `tests/unit/test_kb_multisource.py` 已锁住"条目数不低于下限"，但**不检查键的有效性**。
- [ ] **C4 provider 静默改写 + `kb_prompt_inject` 不可达。**
      `api/settings/write.py` 有"GLM 实际跑 deepseek"的静默回退（已有注释警示）；
      `kb_prompt_inject` 的开关路径**当前不可达**，需要么接通要么删掉，别留着当装饰。
- [ ] **C5 旋转探测的重试不区分拥塞/瞬态（#120 残留）。**
      已修"拥塞不消耗角度预算"，但**40s 内仍会耗尽全部角度**的情形未闭环。
      ⚠️ 定位时注意：**字段缺失 ≠ 路径未走**（`self_heal.py` 里 upgrade 页探测失败
      **故意不动诊断**）⇒ **只有日志能区分"没跑"和"跑了但失败"**。

## D1. Round 32 已完成 —— 存档，勿重复做

- [x] **修 #132（失败页被前端静默丢弃）**：`jobs.failed_pages` 是 SQLite TEXT 列（存 JSON），
      JSON 接口**原样透传** ⇒ 响应是字符串 `"[2, 1]"` ⇒ 前端 `Array.isArray()` 静默退化成
      `[]` ⇒ **失败页数与页码在任务列表里永不显示**；而复核页（SSR 自行 `json.loads`）却
      正常 ⇒ 同一字段两张页面不一致，用户只会读成"这次没有失败页"，#127 的前端渲染形同虚设。
      修法：`status.py` 新增 `_parse_failed_pages` 并接入 `GET /{id}` 与 SSE **两个**入口；
      `main.py` 复用**同一**解析器（单一真值源）；降级**不伪造 `[]`**（无失败页 → `None`，
      非法值 → `None` 并记 warning，否则"数据损坏"与"真的无异常"在界面上不可区分）。
- [x] **护栏（含变异验证）**：6 例纯函数单测（断言**类型**而非取值）+ 1 例接口级
      （断言**运行时返回数组**）+ 1 例产物级运行时断言 `failed_pages_type`。把两个入口分别
      临时改回"原样透传"，新护栏**当场变红**（GET `实得 '[2, 1]'`；SSE `实得 ['[2]', '[2]']`），
      还原后 149 passed ⇒ **实测能失败**，不是"两分支都判 PASS"。
- [x] **修正两条"锁死错契约"的旧断言**：`test_api_jobs_coverage.py` 里写死的
      `'"failed_pages": "[2]"'`（断言序列化字符 ⇒ 把缺陷本身固化成契约）改为**解析 SSE 的
      `data:` 帧后断言结构**；`test_config_error_visibility.py` 补上前端类型预期
      （`Array.isArray(job.failed_pages)`）。
- [x] **升版 1.1.8 并重建产物**：改动落 `api/jobs/status.py` 与 `main.py`，**都进 PyInstaller
      产物** ⇒ 4 处版本真值同步 + PyInstaller（6m30s）+ electron-builder（1m34s；因标准目录
      被锁而自愈到 `dist-electron-out-20260917-142437`）。
- [x] **产物验证**：asar 内版本 1.1.8 == 源码；`BatchSentry.exe` 188.8 MB；内嵌
      `pbc-server.exe` 20.3 MB；`extraResources` 与 `dist/pbc-server` **811 文件 / 112.2 MB
      逐一致**；备用目录已按约定写 `PROVENANCE.txt`。
- [x] **文档**：`CLAUDE.md` 新增 Round 32 + 更新 `Current phase`；`CHANGELOG.md` 新增
      `[1.1.8]`；`docs/PROJECT_PITFALLS.md` 新增 **§二十一**（字符串探针会给出**错误的否定**——
      PYZ 是压缩的；同文件并行 `Edit` 会互相覆盖，回执不可信）。

## D2. Round 31 已完成（存档，勿重复做）

- [x] D1 e2e 夹具的"假绿"两处（空白样例 + 提供方与密钥错配）→ 提交 `417d3ee`；
      实测冒烟 **21/0**，`provider=siliconflow`、`findings=4`、`report 7049 B`。
- [x] D2 `clean_dist` 认锁探测 **10m52s（未完成）→ 11.5s** → 提交 `f1c5457`；
      `tests/unit/test_clean_dist.py` 20 例。
- [x] D3 仓库卫生机检（生成物禁止入库 + 变体可见化 + 落点三处联动）→ `101acb5` / `80d5bcd`；
      `tests/unit/test_repo_hygiene.py` 34 例。
- [x] D4 `test_page_analyzer.py` 文档字符串无效转义告警（全量刷屏的根因）→ 改 `r"""`。
- [x] D5 文档校正：`CLAUDE.md` 补 Round 31、**修正 Round 30 里被证伪的"20/0"** 与
      5 处轮次错标；`PITFALLS` 新增 §十九 / §二十。

---

## E. 当前地面真值（可复核，来自本轮实测）

| 项 | 数值 | 证据 |
|---|---|---|
| 全量单测+集成 | **2635 passed / 0 failed**（= 上轮 2629 + 本轮 6 例新用例） | 门禁 `tests_coverage` / `devlogs/gate_junit_20260917_160451.xml` |
| 覆盖率 | **95.5%**（门禁 95%） | `devlogs/gate_report_20260917_162936.json` |
| 打包信号 | **OVERALL pass（8 PASS / 0 FAIL / 0 WARN）**，在**最终提交 `7b3dd52`** 上复跑 | `devlogs/gate_report_20260917_162936.json` |
| ⚠️ 本机门禁耗时波动 | 同一套测试两次实测 **3m25s vs 24m48s**（6.7×）。排查结论：**非死锁** —— 进程存活且在推进（20 秒内新建 117 个测试目录 ≈ 5.9 个/秒）、CPU 100%、`read_count` 1131 万次小读；判定为**本机环境波动**（重负载用例段被拖长），非产品缺陷 | 进程采样（psutil）+ `pytest-316` 临时目录计数 |
| 版本真值 | 4 处一致 = **1.1.8**（`test_version_consistency` 4 passed） | `tests/unit/test_version_consistency.py` |
| 产物（v1.1.8） | asar 内版本 = 1.1.8 == 源码；入口 188.8 MB；内嵌后端 20.3 MB；`extraResources` 与 `dist/pbc-server` **811 文件 / 112.2 MB 逐一致** | `%TEMP%/pbc_verify_artifact.py` |
| 冻结冒烟（v1.1.8 产物） | **22 passed / 0 failed**（`health: v1.1.8`、`provider=siliconflow`、`pipeline_terminal=review`、`findings=3`、`report 4531 B`） | `%TEMP%/pbc_e2e_frozen_118.log` |
| #127/#131 验收（v1.1.8 产物，**失效凭据**复现触发） | **7 passed / 0 failed**；含 **`failed_pages_type: type=list value=[2, 1]`**（#132 修复在产物内的**判别性**证据）、`terminal_is_error`、`reason_visible 201 字`、`pages_analyzed=0` | `%TEMP%/pbc_127_accept_v118.log` |
| 多轮产物 e2e（`pdf,img`） | **2026-09-17 FAIL（外部阻塞：402）→ 2026-09-18 阻塞已解除**（key 恢复 HTTP 200，但**用 `%APPDATA%/PBC/config.json` 那把**）。**尚未重跑** | `%TEMP%/pbc_e2e_rounds_118.log`；详见 **A4** |
| 关键配置真值（2026-09-18 复核） | `LLM_PROVIDER=siliconflow`、模型 `deepseek-ai/DeepSeek-V3.2`、`OCR_BACKEND=mineru`；**密钥源 = `%APPDATA%/PBC/config.json`（`sk-vhxq…`）**，仓库 `config.json` 里是**失效旧 key**（`sk-vprn…`） | 两文件直读 + 各一次 HTTP 探测 |
| 目录锁持有者（Round 33 实测） | **WorkBuddy.exe（宿主进程）pid 16220 / 14048**；**只锁 `*.asar`**（同目录 exe 全 FREE）；**不是安全软件** | Restart Manager `RmGetList`；复现实验与证据链见 `docs/PROJECT_PITFALLS.md` §二十二 |
| 产物目录 | `dist-electron-out-20260917-142437`（标准路径被锁 ⇒ 已按约定写 `PROVENANCE.txt`，其 `unblock` 段已更正） | 见 A1 |
| 远端 | `e4f5b81`（已推送，`HEAD == origin/main`，worktree 干净） | `git rev-parse HEAD` / `origin/main` |
| **视觉第四读可行性（2026-09-18 定点实测）** | 7 个 VLM 直读 `test1.jpg`，只问上一轮 OCR 读错的 13 字段：**5 个模型 12/13**（`GLM-4.5V`/`Qwen3-VL-32B-Instruct` 各 3 次重现均 12/12/12），`Qwen3-Omni-30B-A3B` 仅 5/13（不可用）；**中文姓名 7 个模型全错**（无人读对"眭"） | `scripts/probe_vlm_fields.py`；`%TEMP%/vlm_probe_result.json` |
| 产物接口行为（Round 34 实测，**唯一可信判据**） | `/health` → `{"status":"ok","version":"1.1.8"}`；`/api/jobs/{id}` → `failed_pages=[1] type=list`（#132 在产物内**行为验证通过**）；`/api/jobs` 字段集 = `created_at, filename, finished_at, id, ocr_progress, status, total_pages`（**无 failed_pages / error_message / stall**） | `%TEMP%/pbc_probe_out.log` |
| 产物内容 vs 源码 | `static/*.js` + `templates/*.html` 构建快照与工作树**哈希一致**；后端 `.py` 在 PYZ 内（松散文件里找不到属**预期**，字符串探测不可信） | sha256 逐文件比对 |

---

## F. v1.1.9 迭代计划（Round 34 对抗性审查产出，2026-09-17）

> **结论先行**：**v1.1.8 主体可用，可作为基线。** 产物完整、`extraResources` 与
> `dist/pbc-server` 811 文件逐字节一致、`/health` 正常、`#132` 的修复经**行为验证**
> 确在产物内、且产物**不陈旧**（exe 建于改动之后）。
> 但它带着 **2 个 P0 级前端缺陷**，两者同属一类：**"GMP 假阴性表面"** ——
> 出错的状态在界面上看起来像成功。**建议 P0+P1 修完再对外分发；内部基线可直接用。**
>
> 编号沿用全局缺陷序号，本轮从 **#133** 起。

### F1. 前端（P0 是"错误态伪装成功"）

- [x] **#133【P0】复核页页头状态点硬编码绿色。** ✅ **2026-09-18 已修（`177a6d9`）**
      修法：抽出共享件 `static/status.js`（照 `eta.js` 既有先例），SSR 侧加
      `core/zh_map.py:status_dot_class` —— 二者**逐值等价由机检锁定**
      （`tests/unit/test_status_js.py`，12 例）。`upload.js` / `review.js` 均改为
      委托共享件（顺带收敛 #139 的状态映射重复）。模板改为
      `{{ status_dot_class }}`，`review.js` 的 SSE 显式更新 `#status-dot` 的 class。
      **验证**：端到端（真起 ASGI）`partial_review→bg-warning`、
      `review→bg-success`、`error→bg-destructive`；**负例验证**——把
      `bg-success` 写回模板 ⇒ 护栏立刻变红（不是空护栏）。
- [x] **#134【P0】列表页冷加载看不到失败页与失败原因。** ✅ **2026-09-18 已修（`177a6d9`）**
      修法：`listings.py` 投影 `failed_pages`（复用 `_parse_failed_pages`，避免
      TEXT 透传成字符串被前端 `Array.isArray` 静默丢弃）与 `error_message`；
      `buildRowFromSnapshot` 透传 `failed_pages`；摘要行抽成 `buildMetaLine`
      供**静态与实时两条路径共用**，结构上杜绝再次漂移。
      **验证**：端到端列表返回 `failed_pages=[2, 1]` 且 `type=list`、
      `error_message` 可见；**负例验证**——还原旧 SELECT ⇒ 3 条用例必红。
      ⚠️ 注意 `None`（"未能给出清单"）与 `[]`（"确认零失败页"）在 GMP 语义下
      **不同**，接口刻意保留该区分（`api/jobs/status.py:_parse_failed_pages` 的注释），
      前端用 `Array.isArray` 容错 —— **不要**为了"接口好看"把它们归一。
- [x] **#172【P1】列表端点还差 5 个"行字段"—— #134 只修了它点名的那两列。** ✅ **2026-09-18 已修**
      **由 #171 契约机检发现**（正是它该干的活）：`static/upload.js` 的
      `renderJobRow` 是**冷加载唯一的行构建器**，共消费 **13** 个字段，
      而 `/api/jobs` 只给了 **9** 个。缺 `ocr_backend_used` / `ocr_backend_display` /
      `pages_analyzed` / `phase` / `self_heal_progress`。
      其中 `ocr_backend_used` 影响最实：失败时 `ocrTagEl` 被隐藏，而该标签的用途
      正是"failover/自愈后留痕，GMP 追溯可见" ⇒ **老任务（超出 SSE 的 10 分钟
      推送窗口，永远收不到快照）永不显示它**，界面呈现为"这份记录没用过 OCR"。
      **修法**：SELECT 加 `ocr_backend_used`；其余 4 个**一律复用
      `api/jobs/status.py` 的既有派生函数**（`_count_analyzed_pages` / `_derive_phase` /
      `_ocr_backend_display` / `_parse_self_heal_progress` / `_parse_cross_progress`），
      **不新写第二套口径**。
      **实测成本**（20 job × 51 页 ≈ 1020 行结构化数据）：逐 job 调用 **5.3 ms**，
      与"一条分组查询"的 **4.5 ms** 仅差 **1.2×** —— 瓶颈是 `structured_json` 的
      读取量而非往返次数 ⇒ 复用单 job 口径函数即可，不值得为 0.8 ms 引入第二套判据。
- [x] **#173【P1】`page_ocr_empty` 漏在 JS 桥接对象里 ⇒ OCR 空页被显示成"本页无问题"。** ✅ **2026-09-18 已修**
      **由 #171 契约机检发现**。`page_ocr_empty` 一直在 Jinja 上下文里
      （`main.py:679` 已注入，模板 314/573 行据此 SSR 渲染），却**漏在
      `window.__PBC__` 里** ⇒ `review.js` 读到 `undefined` ⇒
      `currentPageFlags.ocrEmpty` **恒为 false** ⇒ 首屏兜底
      （`applyInitialPageLevelUI` → `emptyFindingsNote`）把该页的 SSR 文案
      "本页无 OCR 内容"**覆盖成"本页无问题"** —— 恰好是 #136 要消灭的 GMP 假阴性。
      ⚠️ **这是上一轮 #136 修复的残留**：模板分支改对了，但 JS 兜底又把它改回去。
      ⚠️ 这类缺陷**不可能**被"抓取页面 HTML 断言文案"的用例发现 —— 拿到的是 SSR
      渲染结果，而覆盖发生在浏览器里 JS 执行之后。契约必须落在**数据通道**上。
      **修法**：模板补 `page_ocr_empty: {{ page_ocr_empty | tojson }}`。
      **验证**：`test_ocr_empty_page_bridges_the_flag_to_js` 断言**真实响应**里出现
      `page_ocr_empty: true`；`TestSsrBridgeContract` 锁"消费集 ⊆ 注入集"。
      **负例验证**——从 `__PBC__` 去掉该键 ⇒ 两条用例立红。
- [x] **#135【P1】复核页首屏 parse-error 横幅只有通用文案。** ✅ **2026-09-18 已修**
      修法：`main.py` 从 `structured_json` 提取 `_error` 注入 SSR 上下文
      （`page_parse_error_reason`），`review.js` 新增 `applyInitialPageLevelUI()`
      在 `DOMContentLoaded` 调用 —— 复用**同一个** `updatePageLevelUI`
      （不另写一套文案，避免与 AJAX 路径漂移）。
      **验证**：`test_first_paint_shows_specific_reason` 断言首屏 HTML 里能读到
      `401 Token is invalid` 这类**具体**原因；**负例验证**——移除模板注入 ⇒ 该用例红。
- [x] **#136【P1】失败页仍显示"本页无问题"。** ✅ **2026-09-18 已修**
      修法：空态文案抽成 `emptyFindingsNote()`（JS **单一副本**），按
      「分析失败 / 空页 / 确实无问题」三分支；模板同步按 `page_parse_error` /
      `page_ocr_empty` 分支。判据读**当前页**标记 `currentPageFlags`
      （由 `updatePageLevelUI` 每次刷新写入）—— **不能用** `ctx`（首屏注入，翻页后过期）。
      **验证**：`test_failed_page_does_not_say_no_issues` 断言失败页**不含**"本页无问题"
      且含"不代表本页无问题"；`test_clean_page_still_says_no_issues` 是**对照组**
      （防"为修缺陷把三种空态全改告警"→ 告警疲劳也是假阴性）；
      **负例验证**——把失败页分支退回 ⇒ 该用例红。
- [ ] **#137【P2】上传页完全不渲染 stall。** 后端已在 SSE 快照提供（`api/jobs/status.py:200`），
      `static/upload.js` **0 处引用** ⇒ 停滞任务在列表上无任何提示。
- [ ] **#138【P2】设置页协议切换无默认值联动，且可能静默改协议。**
      `settings.js` 无 `_protocol` 的 change 监听（默认值只在**新增** provider 时填，
      `:1634-1659`）；渲染只判 `prov.protocol === "anthropic"`（`:118,152`）**无兜底** ——
      后端返回空/未知协议时浏览器自动选第一个 option（openai）⇒ 保存可能**静默把 anthropic 改成 openai**。
      另：`testProvider` 无 loading/禁用可并发重复请求；底部"测试连接"不采集未保存值，与单行行为不一致。
- [ ] **#139【P3】前端重复实现漂移。** `esc()` **6 份**副本
      （`upload.js:354`、`review.js:1116/1275/1647`、`settings.js:46`）；状态→中文 **4 份**
      （`upload.js:301`、`review.js:369`、`review.html:16`、`core/zh_map.py:34`）。
      另：`upload.js:498` 使用 `errCount` 早于其声明（`:516`）；`hover:bg-destructive/5`
      （`:838,1111`）在 `app.css` 里不存在；`review.js:1665` 的 `e.id` 未 `Number()`。

### F2. 后端（失败原因链路 / 并发）

- [x] **#140【P1】外部取消只终止父 task，派生子任务继续跑 LLM 并写库。** ✅ **2026-09-18 已修**
      修法：新增 `locks.ChildTasks`（`spawn` 登记 + `cancel_all`/`drain` 级联），
      `run_pipeline` 的 **`finally` 首要动作**就是 `children.drain(cancel=True)`
      —— 顺序不可颠倒（子任务会写库，会把刚收敛的终态再改回去）。
      `stage2._run_stage2_analysis`（含早停/取消分支）与分片路径的
      `analysis_tasks` / `heal_tasks` 全部改经 `children.spawn`（原先裸 `create_task`）。
      默认参数 `children=None` 时内部自建 → 保持旧调用方/测试可用。
      **验证**：`test_cancelling_pipeline_cascades_to_child_page_tasks` 断言
      "父被取消后子任务**真的停了**"（行为级，不是"代码里调了 cancel"）；
      另断言注册表与 per-job 锁都干净；**负例验证**——移除 `drain` ⇒ 该用例红。
- [x] **#141【P1】任务注册表按 job_id 单值覆盖 ⇒ 持锁孤儿失去引用 ⇒ 重试被永久挂死。**
      ✅ **2026-09-18 已修**
      修法：`_pipeline_tasks` 改 `dict[str, set[Task]]`（**累积**而非覆盖），
      新增 `register_pipeline_task` / `unregister_pipeline_task`（幂等，
      只摘自己、不删整个键）/ `live_tasks_for`；`_terminate_pipeline_task`
      取消**该 job 全部**未完成 task；`main.py` 关闭端点同步改为双层遍历。
      ⇒ 取消持锁真凶后它走 `CancelledError` 分支**释放锁**，等待者随之取到锁并
      因 `status=cancelled` 干净退出 —— 死结解开。
      **验证**：`test_holder_and_waiter_both_cancelled` 用真实 per-job 锁复现
      "持锁者 + 子任务 + 等待者"三者共存，断言三者全被取消**且锁已释放**；
      `test_single_value_registry_would_lose_the_holder` 反向固化"单值 dict 会丢掉真凶"；
      **负例验证**——把 `_terminate_pipeline_task` 改回只取消第一个 ⇒
      `assert task_holder.done()` 当场红（"持锁真凶必须被取消"）。
- [x] **#142【P1】`pending` + 无注册 task = 无终态黑洞；两处"先写 pending 后 launch"不在 try 内。**
      ✅ **2026-09-18 已修**
      修法：新增 `api/jobs/__init__.py:mark_launch_failed()`（条件 UPDATE
      `WHERE status='pending'` + 审计 `launch_failed`，**在 db_lock 之外**写审计避免
      不可重入死锁）；`upload.py` 与 `actions.py` 两处 launch 均包 try/except，
      失败即转 `error` 并返回 500（把黑洞变成可见、可重试的失败）。
      **验证**：`test_retry_launch_failure_marks_error_not_pending`（真调接口 + 断言
      不停在 `pending`、有 `error_message`、留审计）；
      `test_mark_launch_failed_is_noop_when_status_moved_on`（并发不误伤）；
      `test_launch_call_sites_are_wrapped_in_try`（**AST 静态机检**两处调用点都在 try 里，
      防将来新增第三个调用点又忘包 —— 那正是本缺陷的成因）；
      **负例验证**——还原裸 launch ⇒ 行为用例与机检**双双变红**。
      ⚠️ 机检读源码用 `utf-8-sig`（`actions.py` 带 BOM，普通 utf-8 解出 `U+FEFF`
      会让 `ast.parse` 崩）。
- [x] **#143【P2】"不可重试"判据是整段错误串的子串匹配 ⇒ 误判为配置级 + 误导文案。**
      ✅ **2026-09-18 已修**。**定位（真机实测，不是读代码推的）**：用真实 SDK 异常
      对象跑 8 个场景 → **3 例误判**：
      ① `openai.RateLimitError`(429, `"Rate limit reached … Limit 40000"`) 命中裸 `"400"`
      ⇒ 判成配置级 ⇒ Stage 2 **整份早停** + 提示"请检查 API Key"（用户遇到的只是
      一次可自愈的限流）；
      ② `ValueError("invalid literal for int() …")`、③ `KeyError("invalid key …")`
      命中裸 `"invalid"` ⇒ 本地代码缺陷被报成凭据故障。
      **修法**：判据抽成唯一入口 `is_config_error(exc)`，**结构化优先** ——
      先取 `exc.status_code`（取不到再退 `exc.response.status_code`），
      有状态码时**文本判据一行都不参与**（`{400,401,403}` 为配置级，其余一律可重试）；
      拿不到状态码才走文本兜底，且词表加 `\b` 词边界、裸 `"invalid"` 换成具体短语
      （`invalid api key` / `invalid token` …）；另加一条类型规则：
      **本地编程错误（`ValueError`/`KeyError`/`TypeError`/…）永不算配置级**。
      **测试**：新增 12 例（**含 401/400 对照组**，防"为修误报一刀切成可重试"，
      那会让 #127 的假阴性回归）；**两轮变异验证**（摘掉结构化判定 → 3 例红；
      词表退回裸状态码 → 2 例红）；同一探针复测 **8/8 正确（原 5/8）**。
      ⚠️ **登记一条实测副产物**（见 #174）：`anthropic`/`openai` SDK **自带**
      `max_retries`，与我们的重试循环**相乘**。
- [ ] **#144【P2】分片路径未传 `config_error` ⇒ #127 的 job 级首因在 `OCR_SLICES>1` 下失效。**
      `engine.py:527-534` / `:650-660` 两处 `_analyze_one` **无 `config_error=` 实参**，
      而 `stage2.py:151` 是**唯一**传参点 ⇒ `stage2.py:56` 的 `escalate` 恒 `False` ⇒
      403/401 的具体原因只落在页级 `_error`，`jobs.error_message` 只剩通用句。
      **修法**：分片路径建 `config_error: dict` 并传给两处 `_analyze_one`；更稳的做法是把
      "失败性质"提升为 job 级列（`error_kind`），消除"每个调用点都得记得传"的结构性遗漏。
- [ ] **#145【P2】日志：root 永远 DEBUG + 第三方 INFO/DEBUG 全量落盘 + 原始 LLM 输出写日志。**
      `logging_config.py:119,141` 把 root 与 file handler 都钉在 DEBUG；`level` 参数**只作用于 console**，
      无 `PBC_LOG_LEVEL`；第三库（httpx/httpcore/openai）DEBUG 经 root 全进 `pharma.log`（10MB 很快滚完）。
      `llm/client.py:361-365,393-397,477` 把**原始模型输出前 200 字**写日志（含批记录正文）。
      Electron 侧 `electron/main.js:59-61` 的 `backend-boot.log` **无大小上限**。
      **修法**：显式 `setLevel(WARNING)` 给第三方 logger；抽 `PBC_LOG_LEVEL` 供 file handler 用；
      raw 只记长度/摘要哈希；`backend-boot.log` 加轮转。
- [ ] **#146【P2】页级失败原因未脱敏，直达复核 UI（job 级已脱敏，口径不一致）。**
      `stage2.py:61-66` 的 `"_error": str(exc)[:200]` 未过 `_mask_secrets`/`redact_urls`；
      而 job 级 `engine.py:366-370` 做了两层脱敏。该字段经 `review.js:899-902` 直接上屏，
      并随报告/DB 长期留存。**修法**：改为 `redact_urls(_mask_secrets(str(exc)))[:200]`。
- [ ] **#147【P2】重试不清 `failed_pages` / 阶段耗时 ⇒ 重跑期间 UI 展示上一轮失败页。**
      `actions.py:85-88`（只清 `error_message`/`finished_at`）、`engine.py:165-169`（只清三列），
      而 `failed_pages` 要到 `stage3.py:328-339` 才覆盖。
      **修法**：retry 与 pipeline 启动时把 `failed_pages`、`stage1/2/3_ms` 一并置 NULL（旧值留审计）。
- [ ] **#148【P3】报告不携带失败信息（GMP 导出件）。**
      `api/report.py:228-238` 的 `job` 块只有 status/页数/耗时；MD 报告只有"页面覆盖"计数（`:348-354`）。
      **修法**：`report.json` 增加 `error_message`/`failed_pages`/`pages_analyzed`；
      MD 头部增加"失败页清单 + 任务状态"。
- [ ] **#149【P3】其余后端小项。** ① `core/health.py:48-49` OCR 探测异常**未走** `_mask_secrets`
      （与 LLM 分支 `:133` 不对称）；② `db/schema.sql:6` 的 `status` **无 CHECK 约束**，
      3 处直写 UPDATE 绕过状态机（`state.py:198-204`、`watchdog.py:375-381`、`engine.py:339-344`）；
      ③ `/api/health/watchdog` 的 `stall_limits_s` 只是**基准值**（未叠加 `per_page_s × 页数` 与 `cap_s`），
      名字易被现场误读为"最终阈值"，建议补 `effective_limit_s_example`。

### F3. 接口一致性（DX）

- [ ] **#150【P2】健康探针路径不一致。** 主探针在 `/health`，子探针在 `/api/health/watchdog`
      与 `/api/health/downstream` —— **`/api/health` 本身不存在（404）**。
      我自己写的探针就踩了这个 404（并因 `stdout=PIPE` 未排空而二次死锁）。
      **修法**：`/api/health` 加一个别名（或统一到 `/api/health`）；并在 `README`/`DEPLOYMENT`
      的排障段写清三个探针的确切路径与各自含义。

### F4. LLM provider 兼容（结论：实现完整，缺实测）

- [x] **实现层面已完整**（本轮核验）：协议白名单（`api/settings/write.py:320-324`）、
      设置页 select（`static/settings.js:116-118,150-152`）、两个适配器
      （`llm/adapters/openai_adapter.py` / `anthropic_adapter.py`）、
      依赖精确锁定（`requirements.txt` 的 `anthropic==0.109.2`，本机 `find_spec` 已装）。
      Anthropic 的四处协议差异（`x-api-key`、`anthropic-version`、顶层 `system`、
      `content[0].text` / `input_tokens`）都处理了；`response_format` 在 anthropic 侧
      **显式忽略**并说明由 prompt + 客户端修复链承担 —— 合理。
- [ ] **#151【P2】anthropic 协议的「厂商真机」从未被实测。** 本机只配了
      `siliconflow` / `deepseek`（两者 `protocol=openai`）⇒ 该分支原先**零运行证据**。
      ✅ **2026-09-18 已补上"协议层"的运行证据**：新增
      `tests/integration/test_anthropic_protocol_live.py` —— 起一个忠实于
      Anthropic Messages API 的**本地协议桩**，让**真实** `AnthropicAdapter` +
      `LLMClient` 走**真实 socket**（不是 mock 掉 SDK 调用），逐项验证：
      `x-api-key` 头（且**不得**出现 `Authorization`）、`anthropic-version`、
      `system` 是**顶层字段**（不在 messages 里）、响应取 `content[].text` 拼接、
      `input_tokens`/`output_tokens` → prompt/completion/total、`response_format`
      被忽略（不透传）；失败侧验证 401 → `LLMConfigError` 且**不重试**、
      429/500 → 可重试。7 例；变异验证（用量字段退化成 openai 字段名 ⇒ 红）。
      ⚠️ **仍未完成的验收**：与 `api.anthropic.com`（或任一真实 anthropic 协议
      服务）的实际连通 + 一轮真实分析 —— 那需要真实 key 与出网。
      **本条目保持未勾**，不得用协议桩冒充厂商实测。
- [ ] **#174【P2】两层重试相乘：SDK 自带 `max_retries` × 我们的重试循环。**
      **实测**（Round 39，`tests/integration/test_anthropic_protocol_live.py` 的协议桩）：
      `LLMClient.chat(retries=2)` 打一个持续返回 429 的桩，桩收到 **6 个请求**
      —— 因为 anthropic SDK 默认 `max_retries=2`（每次 `create` 最多 3 次尝试），
      两层相乘。openai SDK 同样是默认重试。
      **为什么值得修**：① 单页 LLM 的**最坏耗时没有单一真值** —— `client.py` 里
      "单页最长 240s" 的注释只算了我们这一层；② 看门狗阈值是**派生量**，
      派生依据若少算一层，静默上界就会被低估（本项目已因同类问题翻过车）；
      ③ 重试策略有两处真值，改一处不生效。
      **修法（择一，需先定案）**：SDK 侧 `max_retries=0`，让**我们的循环成为唯一
      重试权威**；或反之（SDK 重试、我们只做分级）。⚠️ 若选前者，必须同步复核
      `client.py` 里 timeout 分支"跳过客户端重试"的理由是否还成立。
      ⚠️ 本条**不在本轮修**（会改变时延行为，须与 e2e 超时预算一同复核），
      且现有用例刻意只断言 `>= 2`、不锁精确次数 —— 免得把未定案的实现细节固化成契约。
- [ ] **#152【P3】未覆盖其它主流协议。** 现仅 openai / anthropic 两种；
      Gemini（`generateContent`）、Azure OpenAI（`api-key` 头 + `api-version` 查询参数）、
      Bedrock 均不支持。**按需**再评估，不预先实现。

### F5. 流式输出（对比市面主流）

- [ ] **#153【P2】缺"断点续传"。** 聚合流 `/api/jobs/live` 只发无名 `data:` 帧，
      **无 `event:`、无 `id:`** ⇒ 断线重连后客户端无法用 `Last-Event-ID` 增量补齐
      （主流 SSE 做法：命名事件 + `id:` + 服务端保留短期事件缓冲）。
- [ ] **#154【P2】缺心跳注释帧。** 无 `:keepalive`（主流做法是每 15–30s 发一行注释帧）
      防中间代理按空闲超时断连。本机直连风险低，**一旦经反向代理/网关部署就会暴露**。
- [ ] **#155【P3】`done` 命名事件可能被静默忽略（范围已收窄）**。单任务流
      `/api/jobs/{id}/stream` 会发 `event: done`（`api/jobs/status.py:422`）。
      ✅ **2026-09-20 复核：`review.js:437` 已有 `es.addEventListener("done", …)`**
      ⇒ **复核页不受影响**（原登记的"会漏"对复核页不成立）；
      隐患**仅剩 `upload.js`**（它只监听 `es.onmessage`，`:488`），
      而 `upload.js` 当前**并未使用该端点** —— **一旦改用就会漏**。
      动作：给 `upload.js` 补 `addEventListener("done", ...)`（或改用该端点之前先补）。
- [x] **已做对的部分**（勿重复改）：`requirements.txt` 把 `starlette>=0.47` 的理由写清楚了
      （更早版本会把 SSE 帧缓冲到 ~64KB ⇒ 进度看似冻结后一次性涌出）；
      `review.js` 主动 `close()` + 手动指数退避 2/4/8s + 10s 兜底轮询；
      `EventSource` 原生自动重连 + 后端 `retry:` 帧双保险。

### F6. 规则集（主体合理，缺口具体）

- [ ] **#156【P2】多个规范类型没有确定性规则。** 22 条规则覆盖 **17 / 21** 类
      （✅ **2026-09-20 实测更正，原写 16/21**。实测方法：从 `core/rules/*.py` 提取
      `"type": "<x>"` 字面量与 `CANONICAL_TYPES`（21 类）取交 ⇒ **17**；
      规则注册表 22 条 = R1a/R1b/R2/R4/R5/R9a/R10/R6/R7/R8/R9/R8b/R-M1/R-M2/R3/R11–R17）。
      **真正"应补未补"的只有 `ocr_noise` 与 `signature_mismatch`**；
      `user_rule` / `uncategorized` 无规则属**设计如此**（前者靠用户规则、后者仅 LLM）
      ⇒ **不要为凑数给它们硬加规则**。
      其中 `ocr_noise` 缺失最值得补 —— 与我们刚实测到的**数字误读**直接相关（见 F7）。
- [ ] **#157【P2】R12 自检自核只做页内 step 粒度。** `core/rules/rule_gmp.py:213-235`
      比对"同一 step 内 `操作人 == 复核人`"，**无跨页/跨工序**的同人复核判定
      ⇒ "独立复核"失效在跨页情形下漏检。
- [ ] **#158【P3】阈值风险复核（逐条实测，别凭注释改）。**
      ① R3 `_EDGE_MARGIN = 0.10`（`rule_spec.py:202,222`）把相对偏差 ≤10% 一律降 info；
      ② `_decimal_loss_factor` 允许 10×/100× 缩放（`parsing.py:558-608`）⇒ 可能放过真实 10 倍超差；
      ③ R4 `year < 2000`（`rule_time.py:283`）会误报合法引用的旧年份（稳定性/历史物料）；
      ④ R7 编辑距离 ≤1 吸收（`rule_doc.py:226-235`）⇒ 若真实混批差异恰为 1 位，可能被当"OCR 变体"吃掉；
      ⑤ R10 缺号在 `len(missing)*2 > len(nums)` 时降 info（`rule_time.py:518`）。
- [x] **设计上做对的部分**（勿重复改）：`spec_guard.py` 用**同一个解析器**复核 LLM 自报的
      `param_out_of_spec`，合规即抑制并留痕 —— 这是"LLM 提议、确定性复核"的正确形态；
      三色按 **source** 分层（`finding_quality.py:197-213`）且**未知非空来源保守归 `llm`**；
      `rule_coverage()` 提供 `pass` 层（"系统校验通过"不来自单条 finding）。

### F7. 精确性与准确性（本轮**最有价值**的发现）

> **结论：当前精度瓶颈在 OCR，不在规则。** 我用视觉能力直接读原始扫描页
> （`test1.jpg`，真实批记录），与库里的 OCR 文本**逐格比对**，同一页上就有 4 类系统性错误：

| 位置 | 视觉真值 | OCR 读出 | 会被哪条规则放大 |
|---|---|---|---|
| 干燥温度行（规格 50-60 °C） | `50 52 54 54 … 55 58` | `50 **32 34 34 35 36** 55 58` | **R3 → 5 条假阳性超差** |
| 收率公式分母 | `30.0 kg` | `**930.0**` | R11 物料平衡：21.5/30.0=71.67% ✓，21.5/930=2.3% ✗ |
| 记录者姓名 | `眭东鹏` | `薛东鹏` / `薛杰鹏` | R5 / R9 / R12 签名比对假阳性 |
| 时间 | `02:03` / `06:12` | `02:23` / `05:12` | R1 / R5 时序假阳性 |

- [ ] **#159【P0，但依赖外部】建立在**数值单元格**上的"视觉第四读"。**
      这不是要重做多模态替换（`docs/VISUAL_CROSSCHECK.md` 已定案：多模态**不能当降噪手段**，
      第 9 页三方 72/72 逐格一致、替代净损坐标/互证/成本 ≈4.2×），而是**定向互证**：
      只对 **数字 / 时间 / 姓名** 三类单元格，让视觉模型复核 OCR 抽取结果，
      **不一致时降级为"待人工核对"而非直接采信任一方**。
      这与 **B3** 的既有结论完全吻合，但本轮给出了**具体的触发条件与真实反例**。
      **验收**：对 `test1.jpg` 这一页，能给出"温度行 5 个单元格 OCR 与视觉不一致"的显式输出。

      > **2026-09-18 补充实测（LLM 恢复后立刻做的定点验证，把本项的"待定"变成"已定"）：**
      > 用 `scripts/probe_vlm_fields.py` 把 `test1.jpg` 交给 7 个候选 VLM，只问上一轮
      > OCR 读错的 13 个字段（含那 4 类系统性误读），与**人工视觉真值**逐格比对：
      >
      > | 模型 | 正确/13 | 3 次重现 | 耗时 |
      > |---|---|---|---|
      > | `zai-org/GLM-4.5V` | **12** | 12/12/12 | ~8s |
      > | `Qwen/Qwen3-VL-32B-Instruct` | **12** | 12/12/12 | ~8s |
      > | `Qwen/Qwen3-VL-32B-Thinking` | **12** | — | ~9s |
      > | `Qwen/Qwen3-VL-30B-A3B-Thinking` | **12** | — | ~7s |
      > | `Qwen/Qwen3-VL-8B-Instruct` | **12** | — | ~11s |
      > | `Qwen/Qwen3-VL-30B-A3B-Instruct` | 11（温度54→55） | — | ~10s |
      > | `Qwen/Qwen3-VL-8B-Thinking` | 10（04:10→09:10、06:12→09:12） | — | ~11s |
      > | `Qwen/Qwen3-Omni-30B-A3B-Instruct` | **5**（整行错位，不可用） | — | ~3s |
      >
      > **关键结论（三条，都推翻或收紧了旧假设）：**
      > 1. **VLM 直读像素确实能读对 OCR 读错的东西**：上一轮 OCR 把温度读成
      >    `32/34/34/35/36`、投料量读成 `930.0`、时间读成 `02:23/05:12`，
      >    而 5 个模型**全部读对**了这些。⇒ 视觉第四读**技术可行**，#159 有落地依据。
      > 2. **但成本优势不存在**：`Qwen3-VL-8B`（12/13、10.8s）与 `32B`（12/13、7.7s）
      >    **准确率相同、8B 还更慢**（排队效应）。⇒ 没有"小模型够用"的省钱空间，
      >    选型应按**可得性与稳定性**而非参数量。
      > 3. **13 个字段里唯一稳定读不出的是中文人名**：7 个模型给出
      >    `胖东鹏/胖东朋朋/胖乐鹏/薛东鹏/周小英` —— **没有一个读对"眭"**。
      >    ⇒ **#160 的降级范围应把"姓名"排除出"可由视觉互证裁决"的类别**，
      >    姓名只能走"标记待人工核对"，不能指望任一模型拍板。这是本轮新增的硬约束。
      >
      > ⚠️ 另有一条**非阻塞但必须登记**的发现：`Qwen/Qwen2.5-*` 系列（共 7 个，
      > 含 Pro/LoRA 变体）**不是 VLM**，API 直接返回
      > `20041 The model is not a VLM`。设置页若允许用户凭列表任选，会给出运行时才爆的错。
      >
      > ⚠️ **工程缺口**：产品链路目前**根本喂不进图像**——`llm/client.py:143`
      > 的 `user_content` 是 `str`，`llm/adapters/openai_adapter.py:49` 直接
      > `{"role":"user","content": user_content}`。要做视觉第四读，必须先扩这条签名
      > （涉及两个 adapter + 审计表）。**这是 #159 的真实工作量所在，不是调提示词。**

      > **2026-09-18（Round 39）落地进度 —— 签名已扩、原型已跑通，但未接入 pipeline：**
      >
      > ✅ **adapter 多模态签名已扩**（本项原先点名的"真实工作量"）：
      > `user_content: str` → `str | list[str | ImagePart]`，涉及 `base.py` /
      > `openai_adapter.py` / `anthropic_adapter.py` / `client.py`。协议差异**留在
      > adapter 里**（OpenAI 出 `image_url` data-URL；Anthropic 出 `image` +
      > `source.base64`）—— 把协议细节留给调用方，等于要求每个调用点各学一遍两套
      > 协议。**纯文本路径的请求体字节级不变**（有护栏锁定：不为支持多模态而把
      > 每一次普通调用都改成 parts 列表）。另修掉一个连带调用点：`chat_json` 的
      > fix-hint 重试原做 `user_content + "…"` 字符串拼接，list 形态下会 TypeError
      > ⇒ 改走 `append_text_part`（有护栏，且**变异模拟该形态果真报出**
      > `can only concatenate list (not "str") to list`）。
      >
      > ✅ **原型 `core/vision_crosscheck.py` 已实现并跑通真实调用**：
      > 只对**数值 / 时间**两类单元格互证，三态 `agree` / `disagree` / `unreadable`；
      > **不一致一律降级为「待人工核对」，不采信任一方**（OCR 会读错，VLM 也会 ——
      > 实测最多 12/13）；**中文姓名不由视觉裁决**（依 Round 35 实测硬约束），
      > 即使模型答得与 OCR 一致也**不确认**（防"猜中即静默通过"）。
      > 真实验收（`scripts/vision_crosscheck_demo.py`，走产品代码路径）：
      > `Qwen/Qwen3-VL-32B-Instruct` 对 `test1.jpg` 温度行 **7.3s** 返回
      > 「6 格中 **5 格** OCR 与视觉不一致」——序号 2–6 的 OCR `32/34/34/35/36`
      > vs 视觉 `52/54/54/55/56`，而序号 1 两侧同为 `50`（反例，说明不是"全判不一致"）；
      > 视觉读数与人工真值（52/54/54）**逐格吻合**。
      >
      > ⛔ **仍未做（故本项不勾）**：接入 job 流程（触发条件、"待人工核对"在前端的
      > 呈现）、**审计表记录**（把页图送给第三方模型是 GMP 需要留痕的事实）、
      > 以及 #160 的"不确定性 → 规则结论降级"联动。这些是独立的一步。
      > 现状定位：**原型**，不写库、不改状态、不影响任何既有流程。

- [ ] **#160【P1】把 OCR 不确定性与规则结论绑定。**
      现有 `extraction_uncertain`（`finding_noise.py:219-223`）只在 `kind=time` 且**该页有 OCR 告警**时降级。
      应扩到"数值单元格 OCR 低置信 / 与视觉互证不一致" ⇒ 对应规则 finding 降级为 info + 标注原因。
- [ ] **#161【P1】用真实标注样本替换合成 ground truth（缓解 C1）。**
      `docs/FINDING_GROUND_TRUTH.json` 是**合成件**，结论不可外推。
      我们现在有了**第一份真实标注**：`用户标注.png` 圈出 `▲pH 为 6.8~7.0`（规格）
      与 `中和后 pH 6.88`（记录值）—— 应作为**首个真实样本**登记，并在报告里
      **区分"合成集口径"与"真实样本口径"**，不再混用。
      ⚠️ 诚实登记：该样本的"应为 OOS 还是合规"**尚需与业务方确认**（6.88 落在 6.8~7.0 内），
      不得自行假定结论。

### F8. 文档（大体及时，有几处自相矛盾）

- [ ] **#162【P2】`docs/TODO.md` 内的远端值自相矛盾**（已在本次一并修正：统一为 `09e6623`）。
      建立习惯：**每轮收尾时只改一处"最后更新"行**，避免头部与 E 表各写一份。
- [ ] **#163【P3】陈旧注释与口径漂移。**
      ① `core/kb/retriever.py:5` 写 "29K chars"，实际语料约 **59.4K**；
      ② `core/rules/rule_doc.py:11` 注释称 "R8 low_confidence"，但 R8 实为 `completeness` 低置信度参数。
- [ ] **#164【P3】文档体量治理。** `docs/` 下 12+ 个 `.md`（含 `NOISE_REDUCTION_TODO.md`、
      `ROADMAP_v1.1.md`、`PLAN_v1.1_EXECUTION.md` 等存档件）。存档件**不再更新**的约定已写进
      `docs/TODO.md` 头部，但缺**机检**。**验收**：加一条"存档文件不得含 `[ ]` 未完成项"的护栏。

### F9. 知识库（结构完整，4 个键级缺口 + 1 处浪费）

- [ ] **#165【P2】`TYPE_QUERIES` 2 个死键。** `batch_logic`（`retriever.py:52`）与
      `low_confidence`（`:54`）**不在 `CANONICAL_TYPES`** ⇒ `normalize_finding_type`
      永不产出该 type ⇒ 词表永不命中。`GMP_BASIS_MAP` 另有 3 个非规范键
      （`gmp_basis.py:22,67,76`；其中 `time_anomaly` 靠同义词兜底才不空）。
- [ ] **#166【P2】2 个规范类型无词表。** `user_rule` / `uncategorized` 无 `TYPE_QUERIES` 条目
      ⇒ `query_for` 退化为泛词 `["批记录","记录"]`（`retriever.py:200-202`），检索质量形同随机。
- [ ] **#167【P2】缺护栏。** 加一条断言 **`TYPE_QUERIES.keys() ⊆ CANONICAL_TYPES`**
      且**每个 `CANONICAL_TYPES` 都有词表**的机检 —— `docs/TODO.md:101` 已自认"不检查键的有效性"，
      这正是 F1 那两个 P0 的同款缝（**护栏通过但接线是断的**）。
- [ ] **#168【P2】每页 KB 检索跑了两次。** `core/page_analyzer.py:489-508` 为构造 prompt 检索一次，
      `:560-571` **又检索一次**（仅用于 `:611`/`:716` 的 `kb_used` 审计留痕），第二次 `kb_block`
      还覆盖了第一次。**修法**：复用第一次的结果做审计。
- [ ] **#169【P2】`kb_prompt_inject` 设置开关不可达（C4 的具体化）。**
      **注入代码是活的**（`page_analyzer.py:490/561` 真实调用，默认 `True`），
      **但开关是死的**：`api/settings/write.py:37-64` 的 `_STATIC_FIELDS` 不含它，
      未知字段在 `:315-316`（`prov_name is None`）**静默丢弃**，read 接口与 UI 也无此控件。
      ⇒ 只能靠 env/config.json 切换。**修法**：接通或删除，别留着当装饰。
- [ ] **#170【P3】主源 `gmp2010`（313/441 条）缺 `license` 且 `raw/` 下没有对应 md**
      （5 个 md 覆盖其余 5 源）⇒ 主源**无法从仓库复现**，只能经 Word COM 重建。

### F10. 工程实践（一条通用护栏，能一次性堵住上面那类缝）

- [x] **#171【P1】"前端消费的字段必须在它实际的数据源里存在"的契约机检。** ✅ **2026-09-18 已建**
      这是本轮**反复出现的缺陷模式**：`test_config_error_visibility.py:198` 只断言
      `"job.failed_pages" in js`（**字符串存在**），而 `listings.py:46` **根本不返回该字段**
      ⇒ 护栏全绿、缺陷照旧（P0 #134 与 #133 都从这条缝漏过）。
      **做法**（`tests/integration/test_frontend_field_contract.py`，15 例）：
      字段集**不手写**，而是从 `upload.js` 的行构建函数体里**派生**
      （`renderJobRow` / `buildMetaLine` → `job.*`；`buildRowFromSnapshot` /
      `updateJobRowLive` → `d.*`），再对**三个真实数据源**
      （`GET /api/jobs`、`GET /api/jobs/{id}`、SSE 快照 `_get_job_progress`）
      核对**存在性 + 类型 + 取值**，并**跨来源比对**类型与取值一致性
      （"同一字段在详情与列表行为不一致"正是 #132/#134 的成因）。
      另有 `TestSsrBridgeContract` 把 `window.__PBC__` 也当作数据源，
      锁"review.js 的 `ctx.*` 消费集 ⊆ 桥接注入集"。
      **自扩展**：前端新增消费一个字段，护栏立刻要求对应端点提供它。
      **验收（变异验证，逐条实测）**：
      | 注入的缺陷 | 护栏反应 |
      |---|---|
      | SELECT 去掉 `ocr_backend_used` | 存在性红 + **跨来源一致性也红**（列表 None / 详情 str） |
      | `failed_pages` 改回字符串透传 | **三条**用例红（类型 / 一致性 / 定点 list 断言） |
      | `listings.py` 内联 `json.loads` | 结构侧红 |
      | `__PBC__` 去掉 `page_ocr_empty` | 两条红 |
      ⚠️ **变异验证当场揪出护栏自身的漏洞**：第一版用
      `assert "json.loads" not in src` 查"第二套口径"，而变异写成 `_j.loads(...)`
      （只差别名字符串）**就溜过去了** —— 已改为走 **AST** 认调用形态
      （`ast.Attribute.attr == "loads"` 或 `Name.id == "loads"`），别名/import 方式都挡得住。
      ⇒ **护栏必须做变异验证**：写完就绿的空护栏比没有护栏更危险。
      **产出**：见 #172（列表缺 5 个行字段）与 #173（`page_ocr_empty` 漏桥接）
      —— **两条都是本护栏发现的真实缺陷**，这正是它存在的意义。

---

### F11. 下一轮开工顺序（建议）

1. ~~**先修 2 个 P0（#133 / #134）**~~ ✅ **2026-09-18 已完成（`177a6d9`）** ——
   两条都做了负例验证；全量 2651 passed。**下一步转 2。**
2. ~~**同步修 P1 的错误可见性（#135 / #136）与后端并发（#140 / #141 / #142）**~~
   ✅ **2026-09-18 已完成（见本轮提交）** —— 5 条全部修完，每条都做了**负例验证**；
   全量 **2658 passed / 3 skipped**（较上轮 2651 +7 = 2 级联取消护栏 + 3 launch 失败护栏
   + 4 失败页渲染护栏，其中 2 条合并计数）。**下一步转 3。**
3. ~~**加 #171 契约机检** —— 先补护栏，再改代码，避免修完又漂。~~
   ✅ **2026-09-18 已完成** —— 契约从 JS 函数体**派生**（不手写）+ 三个真实数据源断言 +
   跨来源一致性；`window.__PBC__` 也纳入契约。**护栏建成即发现两条真实缺陷**：
   **#172**（列表端点还差 5 个行字段，`ocr_backend_used` 导致老任务永久丢失 OCR 追溯标签）
   与 **#173**（`page_ocr_empty` 漏桥接 ⇒ OCR 空页被显示成"本页无问题"，是上一轮 #136 的残留）。
   三条都已修 + 五轮**变异验证**（含"变异揪出护栏自身字符串匹配漏洞 → 改走 AST"）。
   **下一步转 4。**
4. ~~**#143 负例测试** —— 它现在会把 429/本地错误误报成"请检查 API Key"，误导排障。~~
   ✅ **2026-09-18 已完成** —— **先定位**：用真实 SDK 异常对象跑 8 个场景，
   实测 **3 例误判**（真实 `429 … Limit 40000` 命中裸 `"400"`、本地
   `ValueError("invalid literal …")` / `KeyError("invalid key …")` 命中裸 `"invalid"`），
   其中 429 那条会让 Stage 2 **整份早停**并提示"请检查 API Key"。
   **再解决**：判据改「结构化优先（`status_code`，含 `response.status_code` 回退）
   + 词边界文本兜底」，并明确"本地编程错误类型永不算配置级"。
   **最后测试**：新增 12 例（含 401/400 对照组，防"一刀切改成可重试"）；
   两轮**变异验证**（摘掉结构化判定 ⇒ 3 例红；词表退回裸状态码 ⇒ 2 例红）；
   复测同一探针 **8/8 正确（原 5/8）**。**下一步转 5。**
5. ~~外部解阻后再做 **#151（anthropic 实测）** 与 **#159（视觉第四读原型）**。~~
   ✅ **2026-09-18 已完成**（A4 已解阻）：
   - **#151**：本机无 anthropic key ⇒ 起**忠实于 Messages API 的本地协议桩**，
     让真实 `AnthropicAdapter` + `LLMClient` 走**真实 socket** 往返，
     逐项验证四处协议差异 + 401/429/500 分级（7 例）。⚠️ 桩 ≠ 厂商真机，
     "与 api.anthropic.com 实际连通"仍未验证，条目**保持未勾**（见 F4 的 #151）。
   - **#159**：先扩 adapter 多模态签名（`user_content: str` →
     `str | list[str | ImagePart]`，**纯文本路径字节级不变**），再写
     `core/vision_crosscheck.py` 定向互证原型。**真实验收达标**：对 `test1.jpg`
     温度行给出显式输出「6 格中 **5 格** OCR 与视觉不一致」（序号 1 两侧同为 50，
     作反例），视觉读数与人工真值逐格吻合。
     ⚠️ **原型未接入 pipeline**（不写审计表、不改 job 流程）—— 接入是 #160 的活。
   **下一步转 6。**
6. **收尾：先重建产物 → 再跑门禁 → 推送到干净的 worktree**（`tests_coverage` 含分发一致性，
   升版未重建必然变红，那是**正确信号**）。见 F11 之后的状态行。

> ✅ **产物已重建**（2026-09-18，v1.1.9）⇒ #133–#142 + #172/#173 + #143 共 **11 条**
> 已进入可分发产物。构建顺序严格照第 6 步：**Tailwind → PyInstaller → electron-builder
> → 产物核验 → 门禁 → 推送**。
>
> **实证（全部实测，非推断）**：
> - Tailwind exit 0 → PyInstaller exit 0（`dist/pbc-server/pbc-server.exe` 20.3 MB）
>   → electron-builder exit 0（19s，**日志无任何 download 行** = 命中缓存）。
> - 冻结点烟：`/health` → `{"status":"ok","version":"1.1.9"}`；**Electron 资源里那份 exe
>   也实跑过**（不只验字节一致）—— 起服 3.1s，`/api/health/watchdog` 回传 `ocr_running: 4200`
>   （符合阈值不变式），隔离库 `total_jobs: 0`。
> - 目录唯一性：`clean_dist.py` dry-run → **待清理:(无)**、`dist-electron` 状态「完整」版本 1.1.9；
>   **未产生**多余的 `dist-electron/resources/`（`ls dist-electron/` 仅 `builder-debug.yml` + `win-unpacked/`）。
> - `asar_version()` **子进程读** = `1.1.9`（绝不用宿主读取 —— 那会锁死 asar，是目录堆积的复发机制）。
> - `test_distribution_parity.py` **13 passed / 0 skipped**（无产物时其中 3 条会 skip）。
> - 门禁六项 **OVERALL: pass（pass=8 fail=0 warn=0 skip=0）**，覆盖 **95.24%**，
>   报告 `devlogs/gate_report_20260918_112932.json`。
> - ⚠️ **新认知（省下下次 10 分钟排查）**：**用例数会随「产物是否存在」变化** ——
>   junit 审计副本实测 `tests=2713 failures=0 errors=0 skipped=0`（产物就位）；
>   同一提交在产物被清空时是 `2710 passed / 3 skipped`。**collect 数恒为 2713**。
>   看到 2713 vs 2710 的差异**不是回归**，先看 `dist-electron/` 在不在。
> - ⚠️ **踩坑记录（探针问题，非产物缺陷）**：产物冒烟必须传 **`PORT`**（`server.py` 读它，
>   默认 58765），**不是** `APP_PORT`（那是 `config.py` 的值）；隔离数据目录要**覆盖 `APPDATA`**
>   （`config.py:41`），**不是** `PBC_APPDATA`。写错变量名的后果很隐蔽：服务照常启动，
>   但监听在 58765（探针在 8123 上等 60s 超时），且**读写真实 `%APPDATA%\PBC`**。
>   （本次误用后已核验：`data.db` 与 `data.db-wal` mtime 未变，仅 `data.db-shm` 被触碰，无数据损失。）
>
> ⚠️ **外部阻塞**：
> ~~**A1**（`dist-electron` 被宿主进程持句柄）~~ ✅ **2026-09-18 已解除**（用户手工收敛）。
> ~~**A4**（SiliconFlow 余额）~~ ✅ **2026-09-18 已解除**（key 恢复 200），
> 多轮 e2e 与真实 VLM 调用均已跑通。
> **仅剩**：anthropic 的**厂商真机**连通性（需真实 key 与出网），见 #151。
>
> ✅ **第 6 步已闭环并推送**（2026-09-18）：`8f80a58..90e86a3  main -> main`
> （经代理 `127.0.0.1:7897`；同刻直连 `Recv failure: Connection was reset`）。
> 推送的两个提交：`361c8fd`（#143/#151/#159 + 升版）、`90e86a3`（本轮实证文档）。
> **F11 第 1–6 步全部完成。**
>
> **下一轮候选**：#127（全局性故障被降级为页面级失败 → 界面绿点假阴性，**仍未修**，
> 需升版重建）、#174（两层重试相乘）、#125（巡检首扫空窗）、#126（第 2 份真实批记录泛化）、
> #151（anthropic 真机）、#159 接入 pipeline（= #160）。

---

## v1.2.0 迭代清单（Round 40 对抗性审查产出 · 基线 v1.1.9）

> 依据：`docs/ADVERSARIAL_AUDIT.md` 的「第二轮对抗性审查（Round 40）」。
> **判定：v1.1.9 无 P0 阻塞，可作为分发基线。** 以下为下一版迭代项。
> 纪律：每条都要求**先定位 → 再解决 → 最后测试（含变异验证）**；改了进入产物的东西 ⇒ **必须升版 + 重建产物**。

### B0 基线冻结（先做，代价低）

- [x] **B0-1 给 v1.1.9 打 tag 并写分发说明** —— ✅ **2026-09-18 完成**
      （附注 tag `v1.1.9` → `1ee2c17`，与现有 4 个 tag 同为 annotated；tag 正文含
      代码面 / 实测证据 / 密钥卫生 / **证据边界** 四段）。
      证据：版本四处一致（`main.py` / `package.json` / `package-lock.json` 两处 /
      `PORTABLE_README.txt`）、`dist-electron` 唯一且 dry-run 报「待清理: (无) · 完整 · 1.1.9」、
      asar 子进程读 = 1.1.9、内嵌 exe 实跑 `/health` 通过、门禁 8 项全绿（95.24%）。
      动作：`git push origin v1.1.9`（随本轮分支提交一并推）。
      ⏳ **仍待用户决策**：`v1.1.3`–`v1.1.8` 的 retro-tag **未创建**（现有 tag 只到 `v1.1.2`）。
      候选锚点已用 `git log -S 'APP_VERSION = "1.1.x"'` 定位（确定性、非猜测）：
      v1.1.3→`e6b9fce`、v1.1.4→`cad2544`、v1.1.5→`c48bdca`、v1.1.6→`5b9f534`、
      v1.1.7→`cc55eeb`、v1.1.8→`36569ae`。
      ⚠️ 但"首次出现的提交"≠"发布时刻的 tip"（其后可能仍有属于该版本的提交）⇒
      **retro-tag 是对发布历史的公开声明，不由我单方面决定**，请用户确认口径后再补。

- [x] **B0-2 记录"分发基线"的诚实边界** —— ✅ **2026-09-18 完成**
      在 `DEPLOYMENT.md` 新增「**本分发基线（v1.1.9）的证据边界**」小节，紧邻
      「分发前检查清单」，分两栏：**已在本机实测**（产物结构 / 版本四处 / 内嵌 exe 实跑 /
      门禁 95.24% / 产物二进制无密钥）与**未验证（不得当作已通过）**
      （无他机验证、未在 v1.1.9 产物上重跑 51 页全链路、Anthropic 真机未连通、
      无标注集故 P/R 无可信数字、200 页上限仅线性外推）。
      验收达成：新接手的人只看 `DEPLOYMENT.md` 就能知道哪些结论未被验证。
      ⚠️ **Round 42 更新**：其中「未在 v1.1.9 产物上重跑 51 页全链路」一条**已于本轮消除**
      （见 B0-3），`DEPLOYMENT.md` 的「未验证」栏已同步收敛。

- [x] **B0-3 `#149` 产物级端到端测试**（唯一未验的执行项）—— ✅ **2026-09-18 完成**
      被测对象：`dist-electron/win-unpacked/resources/pbc-server/pbc-server.exe`
      （sha256 `cfe28a30…`，20 340 967 B）—— **即用户双击运行时内嵌的那一份**。
      驱动：`tests/e2e_run.py`（`PBC_E2E_EXE` 指向上述 exe；driver 自行以
      `APPDATA=%TEMP%/pbc_e2e_appdata` + `PORT=58799` 隔离）。
      **结果：7 个轮次全过、`ALL ROUNDS PASSED`、exit=0**：

      | 轮次 | 终态 | 耗时 | 页 | findings | 类型 | gmp_basis |
      |---|---|---|---|---|---|---|
      | `pdf` | review | 285s | 6 | 27 | 7 | 27/27 |
      | `img` | review | 120s | 1 | 17 | 4 | 17/17 |
      | `cancel` | cancelled | 9s | — | 0 | — | — |
      | **`real`** | **review** | **1049s** | **51** | **293** | **14** | **293/293** |
      | `rot` | review | 100s | 4 | 16 | 3 | 16/16 |
      | `robust` o6（小字号低 DPI） | review | 213s | 1 | 1 | 1 | 1/1 |
      | `robust` o1（小框） | review | 33s | 1 | 1 | 1 | 1/1 |

      **独立核验（不采信 driver 自述）**：
      - findings **直查隔离库** 与 driver 逐条一致（27/17/0/**293**/16/1/1）；
        real 轮 **缺 `gmp_basis` 的条数为 0**（证明 293/293 有依据）。
      - SSE 帧数吻合（143 / 61 / 5 / **507** / 51 / 107 / 18），phase 链均为
        `ocr → analyze → cross → done`（cancel 为 `ocr → idle → done`）。
      - `ocr_backend_used` **全部 `paddle`**、`backend_mismatch=null`
        ⇒ 无「Paddle 失败静默 failover 到 MinerU」的掩盖。
      - `error_message=null`、`failed_pages=null`、7 个 job 全部 `terminal=True`。
      - 审计留痕：`audit_log=89` / `llm_call_audit=71` / `finding_suppressions=15`。
        rot 轮的旋转自愈**有审计**：`action=stage1_rotation_recovered,
        recovered_pages={2: 90, 3: 90}`，且 `rot_lost=[]`（内容未丢失）。
      - **污染核查**：真实 `%APPDATA%/PBC/data.db` mtime 仍为 **09-16 10:12**
        （`-wal` 09-16 11:06）⇒ 本轮 e2e **未写入真实库**。
      - **字节一致**：`win-unpacked` 内嵌 exe 与 `dist/pbc-server/pbc-server.exe`
        **sha256 完全相同** ⇒ 分发件 = 构建产物。
      - robust 契约满足：`o6_page1_signal=true`、`integrity=incomplete`、
        `reasons=["小字号（4.0pt < 6pt）叠加低 DPI（72.0 < 150），识别风险高，需人工复核"]`
        ⇒「不可无损修复页不得静默标记成功」成立。

      ⚠️ **测试保真度留白（如实标注）**：driver 在 `e2e_run.py:308` **硬编码**
      `siliconflow_model = "Qwen/Qwen2.5-72B-Instruct"`，而生效生产配置是
      `SILICONFLOW_MODEL = deepseek-ai/DeepSeek-V3.2` —— 两者均走 OpenAI adapter、
      协议一致，故链路验证有效，但**「生产模型」这一具体路径本轮未覆盖**
      （已登记 B5-4）。

### B1 精度与准确性（用户最关心，优先级最高）

- [x] **B1-1 同根因 finding 聚合**（✅ **2026-09-21 Round 53 已修**）
  现象：单个 OCR 误读会在每个时间点各产 1 条 finding。旧版实测 `4.6→46` 生成 **9 条**
  （7 warning + 2 critical）；当前版本 severity 已正确降级为 info，但**条数不变**。
  证据：`core/rules/rule_spec.py:228` 逐值判定，全链路无聚合层；旧 job `0c5cfb00` 实测
  p8 命中 9 条、全 job **442 条**。
  **定位（Round 53，`devlogs/_verify/probe_b11.py`，两个真实语料）**：
  - job `6f80145a-47f`：p8 `进料压力` 在 7 个时间点均为 `46bar` ⇒ R3 产出 **7 条**；
  - job `0c5cfb00-897`（= 442 条那个 job，51 页缓存齐全）：R3 38 条、其中
    **p8 进料_压力 ×7** + **p33 D2101/D2102_真空度 各 ×8** ⇒ 三组共 23 条同根因副本。
  - ⚠️ **关键定位结论**：不能用描述文本反推聚合键 —— 粗口径（page+type+前 60 字）
    在该语料上给出"12 组可合并"，其中**多数是假合并**（如"缺少 QA 签名"与
    "缺少复核人签名"是**不同根因**，合并会丢信息）。
  **处置**：新增 `core/rules/finding_aggregate.py`（**单一构造点 + 派生键**）：
  - `rule_spec._judge_param/_judge_cell` 的 4 个发射口统一走 `make_oos_finding`，
    同时挂上**不落库**的派生字段 `_agg_parts`（结构化部件）/`_agg_key`（分组键）；
  - 键含 page/type/kind/主语/**根因**/取值/单位/规格/提示语 ⇒ **取值或根因不同者不会被并**；
    位置（时间点）是变化维度，**故意不进键**；
  - `_grade_out_of_spec` = 判级**唯一**实现（返回 `severity/hint/cause`），
    `_severity_for_out_of_spec` 保留为 2 元组兼容包装（无第二份判级逻辑）；
  - 文案由 `render_description` **唯一**构造（聚合时只改"位置短语"，
    **不做文本裁剪/拼接**）⇒ 单条文案与原实现**逐字节相同**（真实库实测 **7/7 一致**）；
  - 合并后 severity 取组内**最高**；`ocr_text` 拼接全部成员明细（留痕）；
    `aggregate_by_root_cause` 返回**明细行**（组键/成员数/成员原文/severity 分布），
    主入口写 `logger.info` 台账。
  **实测（两个真实语料 × 官方重放路径，非单测）**：
  `devlogs/_verify/replay_b11.py`（复用 `scripts/replay_rules.py` 的 `load_job` /
  `run_rule_layer`，即**生产同一条 `analyze_cross_page` 链路**，LLM 两条支路置空），
  同输入、**仅切聚合开关**：
  - job `6f80145a-47f`（`devlogs/_replay/data.db`）：**493 → 487**（−6），1 组（p8 ×7）；
  - job `0c5cfb00-897`（真实库，51 页）：**426 → 406**（**−20**），3 组：
    p8 `进料_压力` ×7、p33 `D2101_真空度` ×8、p33 `D2102_真空度` ×8；
    severity 分布 `warning 198→184 / info 212→206 / critical 16→16`（**critical 零变化**）；
  - 单条等价性：`devlogs/_verify/verify_b11.py` 对照生产库 p8 的 7 条，
    description/ocr_text/severity **7/7 逐字节一致**。
  - ⚠️ **如实标注口径**：剩余 406 条里 R6 completeness / R9 handwritten / R1b 等
    **不是**同根因副本（各不相同的独立问题），聚合**不应**动它们 —— 故原始验收口径
    "442 条 → 显著下降"**不可直接套用**（442 条含 llm_page/llm_cross 与旧构建产出）。
    本条**可证伪**的效果量是"**同一列在每个时间点各一条**"这一类的 **23 → 3**。
  护栏：`tests/unit/test_finding_aggregate.py`（22 例：单条字节不变 / 真的合并 /
  **不误并**（取值·根因·页·种类·提示语五个反向对照）/ 留痕 / 派生键覆盖率）
  + `tests/unit/test_cross_page_entry_coverage.py::TestEntryPointAggregationWiring`
  （2 例，专防"机制写好但入口没接"的假绿）。
  变异验证：`devlogs/_verify/mutate_b11.py` **12/12 CAUGHT**（含"聚合键漏字段"4 条、
  "机制失效"4 条、"留痕失真"2 条、"入口不接线"1 条、"覆盖率护栏"1 条）。
  ⚠️ 变异过程中发现并修正**两处真实缺陷**：① 单值参数的"共 N 处"从位置列表推会
  **恒为 0**（措辞永不出现）⇒ 改用成员数；② "不同根因不合并"的**首版用例钉不住**
  `cause` 字段（靠 suffix 差异侥幸通过）⇒ 改用 `printed` vs `hard` 这一对
  （提示语**都是空串**，唯一能拦住合并的只有 cause）。
  全量回归：`2997 passed / 0 failed`（Round 52 基线 2881 ⇒ 新增用例随产物）。

- [ ] **B1-2 把 `core/vision_crosscheck.py` 接入 pipeline**（= #159 剩余 / #160）
  现状：原型已跑通（`test1.jpg` 真实 VLM 调用，输出显式不一致清单），**未接入**。
  动作：① 触发条件（只对**数值/时间**单元格，且该页存在可疑值时）；② 前端「待人工核对」呈现；
  ③ 写审计表（可追溯）；④ 保证**姓名永不由视觉裁决**、**空回答 ≠ 一致**。
  验收：端到端跑一次真实页面，界面上能看到「OCR 46 / 视觉 4.6 → 待人工核对」；
  变异验证：把"空回答当一致"改回 ⇒ 护栏变红。
  ⚠️ 成本已知（≈4.2×），**只做定向互证，不要全页替代**（见 `docs/VISUAL_CROSSCHECK.md`）。

- [ ] **B1-3 R4：建立真实标注集（精度度量的前提）**
  现状：**没有可信 P/R**；`docs/FINDING_GROUND_TRUTH.json` 是合成件，不可外推。
  动作：用「丝裂霉素提取批记录.pdf」+「试用批记录.pdf」人工标注**逐页**真值
  （至少：本页有哪些真实偏差、哪些是噪声），标注过程与判据写进文档。
  验收：能算出**可复现**的 P/R 与"每条 finding 的人工判定"；此后所有"提准"改动都必须挂 R4 数字。
  备注：**这是 B1-1/B1-2 效果的唯一可信裁判**，建议与它们并行推进。

- [x] **B1-4 LLM 的"日期 vs 当前日期"判据方向不稳定（P1，Round 42 实测新发现）**
  现象：同一次 51 页真实运行内，LLM 对同一类比较给出**互相矛盾**的结论，且把
  **当前日期误当成生产日期**。全部来源为 `source='llm_page'` / `'llm_cross'`（**不是规则**）。
  证据（real 轮 job `6f80145a`，均 `severity=critical`）：

  | 页 | LLM 断言 | 用视觉读原图核实的结果 |
  |---|---|---|
  | p2 | 「车间负责人审核日期 2025.01.30 **晚于**当前日期 2026.09.18」 | `p02.jpg`：该处手写**就是 `2025.01.30`** ⇒ 日期读对，**比较方向反了** |
  | p38 | 「复核者/操作者签名时间 **2027.01.17 早于**生产日期 2025年01月20日」 | 2027 明显**晚于** 2025 ⇒ 方向反 |
  | p49 | 「审核人签名日期 2025.02.24 **早于**当前年份 2026.09.18」 | 过去日期**本是记录常态**，不该判 critical |
  | p38 | 「生产日期 2025年01月20日与当前年份 2026年09月18日**不符**」 | `p38.jpg`：生产日期确为 `2025年01月20日`；二者不等是**必然**，非异常 |
  | p28 | 「记录的日期 2015-01-23 与**生产日期 2026-09-18** 矛盾」 | 与 p38 自述互斥 ⇒ **同一次运行内自相矛盾**（把当前日期当生产日期） |

  动作：① 把"日期语义"从 LLM 自由裁量改为**规则化判据**（给定 today、生产日期、
  各签名日期，用确定性比较得出结论，LLM 只负责**抽取值**）；
  ② 对"生产日期"这类**基准值**改为跨页**单源确定**（一次解析、全页复用），禁止各页各解一次。
  验收：构造上表 5 个场景 ⇒ **0 条 critical 假阳性**；变异验证：把基准值改回"每页各解"
  ⇒ 立刻重现 p28/p38 互斥。
  ✅ **Round 43（2026-09-20）二次确证**（独立读原图 + 直查隔离库）：
  - **方向反**：p2 原件该处手写为 **2025.09.30**（年份 2025），LLM 判「**晚于**当前 2026.09.18」；
  - **基准各页各解**（本轮最刺眼的一条）：p2 原图上「开始生产日期 **2025.01.20**」清晰可读，
    p38 的 finding 引用为「生产日期 **2025年01月20日**」（**正确**），
    而 **p28** 的 finding 却写成「与**生产日期 2026-09-18** 矛盾」（把**当前日期**当生产日期）
    ⇒ **同一次运行内同一基准值被解出两个答案**。

  **✅ Round 48（2026-09-20）修复记录**

  **定位（离线回放，`devlogs/_replay/probe_b14.py`，job `6f80145a`）**——比 Round 43 的
  单点确证更进一步，把**全文档**的基准分布拿下来了：
  - `page_info.production_date` 在 **51 页里有 23 种取值**；
  - 其中 **`2026-09-18`（= prompt 注入的当前日期）占了 7 页**（p8/p10/p19/p39/p41/p45/p46）
    ⇒ 基准值不是"偶尔读错"，而是**被当前日期系统性污染**；
  - ⚠️ **多数值本身是错的**（`2026-09-18` 7 页）⇒ **不能靠多数投票取真值**；
    且候选里 `2025-01-25`(7 页) 比 `2025-01-20`(6 页) 还多，批号后缀 `-04/-05/-06/-07`
    表明该 51 页是**多子批复合体** ⇒ **本就没有唯一真值**。
  - 旧实现只收掉 1 条（p2，因为它**方向恰好反了**）：**方向对的 22 条全部漏网**。

  **处置（`core/rules/llm_finding_guard.py`，两条判据都只否定可证伪的前提、绝不猜真值）**：

  | # | 判据 | 可证伪依据 | 落点 |
  |---|---|---|---|
  | ① | 比较的**参照物是「当前日期/当前年份」**⇒ 该比较不构成异常（**方向无关**） | 过去日期早于现在本是记录常态 | `_check_current_date_reference` |
  | ② | 引用的「生产日期 X」而 X 恰是**文档级单源**解出的当前日期 ⇒ 前提不成立 | 同一次运行内自述互斥 | `_check_production_date_premise` + `_document_current_dates` |

  - **① 的反向控制（不可省）**：记录内日期**晚于**自述当前日期 ⇒ 真未来日期 ⇒ **保留**。
  - **② 的"单源"= `_document_current_dates` 在入口处解析一次、全页复用**（禁止各页各解
    —— 那正是缺陷本身）。**只做否定、不做选择**：不尝试判定"真生产日期"，因为数据不支持
    （见上）。引用值与**本页**抽取不一致 ⇒ 降级（弱证据）。
  - **语义搬家（B3-4 同族）**：① 的语义**整体移出** `_check_declared_order`（它现在只管
    "两侧都是记录内日期 ⇒ 方向错则降级"）。旧实现把"参照物是墙钟"和"方向反"两个独立
    判据挤在一处，**必然**漏掉方向对的条目 —— 这是本缺陷的机制性根因，不只是漏了几个 case。

  **验收（`devlogs/_replay/verify_b14.py`，真实数据）**：
  - 落库 LLM 行 156 条：抑制 23 / 降级 22 / 保留 87；处置后 severity = `{warning:117, info:9}`。
  - **TODO 表格 5 场景逐条处置全部符合预期（5/5）**：p2 抑制、p38（2027）降级且线索保留、
    p49 抑制、p38（"不符"）抑制、p28 抑制。
  - ⚠️ **验收条款「0 条 critical 假阳性」是空断言**：L4 对 LLM 源**无条件**封顶
    ⇒ 任何输入都不会有 critical，该条无判别力。故本轮验收改以"**5 场景逐条处置**"
    为准，并把这一事实写进模块 docstring，避免后人再拿"0 critical"当通过依据。
  - **变异验证（`devlogs/_replay/mutate_b14.py`）4/4 通过**（每处变异只打红对应断言，
    逐字节还原自校验通过）：删未来日期反向控制 ⇒ 红；① 退化为只认方向词 ⇒ 红；
    单源池永不收集 ⇒ 红；参照物解析要求"年+月"（丢纯年份）⇒ 红。
  - **跨页单源的两条成对用例**：单独喂 p28 ⇒ 判不了（fail-open，只降级）；
    与 p38 的"当前年份 2026年09月18日"一起喂 ⇒ p28 被抑制。这条对
    `test_p28_suppressed_when_current_date_declared_elsewhere` 钉住了"单源"不变式。
  - 单测：`tests/unit/test_llm_finding_guard.py` 新增 `TestL3DateSemanticsB14`（9 条）。

  **登记的新问题（本轮暴露，未修）**：
  - **`year_vote` 的输入被污染的基准反喂**：`year_vote.date_strings()` 把
    `page_info.production_date` 当**证据源**，于是错误基准会"投自己一票"。
    实测年份层级仍侥幸正确（2025 得 36 页 vs 2026 得 8 页），但机制上是**依赖巧合**。
  - **无字面量的"当前年份"类条目仍 fail-open 留存**（p1×4「签名年份与当前年份不符」、
    p36「记录发放年份 > 当前年份+1」）—— 文案没给年份，无墙钟则无法证伪。属**已知留白**。
  - **p32「生产日期 2025.01.25 为未来日期」**：等价于"与当前日期比较"但**没有**参照物字面量
    ⇒ ① 接不住。"未来日期"这个措辞本身值得单独收权（见 B2 段）。

- [x] **B1-5 LLM 对"非法形态输入"无防线 + 跨页串位（P1，Round 42 实测新发现）**
  现象 A（**非数值当超差**）：`param_out_of_spec` 的"实际值"是 `'A'` 或一个日期字符串时，
  LLM 仍判 `critical`「不符合规格范围」，**不质疑输入合法性**。
  证据：p17 `llm_page`×2 + `llm_cross`×2（**同一根因重复 4 条**）——
  「13:18 时 P3 (MPa) 的实际值为 `'A'`」「F3 累计流量 (L) 的实际值为 `'2025.01.21'`」。
  视觉核实（`p17.jpg`）：附表 6 的 `P3` 列**四行全是 `0`**、`F3` 列为 0/854/1708/2578；
  表格**下方**的手写签名日期 `2025.01.21` 被**串进了 F3 列**。
  ⇒ 链条 = OCR 误读（`0`→`A`）+ 列串行 → **LLM 照单全收并升格为 critical**。

  现象 B（**串位 + 幻觉出图上不存在的值**）：p38 `llm_cross`
  「清洗罐搅拌**结束时间 18:30 早于**清洗罐搅拌**开始时间 19:15**」。
  视觉核实（`p38.jpg`）：该页「清洗罐搅拌」实为 **开始 19:04 / 结束 19:41**（顺序正常）；
  LLM 用的是同页**另一行「退出循环」**的时间（开始 18:20 / 结束 19:15），并把 18:20 读成 18:30。
  ⇒ **跨行串位**（把 A 行的时间配到 B 行的字段上）。

  动作：① 在页级/跨页 LLM **之前**加一道**形态闸门**：`actual` 若不含任何数字、
  或匹配日期形态（`\d{4}[-./]\d{1,2}`），则**不得**判 `param_out_of_spec`，
  改判"待人工核对"并保留 OCR 原文；② 时间倒序判据必须校验**两个时间来自同一行/同一字段组**
  （LLM 需回填 `source_row`/`field`，无法回填则降级为 warning）。
  验收：把 p17/p38 的两段输入喂进去 ⇒ 产出 **0 条 critical**（转为"待人工核对"）；
  变异验证：摘掉形态闸门 ⇒ p17 的 `'A'` 重新变成 critical。
  ⚠️ 本条与 B1-2（`vision_crosscheck`）天然互补：视觉读到的 `0` vs OCR 的 `A`
  正是"待人工核对"的触发条件。

  **🔴 Round 43（2026-09-20）扩充：又核实出 3 个新形态**（同一"LLM 自由取值 + 自由判定"根因）

  | 形态 | 页 | 系统给的 critical | 读原图核实的结果 |
  |---|---|---|---|
  | **D 跨字段串位** | p6 | 「接收结束时间 **13:6** 早于接收开始时间 09:00」 | 该行三格为 开始 `09:00` / 结束 **`09:52`** / 体积 **`13.6 m³`** ⇒ 顺序**正常**；LLM 把**体积**串成了**结束时间** |
  | **E 布尔项勾选读反 + 类型错配** | p39 | 「步骤 6/7 … 参数值为**否**，不符合规格范围」 | 序号 6/7 的**是/否框全部勾的是「是」**；且类型塞进了 `param_out_of_spec`（**数值超差**类型，不适用于是/否项） |
  | **F 凭空数字** | p6 | 「操作者签名日期 **18222** 无法识别」 | 该页签名只有「郑雅」「李伟胜」，**图上不存在 `18222`** |

  ⇒ 形态 E 还暴露一个**类型体系**问题：是/否类检查项**没有对应的 finding 类型**，
  只能硬塞进数值类型。建议一并评估 `CANONICAL_TYPES` 是否需补「检查项未通过（布尔）」，
  但**改动面大（前端三色/知识库词表/门禁都要跟），必须先出方案再动**。

  **✅ Round 48（2026-09-20）处置记录 —— 本轮不新增判据，只做"验证 + 补齐变异证明"**

  逐形态复核（离线回放 `devlogs/_replay/replay.py` + `verify_b14.py`，真实 51 页）：

  | 形态 | 页 | 现状（有真实数据证据） |
  |---|---|---|
  | **A 非数值当超差** | p17×2 | ✅ **已修**：L1-value-shape 抑制（`'A'` 非数值 / 日期串入数值列） |
  | **D 跨字段串位** | p6 | ✅ **已修**：L3-time-reversal 同源重算（structured 实为 09:00→09:52）抑制 |
  | **E 布尔勾选读反 + 类型错配** | p39、p46 | ✅ **已修**：L1-value-shape 抑制（`否` 为勾选形态，不构成数值超差） |
  | **F 凭空数字** | p6「18222」 | ✅ **已修**：L1' 推测性表述 + L2 溯源降至 info |
  | **B 跨行串位（时间）** | p38 | ⚠️ **降级保留，未达"有据抑制"** —— 见下 |

  **现象 B 为什么本轮修不动（定位结论）**：TODO 原拟的修法是"LLM 回填 `source_row`/`field`，
  判据校验两个时间来自**同一行/同一字段组**"。实测 p38 的 `structured.steps`
  **start/end 全为空串**（只有 3 段长文本工序），raw 原文里也**不存在** 19:04/19:41
  ⇒ 该页**没有可用的行级证据**去证伪 LLM 的 18:30/19:15。
  "按工序名定位"同样不可行：p38 的 step `operation` 是段落文本，与文案里的
  「清洗罐搅拌」对不上。⇒ 现状（判不了 ⇒ 降级为 warning）**已是该数据下的最优处置**；
  要升级为"有据抑制"**必须改 prompt + LLM 输出 schema**（让 LLM 回填行号/字段名），
  **改动面与风险等同一次协议变更 ⇒ 转 P2**（见新增条目 **B1-13**）。

  **变异验证（`devlogs/_replay/mutate_b15.py`）5/5 通过**（逐字节还原自校验通过）：
  摘"非数值"闸门 ⇒ p17 红；摘"布尔"闸门 ⇒ p39 红；摘"日期形态"闸门 ⇒ p17（日期）红；
  三态退化（`None`→`False`）⇒ p43 红；`structured` 缺失时退化 ⇒ p46 红。
  ⚠️ 首次运行时两条预期写错（把**并列分支**当成同一变异的目标）：布尔分支与"非数值"
  分支并列且在前、`structured` 缺失走的是**早返回** ⇒ 已拆成 5 个定向变异。

  ⚠️ **原验收的变异条款已过时**：「摘掉形态闸门 ⇒ p17 的 `'A'` **重新变成 critical**」
  —— L4 对 LLM 源**无条件**封顶，去掉闸门只会变成 **warning**（不再被抑制），
  不可能再是 critical。已按**真实可观察信号**（"该条是否仍出现在抑制清单里"）重写。

- [x] **B1-6 【主路径】把"取值"与"判据"从 LLM 手里收回到规则层**（P1，**Round 44 已落地**）
  动机（本轮最重要的判断）：B1-4 / B1-5 的**五类形态**（日期方向反、列串位、
  跨字段串位、布尔勾选读反、凭空数字）**根因只有一个** —— LLM 同时做了两件它不该
  单独做的事：**自己从 OCR 文本里挑值** + **自己下判定**。故"再调提示词"是治标。
  动作：链路拆成 **① LLM 只做结构化抽取**（输出值 + `source_row` / `field` 溯源）
  → **② 确定性代码做判定**（数值比较 / 超差 / 时序 / 跨页一致性）；
  **LLM 输出的 `severity` 不再直接采信**，由规则层按同一套判据重算。
  验收：把本轮 p2 / p6 / p17 / p39 四页的输入喂进去 ⇒ **0 条假 critical**
  （全部转"待人工核对"或直接不报）；**变异验证**：把判定改回"直接采信 LLM severity"
  ⇒ 立刻重现这 4 条。
  ⇒ 本条是 B1-4 / B1-5 的**共同上游**，建议**先于**它们动手（否则修完仍会被新形态绕过）。
  **✅ Round 44 实施记录**：
  - 新模块 `core/rules/llm_finding_guard.py` —— 四层复核，纯函数/零 LLM 调用/零 IO：
    **L1 值形态**（`param_out_of_spec` 声明的实测值须为数值形态；布尔值/日期值/
    非数值 ⇒ 强证据**抑制**；数值但查无此值 ⇒ 弱证据**降级**，漏检代价高于误报）；
    **L2 溯源**（文案长数字须能在 OCR 原文定位，复用 `page_analyzer._value_grounded`
    同源实现）；**L3 判据重算**（`time_reversal` 用 `parsing._parse_time_interval`/
    `_interval_after` 重算；`suspicious_date`/`signature_time_anomaly` 核对话里声明的
    先后方向）；**L4 severity 封顶**（LLM 独断的 critical 一律降 warning 待人工核对）。
    另含 **L1' 推测性表述**判据（「无法识别」不是异常结论 —— v4 prompt 已明令禁止，
    LLM 仍输出，见 B1-8）。
  - 接入两条落库路径：`stage2.py`（`llm_page`，单页）与 `stage3.py`
    （`llm_cross`/`llm_fallback`，跨页）。抑制走 `finding_suppressions` 台账
    （复用 `spec_guard.suppression_rows` **唯一构造点**，带非空 reason + evidence，
    可回退、可抽检）；降级理由追加到 description（GMP：结论变更须可解释）。
  - 护栏：`tests/unit/test_llm_finding_guard.py` **20 用例**（样本全部取自真实页形态）
    + `tests/unit/test_pipeline.py::test_llm_guard_suppresses_unfounded_cross_finding`
    链路级 1 例。**变异验证 3 个方向全部变红**（复核器短路 ⇒ 12 failed；
    去"评价词非实测值"过滤 ⇒ 1 failed；去 L4 封顶 ⇒ 4 failed）。
  - **真实数据回放**（Round 42 的 51 页 real 轮隔离库，`devlogs/_replay/`）：
    152 条 LLM findings（spec_guard 后 132）⇒ 抑制 26 / 降级 42。
    **critical 49 → 0**；4 条目标假 critical（p2 方向反 / p6 跨字段串位 /
    p17 列串位 ×2 / p39 类型错配）全部抑制，p6「18222」凭空数字降级。
  - **反向断言（真阳性不误杀）**：rule 层 6 条 critical **原样保留**（不受复核）；
    并修掉三处自身误杀（p9 评价词被当实测值、p22「时间重复」被倒序判据否定、
    p26 弱证据误为强证据）。
  - ⚠️ **收权后 critical 只剩规则层 6 条，其中 p48 经视觉复核为假阳性 ⇒ 见 B1-9**；
    p1 批号条待核 ⇒ 见 B1-10。

- [x] **B1-7 规则层基准"随运行时刻漂移" ⇒ finding 不可复现**（✅ **2026-09-21 Round 53 已修**）
  现象：`core/rules/rule_time.py` 原用 `current_year = datetime.now().year` 当基准
  ⇒ **同一份 PDF 在不同年份重分析会得到不同结论**。
  ⚠️ 注意：**判据方向本身是对的**（记录里出现未来日期确实可疑），问题是**基准未固化**。
  **解决**（`core/rules/rule_time.py`，三处单一实现点、纯函数）：
  * `record_production_year(pages)` —— 从记录里**多数票**取生产年份（平票取小年）；
  * `_baseline_year(pages, override)` —— 优先级 **显式入参 > 记录年 > 当前年**，
    回退当前年时返回 `year_inferred=True`，**在 finding 文案里注明**
    （「基准年取当前年份（记录中未解析出生产年份）」）；
  * `_suspicion_reason(year, ceiling, now_ceiling)` —— 四条语义分流：
    「早于 2000」/「晚于 {ceiling}」/「晚于当前年份+1…（按运行时刻判断）」/`None`。
  * `_check_suspicious_dates(pages, analysis_year=None)` —— 新增显式 `analysis_year` 入参。
  ⚠️ **保留"绝对未来"判据（防静默削弱）**：固化基准后，若记录**整体**落在未来，
  仍须判出，且**如实声明**该判据依赖运行时刻 —— 否则"固化基准"会把真信号一起吃掉。
  这是 Round 53 踩过的坑：初版让"记录内生产年"当基准，导致记录自身 production_date
  设成未来年的回归用例**变绿**（检测被削弱）；修法是把两条语义**拆开**
  （相对基准年 vs 绝对"当前年+1"），并补 `TestAbsoluteFutureIsStillCaught` 防再退化。
  护栏：`tests/unit/test_suspicious_date_baseline.py`（17 例）——记录生产年/基准优先级/
  **决定性（两个不同系统时间 ⇒ findings 一致）**/回退注明/**绝对未来仍捕获**/显式 override。
  变异验证 **8/8 CAUGHT**（`devlogs/_verify/mutate_b17.py`；M8 首次锚点缩进写错、
  脚本安全中止并还原，修正后全过）。

- [x] **B1-8 推测性表述被当作异常结论**（P2，Round 44 发现并随 B1-6 修）
  现象：LLM 把「识别不出」写成 finding —— 实测 p6「操作者签名日期 18222 无法识别」
  被判 critical（原文里那些数字是 OCR 粘连产物）。而 v4 prompt 的「完整性检查规范」
  **已明令禁止**输出「无法准确识别」「可能缺失」「疑似遗漏」等表述。
  ⇒ **提示词约束不住，必须代码收权**（这正是 B1-6 的论证）。
  动作（**已完成**）：`llm_finding_guard._check_speculative` 命中推测词即降级
  （保留为人工核对线索，不给确定性语气）。
  验收：`TestL1Speculative::test_p6_unreadable_is_not_an_anomaly_conclusion`。

- [x] **B1-9 规则层把"区间重叠"当成"倒序"并给 critical**（P1，Round 44 发现 / Round 45 修复）
  现象：**收权后仅存的 6 条 critical 里，p48 经直读原页确认是假阳性** ——
  规则层报「工序4 开始(03:34) 早于 工序3 结束(03:36)」，但原图上：
  ① 序号 3 与序号 4 是**不同设备的两道清洗工序**（D2101/D2102 真空干燥箱
  03:12→03:36 vs 烘盘/盘罩/勺子 03:34→04:12），**并行重叠属正常**；
  ② ⚠️ **2026-09-20 更正**：原登记写「两处手写日期不同（15日 vs 25日）」，
  实测**是误读** —— 该页 `handwritten` 字段原值为 `25日03时12分` 与
  `25日03时34分`，**两处都是 25 日**。真正站住的是①与③。
  ③ 更根本：这**违反项目自身的约定** —— v3/v4 prompt 明确要求「手写体字段
  （操作人签名/手填数值/手写日期）应标记 confidence=low，**规则不直接判定**」，
  规则层却对手写时刻给了 critical。
  **根因**：`_check_time_reversal_cross_page` 的判据是
  `curr.start < prev.end` —— 这只是**区间重叠**，而"倒序"的语义应是
  `curr.start < prev.start`（编号与开始时刻矛盾）。判据用错 + 严重度给满。
  动作（**已完成**，Round 45）：
  ① 判据分级：真倒序（`curr.start < prev.start`）⇒ critical；
  仅重叠 ⇒ **warning**，文案明说"两工序时间重叠（若为不同设备/物料的并行操作
  则属正常，请核对）"；
  ② 证据封顶：所依据的时刻**为手写填写**、或**字面量缺日期成分**（比较所用
  日期系由该页 `production_date` 推定）⇒ **一律封顶 warning** 并在文案注明理由
  （与 B1-6 的 L4 同源：不确定来源不给最高严重度）；
  ③ 同源封顶一并施加到 R1a（页内倒序）；**未新增类型**（保持 `time_reversal`，
  避免牵动 `CANONICAL_TYPES`/`TYPE_QUERIES`/前端 zh_map 多处同步）。
  验收（**已实测**）：
  - 真实 51 页回放：p48 那条 `critical → warning`，文案含"重叠"+"手写"说明；
    全语料 R1b 只此 1 条，无其他变化（未弱化检测）；
  - 反向断言：真倒序（p9/p10 用例）**仍为 critical**；
  - 新增护栏 `tests/unit/test_time_reversal_evidence.py`（33 例）；
  - 变异验证 4 方向全红（重叠打回 critical / 去 R1b 封顶 / 复现粘连 bug /
    去 R1a 封顶）。
  ⚠️ **实现期踩到的坑（已固化为回归用例）**：`_time_is_handwritten` 最初先
  `lit.replace(" ", "")` 再匹配 `(?<!\d)(\d{1,2})[:时](\d{2})` ⇒
  `2025-09-25 03:36` 去空白后粘成 `2025-09-2503:36`，日期的末位数字把
  `(?<!\d)` 触发掉 ⇒ **整条手写封顶静默失效**。必须在原字面量上匹配。
  ⚠️ 与 B1-6 的关系：B1-6 只管 LLM 层；**规则层的假阳性是另一条独立战线**。

- [x] **B1-10 跨页批号把 OCR 变体当成不同批号**（✅ **2026-09-21 Round 53 已修**）
  **定位（真值来自 `devlogs/_replay/data.db` 的 job `6f80145a-47f`，46 页有批号）**：
  库里那条 critical 写的是「检测到 **5 组**不同批号」而日志明写 **`0 voted`** —— 即
  旧 R2 投票**一次都没触发**。逐核心串核对后，5 组里 4 组是同一批号 `1127011N250101`
  的 OCR 写法：
  | 页面 | 原始读数 | 归一化 | 噪声来源（**已渲染原页核验**） |
  |---|---|---|---|
  | p1/p2/p24… | `1127011N 250101` | `1127011N250101` | 空格分隔 |
  | p22 | `1127011N ^4 250101 -07` | `1127011N4250101-07` | 原页 `N` 后的**角标**被读成数字 `4` |
  | p5 | `11270111/250101-02` | `11270111250101-02` | `N `被读成 `1/`（N→1 单字符） |
  | p13 | `1127011/250101-05` | `1127011250101-05` | `N `整段丢失 |
  | p3/p4…（**14 页**） | `112701` | `112701` | LLM 只抽出前缀，**尾段被丢掉** |
  | p43 | `1127011N` | `1127011N` | 表格把批号切成两格（`产品批号：1127011N` / `250101-06`），LLM 只取第一格 |

  **根因（两条，互相叠加）**：
  ① **旧归并按「核心串长度」降序用前缀包含互相吸收** ⇒ 噪声最重的串
  `1127011N4250101` 成了 host，把 14 页干净的截断读法 `112701` 挂在它名下 ——
  组标签**本身就是 OCR 噪声**（复核员看到的是 `1127011N4250101(第3,4,7…)`）；
  ② 投票以「**精确核心串**」为单位计页数：最大核心串 `1127011N250101` 只占
  27/46=**58.7%** < `_BATCH_VOTE_MIN_SHARE`(0.6) ⇒ 结构上本属同一批号的 14 页
  截断读法把票数劈开 ⇒ **投票不决定性 ⇒ 一条都不吸收**。
  ⇒ 旧实现**在真实数据上判据失效**（R2 文档记的"5 组 → 2 组"是在另一 job
  `0c5cfb00-897` 上测的，抽取行为变化后即回归）。

  **改法（`core/rules/parsing.py` + `rule_doc.py`，对称、无顺序依赖）**：
  新增批号**身份分解** `parse_batch_key(base) -> (prefix, date6)` —— 取末尾**整段**
  数字的后 6 位、且须是合法 YYMMDD（月 1–12/日 1–31）才算日期段（先扫整段数字才能
  吃掉 `^4` 角标）。相配的 `batch_keys_compatible(a, b, allow_edit_distance=)`：
  * **双方有日期段 ⇒ 日期段必须完全相等**（独立佐证）且前缀相等/互为前缀；
  * **一方无日期段**（尾段被丢）⇒ 无日期方须是**有日期方前缀的前缀**（反向不成立）；
  * **双方都无日期段** ⇒ 只认前缀包含，**从不认编辑距离**（保护 `B202201`/`B202202`）。
  归并分两档：**Tier 1 结构档**（前缀包含/尾段截断，关系可判定）+
  **Tier 2 弱证据档**（单字符近邻 `N`↔`1`）；两档都过**同一个投票闸**，
  不决定性 ⇒ **一条都不吸收**（fail-open 保持）。锚点改按**页数**选（不再按长度）。
  被归并的**完整别名表**写进 `ocr_text` 的 `merged_aliases=`（描述里列前 3 个）。

  **实测（隔离库回放，同 job 同数据）**：**5 组 → 2 组**；19 个原始读法、17 个被归并、
  其中 5 个按结构证据并入基准；基准组标签由噪声串 `1127011N4250101` 纠正为
  `1127011N250101`（45 页）。
  **存活的那 1 组是 p50 —— 不是变体**：渲染原页可见 `清场批号：1127011N'` + 蓝字
  `241203`，**整页无 `250101`** ⇒ 是真实的另一个值（清场记录引用他批），
  日期段不同 ⇒ 归一化**永不**合并它，critical 保留且带页号。
  ⚠️ **验收口径已按原图核验修正**：原文"同批号变体不产出 critical"改为
  「**同批号 OCR 变体不再产出独立组**（真实值差异仍报 critical）」。理由见上：
  `241203` 是印在纸上的真值，把它并掉才是漏检。

  **护栏**：新增 `tests/unit/test_batch_variant_identity.py`（30 例）——`parse_batch_key`
  参数化（含角标/截断/非法月日）、兼容关系**对称性（含结构档单独钉）**、
  p1 原 finding 的 5 个读数 ⇒ **无 finding**、**真实 51 页 46 读法内联回放**
  （2 组 / 别名留痕 / 反证"仅归一化仍剩 >2 核心串"）+ fail-open 4 例。
  **变异验证 9/9 CAUGHT**（`devlogs/_verify/mutate_b110.py` → `mutate_b110.log`）：
  日期闸失效 / 前缀包含单向 / 无日期段误用编辑距离 / 去掉投票闸 / 锚点改按长度 /
  去掉残串最小长度 / Tier1 混入弱证据 / 去掉 YYMMDD 合法性 / 去掉别名留痕。
  ⚠️ 顺带更正：`test_vote_absorption_is_disclosed` 的措辞契约随描述改写更新
  （断言从固定句改为「已归并 N 个」+ 点名校验，**更严**）。
  ➡ **遗留（另立条目 B1-14）**：p50 的 `清场批号` 与 `产品批号` 混在同一个
  `page_info.batch_no` 字段里 —— 治本是**字段级溯源**，不是放宽归一。

- [x] **B1-11 【P1】收权复核把「判不了」当成「判据确凿」**（**Round 46 对抗性审查发现并当日修复**）
  现象：**同一个工程错误在两条 L3 路径上各犯一次** —— 判据把"**无从判定**"与
  "**确证不成立**"混为一谈，破坏了 B1-6 自己的原则「只对**逻辑不成立**的用抑制」：
  - `_check_declared_order`：LLM 的**方向词**写反 ⇒ **抑制整条**。但"方向词错"
    ≠ "整条结论不成立"。实测 p38「签名时间 **2027**.01.17 早于 生产日期 2025年01月20日」
    抑制后，该页**再无任何条目提及「2027」这个未来日期**（规则层亦无兜底）；
    p27 更极端（两侧相差 **10 年**，属年份误读线索），只因同页**碰巧**另有
    `year_contradiction` 才没丢。
  - `_recompute_time_reversal`：返 `bool`，把"该页工序**一个都解析不出**"归入 `False`
    ⇒ 当成"判据确凿地无倒序"而抑制。实测 **8 条 L3 抑制里 6 条**该页可解析样本 = 0，
    其中 **p43×4 / p46×1 的文案明写「开始时间晚于结束时间」**（真倒序形态）。
  修复（**按证据强度分档：强证据才抑制，弱证据只降级**）：
  ① `_check_declared_order` 改返回 `(抑制理由, 降级理由)` —— 参照物含"当前日期/
     当前年份"⇒ 抑制（该比较本身无意义，B1-4 型）；两侧**都是记录内日期** ⇒ **降级**
     （错的只是表述，日期对本身仍需人工核对）。新增 `_CURRENT_REF_RE`，**不引墙钟**
     （避开 B1-7）、**不引新阈值常量**（避开"两份阈值表漂移"这一高发坑）。
  ② `_recompute_time_reversal` 改**三态** `bool | None`：有样本且确无倒序 ⇒ 抑制；
     确有倒序 ⇒ 保留；**无样本 ⇒ `None` ⇒ 降级**（fail-open）。
  实测（51 页真实隔离库回放 `6f80145a`）：抑制 **26 → 13**、降级 42 → 55，
  **critical 保持 0**；仍被抑制的 13 条逐条皆强证据
  （p2×2 / p6×2 / p17×4 / p24 / p39×2 / p46×2）。
  ⚠️ **代价（有意取舍）**：p38 的串位幻觉条目也转为降级 —— 该页 0/3 可解析，
  规则层**确实判不了**；按"漏检代价 > 误报代价"宁留 warning 噪声，换 p43/p46 的真倒序不被吞。
  护栏 20 → **23** 例（含**改写 p38 期望值并在 docstring 写明理由**）；变异验证 3 方向全红。
  详见 `docs/ADVERSARIAL_AUDIT.md` §16。

- [x] **B1-12 布尔/勾选类的处置档位由「措辞是否撞上词表」决定**（✅ **2026-09-21 Round 53 已修**）
  **① 定位（先读原页 —— 已完成，渲染 `丝裂霉素提取批记录.pdf` p39/p45/p46）**：

  | 页 | 原页真值 | LLM 主张 | 结论 |
  |---|---|---|---|
  | p39 | 三行均 **`√ 是 / □ 否`** | `参数值为否，不符合规格范围` | **读反** ⇒ 假阳性 |
  | p45 | **`√ 是 / □ 否`** | `勾选了否，不符合操作指导` | **读反** ⇒ 假阳性 |
  | p46 | 工序 5/6 全部 **`√ 是 / □ 否`** | `清洗过程是否无异常参数值为否` | **读反** ⇒ 假阳性 |

  ⇒ **三页真值都是「是」，三处"否"主张全是 LLM 读反**，本就该得到**同一种**处置。
  ⚠️ **旧记载的根因不准（本轮实测更正）**：p45 的 fail-open **不是** `_NOT_A_VALUE`
  里"不符合操作指导"造成的 —— `_NOT_A_VALUE` 是拿**取到的值**去比，而 p45 文案里
  **根本没有 `值` 字**，`_BARE_VALUE_RE`（只认「…值为 X」/引号）压根匹配不上
  ⇒ 取不到值 ⇒ fail-open。真因是**取值器只认一种措辞**。

  **② 解决**：`core/rules/llm_finding_guard.py` 新增**统一类型闸门**
  `_declared_boolean_literal(f)` + `_BOOLEAN_CLAIM_RE` —— 判据是"文案断言了勾选
  **结果**"这一**性质**，与用哪个动词（`值为` / `勾选了` / `标记为` / `选中了`）无关；
  且在 `_check_spec_value_shape` 里**先于"取值"判定**（否则档位又由措辞决定）。
  * 负向预查 `(?![一-龥])` 排除**疑问词「是否」本身** —— 否则「记录中勾选是否正确」
    这类**评价勾选动作**的文案会被误判成布尔主张；
   * 文案抽成**单一实现点** `_boolean_type_mismatch_reason`（`_layer_of` 靠
    「勾选/布尔形态」子串归层 ⇒ 该子串是**契约**，不得改）。
  **实测**：p39/p45/p46 三条 ⇒ **kept=0 / downgraded=0 / suppressed=3**，
  层号全为 `L1-value-shape`、**理由逐字相同**（修前 p45 是"仅降级"）。
  **护栏**：新增 `tests/unit/test_boolean_gate_uniform.py`（21 例）——三措辞同处置、
  `是` 与 `否` 同处置（证明是**类型**闸门而非"找否"闸门）、7 种措辞识别、
  **5 例防误伤**（疑问词/评价词/字母值/非 spec 类型）。
  **变异验证 5/5 CAUGHT**（`devlogs/_verify/mutate_b112.py`）：旁路统一闸门 /
  去掉疑问词负向预查 / 去掉 spec 类型限定 / 破坏 `_layer_of` 契约子串 /
  只认「否」。
  ➡ **遗留（另立条目 B1-15）**：**规则层**（R8 `_check_check_consistency`）也在这
  三页报了「勾选为“否”」的 warning —— 它读的是 `checks[].selected`，同样不可靠。

- [ ] **B1-15 规则层 R8「勾选为否」warning 在 p39/p45/p46 全是假阳性**（P2，Round 53 从 B1-12 拆出）
      🔸 **第一档（☐ 内部矛盾）✅ 2026-09-21 完成**（−5 条假阳性 warning）；
      **第二档（`否`+`√`/`☑`）未闭环**，与 B1-14 一次性定 schema；派生 **B1-17**。
  现象（B1-12 原页核验时发现）：B1-12 修的是 **LLM 层**（`llm_finding_guard`）的布尔
  处置；但**规则层** `_check_check_consistency`（R8）也在这三页产出了 warning：
  * p39 ×6：`检查项「器具排放是否整齐」勾选为“否”（√）` 等；
  * p46 ×1：`检查项「清洗过程是否无异常」勾选为“否”（√）`；
  * p45 ×1：`检查项「是否将柱内甲醇压干」勾选为“否”（☐）`。
  **原页核验（已渲染）**：三页的「是」框才是有 √ 的那个 ⇒ 这 8 条 warning **全部是
  假阳性**（`checks[].selected` 被 LLM 读反）。
  证据链（结构化里可直接看到）：
  * p45 `selected='否'` 而 `marker='☐'`（**空框**）——**自相矛盾**：一个"被选中"的
    选项不可能配一个空框。这一条**可从记录内部证明**，属**可确定性修复**。
  * p39/p46 `selected='否'` 配 `marker='√'` —— 记录**内部自洽**，只有原图能证伪
    ⇒ 这一档**判不了**（与 B1-14 的字段溯源同属"输入里没有可用锚点"）。
  动作：① **先修可确定的那一档**：`selected` 有值而 `marker` 是**空框字形**
    （`☐ □ ◻ ○ ◯`）⇒ 判据内部矛盾 ⇒ 不发射（或降为 info 并注明"勾选标记与选项
    不一致，请人工核对原页"）——**注意不得**把"marker 缺失"也算进来（缺失是不知道，
    不是矛盾，须 fail-open）；
  ② 第二档（`否`+`√`）需**抽取层**改进（LLM 把勾选标记与选项配对错位），
    与 B1-14 的字段/列溯源**同源**，建议合并评估（B1-13 的 `source_row` 方案已于
    Round 53 评估否决 —— 见 `docs/B1-13_ROW_TRACEABILITY_ASSESSMENT.md`）。
  验收：构造 `selected='否', marker='☐'` ⇒ 不产出 warning；构造 `selected='否',
  marker='√'` ⇒ 仍产出（fail-open）；构造 `selected='否', marker=''` ⇒ 仍产出。
  变异验证：把"空框字形"判据去掉 ⇒ 第一条红。

  ---
  **① 第一档：已完成**（2026-09-21，`core/rules/rule_doc.py` + `tests/unit/test_cross_page_llm.py`）。
  新增 `_EMPTY_BOX_GLYPHS = frozenset("☐□◻○◯")` 与 `_is_empty_box_only(marker)`
  （**整串都是空框**才算矛盾：`√☐` 混合、含勾号、空白、缺失一律 False ⇒ fail-open）；
  `selected=='否'` 且内部矛盾时**降为 info** 并如实改写文案
  （「勾选标记与选项不一致……该选项不可信，请对照 PDF 原页人工核对」），
  **不再断言**"需确认是否已启动偏差处理"。选"降为 info"而非"静默丢弃"的理由：
  矛盾本身仍值得人看一眼，丢弃等于把"记录有问题"这条信息也一起丢了。

  **实测（真实 51 页回放，`devlogs/_verify/verify_b115_replay.py`，before = 把新判据换成
  `lambda m: False`，逐位等价）**：
  **总数 487 → 487，critical 38 → 38**；**warning 295 → 290（−5）、info 154 → 159（+5）**
  —— 即**消掉 5 条假阳性 warning**，换成 5 条**如实**的 info。
  ⚠️ **真实语料比本条登记时知道的更多**：原条目只记了 p45 ×1（来自 B1-12 的原页核验），
  回放实测是 **5 条**：p9 / p14 / p24 ×2 / p45（全部 `marker='☐'`）。
  ⇒ 说明"只按人工抽查到的那一例去估影响面"会**低估 5 倍**；这类判据必须以**全语料回放**收口。
  变异验证 **6/6 CAUGHT**（`devlogs/_verify/mutate_b115.py`）：M1/M6 降级分支两个方向
  （`if False` / `if True`）、M2 去掉"缺失=不知道"兜底（违反 fail-open）、
  M3/M4 字形集过宽（把 √ 当空框）/过窄（只认 ☐）、M5 把判据作用域从「否」扩到「是」。

  **② 第二档：仍未闭环**（`否` + `√` / `☑` ⇒ 记录内部自洽，只有原图能证伪）。
  第一档落地后，真实语料里**仍存的**「否」类 warning 共 **11 条**：
  p39 ×7（`√`）、p46 ×1（`√`）、p51 ×3（`☑`）。
  ⇒ 与 B1-14 的字段/列溯源**同源**（都需要抽取层给出"这个值出自哪个位置/哪个字段"），
  须与 B1-14 **一次性定 schema**（避免两次改 prompt 版本 + 两次破 prompt caching）。

  ➕ **新发现（2026-09-21，回放实测，**不是**假阳性那么单纯）—— R8 的**语义前提**对
  「否定式措辞」的检查项不成立**：p51 三条是 `是否发生偏差 / 是否发生 OOS/OOT /
  是否发生变更`，抽取出的 `selected='否'` + `marker='☑'`（**已勾选**）。
  这三项的**正常答案就是「否」**（无偏差），而 R8 的文案却说
  "需确认是否已启动偏差处理并在记录中留痕" ⇒ 对"回答否 = 一切正常"的题面，
  R8 的推断**方向反了**。⚠️ 这与 **B1-12 已明令禁止的做法直接冲突**：
  B1-12 的结论是"布尔/勾选类处置**不得由措辞词表决定**"（那里改用类型闸门）。
  所以**不能**用"题目里有没有『是否发生』『无异常』"这类词表去翻转动级
  （那是把 B1-12 修掉的病再种回来）。可行的方向只有两条，**都需用户决策**：
  (a) 把文案改成**中性**（陈述事实 + 请人工确认，不替用户判断"否"是好事还是坏事）；
  (b) 在抽取层拿到**该检查项的选项集合**（「是/否」各自的措辞与勾选态），
      由规则层按"被勾的那个选项本身是什么"来判定 —— 属 B1-14 的 schema 一并解决。
  ✅ **2026-09-21 已按 (a) 落地（用户决策）**：`否` 的 warning 文案改为
  「勾选为“否”（marker），请对照 PDF 原页确认该答案是否为预期
  （否定式提问的“否”通常即正常答案）」，**删掉**"需确认是否已启动偏差处理"这句
  **替用户下结论**的措辞。定级**仍为 warning**（对「地面是否干净」这类肯定式题面，
  否确实需要人看一眼；极性只在文案里**如实提示**，不参与定级 ⇒ 不引入措辞驱动）。
  判据（`tests/unit/test_cross_page_llm.py::TestCheckConsistency`）：
  `test_no_polarity_inference` 断言**肯定式（地面是否干净）与否定式（是否发生偏差）
  题面的文案模板必须逐字一致** —— 这是"未按措辞翻转"的直接判据；
  `test_checked_no_warning` 加**反向断言** `"偏差处理" not in description`。
  变异 **M7**（把中性文案改回替用户下结论的旧措辞）⇒ 上述两条**必红**（7/7 CAUGHT）。
  真实 51 页回放：**定级分布完全不变**（487 / warning 290 / info 159 / critical 38）
  ⇒ 本次只改文案、未动任何判定 —— 这也是它为什么能安全地紧跟 B1-15① 一起走。
  ➕ **另一处观测（同轮，未修）**：p39 的 `设备及管路是否及时清洗干净` 出现**两条完全
  相同的 finding**（同页/同项/同 severity/同描述）⇒ 疑似**同根因重复发射**，
  与 B1-1（同根因聚合）相邻但不同侧，单独登记为 **B1-17**。

- [x] **B1-13 【P2】时间倒序判据缺"行级溯源"**（Round 48 从 B1-5 拆出）
  ✅ **2026-09-21 Round 53：代价评估已完成 ⇒ 结论「不做 ①，条目改判」**。
  评测文档：**`docs/B1-13_ROW_TRACEABILITY_ASSESSMENT.md`**（本轮只读取证，未改生产代码）。
  - **原前提被推翻**（`devlogs/_verify/probe_b113{,b,c,d,e}.py`，job `6f80145a-47f`）：
    p38 的两个被引用值在抽取结构里**同属一个 measurement 行、同一列组**
    （`step3.meas#0.col[清洗罐搅拌开始时间]='19 时 15 分'` /
    `col[清洗罐搅拌结束时间]='18 时 30 分'`）⇒ **不是"跨行串位"**；
    原文里**有**这两个字面量（形态为「X 时 Y 分」）⇒ 真差异是 **OCR 误读**（视觉真值 19:04/19:41）。
  - **语料级否定**：LLM 层 `type=time_reversal` **12 条，0 条跨行**（逐条定位表见文档 §3）。
    真实机理是四类：值→列绑错（p6 体积 `13:6 m^3` 被当时间）/ 字面量形态不可解析（主线）/
    缺日期 fallback 跨日（p24）/ LLM 方向算错（p46 `08:42<09:15` 却报倒序）。
  - **① 的代价**：触及 `page_analyzer` **全静态 system 块**（破 prompt caching，51 页 job
    省 ~90% system token 的收益归零）、版本号 v4→v5、`semantic_v2`→`v3`、4 处 INSERT +
    `SCHEMA_VERSION 12→13`、≥8 处契约断言必红；**收益 0 且有负作用**（会为 p38 的误读背书）。
    **判据与缺陷不同源 ⇒ 不做**。
  - **替代动作（已登记）**：§6.1 ⇒ **B1-16**（解析器形态，性价比最高）；
    §6.2 ⇒ 并入 **B1-14**（列级"值→列"绑定与批号字段出处同一解法，一次性定 schema）；
    §6.3 ⇒ 归 **B1-2**（视觉互证 —— 印证原文备注"B1-2 落地后收益下降"，实测是**归零**）。

- [x] **B1-16 `_parse_time` 不接受「仅时刻 + 中文单位」形态 ⇒ 规则层与护栏双双饿死**（P2，Round 53 从 B1-13 拆出）
  ✅ **2026-09-21 完成**（`core/rules/parsing.py` + `tests/unit/test_cross_page_analyzer.py`）。
  现象（`devlogs/_verify/probe_b113f.py` 量化，job `6f80145a-47f`）：「看起来是时刻」的
  字面量 **162** 个中 **59（36.4%）不可解析**，其中 **51 个是形态本体不被支持**
  （给了哨兵日期仍失败）：`'09 时 00 分'`、`'19 时 15 分'`、`'21日07时49分'`、
  `'08时 09分'`、`'20 日 18 时 06 分'`；另 **8 个**是 fallback 生产日期缺失
  （p26/p47 的 `production_date=''`，连 `'12:10'` 都解不出）。
  `parsing._parse_time` 只认 `^\s*HH:MM(:SS)?$` 与**带完整日期**的 `H时M分`
  ⇒ R1/R1b 与 `llm_finding_guard._recompute_time_reversal` **同时饿死**，
  「判不了 ⇒ 降级」成为这些页的唯一结局（B1-9 已如实降级，但**根因在解析器**）。
  动作：扩展解析器接受「仅时刻 + 中文单位」与「`N日 H时M分`」（后者按 fallback 日期推月，
  须保守：day 与 fallback 日相差过大 ⇒ 拒绝，不得猜月）。
  ⚠️ **两条护栏必须同时立**：① 维持严格匹配语义 —— `'13:6 m^3'`（体积被误绑到时间列）
  必须**继续**判为不可解析，否则会**激活** p6 那条"体积当时间"的假倒序；
  ② 接受新形态后**必须复跑真实 51 页**，量化"救活了哪些页 / 新增了几条命中"，
  不得只跑单测就宣称改进（同 B1-1 纪律）。
  验收：上列 5 种形态各 1 例 ⇒ 可解析；`'13:6 m^3'` / `'01:01 ~ 01:27'` ⇒ 仍不可解析；
  真实 51 页前后对比表（新增命中逐条人工核）。
  变异验证：把新形态分支去掉 ⇒ 对应 fixture 红。

  **实现（三条约束都是"判不了 ≠ 判据确凿"的直接落地）**：
  - 新增 `_CN_TIME_ONLY_RE`（仅时刻，**分钟必须两位** —— 放成 `\d{1,2}` 会让 `13时6`
    被读成 13:06，与"被截断的数字"不可区分）、`_CN_DAY_TIME_RE`（`N日 + 时刻`，
    **整串锚定** —— 否则 `15时36分008232-2412017` 会被截出 `15时36分` 而误收）；
  - `_DAY_WINDOW_DAYS = 1`：`N日` 与 fallback 生产日期差 >1 天 ⇒ 返回 None。
    为什么是 1（不是 3/7）：窗口内**月份是确定的**；再放宽就必须在"用本月还是相邻月"
    之间猜 —— p15 `21日` 配 1月27日（差 6）、p46 `23日` 配伪造当天日期（差 5）
    都属"输入里没有可用锚点"。
  - **顺带修掉一个同源缺口**：`_TIME_OF_DAY_RE` 原为 `\d{1,2}[:时]\d{2}`，
    **不容忍空白** ⇒ 同一种形态只因空格被判成两种精度（`21日07时49分` 是精确点，
    `09 时 00 分` 退化成"整天"）⇒ 区间被放宽 ⇒ `_interval_after` 更难成立
    ⇒ **倒序被系统性漏报**（判据与解析器不同源）。改为 `\d{1,2}\s*[:时]\s*\d{2}`。

  **实测（可复现，非自述）**：
  - 解析器层（`probe_b113f.py` 重跑，同一冻结回放库）：**可解析 103 → 128**，
    不可解析 **59 → 34**，其中**形态不支持 51 → 12**。
  - 余下 12 条**逐条核过成因**（`probe_b116_ctx.py`）：**8 条是按设计必须拒**
    （`'13:6 m^3'` 体积；`'01:01 ~ 01:27'` / `'11:05 - 11:12'` 区间；
    `'22108时26分'` / `'2109时31分'` / `'22108时13分'` / `'2113时16分'` /
    `'15时36分008232-2412017'` 批号/日期粘连）；另 **4 条**是 `N日H时M分`
    但**生产日期本身不可用** —— p45/p46 抽取出的 `production_date` 是伪造的
    当天日期 `2026-09-18`（与 `22/23日` 差 4–5 天）⇒ 属"输入无可用锚点"，
    与 **B1-14 / B1-15 同源**，**不得靠放宽窗口去"救"**。
  - 规则层真实 51 页前后对比（`verify_b116_replay.py`，同开关前后各跑一次）：
    **总数 487 → 487，critical 38 → 38**（净变化中性）：
    **−1**：p17 的 `completeness/info`「时间格式无法解析（start=08时 09分 end=10时 10分）」
    **消失** —— 即该页时刻已可解析（解析器修复的**直接可观测证据**）；
    **+1**：p38 `time_reversal/warning`（`第22页 12:50 晚于 第38页 20 日 17 时 29 分`）。
    ⚠️ 新增那条**经人工核原页后判定为假阳性**（`probe_b116_hit.py`）：p38 是**转置表**，
    该字面同时出现在 `values` 列 ⇒ `time` 槽被污染，与 **B1-14** 的"列级值→列绑定"同源。
    ⇒ **本修复不能制造新 critical**（`rule_time._literal_has_date` 对无日期成分的字面量
    返回 False ⇒ 时序判定封顶 warning），也**没有**把假倒序激活；
    其治本项在 B1-14，**不在本条**（不在 B1-16 里放宽解析器去掩盖它）。
  - **变异验证 11/11 CAUGHT**（`devlogs/_verify/mutate_b116.py`）：M1/M2 打掉 1c/1d 分支、
    M3 去整串锚定、M4 放开一位分钟、M5 去日窗口、M6 窗口 1→0、M7 绕过 fallback、
    M8/M9 去 `ValueError` 兜底、M10 去 `_TIME_OF_DAY_RE` 空白容忍、
    M11 把 `_TIME_OF_DAY_RE` 放成永远匹配（反向对照）。
    ⚠️ 两条工程记录：**(a)** 负向/关键锚点必须写成**具名用例** ——
    `mutation_harness` 按 nodeid **精确**匹配，而 `parametrize` 的中文 id 会被 pytest
    转义成 `\uXXXX`（用转义串当锚点既不可读又一改就断）；
    **(b)** M11 **不能**全文件跑（实测子进程 300s 超时 ⇒ **无法判定**）：
    "永远匹配"让所有 date-only 变精确点，跨页时序语义整体翻转 ⇒ 组合爆炸；
    故用**最窄目标**（单条反向对照，17s 返回）证明该判据的敏感性。
  - 全量回归：`pytest tests/unit tests/integration -o addopts="" -q` ⇒
    **3020 passed / 0 failed**（489s）。
    ⚠️ 首次全量跑**抓到 5 failed**：新增的两条"精度判定"用例引用了
    `_parse_time_interval` 但测试文件**没导入它**（`NameError`）——
    **单跑新增类不会暴露**（那两条在同一个类里也会红，但当时只单跑了正向用例）。
    ⇒ 印证"改了进产物的东西必须全量跑"，单类通过 ≠ 可提交。
    修法与文件既有约定一致（`TestParseTimeInterval` 的 `self._iv()` 局部导入），
    **不为此改动模块级导入面**。
  - ⚠️ **取证教训（已写入 PITFALLS 三十三）**：变异脚本会**就地改写** `parsing.py`
    再还原，我曾在它运行**期间**跑探针 ⇒ 读到的是**被变异后的中间态**
    （探针一度报"109/53"与 `_parse_time('09 时 00 分') is None`，与单测**直接矛盾**）。
    **凡探针/采样与被测源码可能被并发改写，必须串行**。

- [ ] **B1-14 `清场批号`/`产品批号` 混用同一个 `batch_no` 字段 ⇒ R7 报出的"批号不一致"语义不清**（P2，Round 53 从 B1-10 拆出）
  现象（B1-10 定位时发现并已渲染原页核验）：job `6f80145a-47f` 的 p50 是
  `丝裂霉素精制工序清场记录(H3-MPD-10133-R34)`，页面上写的是
  **`清场批号：1127011N 241203`**（蓝字 `241203`，整页无 `250101`），
  但抽取把它塞进了 `page_info.batch_no`；R7 于是拿"清场批号"与其余 45 页的
  "产品批号"做同字段比较 ⇒ 报 critical「跨页批号不一致」。
  ⚠️ **这不是假阳性**（值确实不同，且 B1-10 的归一化**不该**合并它 —— 日期段
  `241203` ≠ `250101`），但**文案与定级都可能误导**：清场记录引用他批是 GMP 常见
  形态，而当前描述写的是"装订错误或混批"。
  动作：① 抽取阶段给批号**带字段出处**（`batch_no` 之外补 `batch_no_field`，或让
  `page_info` 区分 `product_batch_no` / `clearance_batch_no`）；② R7 按**同字段**
  比较，跨字段差异另走一条**独立判据**（并给出相应文案/定级）。
  ⚠️ 与 B1-13 同属"**输入里没有可用锚点**"一类：治本是补 schema，不是调阈值。
  🔴 **2026-09-21 定位结论（实测，决定"规则层修不了"）**：探针
  `devlogs/_verify/probe_b114_field.py` 只读冻结回放库，得到三条硬事实：
  - `page_info` 的键**并集只有 5 个**：`title` / `file_code` / `version` /
    `batch_no` / `production_date` —— **没有任何字段出处**（`batch_no_field` 不存在）。
  - **「清场批号」字样在全库 51 页的结构化 JSON 里 0 命中**（`walk()` 遍历所有
    键/值/嵌套，含 `ocr_text` 之类）⇒ **标签在抽取时就已丢失**，规则层无从还原。
  - **标题启发式也区分不了**：`title` 含「清场」的只有 **2 页**（p49/p50），
    而 **p49 的 `batch_no='1127011N 250101'` 与本批一致、p50 才是 `…241203`**。
    ⇒ "清场记录页的批号不参与比较"这种按标题放宽的做法**既会误伤 p49、
    又只是把 p50 一个样本硬编码进去**（且属 B1-12 明令禁止的"措辞驱动"）。
  ⇒ **本条的验证必须重新抽取**（改 prompt/schema ⇒ 冻结语料失效 ⇒ 前后对比要重跑
    LLM 链路）。这是一个**有成本、需用户决策**的动作，不要当成纯代码改动推进。
  验收：构造"清场页引用他批"的 fixture ⇒ 不报「混批」critical（但仍有可追溯的一行）；
  构造"产品批号真不一致" ⇒ 仍 critical。变异验证：把同字段比较改回混字段 ⇒ 前者红。
  ➕ **2026-09-21 Round 53 扩围（来自 B1-13 的评估，见
  `docs/B1-13_ROW_TRACEABILITY_ASSESSMENT.md` §6.2）**：本条的"给值补**出处**"解法
  同时覆盖**列级「值 → 列」绑定**这一类缺陷 —— 转置表（列名竖排、值竖排）会被压成
  `measurements=[{time: 第一个值, values: {12 列}}]` 一行，`time` 语义被污染，
  且**列与值只靠位置对齐**（p38 实测 7 个时刻值要分给 6 个时刻列，多出的 `18 时 30 分`
  被重复使用）。**两处是同一解法的两个实例 ⇒ 必须一次性定 schema**（避免两次改
  prompt 版本 + 两次破 prompt caching），故并入本条评估。


- [ ] **B1-17 同页同项 finding 完全重复发射（p39 ×2）**（P2，2026-09-21 Round 53 从 B1-15 回放观测）
  现象（真实 51 页回放，`devlogs/_verify/verify_b115_replay.py` 的 R8 现状清单）：
  p39 的 `设备及管路是否及时清洗干净` 产出**两条完全相同**的 finding
  （同 page / 同 type / 同 severity / 同 description）。
  ⚠️ 与 B1-1（同根因**跨条目**聚合降噪）**不同侧**：这是**同一条**判据在同一次运行里
  重复收集（更可能是 `steps[]` 里同一检查项出现两次，或注册表/包装层被调用两遍）。
  动作：**先定位**是哪一层重复（`_check_check_consistency` 的输入里 checks 是否重复项 /
  `registry` 包装是否多注册 / 落库前是否少了一次去重），**再**决定在哪一层去重；
  ⚠️ 不要用"按描述字符串集合去重"这类**抹平**做法收尾 —— 那会把**同一页两个步骤
  里合法的同名检查项**也一起吞掉（真实记录里同一检查项在不同步骤重复出现是正常的）。
  验收：构造"同一步骤内 checks 重复"⇒ 去重；构造"两个步骤各有一条同名检查项"
  ⇒ **两条都留**（正向对照，防过度去重）。变异验证：把去重判据去掉 / 放宽成按 item 名去重 ⇒ 各红一条。
  定位探针：`devlogs/_verify/probe_b117_dup.py`（待建）。


### B2 正确性与可观测性

- [x] **B2-1 `_get_analyzed_pages` 改 fail-safe**（P2，本轮新发现）
  ✅ **2026-09-20 Round 49 已修**。`core/pipeline/stage2.py:_get_analyzed_pages`
  的 `except Exception: pass` 改为：**视为未分析 + `logger.warning`（带 job_id/page/
  异常摘要）+ `continue`**。旧行为下**损坏（截断/半写）的 `structured_json`** 会
  落到 `analyzed.add(page)` ⇒ 该页被**永久**当作"已分析"，retry 不再重跑且**零日志**
  （用户点重试毫无变化、日志里也无任何线索）。
  ⚠️ 注意与既有行为的边界：`_parse_error` **合法 JSON** 分支早已 `continue`（旧注释
  已描述），本条修的是它**下面的**那条静默路径 —— 两者不可混为一谈。
  护栏 `tests/unit/test_pipeline.py::TestSilentFailureLogging::
  test_corrupt_structured_json_is_not_treated_as_analyzed`（三类输入：合法+标记 / **损坏** /
  正常 ⇒ 断言 `analyzed == {3}` 且 caplog 含"无法解析"）；反向控制 = 正常页**必须**
  仍被判为已分析（修复不得把正常页也算成未分析）。
  变异验证：改回 `except: pass` ⇒ **红**。
  现象：`structured_json` 解析失败 ⇒ `except: pass` ⇒ 该页被算作"已分析"⇒ **retry 不重跑 + 零日志**。
  证据：`core/pipeline/stage2.py:488-497`。
  动作：解析失败 ⇒ **视为未分析**（可被 retry 重跑）+ 记一条 warning（含 page 与异常摘要）。
  验收：构造损坏 `structured_json` ⇒ retry 能重新分析该页且有日志；变异验证：改回 `pass` ⇒ 护栏红。

- [x] **B2-2 诊断类静默失败补日志**（P2）
  ✅ **2026-09-20 Round 49 已修**。三处静默补 `logger.warning`，**控制流一律不变**
  （保持软失败语义）：
  ① `core/pipeline/stage1.py` `_pdf_page_diagnostics(ocr_pdf_path)` 失败；
  ② 同文件「原件诊断 + `ocr_input_normalized_diag` 审计行」失败（这条会让
     "此件为何被规范化"的**审计行缺失**，必须留痕）；
  ③ `core/pipeline/self_heal.py` 既往 `ocr_diagnostics` 读取 —— **顺手抽成单一
     实现点 `_load_prior_diagnostics(db, job_id, pages)`**（原本是内联的 14 行
     双 `except: pass`，不可单测；抽出后既可测也消除了重复）。
  为什么必须留痕：这两类失败都会**把失败伪装成正常** —— ① 让"页级诊断全空"与
  "这份 PDF 真的没有异常页"不可区分；③ 让"既往诊断**读不出来**"与"这页**本来
  就没有**诊断"不可区分，而 GMP 追溯链要的恰恰是这个区分。
  护栏 `TestSilentFailureLogging::{test_page_diagnostics_failure_is_logged_and_soft,
  test_load_prior_diagnostics_logs_corrupt_and_db_error}`；后半条同时断言
  **DB 失败仍返回空 dict（软失败）而不是抛出**。
  变异验证：逐处改回静默 ⇒ **4/4 各自打红**（含 DB 失败那处）。
  证据：`core/pipeline/stage1.py:179`（`_pdf_page_diagnostics`）、
  `core/pipeline/self_heal.py:727`（`ocr_diagnostics` 解析）。
  动作：失败时 `logger.warning`（带 job_id/page/原因），**不改变控制流**。
  验收：注入异常 ⇒ 日志可见；且**不得**因此让整页失败（保持软失败语义）。

- [ ] **B2-3 `self_heal` 的 `to_thread(fitz)` 移入 procpool**（P1，旧）
  现象：fitz 是 C 扩展、持 GIL 数十秒，会饿死事件循环；且**取消请求在该段最长要等到看门狗 2400s**。
  证据：`core/pipeline/self_heal.py` 多处 `asyncio.to_thread(fitz...)`；`watchdog.py` 取消阈值。
  动作：重活走 `core/procpool.py`；并补**段内取消检查点**。
  验收：人为拉长 fitz 调用 ⇒ 事件循环仍响应 `/health`；取消请求在**秒级**生效（不再等 2400s）。
  ⚠️ 先做**定位实验**证明当前确实阻塞（不要凭读码改）。

- [ ] **B2-4 Stage3 同步 CPU 移出事件循环**（P1，旧）
  证据：`core/rules/__init__.py:133` 同步 `spec.check(...)`；`core/stage3.py` 多处同步挂载。
  动作：与 B2-3 同批处理（走 procpool 或分片让出）。
  验收：51 页 job 的 `/health` 延迟在 Stage3 期间不劣化。

- [ ] **B2-5 #127 全局性故障仍呈现为绿色成功**（P1，**机制已落地 · 验收未实测**）
  ⚠️ **2026-09-20 状态更正（勿再当"完全未修"报）**：本条主体已在 **v1.1.6 落地** ——
  提交 `4513395`（`fix(llm/stage2/stage3): #127 配置级故障类型化 + 首因提升到 job 级 +
  早停 + 零页产出落 error`）；`core/pipeline/stage2.py` 现有 `config_error` 暂存 dict
  （`stage2.py:33/56/150/181`）+ `config_error_job_message()`，前端非绿点见 #127-D。
  **仍未闭合的两处残留**：① **分片路径**（`OCR_SLICES>1`）不传 `config_error`
  ⇒ job 级首因失效，已单列 **#144**；② **本条的验收动作从未真跑过**（"把 key 改成无效
  ⇒ 界面明确失败"）—— Round 42 的 7 轮 e2e **全部用可用 key**，所谓"LLM 路径坏了"
  这一支未覆盖，e2e 驱动也**不做**该轮次（已单列 **B1**）。
  ⇒ 待 **B1 + #144** 完成后，本条才可勾选。
  现象（原记录）：LLM key 失效 ⇒ 页级 `_parse_error` ⇒ `partial_review` 时
  `jobs.error_message` 恒 NULL ⇒ 前端 `upload.js:326` 是 `bg-success` 绿点、
  **0 条 finding、无原因** ⇒ 与"记录真的无异常"不可区分。
  动作：类型化异常透传 + 早停 + job 级信号 + **非绿点** + 机检护栏。
  验收：把 key 改成无效 ⇒ 界面**明确失败**且给出原因；变异验证：去掉 job 级信号 ⇒ 护栏红。

- [x] **B2-6 `Stage 2` 日志的 `measurements=` 恒为 0（P2，Round 42 实测新发现）**
  ✅ **2026-09-20 Round 49 已修**。`core/pipeline/stage2.py:_analyze_one` 改为
  **从 steps 汇总**并**同时打印两个口径**：`steps_count` 与
  `measurements_count = sum(len(s.get("measurements") or []) for s in steps)`，
  日志形如 `(confidence=high, steps=2, measurements=4, findings=0, payload=… bytes)`。
  定位复核（先实证，非推断）：真实 51 页回放库逐页实测，`structured_json`
  **顶层确无** `measurements` 键（顶层键为 page_info/event_year_groups/steps/
  findings/time_anomalies/ocr_noise/overall_confidence），值全在
  `steps[].measurements[].values` ⇒ 旧写法在任何页上都恒为 0。
  护栏 `tests/unit/test_pipeline.py::TestLogTruthfulness::
  test_stage2_log_counts_measurements_from_steps`：断言**直接打在日志文本的数字上**，
  且 fixture 刻意让 `steps=2` 与 `measurements=4` **不相等** —— 否则无法区分
  "读错键"与"读对了但凑巧相同"。
  变异验证：把该行改回 `len(structured.get("measurements", []))` ⇒ **红**。
  现象：逐页日志形如
  `Stage 2: Page 42/51 LLM done in ...ms (confidence=low, measurements=0, findings=4, ...)`，
  其中 `measurements` **在任何页上都是 0**，无论实际提取到多少测量值。
  证据：`core/pipeline/stage2.py:320`
  `measurements_count = len(structured.get("measurements", []))` —— 读的是**顶层**键，
  而真实结构里 `measurements` **嵌在 `steps[]` 内部**，顶层根本没有这个键。
  实测（视觉 + 直查库双证）：
  - 真实文档 p8 的 `structured_json` 顶层 keys = `page_info / event_year_groups / steps /
    findings / time_anomalies / ocr_noise / overall_confidence …`，**无 `measurements`**；
    而 `steps[0].measurements` 有 **7 条**（每个时间点一条），每条含 **13 个**测量值
    （`进料_压力` / `回流_流量` / `TMPs` …）。
  - 本轮 real 轮 48 个已完成页**全部** `measurements=0`。
  - **只影响该日志行**（全仓 `measurements_count` 仅 2 处引用，均为日志）⇒ 不改变功能：
    `param_out_of_spec` 正常产出（pdf 2 条 / img 12 条 / real 38 条）。
  影响：**误导排障** —— 看日志会以为「LLM 完全没提取到测量值」，从而去查一个不存在的
  提取故障；而数据完好地存在 `steps[].measurements[].values` 里。这正是「日志要方便定位」
  的反面（用户明确关注项）。
  动作：改为从 steps 汇总，例如
  `sum(len(s.get("measurements") or []) for s in structured.get("steps") or [])`；
  并顺带记 `steps` 数与测量值总数（两个口径都可见，避免下次再被单一数字误导）。
  验收：跑一页含测量值的真实页 ⇒ 日志 `measurements` > 0 且与直查库一致；
  变异验证：改回读顶层 ⇒ 立刻重回恒 0。
  ⚠️ 需升版重建。

- [x] **B2-7 TOCTOU：并发上限检查与 INSERT 原子化**（P2，旧）
  ⚠️ 2026-09-20 重编号：本条原写作 `B2-6`，与上方 Round 42 新增的
  「`measurements=` 恒为 0」**撞号**（清单里出现两个 `B2-6`）⇒ 此处让位改 `B2-7`，
  上方 Round 42 那条保持 `B2-6` 不变。
  证据：`api/jobs/upload.py:99-104`（COUNT 持锁）与 `:349`（INSERT 持锁）之间隔写盘/转 PDF。
  ✅ **2026-09-20 Round 51 已修**（做法与原文**略有调整**，见下）。
  **定位（实证）**：把 N=4 个上传用"闸门"同时卡在两道检查之间（首次读盘时等待），
  在 `MAX_CONCURRENT_JOBS=1` 下实测 **4 个全部成功**（`len(ok)==4`）—— 上限形同虚设。
  **处置**：把**授权式**配额检查放进 **INSERT 的同一把 `db_lock` 内**
  （检查与写入之间不释放锁、无 await 让出点）⇒ 对外原子。函数开头保留一道
  检查，但**明确降级为"尽力而为的前置快检"**（只为避免让用户白传 200MB 大文件，
  非授权依据）。抽出 `api/jobs._count_active_jobs()` 作**单一实现点**，upload（两道）
  与 retry 共用（此前 SQL 各写一份）。
  ⚠️ **为何不用"占位 INSERT"**（原文建议）：占位行需要在**每一条失败路径**上删除
  （大小超限/页数超限/magic bytes/图片转换失败/去重 409…），任何一条漏删都会留下
  `pending` 且无 task 的**幽灵任务** —— 它会永久占额度、在列表里可见、且看门狗
  只监视"pending + 有活 task"（见 `upload.py:404-408`），不会被收敛。
  改为"检查与真实 INSERT 同锁"后**无需任何占位/清理机制**，语义更简单也更强。
  **性能未退化**：PDF 写盘仍在锁外，锁内只多一次 COUNT。
  **验收（已达成）**：`tests/integration/test_api_jobs_upload.py::TestUploadQuotaIsAtomic`
  —— 闸门确定性复现窗口（**反空断言**：先证 N 个请求都进到窗口）+ 断言成功恰 1 个、
  其余 409、DB 活跃数恰为 1；另有**正向对照**（额度已满时应在快检即被拒、不得读盘）。
  **变异 2/2**：① 授权式检查失效 ⇒ 红（红灯原因已核 —— 4 个全成功）；
  ② 前置快检失效 ⇒ 对照用例红。脚本 `devlogs/_verify/mutate_b27.py`（逐字节还原）。
  📌 **同族未修（另记 B2-14）**：`retry` 的守卫是**同样的两段式**形状。

- [ ] **B2-14 retry 的并发守卫与状态迁移之间同样有 TOCTOU**（P3，Round 51 修 B2-7 时发现）
  证据：`api/jobs/actions.py:66-77` 在 `db_lock` 内 COUNT，**释放锁之后**才
  `await transition_status(...)` 把 job 置为 `pending` ⇒ 两个并发 retry（或
  retry 与 upload 同时进行）可**双双**通过计数，实际活跃数超出 `MAX_CONCURRENT_JOBS`。
  与 B2-7 是同一类缺陷，但**修法不同**：`transition_status` 内部**自己取
  `core/pipeline/db_lock`**（同一把、**非重入**，见 `core/pipeline/state.py:114-136`
  与 `locks.py:127`）⇒ 不能"持锁再调它"（会死锁），必须改用
  `_transition_status_unlocked` 并在同一临界区内完成"计数 + 迁移"。
  因涉及状态机锁语义（并要核对取消/删除路径是否也走该函数），本轮**不动**，
  单独作为一项处理。验收：并发 retry 脚本卡在 `MAX_CONCURRENT_JOBS`；变异 ⇒ 红。

- [x] **B2-8 分片路径逐片日志把"累计新增"当成"本片新增"（P2，Round 43 新发现）**
  ✅ **2026-09-20 Round 49 已修**。`core/pipeline/engine.py` 片循环内新增
  `slice_new`（**片内**计数），日志改为
  `persisted ({slice_new} this slice, {new_pages} total)` —— 两个口径同时可见，
  既不丢"共落多少页"也不再说谎。
  ⚠️ `:611` 的最终汇总 `{new_pages} new pages` 仍用累计值（**那是对的**，未改动）。
  护栏 `TestLogTruthfulness::test_sliced_log_reports_per_slice_new_pages`：两片规模
  刻意不同（**1 页 / 2 页**）⇒ 断言 `(1 this slice, 1 total)` / `(2 this slice, 3 total)`；
  旧写法会打 `(3 new)`（累计），与"本片 2 页"不符 ⇒ 断言必红。
  变异验证：日志改回 `({new_pages} new)` ⇒ **红**。
  现象：分片 OCR 的第 2 片起，日志 `Stage 1 (sliced): slice start=N pages=10 persisted (X new)`
  里的 `X` 是**跨片累计值**，不是本片新增页数。
  证据：`core/pipeline/engine.py:488` `new_pages = 0`（片循环**外**）→ `:548` `new_pages += 1`
  → `:556-559` **在片循环内**打印 `({new_pages} new)`。
  影响：属"**日志说谎**"（用户明确关注项）—— 按日志核对"本片落几页"会被误导。
  ⚠️ `:611` 的最终汇总用累计值是**对的**，别一起改错。
  动作：片内日志改为 `({len(pages)} this slice, {new_pages} total)`。
  验收：跑一次 `OCR_SLICES>1` 的两片输入 ⇒ 第 2 片日志的"this slice"= 本片规模；
  变异验证：改回原样 ⇒ 第 2 片日志再次显示累计值。

- [ ] **B2-9 审计写失败被静默丢弃 + 看门狗自述不含"停滞 job 清单"**（P3×2，Round 43 新发现）
  ① `core/pipeline/state.py:31-32`：`_audit_log` 失败仅 `logger.warning`，**无重试、无告警**
  ⇒ GMP 追溯链可能出现**空洞且无信号**（`pipeline_complete`/`stuck_recovery` 等关键动作失败
  会淹没在一条 warning 里）。
  ② `core/watchdog.py:501-518` `status_snapshot()` 只回聚合**计数**，
  而 `find_stalled_jobs()`（`:279`）本可给出具体 job —— 外部监控因此**无法定位是哪个 job 卡了**，
  与「不看日志也知道卡在哪」的目标有落差。
  动作：① 关键审计失败升级为 `logger.error` 并计入可观测指标（或轻量重试）；
  ② 自述端点增加 `currently_stalled`（复用 `find_stalled_jobs`，limit 保护）。
  验收：① 注入一次 DB 写失败 ⇒ 日志出现 error 级且指标可见；② 造一个停滞 job
  ⇒ `/api/health/watchdog` 直接给出该 job id 与已停滞秒数。

- [x] **B2-10 前端「过渡期与口径」4 处**（P2×2 + P3×2，Round 43 新发现）
  ✅ **2026-09-20 Round 50 处置完毕**（4 处全修；机检新增 16 条；**变异 9/9** 全被捕获）。

  **① 两类 error 帧不分（P2）—— 定位（实证）**：`api/jobs/status.py` 确有两类
  `type=error` 帧，语义相反：`:407` 的 `'进度查询失败'` 发帧后 **`continue`**
  （设计上应重试、连接不关），`:419` 的 `'任务不存在'` 发帧后 **`return`**（终态）。
  而 `static/review.js:298` 只判 `d.type === "error"` 就把两者混为一谈
  ⇒ **一次 DB 抖动被谎报成"任务已被删除"，同时进度流永久断开**（丢实时更新 + 理由说谎）。
  **处置：判据改由服务端下发** —— 两类帧各带显式 `terminal`（`False`/`True`），
  前端经 `PbcStatus.sseErrorAction(d)` 分支：仅 `terminal` 关流；瞬态只把文案换成
  「进度查询异常，重试中…」并**保持长连**（不 close / 不清轮询定时器）。
  ⚠️ **不按 `message` 文案判定**：文案属展示层，改文案会静默改变控制流（与"理由说谎"
  同族）。字段缺失按**瞬态** fail-safe（宁可多留连接，也不谎报任务被删）；服务端
  **必带该字段**由集成用例锁定，故不会长期缺失。

  **② 终态文案漏中文映射（P2）**：`else` 分支改调 `PbcStatus.statusZh(d.status)`，
  不再落**裸英文 token**（此前 `review` / `partial_review` / `done` 都显示英文，
  而同页徽章是中文 ⇒ 同页两处不一致）。

  **③ error 转态期不显示原因（P3）**：新增 `showJobErrorBanner()` —— 按需创建/更新
  SSR 的**同一条**横幅 `#job-error-banner`（幂等；文案与 SSR 逐字一致；原因走
  `textContent` 防注入）。终态帧取 `d.message`，普通帧取 `d.error_message`
  ⇒ **转态即刻可见原因**，不必等 1.5s 自动刷新后由 SSR 给出。

  **④ 状态中文映射第 3 份副本（P3）**：删除 `review.js` 内联 `statusZh`，徽章与进度
  文案统一走 `PbcStatus.statusZh`（此前**文字与颜色不同源**：颜色早已走共享件）。
  注：同页另有**合法**的两份映射（finding 状态 / finding 类型）⇒ 机检以"**job 专属键**"
  精确定位（`ocr_running`/`analyzing`/`partial_review`…），并按 §二十八 做
  花括号配对的结构化提取，不误伤、不误报。

  **护栏（`tests/unit/test_status_js.py` 新增 4 类共 16 条）**：
  `sseErrorAction` 纯函数**行为**判据（node 实跑）＋ 错误分支「**包围条件 + 相对位置**」
  结构化判据（`es.close()` 必须晚于 `terminal` 判定、瞬态分支内不得出现 close/clearInterval）
  ＋ 提取器**正向对照** `test_detector_is_not_vacuous`（防空断言）＋ 键集覆盖
  （`JOB_STATUS_ZH` 每个键 JS 侧都必须有）。集成侧：两类帧**各带** `terminal` 字段。
  **变异验证 9/9**（`devlogs/_verify/mutate_b210.py`，逐字节还原自校验）：瞬态分支短路 /
  按文案判定 / 恒终态 / 文案退回裸英文 / `statusZh` 原样返回 / 徽章重新内联映射 /
  不再消费 `error_message` / 两类帧各缺 `terminal` ⇒ **各自打红对应用例**。

  ⚠️ **过程中修掉一个自己写的空断言**：③ 初版判据是 `"d.error_message" in src`。
  变异 M6（把调用改成 `showJobErrorBanner(undefined)`）**照旧全绿** —— 因为**条件行**
  `if (d.status === "error" && d.error_message)` 里还有一处同名标识，`in src` 被
  "另一处提及"满足。已改成钉**消费点本身**（完整调用表达式 + 其包围条件）。教训固化进
  `docs/PROJECT_PITFALLS.md` **§三十**（连同"验证脚本必须带超时，否则挂起会**留下源码
  处于变异态**"）。

  ---

  **原始记录（问题陈述，保留备查）**
  ① **`type:error` 两类帧不分**（P2）：`static/review.js:259-268` 只判 `d.type === "error"`
  就关流并写「任务不存在或已被删除」；而后端 `api/jobs/status.py:402` 的
  `'进度查询失败'` 是**设计上应重试**的（发帧后 `continue`），只有 `:411` 的
  `'任务不存在'` 才是终态 ⇒ **瞬态 DB 抖动被误判为"任务被删"**，进度流永久断开 + 文案说谎。
  ② **终态文案漏中文映射**（P2）：`review.js:372-379` 只处理 `cancelling/cancelled/error`，
  其余落 `else` 显示**原始英文 token**（`partial_review`/`review`/`done`），
  而同页徽章走 `statusZh` 是中文 ⇒ 同一状态同页两处不一致。
  ③ **error 转态期不显示原因**（P3）：`review.js` 全文未消费 `error_message`，
  仅显示"出错"，要等 1.5s 自动刷新后由 SSR 横幅给出。
  ④ **状态中文映射第 3 份副本**（P3）：`review.js:385-390` 自带 `statusZh`，
  而同页颜色却走共享 `window.PbcStatus.statusDotClass` ⇒ **文字与颜色不同源**。
  动作：① 按 `d.message` 分支（仅"任务不存在"关流）；② `else` 改调
  `window.PbcStatus.statusZh(d.status) || d.status`；③ error 分支复用 SSR 的
  `#job-error-banner`；④ 删本地副本、统一走 `PbcStatus`。
  验收：① 模拟一次 DB 异常帧 ⇒ 流**不断开**；② 终态过渡期文案为中文；
  ③ error 转态即刻显示原因；④ 机检断言"状态中文映射只有一处真值"；
  **变异验证**：逐条改回原样 ⇒ 对应护栏必须红。

- [ ] **B2-11 聚合流 `/live` 在 DB 异常期不 yield 任何帧**（P3，Round 43 新发现）
  现象：`api/jobs/listings.py:187-192` 异常分支只 `logger.error` + `await asyncio.sleep(...)` + `continue`，
  **不发送任何帧**；对照单任务流 `api/jobs/status.py:399-404` 异常时仍每 2s 发 `id:` + 错误帧。
  影响：经反向代理时聚合流可能因**空闲超时**被断（前端有自动重连 + 10s 轮询兜底，
  故后果是瞬时空窗，非永久丢失）。
  动作：与单任务流对齐 —— 异常期也发一帧（带 `type` 字段的普通 message 帧，**不得**用 `event: error`）。
  验收：注入 DB 异常 ⇒ `/live` 仍每 2s 有帧。

- [x] **B2-12 `_check_grounding` 把「当前年份」当幻觉数字 ⇒ 降级理由说谎**（P2，Round 46 发现）
  ✅ **2026-09-20 Round 49 处置（含一处结论更新，勿再按旧描述报）**。
  ① **定位结论（先实证，非推断）**：Round 46 记录的现象**已被 B1-4 ① 顺带消除**。
  51 页真实回放实测：采用「当前日期/当前年份」参照物的条目共 **21 条** ——
  **15 条被 ① 正确抑制**（理由="依据是墙钟而非记录内部一致性"）、**6 条 fail-open 保留**
  （如「操作者签名日期早于当前日期」「记录发放年份 > 当前年份+1」凑不出两侧字面量，
  本就没有被 L2 扣帽子）；**L2 降级中 token 命中「文档自述当前日期」池的残留 = 0 条**
  （原记录为 14/19）。⇒ 旧现象不再出现，但**护栏缺失**：判据顺序一旦调整
  （① 提前 fail-open），现象会**静默回归**且无人看得见。
  ② **处置**：`_check_grounding` 新增 `exclude_dates` 参数，按**年份**排除文档级
  自述当前日期（`_document_current_dates`），调用点传入 `current_dates`。
  这样即使 ① fail-open，L2 的理由也不会变成假话 —— "2026" 本就来自系统提示词
  注入的当天日期，**不该出现在 OCR 原文里**，因此"无法定位"不能等于"幻觉"。
  ⚠️ 按年份而非整条日期排除：字面量粒度不一（`当前年份 2026` vs `2026.09.18`），
  取**保守方向**（宁可少扣一次"幻觉"，也不把系统注入值说成编造）。
  护栏 `tests/unit/test_llm_finding_guard.py::TestL2GroundingExcludesSystemDates`
  （4 例）**含双向对照**：`test_exclusion_is_load_bearing` 证明"去掉排除集就真会被
  扣幻觉帽子"（防空断言）、`test_genuinely_fabricated_number_still_flagged` 证明
  真·凭空数字仍被标出（防把 L2 打哑）、`test_exclusion_tolerates_mixed_literal_granularity`
  覆盖粒度混合与"池外年份不得被误排除"。
  变异验证：① 调用点不传排除集、② 去掉内部过滤 ⇒ **各自打红对应用例**。
  现象：实测 **19 条 grounding 降级里 14 条**是 `suspicious_date` 的"当前年份 2026"，
  例如「生产日期 2025年01月20日 与 当前年份 2026年09月18日 不符」被降级的理由是
  "数字（2026）在 OCR 原文中无法定位，疑似提取幻觉"。
  **"2026"来自系统时钟，本就不该出现在 OCR 原文里** ⇒ 这个理由**是错的**。
  （降级的**方向**没错 —— 这些条目确实不构成异常 —— 但**理由**会误导排障，
  与 B2-6「`measurements=` 恒为 0」同属"日志/理由说谎"一类。）
  动作：L2 的溯源对象应**排除系统注入值**（当前日期/当前年份），或让这类条目改走
  B1-4 的"与当前日期比较本就无意义"分支 —— 不要扣"凭空捏造"的帽子。
  验收：回放中"当前年份"型降级不再出现"疑似提取幻觉"字样；变异验证：放宽排除 ⇒ 红。

- [ ] **B2-13 SSR(Python) 与 SSE(JS) 的 job 状态**中文措辞不一致**（P3，Round 50 新发现）
  现象：同一个 job 状态，**首屏**（Jinja 走 `core/zh_map.py::JOB_STATUS_ZH`）与
  **SSE 实时更新后**（走 `static/status.js::STATUS_ZH`）显示的中文**不同**：

  | 状态 | SSR(Python) | SSE(JS) |
  |---|---|---|
  | `ocr_running` | OCR 解析中 | 识别中 |
  | `review` | 待复核 | 可复核 |
  | `partial_review` | 部分完成待复核 | 部分可复核 |
  | `error` | 失败 | 出错 |

  ⇒ 同一页面**刷新前后文案会变**（用户会以为是不同状态）。B2-10 的 ② / ④ 消除了
  "英文 vs 中文"的不一致，但**没有**消除"两套中文"的不一致。
  为何本轮不动：统一措辞 = 改变用户可见文案，需先定"以哪一侧为准"（SSR 侧更贴近
  `db/schema.sql` 的语义、JS 侧更短），且可能影响既有文案断言（`e2e_*` / 模板测试）。
  动作（待定）：① 定基准侧；② 让另一侧**派生**（而不是各自维护）；③ 机检两侧
  **逐值等价**（现只锁颜色 `statusDotClass` 与**键集**，未锁措辞）。
  验收：同一状态在 SSR 与 SSE 下文案逐字相同；变异验证：改任一侧措辞 ⇒ 红。

### B3 可维护性

- [ ] **B3-1 状态串收敛为单一真值**（P1，旧）
  现状 ≥4 处：`state.py:35` / `state.py:139` / `api/jobs/__init__.py:111` / `zh_map.py:28`，
  外加 SQL 字面量 `'pending'`/`'error'`/`'archived'`。
  动作：一份 `JobStatus` 枚举 + 派生集合（`_ACTIVE`/`_STUCK` 从转移表派生）+ 中文名映射。
  验收：机检断言"除真值源外无状态字面量"；变异验证：新增一个状态 ⇒ 缺映射处**立即红**。

- [x] **B3-2 procpool 并发度**（P2，旧）
  现状 `max_workers=1` ⇒ 多 job 时本地 CPU 重活串行。
  动作：先**实测**单 job 与并发 job 的收益，再决定是否提到 2；不可盲目调大（内存/CPU 争用）。
  验收：给出实测数据（不是推断）后再改。
  ✅ **2026-09-20 Round 51：实测后提到 2**。脚本 `devlogs/_verify/bench_procpool.py`
  （走**真实** `core.procpool.run_cpu` 路径，只替换池大小；12 个 CPU 任务/场景，14 核机）：

  | worker×jobs | 墙钟(s) | 单任务(s) | 子进程 RSS |
  |---|---|---|---|
  | 1 × 1 | 1.40 | 0.116 | 59 MB |
  | 1 × 3 | **1.41** | 0.117 | 59 MB |
  | 2 × 3 | **0.85** | 0.071 | 125 MB |
  | 3 × 3 | 0.71 | 0.059 | 190 MB |
  | 2 × 1 | 1.42 | 0.118 | 119 MB |

  结论：① 单 worker 下 **jobs=1 与 jobs=3 墙钟相同**（1.40 vs 1.41）⇒ 并发 job 完全被
  串行化、**零收益**；② 3 并发 job 时 1→2 worker **加速 1.64x**，而 `MAX_CONCURRENT_JOBS`
  默认就是 3 ⇒ 并发是**设计内**场景；③ 每 worker ≈60MB（+60MB ≈ 已记录最坏占用 ~2GB 的 3%）；
  ④ 提到 3 的**增量只有 1.21x** 而再 +65MB ⇒ 取 **2**。
  另有一条**与负载无关**的理由：worker 数很少时，一个挂死的 worker 会占住槽位；
  worker=1 时即占满 ⇒ 故障从"某个 job 卡住"扩散成"整个应用不再处理新任务"。取 2 使
  爆炸半径减半（`_recycle_pool` 仍是兜底）。
  ⚠️ **顺带更正一处文档说谎**：原注释称"多 worker 只会增加内存而无吞吐收益"，
  实测**只对单 job 成立**，已按数据更正（否则会误导后续决策）。
  护栏：`tests/unit/test_procpool.py::TestPoolConcurrencyIsCalibrated` —— ① 标定值未被
  无声改动（改它必须先有新测量）；② 并发度由 `_POOL_MAX_WORKERS` **单点**决定，
  用 **AST** 判据（首版用正则查 `max_workers=<数字>`，**被顶部数据表的注释打红** ⇒
  §二十八 的文本匹配坑，已改为 AST + 反空断言）。**变异 2/2**（`devlogs/_verify/mutate_b32.py`）。

- [ ] **B3-3 `RuleSpec.severity` 与实际发射不一致，且无人消费**（P3，Round 45 修复 B1-9 时发现）
  现象：注册表声明 R1a/R1b 的 `severity="warning"`
  （`core/rules/registry.py:119,121`），但实现里硬编码发 `critical`
  （`core/rules/rule_time.py`）—— **声明与实现漂移**，且 `RuleSpec.severity`
  **全仓无任何消费点**（`grep '\.severity\b'` 在 `core/rules`、`core/pipeline`、
  `api`、`scripts` 均无命中），只有 docstring 说它"用于审计与前端分级"。
  影响：该字段目前是**纯摆设** —— 任何"以注册表为准"的外部工具（文档生成、
  前端分级、未来的规则目录页）都会拿到与真实行为不符的严重度。
  动作：二选一并补护栏 ——
  ① 让 `severity` 表示"本条规则可能发射的最高严重度"，并在规则注册表单测里
     **从发射结果反推**校验（需为每条规则造一个命中样本，成本较高）；
  ② 或明确废弃该字段（改为从实现里派生），避免两处真值。
  ⚠️ 先定位再决定：B1-9 已把 R1b 变为**同一 type 可发两种严重度**
  （真倒序 critical / 仅重叠 warning），单一 `severity` 字段本身已不足以表达。
  验收：机检能抓住"声明与实现不一致"（构造一处漂移 ⇒ 护栏红）。

- [x] **B3-4 写路径单一实现点 + 全仓生产死接口清零**（P2 → **已修**，2026-09-20）
  **① 定位（先定位再动手）**：写全仓 AST 扫描 `devlogs/_lint/deadscan.py`（判据 = 定义在库代码里
  + **生产侧零引用**；带装饰器的跳过；`tests/` 只算消费方），一次把"还有多少此类死接口"问清 ⇒
  共 **7 个**（5 个「仅测试引用」+ 2 个「彻底无引用」）。
  ⚠️ 首版扫描器太粗（把 pytest fixture、FastAPI 路由处理器都算进来，误报 268 个）⇒
  判据必须与 B3-4 对 `apply_review` 的**原判定同口径**。

  **② 逐项处置（关键：先判"是重复"还是"纯负债"，再决定"接线"还是"删除"）**
  1. `llm_finding_guard.apply_review` —— 死代码，**且 stage2/stage3 各抄一份并已漂移**（`.rstrip()`）
     ⇒ ✅ **接线**：两处改用它，重建逻辑只剩一个实现点。
  2. `kb.store.source_version` —— 死，但 `kb_version()` **内联同一取值** `srcs[sid]['_version']`
     ⇒ ✅ **统一**：`kb_version()` 改用它。
  3. `kb.store.entries_by_source` —— 死，但 `entries()` 无参分支**内联同一并集**
     ⇒ ✅ **统一**：`entries()` 改用它。
  4. `finding_quality.is_canonical` —— 死，但 `normalize_finding_type()` **内联同一判定**
     ⇒ ✅ **统一**：改用 `is_canonical()`。
  5. `rules.registry.rule_by_type` —— **纯死**（与 `rule_coverage` 的 `by_type` **语义不同**：
     前者含被禁用规则、后者只含启用）⇒ ❌ 删除 + 删其**直接单测**。
  6. `pipeline.locks.begin_children` —— **冗余第二构造入口**（`ChildTasks(job_id)` 已被
     `engine.py` / `stage2.py` 直接用）⇒ ❌ 删除 + 修正那条**推荐使用它**的用法注释。
  7. `rules.year_vote.vote_report` —— **纯死**，docstring 却称"落日志 / audit"
     ⇒ ❌ 删除 + 登记缺口 **B3-5**。

  ⚠️ **两个关键判断**（避开"一刀切删死代码"的错）：
  - **#5 不能统一**：`rule_by_type`（全表）与 `rule_coverage` 的 `by_type`（仅启用）**过滤集合不同**，
    强行合并会改变语义 ⇒ 只能删，不能并。
  - **#2/#3 不能直接删**：它们在测试里是**独立判据（oracle）**——`test_kb_version_is_combined_and_
    content_derived` 用各源版本**重算**组合版本、`test_entries_default_is_union_of_sources`
    用各源并集**重算** `entries()`。删掉函数会**削弱测试判别力** ⇒ 正解是让**生产侧用它们**，
    既消灭重复又保住对照。

  **③ 护栏**：`tests/unit/test_llm_guard_single_impl.py`（6 例）
  - (a) 正向：stage2/stage3 必须以 AST 形式调用 `apply_review`；
  - (b) 反向：`core/pipeline/` 不得直接调底层引擎 `review_llm_findings`（= 绕过唯一入口）；
  - (c) 反向：物化降级的标记 token `to_severity` 不得出现在 `core/pipeline/`；
  - **正向对照**：token 必须确实存在于唯一实现点，否则 (c) 是**空断言**（"扫不到"≠"没问题"）；
  - 断言走 `ast.Constant` **精确等值**而非文本搜索 —— pipeline 里 `｜` 另有合法用途（错误文案），
    文本搜索会假红。

  **④ 变异验证**：`devlogs/_lint/mutate_b34.py` ——
  M1 把重建逻辑抄回 stage2 ⇒ (a)(b)(c) **三条全红**；M2 改掉标记 token ⇒ **正向对照红**
  （证明 (c) 非空断言）；还原后全绿，并做**逐字节还原自校验**。
  ⚠️ 变异脚本首版用文本模式改源码，**踩到两个静默副作用**：`utf-8-sig` 写回给无 BOM 的文件
  **加了 BOM**（BOM 是**内容**、会进提交）、通用换行把 CRLF 折成 LF。已改**字节级**并加自校验
  （记入 `docs/PROJECT_PITFALLS.md` §二十四）。

  **⑤ 验收**：全量 `tests/unit + tests/integration` 通过（基线 2771）；`deadscan` 重跑 =
  **死接口 0 + 仅测试引用 0**。

- [ ] **B3-5 年投票（YearVote）的归一结论未落审计**（P3，2026-09-20 **由 B3-4 清理暴露**）
  `core/rules/year_vote.py::vote_report`（自述"投票结果的可审计摘要"）**从未被调用** ⇒
  把 `2027` 归一成 `2025` 这类**跨页年份投票**的结论，除规则产出的那条 finding 外**没有独立台账**，
  出问题时无法回答"为什么把 2027 判成 2025、各年支持页数多少、归一理由是什么"。
  已做的一半：删掉那个"看起来已实现审计"的死函数（它的存在本身在**误导**后来者）。
  待办：若要可审计，把 `vote.resolve(y)` 的逐票理由落 `audit_log`（至少 `logger.info`）。
  ⚠️ 这是**功能增强**（会改变输出），需与产品口径确认后再做，**不要顺手接上**。


### B4 体验 / 健壮性

- [ ] **B4-1 SSE 断点续传与心跳**（P3，**2026-09-20 Round 43 降级并更正**）
  ⚠️ **本条原登记的两项均不成立，勿再当缺陷报**：
  ① **`id:` 帧早已存在** —— `api/jobs/status.py:401/410/418/422`、`api/jobs/listings.py:194`
     （含终态 `event: done` 帧也带 `id`）；
  ② **不需要心跳注释帧** —— 循环**每 2s 必发一个真实数据帧**（`_SSE_POLL_SECONDS=2`），
     这本身就是天然保活。
  ③ `Last-Event-ID` **未解析是有意的设计**：每帧都是**幂等全量快照**，
     重连后立即自愈（`status.py:417` 有注释说明），故"真断点续传"在当前架构下非必需。
  **保留的真实缺口**（已单列 **B2-11**）：聚合流 `/live` 在 DB 异常期**不 yield 任何帧**，
  单任务流则每 2s 有帧 ⇒ 只有 `/live` 存在经代理空闲超时的风险。
  动作：只做 B2-11（对齐单任务流的异常期行为），本条**无需再做**。
  ⚠️ 不得新增 SSE 事件类型（`error` 是保留字，既有 AST 机检会拦）。

- [ ] **B4-2 凭据卫生：`feishu_app_secret` 明文在 config.json**（P3，本轮新发现）
  现象：本机 `%APPDATA%/PBC/config.json` 明文存 `feishu_app_id`/`feishu_app_secret`。
  动作：至少在设置页提示；或支持引用环境变量；评估是否加密存储。
  验收：文档写明存储位置与风险；若改存储方式需兼容旧配置。

- [ ] **B4-3 配置双轨 ⇒ "死 key 假红" 与自查盲区**（P2，Round 41 新发现）
  现象：`config.py:_config_path()` 让**开发模式读仓库根 `config.json`**、**冻结版读
  `%APPDATA%/PBC/config.json`**。两个文件装的**不是同一把凭据**：仓库那份是已吊销的 K1
  （实测 `401 {"code":30014,"message":"Token is invalid."}`），`%APPDATA%` 那份才可用（实测 200）。
  后果：① 开发模式 / 走仓库配置的 e2e **静默拿到死 key**，报 `30014` —— 与"凭据真有问题"
  不可区分（A4 已记录过一次同类假红）；② `scripts/check_leaked_keys.py:117` 只读仓库那份
  ⇒ 在只按装版使用的机器上会把"生效值"判成"不存在"，**漏报**（安全护栏漏报比假阳性更糟）。
  动作：自检脚本与轮换自查**同时读两条路径**；开发模式遇 `30014` 时显式提示"你正在用仓库
  `config.json` 里的已吊销 key"，而不是笼统的凭据错误。
  验收：护栏可离线断言"自查工具会读两条路径"（造两个临时 config，断言都被读到）；
  `DEPLOYMENT.md` 已写明该风险（✅ 本轮已加）。

- [x] **B4-4 【P1】provider 被启动逻辑自动改写并持久化（界面零提示）**（Round 43 确证）
  ⚠️ 本条即上一轮 §14.4 列为「**未复核**」的那一项，**本轮已确证**（不再是"疑似"）。
  现象：启动时若 active provider 的 key 未通过 `_is_real_key`，则**自动切到第一个
  "有真 key"的 provider**，改写 `os.environ["LLM_PROVIDER"]` **并写回配置文件**。
  证据：`config.py:714-726`（改写 + `_persist_env_to_config`）；判定在 `config.py:659-669`
  （`_is_real_key` = **子串匹配** `TEST_KEY_PATTERNS`，`config.py:645-656`）。
  精确表述（**比转述更收敛**）：它**有日志**（`logger.warning("Auto-activating…")`），
  所以**不是"完全静默"**；真正的问题是 ① 决策**只落日志、界面零提示**，
  ② **会持久化覆盖用户在设置页的显式选择**，③ 判据是**子串匹配**（真实 key 若含
  `placeholder` / `xxxxx` / `test-key` 等子串会被判"非真"）。
  影响：GMP 场景「**你以为的模型 ≠ 实际跑的模型**」，且事后难追溯。
  动作：① 仅当"**一个 key 都没配**"时才回退；**不得用测试/占位判定做生产切换**；
  ② 覆盖前在设置页**显式提示**（"已从 X 切到 Y，原因：X 未配置 Key"）；
  ③ `_is_real_key` 改精确格式校验（如 `sk-` 前缀 + 长度阈值），不要子串。
  验收：① 造"active 无 key + 另一家有名 key"→ 有提示且**不改写用户显式选择**（若用户已显式选过）；
  ② 造"active 的 key 含 `placeholder` 子串但格式合法" ⇒ **不得**触发切换；
  变异验证：去掉提示 ⇒ 护栏红。

  **✅ Round 48（2026-09-20）修复记录**

  **定位（比 Round 43 的转述更收敛）—— 实际有**两份**实现，且互相掩盖**：
  - 后端 `config.py::load_config()`（**import 期**执行）切换 + `_persist_env_to_config` 落盘；
  - 前端 `static/settings.js::load()` 又自己切一次（`POST /set_active_provider`）。
  后端先切完 ⇒ 前端条件 `!activeProv.configured` 恒不成立 ⇒
  **那条唯一带提示的路径（`autoReason`）被绕过** ⇒ 用户看到的就是"provider 被静默换掉"。
  ⇒ "界面零提示"的真实机制是**第二实现点把第一实现点的提示屏蔽了**，不只是"没写提示"。

  **处置（三处，全部收敛到单一实现点）**：
  1. **新增 `config._resolve_active_provider()` —— 决策的唯一实现点**。
     回退**只允许**在：用户**未显式选择**（配置里没有 `LLM_PROVIDER`）
     **且** active 的 Key **字面为空** **且** 另有 provider 的 Key 字面非空。
  2. **删除前端那份本地切换**（连带 `opts.autoReason` 与 `firstConfigured`），
     改为 `showAutoActivateNotice(current.llm.auto_activated)` —— **只呈现、不决策**。
  3. **`_is_real_key` 改精确校验**（子串 → 「精确值 + 模板前缀」，并统一
     `api/settings/read.py` 里**手抄的副本** `_is_real_api_key`）；
     零消费的旧名 `TEST_KEY_PATTERNS` 一并删除。

  **⚠️ 有意保留的取舍（已写进代码注释）**：切换判定**不再**使用占位启发式
  ⇒ 一个带**占位 key** 的 provider 也会被当作"已配置"而成为回退候选。
  这是刻意的：占位 key 会**响亮失败**（401/403，可追溯），
  而"启发式误判 ⇒ 静默换模型"是**无声**的（GMP 硬伤）。
  启发式保留在**显示层**（`configured` 标志），且新的提示会把它们串起来：
  「已自动切换到 X」+ X 显示"未配置" ⇒ 用户立刻可行动。

  **⚠️ 本轮自踩并当场修掉的两个同类错误**（都由新护栏抓出，记入 PITFALLS §二十七）：
  - 首版前缀表含 `sk-ant-test` ⇒ 朴素 `startswith` 把**真实 key**
    `sk-ant-testing-real-key-...` 判成占位 —— 正是本次要修的同一类错误。
    改为"**前缀后必须是非字母**"；
  - `sk-your-api-key` 是**词模板**（后接字母），前缀规则无法与真实 key 区分
    ⇒ 从前缀表移到**精确值**表。

  **验收（全部有真实执行证据）**：
  - `tests/unit/test_config_internals.py`：4 个决策用例（未显式⇒切换+持久化+提示；
    显式⇒**绝不改写**（并把"持久化即失败"写成断言）；含 `placeholder` 子串的真实 key
    ⇒ 不切换；全空 ⇒ 不切换）+ `TestIsRealKeyExactMatch` 6 例；
  - `tests/unit/test_settings_auto_activate.py`（新）：**单一实现点 + 界面可见性**机检 ——
    判据走 **AST 的"被调用名字"**（不查字面量，否则 docstring 里提到 `_is_real_key`
    就假红），并带**正向对照**（`setActiveProvider` 必须仍在，防"把功能删光也能过"）；
  - `tests/integration/test_api_settings.py`：`llm.auto_activated` 契约（两个取值都验，
    证明它不是恒 `None` 的装饰字段）+ 含子串 key 的 `configured` 必须为 True。
  - **变异验证（`devlogs/_replay/mutate_b44.py`）5/5 通过**（逐字节还原自校验通过）：
    改回启发式 ⇒ 红；去掉"显式选择"闸门 ⇒ 红；不再记录决策事实 ⇒ 红；
    删掉前端渲染**调用点** ⇒ 红；`_is_real_key` 改回子串 ⇒ 红。
  - ⚠️ 过程中被测试抓出的**实现缺陷**：`read.py` 原用 `from config import AUTO_ACTIVATE_NOTICE`
    = **导入期取值** ⇒ 状态变化读不到（也不可测）。改为**调用期读属性**
    （模块名与那个 dict 同名，须 `import config as _config_module` 别名导入）。
  - ⚠️ **跨测试污染**：`test_api_settings.py` 会注册带 key 的自定义 provider
    `anthropictest` ⇒ 合并跑时抢走回退候选。已在测试夹具里清 `LLM_PROVIDERS` 使其自洽。

  **✅ Round 48 追加（提交前复核，2026-09-20）—— 「提示渲染」自身的一处隐形契约**

  复核前端时发现：徽标 `#llm-provider-badge`（定义在 `templates/settings.html:127`）
  的**加载期写入点原本在 `fillOcrForm()` 里**，而 `showAutoActivateNotice()` 排在其**后**
  ⇒ 徽标文案（`（已自动从 X 切换）`）正确与否，取决于**两个函数的调用顺序**这一
  **隐形契约**：一旦重排，提示会被静默覆盖回纯名称，而**所有既有断言仍然全绿**。
  - 处置：把加载期写入**收敛到 `showAutoActivateNotice()` 一处**
    （无通知时也写回纯名称，否则徽标无人渲染）；`fillOcrForm()` 不再碰徽标。
    用户显式切换的乐观更新仍留在 `setActiveProvider()`（另一条流程，写入点共 2 个）。
  - ⚠️ **首版护栏 3 个变异漏过 2 个**（都是"文本断言"的固有盲区）：
    M2 把写入条件改成 `if (badgeEl && info && …)` ⇒ 无通知时徽标不再渲染，
    而"函数体含 `display(activeProvider)`"仍被**三元表达式的另一分支**满足（**空断言**）；
    M3 在函数开头插 `return;` ⇒ 写入成为**运行期死代码**，文本断言完全看不见。
  - 处置：判据改为**结构化** —— 看写入语句**之前**有没有 `return`、
    以及它所在 `if` 的**条件表达式**里有没有 `info`。改后 **3/3 全拦**
    （`devlogs/_lint/mutate_b44b.py`，逐字节还原自校验通过）。
  - 记录：`tests/unit/test_settings_auto_activate.py::test_badge_write_is_unconditional_and_reachable`。

- [ ] **B4-5 KB 条目数口径虚高 + 文档两套数字并存**（P2，Round 43 新发现）
  实测（本轮亲自跑，可复现）：`scripts/release_gate.count_kb_entries('core/kb/data')` = **477**，
  而检索器 `core.kb.store.entries()` = **441**；差值 **36** = 4 个 JSON 的 `chapters`
  （**纯章节标题元数据**：无正文、无 `entry_id`、**检索器根本不索引**）。
  影响：① 门禁 `kb_corpus` 以 477 通过（下限 200 仍过，但**指标失真**）；
  ② 文档据此写"477 条"，**夸大规模 8%**；③ 同一事实在两处写法不一
  （`README.md` 写 441，`ADVERSARIAL_AUDIT.md` / `PLAN_v1.1_EXECUTION.md` 写 477）。
  动作：`count_kb_entries` **分列**「条目数 / 章节数」（或只统计 `entries`）；
  文档权威口径统一为 **441**（可检索语料），并说明 477 的构成。
  验收：门禁报告同时给出两个数字；`grep -rn "477" docs/ README.md` 处已更新或标注；
  变异验证：把 `chapters` 加回统计 ⇒ 新护栏必须红。

### B5 验证补强

- [ ] **B5-1 CI 增加冻结产物 job**（= #106，挂了两轮未做）
  动作：CI 装 PyInstaller → 构建 → 跑产物冒烟与分发一致性；**必须先在本机证明该 workflow 能跑通**
  （未跑过的 CI 配置不算"完成"）。
  验收：一次真实 CI 运行里 A 组用例真的执行（不再是 artifact-gated skip）。

- [ ] **B5-2 #125 巡检首扫空窗**
  现状：巡检循环"先等一个周期再首扫"⇒ 启动后 60s 内 `last_scan_at` 恒 `null`（冒烟不能立刻断言）。
  动作：启动即回收一次 + 存活信号无空窗。
  验收：启动后立刻查 `/api/health/watchdog` 即有 `last_scan_at`。

- [ ] **B5-3 #174 两层重试相乘**
  现象：SDK 自带 `max_retries` × 我们的重试循环 ⇒ 桩收到 6 个请求（`retries=2` 时）。
  动作：显式统一为**一层**（关掉 SDK 重试或关掉自己的循环），并让总次数可预测。
  验收：桩收到的请求数 == 预期值；变异验证：把两层都打开 ⇒ 护栏红。

- [ ] **B5-4 生产模型（DeepSeek-V3.2）路径未被 e2e 覆盖**（P2，Round 42 实测新发现）
  ⚠️ 2026-09-20 补录：本条此前**只在 `docs/TODO.md` 的 Round 42 段被引用**
  （"已登记 B5-4"），清单里**没有对应条目** —— 属**悬空引用**，现补齐。
  现象：`tests/e2e_run.py:308` **硬编码** `siliconflow_model = "Qwen/Qwen2.5-72B-Instruct"`，
  而本机生效的生产配置是 `SILICONFLOW_MODEL = deepseek-ai/DeepSeek-V3.2`
  （`%APPDATA%/PBC/config.json`）。两者**都走 OpenAI adapter、协议一致**，
  故"链路可用"这一结论**有效**；但被测模型 ≠ 生产模型 ⇒ **生产模型这条具体路径本轮未覆盖**。
  风险：模型特有的输出形态差异（JSON 字段命名/嵌套、数值格式化、日期书写习惯、
  对 `response_format` 的遵守度）在 Qwen 上通过、在 DeepSeek 上未必。
  ⚠️ 这不是假设 —— Round 40/42 已实测到同类"模型层"缺陷（B1-4 日期判据方向反、
  B1-5 串位幻觉），**都出在 LLM 层**，因此换模型重跑是有意义的补强。
  动作：给 driver 加一个环境变量覆盖（如 `PBC_E2E_MODEL`，**默认值保持现状以免改基线**），
  用它跑一轮小样本（`--rounds pdf,img`，不必全 51 页），与 Qwen 轮做**同输入对照**。
  验收：产出两轮对照记录（findings 条数/类型分布/severity 分布差异）；
  变异验证：把显式传参去掉 ⇒ 断言"被测模型 == 传入模型"必须红。

### B6 泛化（#126）

- [ ] **B6-1 用「试用批记录.pdf」做第 2 份真实批记录验证**
  理由：目前**只有一份真实批记录**（丝裂霉素），泛化未验证；规则/阈值可能过拟合。
  动作：跑完整链路，记录：轮次、误报、漏检、耗时；与第 1 份对比。
  验收：产出一份对比报告；若发现过拟合 ⇒ 立刻登记缺陷。

### B7 测试与静态检查的基础设施（Round 48 提交前复核新发现）

- [x] **B7-1 全量测试在单次工具调用里会撞沙箱「每轮删除预算」⇒ 顺序性 flake**（P2）
  ✅ **2026-09-20 完成（Round 52）**。两条动作都做了，且都没走"只写文档"的省事路：
  ① **机制上消除**（不是记忆纪律）：`release_gate.run_cmd` 给**子进程**注入
  `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=100000`（新常量 `SANDBOX_BULK_DELETE_THRESHOLD`）。
  为什么必须**强制赋值**而不是 `setdefault`：宿主**已经**显式给了 50，`setdefault` 会
  变成空操作而 flake 照旧。为什么只抬阈值而不清安全网：删除仍走回收站，只是不再按
  次数触发确认 ⇒ 比 `CODEBUDDY_SAFE_DELETE_ENABLED=0` **更保守**。
  护栏 `TestSandboxDeleteIsolation` 用**真子进程打印自己的环境变量**验证（不是 mock 的假绿）。
  ② 文档固化进 `CLAUDE.md` 的 "Run tests"（含**真变量名**与"旧文档的 `BULK_THRESHOLD`
  不是真变量名"的更正）；`CODEBUDDY_SAFE_DELETE_ENABLED=0` 的构建用法仍在原处。
  ③ 🔴 **顺带修掉一个更严重的护栏漏洞**：`env_only` 原先只做
  `nodeid.startswith(ENV_ONLY_FAILURE_PREFIXES)` —— **前缀不区分失败原因**，于是
  `TestServePdf` 里任何**真实回归**（例如 403 变 200）都会被降级成 WARN、门禁退出码为 0。
  现改为**前缀 + 签名**双命中（`SystemExit` **且** `SAFE_DELETE_BULK_CONFIRM_REQUIRED` /
  `_BULK_REJECTED` / `_FAIL_CLOSED` 之一，标记串取自 shim 源码常量）；签名取不到
  （junit 缺失）时**不降级**（fail-closed）。变异 M12/M14 双向验证。
  **实测判据**：失败点是否是 `SystemExit` 且带 `BULK_CONFIRM_REQUIRED`。
  **实测（可复现）**：`pytest tests/unit tests/integration` 一次跑完 ⇒
  `1 failed, 2800 passed`，失败点 `tests/integration/test_main_routes.py::TestServePdf
  ::test_pdf_non_local_host_returns_403` 的 `finally: pdf_path.unlink(...)`，
  抛的是沙箱 shim 的 `SystemExit(1)`：
  `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":847,"threshold":50,"scope":"turn"}`。
  **定位（不是猜）**：守卫实现在 `cli/vendor/shim/safe-delete-bulk-guard.cjs`，
  阈值旋钮 = 环境变量 **`CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD`**（默认 20，
  本次宿主给 50），作用域 `turn`；shim 侧 `_check_bulk_delete_guard()` 只在
  `CODEBUDDY_TOOL_CALL_ID` 存在时才检查 ⇒ **是本机沙箱策略，不是应用缺陷**。
  **决定性验证**：同一份代码、仅加 `CODEBUDDY_SAFE_DELETE_ENABLED=0` ⇒
  **`2801 passed / 0 failed`**（总数与失败轮一致）⇒ 结论成立：环境性，非回归。
  动作：① 全量跑法固化为
  `CODEBUDDY_SAFE_DELETE_ENABLED=0 "$PY" -m pytest tests/unit tests/integration -o addopts="" -q -p no:cacheprovider`；
  ② 或改 `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=<大>`（更保守，不清安全网）。
  ⚠️ **别把这个 flake 当回归修** —— 更别去改 `test_pdf_non_local_host_returns_403`
  （它的 cleanup 是正确的）。判据：失败点是否是 `SystemExit` 且带 `BULK_CONFIRM_REQUIRED`。
  ⚠️ **纠正一处旧记录**：既有文档/记忆里写的构建变量名 `BULK_THRESHOLD` **不是真变量名**
  （实际生效的是并排设置的 `CODEBUDDY_SAFE_DELETE_ENABLED=0`）；按真名记录，避免误导。

- [ ] **B7-2 全仓 ruff 105 条既存欠账（lint 未进门禁 ⇒ 真缺陷会被噪声淹没）**（P3）
  实测：`ruff check .` = **105**（F401 未用导入 100 + F841 未用变量 5），
  分布高度集中：`core/pipeline/__init__.py` 38、`core/rules/__init__.py` 15、
  `tests/unit/test_pipeline.py` 11、`api/jobs/__init__.py` 4 ……
  **58/105 在 `__init__.py` 里，而那是刻意的再导出**（该文件 docstring 明说
  "Public API is re-exported here so `from core.pipeline import X` keeps working"，
  且 `import asyncio  # noqa: F401` 只标了 1 行）。
  ⇒ 与 `pyproject.toml` 的自我声明（"F-class = real defects"）**相互矛盾**：
  105 条噪声的基线意味着**新增一个真缺陷没人看得见**。
  动作：① `__init__.py` 类走 `[tool.ruff.lint.per-file-ignores]`（或在文件里补 `__all__`）
  —— **不要删那些导入**（它们就是公开 API）；② 其余 `--fix` 处理（42 条可自动修）。
  🔴 **禁止无脑 `ruff --fix` 全仓**：`tests/` 里有用字符串 `patch("mod.name")`
  的导入，对 ruff 是"未使用"，删掉会**改掉 patch 目标是否存在** ⇒ 静默破坏测试。
  验收：`ruff check .` 归零且全量测试数**不下降**；变异验证：恢复一条真未用导入 ⇒ 必须红。

- [x] **B7-3 门禁无法发现「产物陈旧」 —— 护栏缺口**（P2，2026-09-20 实测）
  ✅ **2026-09-20 完成（Round 52）**。新增**第 9 项**门禁 `artifact_freshness`
  （`scripts/release_gate.py`），判据实现集中在**新模块** `scripts/bundle_manifest.py`
  （CLI `--write` / `--check` 与门禁共用同一份 `verify_artifact`，**只有一处实现**）。
  **六条判据**（每条都有反向用例 + 定向变异）：
  ① 入包集合覆盖（新增入包文件必须在清单里，否则是永久盲区）；
  ② 版本（清单 vs `main.APP_VERSION`，AST 读 `main.py`，不导入模块）；
  ③ 工作树逐字节 vs 清单（抓"源码改了没重建"）；
  ④ **产物内可读副本逐字节** vs 清单（抓"产物陈旧" —— 唯一能戳破"版本号一致但字节差"的判据）；
  ⑤ asar 成员（`electron/main.js`）与 asar 的 `package.json` 版本；
  ⑥ exe 绑定（清单记录的 `pbc-server.exe` sha256 必须对得上，防清单被挪到别的产物旁）。
  **SKIP 语义**：仓库里**没有**产物 ⇒ SKIP（≠ PASS，本项回答的是"已存在的产物是否新鲜"，
  CI/干净克隆不该因此变红）；**有产物但没清单 ⇒ FAIL**（"无法证明新鲜度" ≠ "新鲜"）。
  🔴 **纠正一处旧记录**：既有记忆写的「比对 `main.APP_VERSION` vs 产物内 `package.json`」
  **对 PyInstaller 产物不成立** —— 实测 `dist/pbc-server/` 里**没有** `package.json`
  （只有 `_internal/` 下的 datas），`package.json` 只存在于 **electron 的 `app.asar`** 里，
  且那份**被 electron-builder 重写**过（源 ~1.6 KB vs 包内 255 B）⇒ 只能比
  **`version` 字段**，**字节比对必假**。另：`core/`、`api/`、`config.py`、`main.py` 等
  **在产物里根本没有对应字节**（编译进 exe 内的 PYZ）⇒ 那一层只能靠"工作树 == 清单"，
  盲区已在模块 docstring 里**显式声明**（判不了 ≠ 判据确凿）。
  **验收（已达成）**：`test_lag_one_commit_then_sync` = 源码改动而产物不重建 ⇒ 红；
  重建产物 + 重生成清单 ⇒ 绿。`devlogs/_verify/mutate_b73.py` **15/15 CAUGHT** 且字节级还原。
  真产物实测：`release_gate --skip-tests` 现在**如实报红**（2 份产物都缺清单；
  `static/status.js` 5354 B vs 产物 3867 B、`review.js` 97926 B vs 93941 B、
  `settings.js` 77547 B vs 77231 B —— 三处铁证，比原记录多两处）。
  **实测铁证（字节级，非推断）**：源码 `static/settings.js`（09-20 16:11，
  含 B4-4 引入的 `showAutoActivateNotice`/`auto_activated`）与产物内同名文件
  （`dist/pbc-server/_internal/static/settings.js`、以及 electron 内嵌那份，
  均 09-18 11:18，**仍含早已删除的 `firstConfigured`/`autoReason`**）
  **字节不一致**（77547 B vs 77231 B）；而 `release_gate.py --skip-tests`
  照样 **7/7 全绿**。
  **机制（三层叠加）**：① `pytest.ini` 为 `testpaths=tests` +
  `python_files=test_*.py` ⇒ `tests/e2e_*.py` **不被收集**；
  ② 唯一依赖产物的 `tests/integration/test_frozen_smoke.py` 只有 3 条断言
  （`/health` 200、上传页含"上传"、`/static/app.css` 200），**都不检查版本或内容**；
  ③ 含版本断言的 `tests/e2e_frozen.py:50-58`（要求产物版本 == `main.APP_VERSION`）
  恰在收集范围之外。
  ⇒ **「改了进产物的模块却没重建」可静默通过全部 8 项门禁**。
  ⚠️ **纠正一处旧记录**：既有记忆/文档所写「`tests_coverage` 含分发一致性，
  升版未重建必红」**不成立** —— 门禁里没有任何产物新鲜度判据；那条"红"
  来自**人手工跑 `e2e_frozen.py`**，是人的纪律，不是门禁的护栏。
  ⚠️ **危险放大**：当版本号恰好一致（都 1.1.9）时，连"人眼扫一眼版本号"
  这条兜底都失效 —— 本轮就撞上了这个情形。
  动作：① 门禁新增 `check_artifact_freshness` —— 既比对版本
  （`main.APP_VERSION` vs 产物内 `package.json`），也比对**内容**
  （把进产物的源码 `static/`、`templates/`、`core/`、`api/`、`config.py`、
  `main.py` 与产物内副本做字节比对；读法现成：`clean_dist.asar_version` 的
  子进程 seek 模式，`_internal/` 可直接读）；② 或把 `e2e_frozen.py` 的版本
  断言收进 `test_*.py` 收集范围。
  验收（必须能失败）：故意让产物落后一个提交 ⇒ 门禁红；产物与源码同步 ⇒ 绿。

- [x] **B7-4 打 tag/发版流程缺「产物新鲜度」前置闸**（P2，由 B7-3 派生）
  ✅ **2026-09-20 完成（Round 52）**。"复用一个清单文件，构建时生成"这一条已落地：
  ① 清单 = `dist/pbc-server/build_manifest.json`（schema 1：`app_version` / `git_head` /
  `git_dirty` / `generated_at` / `files{rel: sha256}` / `artifact{pbc-server.exe: sha256}`）；
  ② 生成点接进 **`build.ps1` 的 2.6 步**，时机**刻意**卡在 PyInstaller 之后
  （要绑定 exe 字节）、electron-builder 之前（随 `extraResources` 进
  `resources\pbc-server\`）⇒ **任何一份产物都能自证**它由哪次提交、哪些字节构建而来；
  写完后**就地自校验**（`bundle_manifest.py --check`）—— 把"门禁才发现"提前到构建现场；
  ③ 发版前置闸写进 `CLAUDE.md` 的 **Repo hygiene 规则 11**（与门禁同判据，不会两边漂移）；
  ④ Bash 构建链（`CLAUDE.md` 的 agent 指令）同步补上该步骤。
  **验收**：清单与产物不符 ⇒ 门禁 `artifact_freshness` / `bundle_manifest.py --check` 拒绝
  （exit 1），变异 M1/M2/M11/M15 分别验证"比对被关掉 / 崩溃被吞掉 / 从编排摘掉"都会变红。
  ⚠️ 与 B7-3 同源的**盲区**（如实声明）：清单是**文本文件**，若有人重建后又改源码并
  **手工重新生成**清单，本闸看不出来 —— 那是**主动绕过**而非**漏检**；补强手段是
  ⑤ 把 `bundle_manifest.py` 登记进 `PACKAGING_FILES`（缺它门禁直接 FAIL）。
  理由：B7-3 说明"版本号一致"根本不蕴含"产物是新构建的"。而分发/打 tag
  面向的是**产物**，不是源码树。当前打 tag 只需工作树干净，**不要求产物
  由当前 HEAD 构建**。
  动作：在发版脚本/检查表里固化一条前置：**产物内 N 个进包文件的 sha256
  必须等于 HEAD 对应文件的 sha256**（复用一个清单文件，构建时生成）。
  验收：清单与产物不符 ⇒ 发版闸拒绝。

- [ ] **B8 发版收尾（v1.2.0）—— 一次性批次的"不变量"清单**（Round 53 登记）

  ⚠️ **为什么单独立一条**：Round 53 的 8 个提交（`c751c4a`..`5e53fce`）**都没有改 `CHANGELOG.md`**
  —— 而 Round 43–52 是**逐提交**写的。这是本轮的**刻意选择**（Round 53 整体作为 v1.2.0 一次性发布），
  但代价是：**"Round 53 的 CHANGELOG 段还没写"这件事在仓库里没有任何痕跡**（grep `CHANGELOG.md`
  里的 `B1-10`/`B5-4`/`B4-3` 全 0 命中，只有 Round 43-48 的"登记"字样）。
  ⇒ 立此条把"会漏"变成"漏了会红"。

  1. **`CHANGELOG.md` 的 `[1.2.0]` 段必须覆盖 Round 53 的全部条目**（不是只写最后一条）：
     `B4-3` / `B4-5` / `B5-4` / `B1-7` / `B1-10` / `B1-12` / `B1-1` / `B1-13`（评估）/ `B1-16`
     ＋ 即时的 `B1-14` / `B1-15`。**逐条对照 `git log c751c4a..HEAD --oneline` 点名核**，
     不得凭记忆写（漏条 = 发布说明说谎）。
     ⚠️ Round 52 的 `B7-1/B7-3/B7-4` 已在 `[Unreleased]` 的 Round 52 小节里 ⇒ **不要重复写**。
  2. 升版 4 处（`main.APP_VERSION` / `package.json` / `package-lock.json` 顶层 ＋
     `packages[""]` / `PORTABLE_README.txt`）—— ⚠️ lock 里另有 2 处 `1.1.4` 系**依赖自带**，勿改。
  3. 重建**三步不许漏中间那步**：Tailwind → PyInstaller → `python scripts/bundle_manifest.py --write`
     → electron-builder（漏掉清单 ⇒ 门禁 `artifact_freshness` FAIL，属**设计**）。
  4. 门禁 **9 项**（含 `artifact_freshness`）；产物级 e2e（`tests/e2e_run.py`，`PBC_E2E_EXE` 指向
     `dist-electron/win-unpacked/resources/pbc-server/pbc-server.exe`）。
  5. tag 一律 **annotated**，且与引用它的文档**同树**。

  **验收**：`grep -c "B1-10\|B5-4\|B4-3\|B4-5\|B1-16" CHANGELOG.md` ≥ 5；
  且 `[1.2.0]` 段里能逐条找到上列 11 个条目号。
