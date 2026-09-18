# TODO —— 活的待办清单（单一入口）

> **怎么用**：这是**唯一**的待办清单。开工先读本文件；完成一项就地把 `[ ]` 改 `[x]`
> 并填上**证据**（提交号 / 日志路径 / 数字），**不要**另开 TODO 文档。
> 历史计划（`PLAN.md`、`docs/PLAN_v1.1_EXECUTION.md`、`docs/ROADMAP_v1.1.md`）是**存档**，不再更新。
>
> 纪律（见 `CLAUDE.md`「Repo hygiene & release discipline」）：**先定位 → 再解决 → 最后测试**；
> 结论必须挂证据；**不升版号的边界** = 改动是否进入 PyInstaller 产物。
>
> 最后更新：2026-09-18（Round 39：**完成 F11 第 4–6 步** —— 修 #143（配置级判据误早停）
> + 实测 #151（Anthropic 协议，本地协议桩）+ 落地 #159（视觉定向互证原型 + adapter
> 多模态签名）；每条都做了**变异验证**；升版 **1.1.9** 并**重建产物**）·
> 可分发版本 **v1.1.9**（**11 条缺陷已修且产物已重建**）·
> 远端 `8f80a58`（Round 38 推送成功；本轮提交见文末）·
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
> ⚠️ **外部阻塞**：
> ~~**A1**（`dist-electron` 被宿主进程持句柄）~~ ✅ **2026-09-18 已解除**（用户手工收敛）。
> ~~**A4**（SiliconFlow 余额）~~ ✅ **2026-09-18 已解除**（key 恢复 200），
> 多轮 e2e 与真实 VLM 调用均已跑通。
> **仅剩**：anthropic 的**厂商真机**连通性（需真实 key 与出网），见 #151。
