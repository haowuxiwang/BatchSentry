# TODO —— 活的待办清单（单一入口）

> **怎么用**：这是**唯一**的待办清单。开工先读本文件；完成一项就地把 `[ ]` 改 `[x]`
> 并填上**证据**（提交号 / 日志路径 / 数字），**不要**另开 TODO 文档。
> 历史计划（`PLAN.md`、`docs/PLAN_v1.1_EXECUTION.md`、`docs/ROADMAP_v1.1.md`）是**存档**，不再更新。
>
> 纪律（见 `CLAUDE.md`「Repo hygiene & release discipline」）：**先定位 → 再解决 → 最后测试**；
> 结论必须挂证据；**不升版号的边界** = 改动是否进入 PyInstaller 产物。
>
> 最后更新：2026-09-17（Round 33：A1 归因更正为宿主进程占用）· 可分发版本 **v1.1.8** · 远端 `6332f17`

---

## A. 需要**用户**动作（我做不到，已在等）

- [ ] **A1（阻塞收敛）完全退出 WorkBuddy 宿主，然后在普通终端跑收敛脚本。**
      **归因已更正（Round 33 实测，旧处置无效）**：持有者**不是安全软件**，是
      **WorkBuddy 宿主进程**（Restart Manager 具名：`WorkBuddy.exe` pid=16220 / 14048）。
      机制：宿主把 `.asar` 当"包"打开后**持久留下未带 `FILE_SHARE_DELETE` 的句柄**
      ⇒ 含 `app.asar` 的目录既不能改名也不能删除 ⇒ electron-builder 写不进标准输出目录
      ⇒ 本次 v1.1.8 自愈到 `dist-electron-out-20260917-142437`（已按约定写 `PROVENANCE.txt`）。
      ⚠️ 因此**"把仓库加入杀软信任区/白名单"对本案无效**（不是杀软）。
      证据链 + 可复现的控制实验（新建 `.asar` 空闲 → 宿主读一次即持续 `winerror=32`；
      对照读 `.txt` 仍空闲；同目录 exe 全部空闲）见 `docs/PROJECT_PITFALLS.md` §二十二。
      **为什么必须由用户做**：宿主就是当前会话的运行环境，我在它里面**杀不掉它自己**。
      步骤（顺序不能反）：
      1. 保存工作后**完全退出 WorkBuddy**（确认任务管理器里已无 `WorkBuddy.exe`）；
      2. 在**普通终端**（cmd / PowerShell）里执行：
         ```
         python scripts/clean_dist.py            # dry-run：应显示「待清理: dist-electron」且不再报占用
         python scripts/clean_dist.py --apply    # 走回收站，可恢复
         ```
      3. 预期收敛到 `dist/` + **一个** `dist-electron*`（保留 1.1.8 那份；版本由 asar 内的
         `package.json` 读出，不靠 mtime 猜）⇒ `dist_variants` 回到 PASS。
      4. 收敛**不影响已交付产物的正确性**（它只是磁盘/仓库卫生），产物本身已通过全部验收。
      ⚠️ **不要在占用未解除时手动删 `dist-electron`**：删到被持有的 `app.asar` 会中途失败，
      把一个完好的 v1.1.7 产物变成**残缺目录**（比不删更糟，且会让"最新产物"判定指向残缺目录）。
      ⚠️ 现状中 `dist-electron/win-unpacked/resources/__lockscan_probe.asar`（729 B）**是 Round 33 我留下的
      诊断探针**，它同样被宿主锁住 ⇒ 删不掉，会随该目录一起进回收站，无需单独处理。
      **在此之前 `dist_variants` 显示 2 属预期，不是回归。**
- [ ] **A2 轮换已泄漏的 LLM 凭据（必须厂商侧操作）。**
      `DeepSeek` / `SiliconFlow` 的 key 曾随提交进入 git 历史，**删文件删不掉历史**
      ⇒ 只能在厂商侧吊销并换新（本地已是新 key `sk-vhx…`，但旧 key 在历史里仍可见）。
      轮换后更新本地 `%APPDATA%/PBC/config.json`，并保留 `config.json.bak_pre_key_rotation`
      （它被 `%TEMP%/pbc_127_accept.py` 用来复现 #127 触发条件）。
- [ ] **A3 提供第 2 份真实批记录 PDF 用于泛化验证（缺陷 #126）。**
      目前**只有一份**真实批记录（丝裂霉素提取批记录，51 页）⇒ 规则/OCR 的泛化性
      **未经检验**。缺它就无法回答"换一份记录还准不准"。
- [ ] **A4（阻塞"需真实 findings 的多轮 e2e"）硅基流动账户余额不足（402）。**
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
| 多轮产物 e2e（`pdf,img`） | **FAIL —— 外部阻塞：账户余额 402**（非产品缺陷）。两轮 OCR 均成功、终态如实 `error`、0 findings ⇒ 驱动**拒绝**记作成功（判别性正确） | `%TEMP%/pbc_e2e_rounds_118.log`；详见 **A4** |
| 目录锁持有者（Round 33 实测） | **WorkBuddy.exe（宿主进程）pid 16220 / 14048**；**只锁 `*.asar`**（同目录 exe 全 FREE）；**不是安全软件** | Restart Manager `RmGetList`；复现实验与证据链见 `docs/PROJECT_PITFALLS.md` §二十二 |
| 产物目录 | `dist-electron-out-20260917-142437`（标准路径被锁 ⇒ 已按约定写 `PROVENANCE.txt`，其 `unblock` 段已更正） | 见 A1 |
| 远端 | `fd93759`（已推送） | `git ls-remote origin main` |
