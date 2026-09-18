# TODO —— 活的待办清单（单一入口）

> **怎么用**：这是**唯一**的待办清单。开工先读本文件；完成一项就地把 `[ ]` 改 `[x]`
> 并填上**证据**（提交号 / 日志路径 / 数字），**不要**另开 TODO 文档。
> 历史计划（`PLAN.md`、`docs/PLAN_v1.1_EXECUTION.md`、`docs/ROADMAP_v1.1.md`）是**存档**，不再更新。
>
> 纪律（见 `CLAUDE.md`「Repo hygiene & release discipline」）：**先定位 → 再解决 → 最后测试**；
> 结论必须挂证据；**不升版号的边界** = 改动是否进入 PyInstaller 产物。
>
> 最后更新：2026-09-18（Round 35：**LLM 恢复后立刻做的视觉第四读定点验证** ⇒ 收紧 #159/#160，
> 新增"姓名不可由视觉裁决"硬约束 + "Qwen2.5 系列非 VLM"登记）· 可分发版本 **v1.1.8**
> （**内部基线可用；对外分发前先修 2 个 P0**）· 远端 `e4f5b81`

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

- [ ] **#133【P0】复核页页头状态点硬编码绿色。**
      `templates/review.html:34` 写死 `bg-success`，而 `static/review.js:375-378`
      只改 `#status-badge` 的 `textContent`，**从不改这个点的 class**（`statusDotClass`
      只存在于 `upload.js`）。⇒ `status=error` / `partial_review` 时页面显示
      **"绿点 + 出错/部分可复核"**。这正是 #127 的同类回归：**upload 页修了、复核页没修**。
      **修法**：把 `statusDotClass` 抽到共享模块（`static/` 已有 `eta.js` / `confirm-dialog.js`
      两个共享件先例），SSR 与 SSE 两处都用它。**验收**：SSR 渲染 `error` 时必须输出非绿色类；
      并加机检（断言模板里**不存在**无条件的 `bg-success`）。
- [ ] **#134【P0】列表页冷加载看不到失败页与失败原因。**
      `api/jobs/listings.py:46` 的 SELECT **不投影** `failed_pages` / `error_message`，
      而 `static/upload.js:739,754` 依赖这两个字段 ⇒ 冷加载时 `failedPages` 恒 `[]`、
      `errEl` 恒 `hidden`；`buildRowFromSnapshot`（`upload.js:667-681`）**丢弃** `failed_pages`；
      `updateJobRowLive`（`:645-652`）只写 `.job-error`、不写 `.job-meta`。
      ⇒ 列表上只剩一个颜色点，用户仍无法区分"**记录真的无异常**"与"**这次没分析成功**"。
      **实测复现**：产物 `/api/jobs` 字段集确无这两列（见 E 表）。
      **修法**：`listings.py` 投影 `failed_pages`（复用 `_parse_failed_pages`）与 `error_message`；
      `buildRowFromSnapshot` 透传 `failed_pages`；`updateJobRowLive` 同步写 `.job-meta`。
      **验收**：冷加载一个 `partial_review` job，摘要行必须出现"失败页 N 页（…）"。
- [ ] **#135【P1】复核页首屏 parse-error 横幅只有通用文案。**
      `templates/review.html:305` 是静态句；具体 `_error` 只由 `review.js:899-902` 在
      AJAX/SSE 后写入，而调用点仅 `review.js:805` / `855` —— `DOMContentLoaded` **不触发**。
      ⇒ 直接打开/刷新终态任务第 1 页，看不到"401 凭据失效"这类全局原因。
      **修法**：首屏也调一次 `updatePageLevelUI`（数据已在 SSR 的 `structured_json` 里）。
- [ ] **#136【P1】失败页仍显示"本页无问题"。**
      `templates/review.html:562` / `review.js:1284-1285` 仅按 findings 数量判断，
      **未结合 `page_parse_error`** ⇒ 分析失败的页与"确实合规的页"视觉相同。
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

