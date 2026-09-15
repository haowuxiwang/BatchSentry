# 降噪与兜底 执行清单（Todolist）

> 上游依据：`docs/NOISE_REDUCTION_RESEARCH.md`（行业调研与法规红线）、
> `docs/NOISE_REDUCTION_SPIKE.md`（对齐粒度与 bbox 资格的实测结论）。
>
> 纪律：**先定位 → 再解决 → 再测试**。每项都必须给出「定位依据（实证）」、
> 「机检不变式」、「验收口径」。没有实证依据的项不列。

## 依赖与建议顺序

```
T-P0-4 ✅ 已完成（4273c1f）
T-P0-2 ✅ 已完成（a5661a6）
T-P0-3 ✅ 完成（含旋转方向独立核验，见 `REGION_ANCHOR_VISUAL_CHECK.md`）
T-P1-3（HTML 栅格化）─┬─→ T-P0-1（选择性双读仲裁）
                      └─→ T-P1-1 / T-P1-2（结构校验）
T-P2-*（度量与治理，可与上并行）
```

---

## P0 — 合规必需 / 高收益低风险

### ✅ T-P0-4 `_parse_spec` 书写变体归一化（已完成）

- **定位依据**：真实落库 3526 个 `(spec, value)` 对中，7 种语义等价写法解析失败 ——
  其中「单位夹分隔符」`972 μg/mg ~1020 μg/mg` **20 处全是含量/效价规格**，
  全部静默降级 `spec_unverifiable` 送人工。详见 spike 文档 §4 D1。
- **解决**：`core/rules/parsing.py` 新增 `_normalize_spec_notation`（剥外层括号 /
  全角+Unicode 符号归一 / LaTeX `\pm|\mp|+/-|+-` → `±` / 同单位夹分隔符折叠，
  保留串尾单位供 `_try_unit_normalize` 用）；在 `≤/≥ → <=/>=` 归一**之前**调用。
- **测试**：9 项新单测；真实数据对照（`git show HEAD:` 取旧版本）→
  **22 处转为可判定、解析结果变化 0 处、新增超差 0 处**；`tests/unit` 1585 passed。
- **护栏（后续改动不得破坏）**：`_parse_spec` 保持"开头锚定 + 只允许非数字尾随"，
  以挡掉正文里的 `每1小时±5分钟记录一次`（见 T-P1-6）。

### ✅ T-P0-2 抑制必须留痕（抑制 ≠ 删除）—— 已完成（`a5661a6`）

- **定位依据**：`core/rules/spec_guard.py::drop_unfounded_spec_findings` 返回
  `(kept, dropped:int)` —— **只有计数，没有明细**；`core/pipeline/stage2.py:209`
  只记 `_dropped_spec` 计数。即 37 条被抑制的误报**不可查、不可回退、不可抽检**。
- **法规依据**：EU GMP Annex 11 §16、中国附录《计算机化系统》第 15/16 条 ——
  关键数据的修改需经批准并记录理由；PIC/S PI 041-1 认可"经验证的异常报告"替代
  逐页复核，前提是留痕可追溯。
- **实际改动**（与原方案的差异已标注）：
  1. `drop_unfounded_spec_findings` → `(kept, 明细列表)`。**未加 `return_details`
     开关**（两套返回契约本身就是漂移源），直接改调用方与其测试。
  2. 落库用**独立台账表 `finding_suppressions`**（schema v11），**未**改
     `findings.status='suppressed'` —— 抑制是"未进入问题清单"的候选条目，与人工
     裁决（confirmed/rejected）不同态，混进 `findings` 会污染裁决口径与统计分母。
     表含 `reason`（CHECK 非空）、`evidence`、`reverted_at`、`reverted_finding_id`。
  3. `audit_log` 增 `action=spec_guard_dropped`（含明细摘要）。
  4. 复核页：页面无"页签"结构，实现为**可折叠面板**（`#suppression-panel`），
     逐条显示理由 + 命中证据，支持**一键回退为正式 finding**。
- **机检不变式**（`tests/unit/test_suppression_ledger.py`）：禁止 `drop_*` **只**返回
  `int`（返回注解扫描）；空理由在 **DB CHECK 与 Python 两侧**都必须被拒；台账建表
  语句**只许在 schema.sql 声明**（迁移体复制 DDL = 漂移源）；`stage2` 不得回归为只留计数。
