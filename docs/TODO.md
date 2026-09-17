# TODO —— 活的待办清单（单一入口）

> **怎么用**：这是**唯一**的待办清单。开工先读本文件；完成一项就地把 `[ ]` 改 `[x]`
> 并填上**证据**（提交号 / 日志路径 / 数字），**不要**另开 TODO 文档。
> 历史计划（`PLAN.md`、`docs/PLAN_v1.1_EXECUTION.md`、`docs/ROADMAP_v1.1.md`）是**存档**，不再更新。
>
> 纪律（见 `CLAUDE.md`「Repo hygiene & release discipline」）：**先定位 → 再解决 → 最后测试**；
> 结论必须挂证据；**不升版号的边界** = 改动是否进入 PyInstaller 产物。
>
> 最后更新：2026-09-17（Round 31 收尾）· 可分发版本 **v1.1.7** · 远端 `bf1ffd2`

---

## A. 需要**用户**动作（我做不到，已在等）

- [ ] **A1（阻塞 R3）把仓库目录加入安全软件信任区/白名单。**
      8 个 `dist-*` 变体目录（≈2.7 GB）**全部**被占用，`--apply` 一个也删不掉。
      实测证据：`win-unpacked/resources/app.asar` 改名被拒 `winerror=32`
      （`ERROR_SHARING_VIOLATION`，句柄未带 `FILE_SHARE_DELETE`），**12 秒内 6 次全部失败
      ⇒ 持续而非瞬态**；目录级 `winerror=5`（`ERROR_ACCESS_DENIED`）；
      `tasklist` 无 electron/BatchSentry/pbc-server/python 进程 ⇒ 外部持有。
      解除占用后执行：
      ```
      python scripts/clean_dist.py            # dry-run，确认方案
      python scripts/clean_dist.py --apply    # 走回收站，可恢复
      ```
      预期：保留 `dist/` + `dist-electron-out-20260917-103254`（v1.1.7），其余 8 个进回收站；
      门禁的 `dist_variants` WARN 随之消失。**在此之前该 WARN 属预期，不是回归。**
- [ ] **A2 轮换已泄漏的 LLM 凭据（必须厂商侧操作）。**
      `DeepSeek` / `SiliconFlow` 的 key 曾随提交进入 git 历史，**删文件删不掉历史**
      ⇒ 只能在厂商侧吊销并换新（本地已是新 key `sk-vhx…`，但旧 key 在历史里仍可见）。
      轮换后更新本地 `%APPDATA%/PBC/config.json`，并保留 `config.json.bak_pre_key_rotation`
      （它被 `%TEMP%/pbc_127_accept.py` 用来复现 #127 触发条件）。
- [ ] **A3 提供第 2 份真实批记录 PDF 用于泛化验证（缺陷 #126）。**
      目前**只有一份**真实批记录（丝裂霉素提取批记录，51 页）⇒ 规则/OCR 的泛化性
      **未经检验**。缺它就无法回答"换一份记录还准不准"。

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

## D. 本轮（Round 31）已完成 —— 存档，勿重复做

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
| 全量单测+集成 | **2622 passed / 0 failed** | pytest 输出（292.9s） |
| 覆盖率 | **95.5%**（门禁 95%） | `devlogs/gate_report_20260917_122123.json` |
| 打包信号 | **OVERALL pass**（7 PASS / 1 WARN / 0 FAIL） | 同上；WARN = `dist_variants`（见 A1） |
| 冻结冒烟（v1.1.7 产物） | **21 passed / 0 failed** | `%TEMP%/pbc_frozen_e2e_v117c.log` |
| #127/#131 产物级验收 | **5 passed / 0 failed** | `%TEMP%/pbc_127_appdata/PBC/acc-server.log` |
| 多轮产物 e2e | `pdf,img,cancel` 与 `pdf,mineru` 均通过（`pdf` 曾因上游 `code:10010` 拥塞 FAIL，属上游） | `devlogs/e2e_sse_*.jsonl` |
| 远端 | `bf1ffd2` | `git ls-remote origin main` |