- [ ] **#140【P1】外部取消只终止父 task，派生子任务继续跑 LLM 并写库。**
      `stage2.py:147-154` 一次性 `create_task` 建**全部**页任务且不登记；
      `watchdog.py:328-331` / `main.py:418-421` 只取消注册的**父** task；父的 `finally`
      （`engine.py:115-129`）也只 flush 进度 future。⇒ 孤儿继续 `touch_activity`、
      写 `page_cache`/`findings`，甚至与用户 retry 后新一轮**抢同一页**（`engine.py:536-538` 注释已承认）。
      **修法**：结构化并发（`TaskGroup` 或显式 `children: set[Task]`），在 `finally` 与
      `except CancelledError` 里**先级联 cancel 子任务再 transition**；`_pipeline_tasks`
      值改 `set[Task]`，`_terminate_pipeline_task` 取消集合全体。
- [ ] **#141【P1】任务注册表按 job_id 单值覆盖 ⇒ 持锁孤儿失去引用 ⇒ 重试被永久挂死。**
      `engine.py:66` `_pipeline_tasks[job_id] = task` 直接覆盖；`watchdog.py:328`
      按 job_id 只能取到**新 task（等待者）**；`engine.py:112` `async with lock:` **无超时**。
      ⇒ 看门狗每轮杀等待者、真凶仍持锁，形成"重试 → 900s 后被杀 → 再重试"的无限循环
      （`watchdog.py:340-344` 已自述"per-job 锁可能仍被持有，需人工介入"）。
      **修法**：注册表改 `dict[str, set[Task]]`（或新增前把旧任务移入 `_orphans` 一并取消）；
      `_terminate_pipeline_task` 取消同 job **全部**未完成 task，并把 `cancelled_ids` 写进审计 detail。
- [ ] **#142【P1】`pending` + 无注册 task = 无终态黑洞；两处"先写 pending 后 launch"不在 try 内。**
      `watchdog.py:174-176` 跳过"pending 且无活 task"；启动恢复要求 `created_at < process_started_at`
      （`state.py:163-165`）⇒ **同进程内**的 pending 孤儿只能等下次重启；期间 SSE 无限等待
      （`api/jobs/status.py:389` 的 `while True` 只认终态），用户无提示。
      可达路径：`api/jobs/upload.py:369-375 → :402`、`api/jobs/actions.py:82 → :111`
      （两处 `launch_pipeline` **都不在 try 内**）。
      **修法**：launch 调用点包 try/except，失败即转 `error` + 审计；或给看门狗加"pending 且
      早于本进程启动 且 无活 task → error"的低频规则。
- [ ] **#143【P2】"不可重试"判据是整段错误串的子串匹配 ⇒ 误判为配置级 + 误导文案。**
      `llm/client.py:32-36` 的关键词表含 `"400"` / `"invalid"`，`:266-269` 用 `kw in err_str`。
      ⇒ `429 Rate limit … Limit 40000` 命中 `"400"`、本地 `ValueError("invalid literal for int()")`
      命中 `"invalid"` ⇒ 抛 `LLMConfigError` ⇒ `stage2.py:170-192` **全单早停** +
      `stage2.py:24-27` 输出"**请检查 API Key**"（与真实原因不符，GMP 排障会被误导）。
      **修法**：优先结构化判据（`status_code` / `openai.AuthenticationError` 等类型），
      子串表降级为兜底并加词边界（`\b400\b`）；**补负例测试**（429 限额串、本地 ValueError）。
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
- [ ] **#151【P2】anthropic 协议从未被实测。** 本机只配了 `siliconflow` / `deepseek`
      （两者 `protocol=openai`）⇒ 该分支**零运行证据**。**验收**：用一个 anthropic 协议
      provider 跑通 `test_provider` + 一轮真实分析，并把结果写进 E 表。
- [ ] **#152【P3】未覆盖其它主流协议。** 现仅 openai / anthropic 两种；
      Gemini（`generateContent`）、Azure OpenAI（`api-key` 头 + `api-version` 查询参数）、
      Bedrock 均不支持。**按需**再评估，不预先实现。