- **实测发现并修复**：SQLite 单参 `trim()` **只去 ASCII 空格**，`reason="\t"` 可绕过
  非空校验 → CHECK 显式给出空白字符集（Python 侧 `str.strip()` 覆盖全角空格）。
- **验收**：`tests/integration/test_api_suppressions.py` 10 项真实 HTTP（查询/过滤/404/
  计数；回退建 finding + 台账留痕 + audit_log；重复回退 400；跨 job 越权 404；
  UNIQUE 去重幂等）。真实轮次的"100% 可查可回退"待打包后目视复核（见文末）。

### ✅ T-P0-3 证据锚（区域级，不做单元格级）—— 完成（含方向独立核验）

- **定位依据**（spike §1/§2/§5）：Paddle 与 MinerU **都**返回表级/块级 bbox，
  但**都不返回单元格级 bbox**；而 `page_cache` 只有
  `job_id / page / raw_html / structured_json / analyzed_at / ocr_diagnostics` ——
  **坐标 100% 被丢弃**。所以区域级高亮是"白拿的能力"，单元格级不成立。
- **实际改动**（与原方案的差异已标注）：
  1. 新增**纯函数** `core/pipeline/regions.py`（唯一定义点）：
     `normalize_bbox` / `canonical_label` / `extract_regions` / `region_anchor`。
     `page_cache.regions_json` 实际形态 =
     `{backend, space:[w,h], space_aspect, regions:[{label, bbox(归一化 0..1), text}]}`
     —— 与原方案的 `[{label,bbox,page_w,page_h}]` 不同：坐标系提到**载荷级**，
     并额外记 `space_aspect`（呈现层宽高比闸门需要它）。
  2. MinerU 侧需把 `layout.json` 的 `page_size` 从 `download_result` 一路带进
     `_split_pages_by_content_list`（原方案未提及该改造）。**已确认 51 页轮次的
     `layout.json` 随 zip 保留**（原方案列为待确认项，现已证实）。
  3. **`findings.region_ref` 未加列** —— 改为**读时推导**：
     `api/review.py::list_findings` 复用已有那次 `page_cache` 批量扫描带上
     `regions_json`，用 `region_anchor()` 现算。理由：区域与 OCR 产物同源，
     重分析后锚自动跟随，不存在"锚指向已消失区域"的漂移；且免去又一处 schema
     变更与历史回填风险。SSR(`main.py::review_page`) 共用同一函数。
  4. 复核页：`#region-overlay` 叠加框 + finding 卡片"定位原图"按钮；
     首屏锚点随 `window.__PBC__.region_refs` 注入（首屏由 SSR 渲染、不经 AJAX）。
- **实测新增的硬约束（原方案未预见）**：
  - **坐标系逐页不同**：51 页中**第 8 页** Paddle 返回 `1920×1440`（服务端旋转过），
    而页面是竖向 → 直接归一化叠加会横竖颠倒。故呈现层必须用 `space_aspect`
    与渲染图宽高比比对（容差 2%），不一致时**明示无法定位**而不是画一个错位的框。
  - **比例实测**：页面 3000×4000(0.75) / 归一化副本 720×960(0.75) / Paddle
    1440×1920(0.75) —— **同向等比**（3000/1440 == 4000/1920 == 2.0833），
    归一化坐标可直接映射，无畸变。
  - **MinerU 的 `page_size` 取自源页自身**（595×842 输入 → 595×842 输出）。
- **机检不变式**：`tests/unit/test_regions_anchor.py`（60 项）+ `test_region_anchor_wiring.py`
  （16 项）：禁止写入未归一化坐标（`regions.py` 是唯一出口，stage1 不得触碰
  `block_bbox` 等原始字段）；区域 label 必须落在**闭集** `CANONICAL_LABELS`
  （未知 → `other`，白名单而非透传）；`regions.py` 保持**纯函数**；
  SSR 与 AJAX 必须共用 `region_anchor`。
- **真值复算**：真实 `paddle_original.jsonl`（51 页）与 `mineru_original.zip` 在场时
  自动执行（全量 bbox 落单位方格、竖向 50 页 + 横向 1 页、真实批号块可被锚中）。
- **验收项已闭环**："抽 5 页人工核对高亮框与页面坐标系一致"不再是不可复现的目视项，
  而是三层各自独立的证据（详见 `docs/REGION_ANCHOR_VISUAL_CHECK.md`）：
  1. **确定性不变量**（无需像素）：逆映射表由不变量推出并由单测钉死；真实产物上
     第 8 页地标（logo/文件编号落左下、页码落右上）落位正确。
  2. **独立地面真值（决定性）**：新增 `scripts/verify_anchor_orientation.py` ——
     配准 Paddle 回传的 `inputImage`（未旋转）与 `outputImages.layout_det_res`
     （它在"旋转后"的图上画的检测可视化，授权串无过期），**实测**服务端施加的旋转。
     结果：第 8 页 θ=270°CCW（轴优势 3.15×，方向可分辨）与上报角一致；第 7 页 0°
     对照成立；`inputImage` 与渲染页在 0° 下墨迹 100% 重合（前提成立）。
     工具判定**分两级**并如实认怂：轴判错=FAIL（框会横竖颠倒），同轴内方向分不清=
     `PASS-AXIS`（密排表格页在 180° 下近乎自对称，是内容限制）。
  3. **本机可视化抽查**：`scripts/verify_region_anchor.py` 用生产函数算框画到真实
     渲染页（`devlogs/region_anchor_check/`，gitignore），5 页机检全 PASS。
- **负面结论（本次调研的重要产出）**：本批记录上**不存在**廉价的像素代理判据 ——
  实测墨迹密度 58.8%、投影空白带奇偶、框内文本行方差 12.0%、整页区域并集 F1 29.4%
  全部不具鉴别力。**故刻意不把任何一条写成测试**：一个"永远绿却无鉴别力"的测试会把
  "未验证"包装成"已验证"，比没有测试更糟。
- **新增机检**：`tests/unit/test_anchor_orientation_tool.py`（27 项，合成图案确定性
  检验核验工具本身 —— 验证器判错等于给出虚假的"已验证"）；真实产物侧新增"旋转奇偶
  ⟺ 坐标系横竖"的自洽护栏与"朝向分类必须开着且逐页有角"的能力退化护栏。
- **已知限制**：上游去畸变（`use_doc_unwarping: true`）导致块 bbox 与渲染图有非刚性
  形变 —— 已实测证明该残差**非旋转引入**（未旋转页同样存在）；高亮框可能比文字略松，
  与"只做区域级、不宣称单元格级"的产品口径一致。

### T-P0-1 选择性双读仲裁（表级粗筛 + 字段级判定）

- **定位依据**（spike §3/§5）：`core/pipeline/dual_compare.py::compare_page` 是
  **页级**；`run_dual_compare` 只在 OCR 后跑一次、结果仅产出 findings，**未进判定链**。
  真实 p19 分歧（paddle `49.0` vs mineru `99.0`，规格 `≥98%`）跨阈值两侧却无人仲裁。
- **行业依据**：`OCR by Consensus` —— "共识把分歧变成信号，而不是静默的错误"；
  ExtractConf 实测单信号置信代理 AUC 0.705/0.692/0.744，**多信号融合 0.928**。
- **改动面**：
  1. 新增 `core/pipeline/dual_arbitrate.py`（**纯函数**，可离线重放）：输入两侧
     `structured_json`（+ 表级 bbox 粗筛），输出 `{一致 / 分歧 / 单侧缺失}`。
     对齐用 **表级 bbox 粗筛 → 标签文本归一化（复用 `spec_guard._norm`）字段匹配**。
  2. 触发条件（**选择性**，非全量双读 —— 上游成本已实证）：仅当**即将下
     `param_out_of_spec`（warning/critical）结论**时对该字段触发第二读。
  3. 判定分级：
     - 两侧一致 → 维持原判定；
     - 两侧分歧 → `type=needs_arbitration`，**复核页并列展示两侧原始读数**；
     - 第二读**不可用**（队列满 / 超时 / 未配置）→ **同样 `needs_arbitration`**
       （fail-closed，**绝不静默采信第一读**）。
- **硬约束**（spike §5.3 实测）：Paddle 上游 `code 10010 任务提交队列已满`
  在本轮 spike 期间 **两次实测复现**，且第二轮 e2e 即因此
  `ocr_failover paddle→mineru`。→ **"第二读取不到"必须是常态分支，不是异常分支。**
- **机检不变式**：
  - `needs_arbitration` 同步进 `CANONICAL_TYPES`（现 21 类）六面：
    `zh_map.FINDING_TYPE_ZH` / `gmp_basis.GMP_BASIS_MAP` / `kb.retriever.TYPE_QUERIES` /
    `templates/review.html` + `static/review.js` 双端 `type_zh`（`test_type_sync.py` 机检）；
  - **源码扫描禁止静默选边**：任何返回"仲裁结论"的函数必须携带两侧读数
    （禁止只返回一个值 + 无出处）。