### F5. 流式输出（对比市面主流）

- [ ] **#153【P2】缺"断点续传"。** 聚合流 `/api/jobs/live` 只发无名 `data:` 帧，
      **无 `event:`、无 `id:`** ⇒ 断线重连后客户端无法用 `Last-Event-ID` 增量补齐
      （主流 SSE 做法：命名事件 + `id:` + 服务端保留短期事件缓冲）。
- [ ] **#154【P2】缺心跳注释帧。** 无 `:keepalive`（主流做法是每 15–30s 发一行注释帧）
      防中间代理按空闲超时断连。本机直连风险低，**一旦经反向代理/网关部署就会暴露**。
- [ ] **#155【P3】`done` 命名事件可能被静默忽略。** 单任务流 `/api/jobs/{id}/stream`
      会发 `event: done`（`api/jobs/status.py:422`），但 `upload.js` **只监听 `es.onmessage`**
      （`:488`）—— 现在没用该端点所以无影响，**一旦改用就会漏**。建议 `addEventListener("done", ...)`。
- [x] **已做对的部分**（勿重复改）：`requirements.txt` 把 `starlette>=0.47` 的理由写清楚了
      （更早版本会把 SSE 帧缓冲到 ~64KB ⇒ 进度看似冻结后一次性涌出）；
      `review.js` 主动 `close()` + 手动指数退避 2/4/8s + 10s 兜底轮询；
      `EventSource` 原生自动重连 + 后端 `retry:` 帧双保险。

### F6. 规则集（主体合理，缺口具体）

- [ ] **#156【P2】多个规范类型没有确定性规则。** 22 条规则只覆盖 **16 / 21** 类：
      `signature_mismatch` 与 `ocr_noise` **无任何规则产出**（仅 LLM prompt 枚举 /
      归一别名）；`user_rule` / `uncategorized` 仅 LLM。
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

- [ ] **#171【P1】"前端消费的字段必须在它实际的数据源里存在"的契约机检。**
      这是本轮**反复出现的缺陷模式**：`test_config_error_visibility.py:198` 只断言
      `"job.failed_pages" in js`（**字符串存在**），而 `listings.py:46` **根本不返回该字段**
      ⇒ 护栏全绿、缺陷照旧（P0 #134 与 #133 都从这条缝漏过）。
      **做法**：为每个前端消费的字段登记"**它由哪个端点提供**"，测试里**在该端点的真实响应上**断言
      字段存在且类型正确（复用本轮 E 表的探测脚本形态）。
      **验收**：把 `listings.py` 的 `failed_pages` 去掉，护栏必须**当场变红**（变异验证）。

---

### F11. 下一轮开工顺序（建议）

1. **先修 2 个 P0（#133 / #134）** —— 它们直接决定"用户会不会把失败读成成功"，且改动面小。
2. **同步修 P1 的错误可见性（#135 / #136）与后端并发（#140 / #141 / #142）** —— 后者是"重试永久挂死"的根因。
3. **加 #171 契约机检** —— 先补护栏，再改代码，避免修完又漂。
4. **#143 负例测试** —— 它现在会把 429/本地错误误报成"请检查 API Key"，误导排障。
5. 外部解阻后再做 **#151（anthropic 实测）** 与 **#159（视觉第四读原型）**。
6. 收尾照旧：**先重建产物 → 再跑门禁 → 推送到干净的 worktree**（`tests_coverage` 含分发一致性，
   升版未重建必然变红，那是**正确信号**）。

> ⚠️ 两条**外部阻塞**仍在，不解决就别假装跑通：
> **A1**（`dist-electron` 被宿主进程持句柄 ⇒ 需完全退出 WorkBuddy 后 `python scripts/clean_dist.py --apply`）
> 与 **A4**（SiliconFlow 402 余额不足 ⇒ 需充值或换有余额的 provider）。