- **测试**：单元覆盖四路（一致 / 分歧 / 单侧缺失 / 第二读不可用）；
  真实用例固定为 p19（`49.0` vs `99.0`）+ p08 温度 + p09 使用次数。
- **验收口径**：
  - 真实 51 页上"跨规格阈值两侧的读数分歧" **100%** 进入 `needs_arbitration`；
  - 误报数**不上升**（对照 `docs/FINDING_GROUND_TRUTH.json`）；
  - 第二读不可用时不得新增"自动通过"。
- **前置**：T-P1-3（需要栅格/字段对齐能力）。

---

## P1 — 结构性降噪

### T-P1-3 HTML 表格栅格化（T-P0-1 的前置）

- **定位依据**：两个后端的表格都只以 HTML 字符串给出（Paddle `block_content`、
  MinerU `content.html`），当前无统一栅格表示；MinerU 在源文含 `|` 时会产出伪单元格
  （`<td>|</td>`，spike §2.1 已确认成因是源文竖线而非模型缺陷）。
- **改动面**：新增纯函数把表格 HTML → `(row, col)` 栅格（**展开 `rowspan`/`colspan`**），
  并标记"内容仅分隔符"的伪单元格。
- **机检不变式**：栅格化必须是纯函数；必须覆盖 `colspan`/`rowspan`/伪单元格三类单测；
  同一 HTML 两次调用结果必须一致（确定性）。
- **验收口径**：`colspan` 展开后行列数与渲染视觉一致（抽 p8/p9/p19 人工核对）。

### T-P1-1 / T-P1-2 值融合与行序配对校验（合并实现）

- **定位依据**（spike §4 D2）：Paddle 把**整个多行区块压成 1 个 `<tr>` + 两个多行
  `<td colspan=N>`**（标签列 / 值列），标签↔值**只靠行序对应**；且分隔符形态**不一致** ——
  p09 用字面 `\n`（23 处）正确成行，p21 **完全无分隔符** →
  `A000626-221201/4` + `17 次` 融合成 `A000626-221201/417 次`，**规则层与 LLM 层同时
  读到 417**（规格 ≤100 → 双双误报）。这是 M8 记为"两个相邻单元格粘连"的**真根因**。
- **不做**：不做"看到 417 就改写"——8/26 轮次同页显示为 `A000626-2212017 ☐次`，
  证明是**服务端非确定性行为**，无法可靠还原切分点。
- **改动面**：在 `core/rules/` 加纯函数：对"多行单元格"区块校验
  **标签行数 == 值行数**；不等（含 0/1 行）→ 该区块所有字段标记
  `type=structure_warning`（新类型，需六面同步）、**降级交人工**，不产出
  `param_out_of_spec`。
- **机检不变式**：校验逻辑必须是纯函数（可离线重放）；`structure_warning` 与
  `needs_arbitration` 一样进 `CANONICAL_TYPES` 六面（`test_type_sync.py`）。
- **验收口径**：p9 / p21 两页的融合字段**不再产出超差**，改为 `structure_warning`；
  其余页面的正常判定**不变**（对照基线）。

### T-P1-4 换行语义归一化

- **定位依据**（spike §4 D3）：同一份 Paddle 输出里**字面 `\n`（193 处）与真换行
  （1057 处）混用** —— 同一"多行单元格"在 HTML 文本里是 `\` + `n` 两个字符。
- **改动面**：在 `markdown.text` 组装/落库路径把字面 `\n` 归一为真换行
  （或统一为显式分隔标记），使下游只有一种语义。
- **机检不变式**：源码扫描禁止在 `markdown.text` 写出路径上残留字面 `\n`。
- **风险**：LLM prompt 依赖现有形态，须做真实页回归（抽 5 页对比 LLM 抽取结果）。

### T-P1-5 Paddle LaTeX 残渣清理

- **定位依据**（spike §4 D3）：`$…$` 残渣 **144 处**（`$ m^{3} $`、`$ \pm $`）。
- **做**：`$…$` 定界符剥离 + `\pm` → `±`（均已由 T-P0-4 在 spec 侧覆盖，
  本项是把**正文侧**也归一，供 LLM 输入更干净）。
- **不做**：`13:6  $ m^3 $` 这类"小数点退化"**不做启发式还原**（不可靠，
  且会触碰真实数值）。
- **机检不变式**：清理必须是纯函数；**不得改动非公式区文本**（以 diff 断言）。

### T-P1-6 记录频次 `±` 隔离护栏（设计约束，非独立改动）

- **定位依据**（spike §4 D4）：`±` 在真实输出里分两种角色 —— 真规格
  （`15±5°C` / `70±5°C` / `pH 7.5±0.2`）与**记录频次噪声**
  （**"每1小时±5分钟记录一次"，出现在 12 页**，如 p10/p11/p17/p18/p20/p21/p22/p26）。
- **要求**：任何"宽 ± 解析"只作用于 `spec_range`，**绝不可作用于正文**；
  保持 `_parse_spec` 的开头锚定。**补一条单测**断言
  `_parse_spec("每1小时±5分钟记录一次") is None`。

---

## P2 — 度量与治理

### T-P2-1 金标集扩充为固定用例

- 把本轮定位的真实缺陷登记进 `docs/FINDING_GROUND_TRUTH.json`：
  p9「417」（值融合）、p19「49.0 vs 99.0」（跨阈值分歧）、p8「`15 \pm 5`」（LaTeX 规格）、
  p21「无分隔符融合」，并接入 CI 回归门禁。
- **验收**：这些用例在任何后续改动中都是硬门禁，回归即 FAIL。

### T-P2-2 `confidence` 校准或改名

- **定位依据**：现值为 `source × page_flag` 的**确定性映射**（0.50–0.85），
  不是校准概率。行业实测（ExtractConf）说明这类单信号代理的 AUC 仅 ~0.70。
- **二选一**：按金标集做可靠性校准；或**改名 `source_priority`**，
  避免被后续读者误读为"正确概率"。

### T-P2-3 KPI 口径补齐

- `noise_types` KPI 增 `needs_arbitration` / `structure_warning` / `suppressed`
  （后者来自 T-P0-2）。
- **必须同时加"漏检对照指标"** —— 行业禁则原文："若假阳率减半但丢掉一个已知真命中，
  那不是改进，那是控制失效套了个更好看的面板。"

### T-P2-4 跨后端可比性标记

- `ocr_failover` 审计已存在；补：**failover 后本轮 findings 标注实际后端**，
  使"跨后端逐条对比"在数据上就被禁止（不同后端读数不可逐条比）。

---

## 明确不做（附理由）

| 不做 | 理由 |
|---|---|
| 宽启发式软化 | M8 `_decimal_loss_factor` 回归已实证会吃掉真实超差（`25 vs ≤5.0℃` 被软化）；合规行业对它的定性更重 |
| "看到 417 就改写" | D2 是服务端非确定性行为（同页两轮结果不同），无法可靠还原切分点 |
| 用 Paddle `layout_det_res.score` 当抽取置信度 | 实测是**版面检测分**（均值 0.579、p50 0.545），小文本块天然低分，与数值正确性无关 |
| 单元格级坐标对齐 | 两个后端都不提供单元格 bbox（Paddle 整表 1 块、MinerU 整表 1 span），成本收益不成立 |
| 单元格级高亮 | 同上；退化为区域级高亮（T-P0-3）已满足"点击回页核对"的需求 |

---

## 当前状态（v1.1.1）

| 项 | 状态 | 提交 |
|---|---|---|
| T-P0-4 `_parse_spec` 书写变体归一化 | ✅ 完成 | `4273c1f` |
| T-P0-2 抑制留痕（台账可查/可回退/可抽检） | ✅ 完成（含 10 项真实 HTTP 集成测试） | `a5661a6` + v1.1.1 |
| T-P0-3 区域级证据锚（含宽高比闸门 + 旋转逆映射） | ✅ 完成（含方向独立核验） | v1.1.1 + 本次 |
| T-P0-1 选择性双读仲裁 | ⬜ 未开始（前置 T-P1-3） | — |

**验收项已全部闭合**。T-P0-3 的"抽 5 页人工核对高亮框与页面坐标系一致"已升级为
可复现的三层证据（确定性不变量 / 独立地面真值配准 / 本机可视化抽查），
过程、实测数据与一条**负面结论**（本批记录上不存在廉价的像素代理判据，故不写伪测试）
见 `docs/REGION_ANCHOR_VISUAL_CHECK.md`。

**下一项**：T-P0-1 选择性双读仲裁（前置 T-P1-3 HTML 栅格化）。
