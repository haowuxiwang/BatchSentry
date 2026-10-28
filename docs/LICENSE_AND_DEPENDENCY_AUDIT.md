# 许可证与依赖许可审计（2026-10-28，R127 / 修订 R129）

> 本文只记**实测**。凡未核过的，就地写「未验证」，不写成结论。
> 结论一句话：**当前实际许可证是 AGPL-3.0-only**（2026-10-28 由 Apache-2.0 改判，理由见 §1.1）；
> 但**加 LICENSE 不解决数据问题**，也**不解决已推送历史里的真实标识符**。

## 1. 决策：先选 Apache-2.0，同日改判 AGPL-3.0-only

### 1.1 为什么最终是 AGPL-3.0-only（2026-10-28 下午，用户裁定）

用户明确：**追求最小最快的闭环**，且**不改 PyMuPDF 依赖**（换 `pypdfium2` 需要重写 6 处调用点、
重跑全部产物级 e2e）。本产品已承诺开源，因此**整体改 AGPL-3.0-only** 是唯一"今天就能合规"的选项：

| 事实 | 改判前（Apache-2.0） | 改判后（AGPL-3.0-only） |
|---|---|---|
| 仓库源码许可 | Apache-2.0 | AGPL-3.0-only |
| 冻结产物含 PyMuPDF（AGPL） | **不成立**（§6） | **成立**：整体同证，§5 的"整体作品"要求被满足 |
| 分发义务 | NOTICE 归属 | §13 网络交互须提供对应源码（NOTICE 已写明） |
| 商业后果 | 药企可用 | **药企采购通常禁 AGPL** ⇒ 等于放弃企业闭源集成市场（用户已知悉并接受） |

⚠️ **改判不撤销 §6 的事实**，只是把"选项③"落成了决定。改用 Artifex 商业许可或换渲染库仍是
未来可选路径；一旦换掉 PyMuPDF，`NOTICE` 里那段 AGPL 依赖说明必须同步删除（有护栏双向检查）。

### 1.2 当初为什么在 Apache-2.0 与 MIT 之间选 Apache-2.0

| 维度 | Apache-2.0 | MIT | 对本项目的权重 |
|---|---|---|---|
| 专利授权 | **§3 明示授予**（贡献者专利许可） | **完全不提** | **决定性** |
| 商标 | **§6 明文保留**，不随代码授权 | 不置一词（沉默） | **决定性**（见 §3） |
| 修改声明 | §4(b) 要求标注改动文件 | 无要求 | 中 |
| NOTICE 机制 | §4(d) 随分发携带归属 | 无 | 中 |
| 专利报复 | §3 起诉即终止授权 | 无 | 中（单向保护） |
| 篇幅 / 认知成本 | 长（11 358 B） | 短（约 1 KB） | 低 |
| 与 GPLv2 兼容 | 不兼容 | 兼容 | 无（本项目不涉及） |

**为什么不是 MIT**：MIT 更短、更普及，这没错。但在**同时满足**下面三个条件时，"简洁"就是"沉默"，而沉默在企业法务眼里是负债：

1. 仓库里**确实有第三方商标**（一家制药企业的中英文名，出现在 `tests/unit/test_ocr_clients_mock.py`、`test_regions_anchor.py`、`docs/REGION_ANCHOR_VISUAL_CHECK.md`）。MIT 不授权商标，但也**不说**它不授权 —— 只能靠解释。
2. 技术面（多模态抽取、区域锚定、规则引擎）**有可专利空间**。MIT 对专利沉默，等于把专利风险整体留给下游使用者；医药/合规买方的法务审查普遍会问这一条。
3. 产品定位是"机器筛查、人来判定"的**辅助**工具，必须持续向客户传达"不是经认证的 GMP 系统"。Apache-2.0 的 NOTICE 机制天然承载这类声明。

## 2. 最容易忽视的一条：LICENSE 授权的是**仓库内容**

Apache-2.0 与 MIT 都是**版权许可**，授予的是"复制、修改、分发**本作品**"的权利。而本仓库里现在有**你不拥有的东西**：

- 真实批记录里的**批号**、**记录编号**、**产品名与规格**、**批量**、**企业名**；
- 真实**员工姓名**（出现在 `docs/ADVERSARIAL_AUDIT.md`、`docs/TODO.md`、`tests/unit/test_rule_gmp.py`、`tests/unit/test_llm_finding_guard.py`）；
- 一把**真实 LLM 密钥**（自 `81964a3` 起在 git 历史中，且该提交**已推送**）。

> ⚠️ **这份文档刻意不列具体值**（2026-10-28 改）。第一版把每个真实批号、记录编号、
> 员工姓名都"记录在案"了 —— 而这份文档**是要提交的**，于是"记录泄漏"本身成了**新的泄漏**：
> 门禁 `no_pii_in_tracked_files` 上线后立刻在这里抓到 **12 处**。要查具体值看
> **gitignored** 的 `devlogs/_verify/pii_map.json`（那才是真值唯一落点）；这里只留类别与位置。
> 换成占位符也不行 —— 读者会把占位符当成真值。

给这样的仓库加一份宽松许可证，法律效果是：**你替第三方宣布"这些数据谁都可以拿去用"**。你没有这个权利。

⇒ **正确顺序是「先脱敏，再加 LICENSE」**。本轮按用户要求先把 LICENSE 落地（它同时修掉了一处真实矛盾，见 §4），但**这不代表数据问题已经解决** —— 见 `docs/TODO.md` 的 0-65 / 0-66。

## 3. 其余六条容易忽视的

1. **`package.json` 里本来就写着一个 `"license": "ISC"`** —— npm init 的默认占位符，从没人核过。也就是说本仓库此前**同时**处于「没有 LICENSE 文件」和「元数据声明 ISC」两个状态，而**没有任何机检**能发现。已改为 `AGPL-3.0-only` 并由 `tests/unit/test_license_consistency.py` 锁住。
2. **公开仓库 + 无 LICENSE ≠ 公有领域**，而是**默认保留所有权利**。这是加许可证之前唯一在保护你的东西；外人不 fork、不用，不是因为没兴趣，是因为**不合法**。
3. **LICENSE 不回溯**。加许可证只对之后的使用者生效；已经 clone 过的人不受影响。**删文件也删不掉 git 历史里的密钥** —— 只有到服务商处轮换才算处置（见 `DEPLOYMENT.md` 的「Secret 轮换流程」）。
4. **版权主体要写真实法定实体**。`NOTICE` 现在写的是 `the BatchSentry authors`（不虚构个人或公司）。若要做申报 / 商业授权 / 维权，必须换成真实主体名 —— 否则将来主体对不上。
5. **依赖许可证兼容性**：见 §5。**PyMuPDF 是 AGPL-3.0**，这条比"选 MIT 还是 Apache"重要一个数量级 —— 它最终决定了整份许可证只能是 AGPL（§1.1）。
6. **`pyproject.toml` 故意不声明 license**。本仓库不以 Python 包分发（产物是 PyInstaller 冻结 exe），`[project].license` 会是一个**没有消费者**的声明 —— 与 R126 删掉的 `RuleSpec.severity` 同型。**缺声明是"没有"，不是"矛盾"**。护栏里有一条**反向**断言挡着它被"好心加回来"。

## 4. 本轮落地的文件

| 文件 | 变更 | 可机检判据 |
|---|---|---|
| `LICENSE` | AGPL-3.0-only **官方原文逐字节**（spdx/license-list-data 的 `AGPL-3.0-only.txt`；LF 归一化后 sha256 `d8a6cc31…96bee`，34 020 B）。初版为 Apache-2.0，同日替换 | `test_license_body_matches_upstream_when_registered`（A 组，含 sha256 + 字节数双断言） |
| `NOTICE` | 归属 + **授权范围排除项**（第三方数据 / 商标 / 打包进产物的第三方组件）+ 点名 PyMuPDF（AGPL）+ §13 网络交互说明 | `TestNoticeStatesTheScope`（4 条） |
| `package.json` / `package-lock.json` | `"license": "ISC"` → `"Apache-2.0"` → **`"AGPL-3.0-only"`**（两处同步） | `test_metadata_spdx_matches_what_license_actually_is` |
| `tests/unit/test_license_consistency.py` | 新增 12 条；改判时改 4 处（登记新哈希、SPDX 名、条款清单按许可证动态取、NOTICE 标题反推），**未删任何断言** | 变异 **7/7 CAUGHT**（`devlogs/_verify/r127_license_mutation.py`） |

**刻意不加版权行到 `LICENSE`**：要写归属就写 `NOTICE`。理由是 A 组要能机检"正文没被改动" —— 一旦允许在正文末尾追加，sha256 断言就得退化成前缀匹配，而前缀匹配挡不住"在中间改一句话"（变异 M3 就是专压这一点：只改一个词、长度不变）。

## 5. 依赖许可扫描（实测，`importlib.metadata` + LICENSE 文件首段交叉核对）

| 包 | 版本 | 许可证 | 证据 |
|---|---|---|---|
| **PyMuPDF** | 1.26.7 | **AGPL-3.0 / Artifex 商业（双许可）** | metadata `License` 字段原文 |
| Pillow | 12.3.0 | HPND（PIL 历史许可） | LICENSE 首段（未做 SPDX 逐字比对） |
| fastapi | 0.139.0 | MIT | LICENSE 首行 |
| uvicorn | 0.48.0 | BSD-3-Clause（Encode OSS） | LICENSE 首段 |
| starlette | 1.3.1 | BSD-3-Clause（Encode OSS） | LICENSE 首段 |
| jinja2 | 3.1.6 | BSD-3-Clause | classifier + LICENSE |
| markupsafe | 3.0.2 | BSD-3-Clause | classifier + LICENSE |
| openai | 2.35.1 | Apache-2.0 | metadata + LICENSE |
| requests | 2.33.0 | Apache-2.0 | metadata + LICENSE |
| python-multipart | 0.0.31 | Apache-2.0 | classifier + LICENSE |
| pydantic | 2.11.5 | MIT | classifier + LICENSE |
| aiosqlite | 0.21.0 | MIT | classifier + LICENSE |
| anthropic | 0.109.2 | MIT | metadata + LICENSE |
| python-dotenv | 1.2.2 | BSD-3-Clause | metadata + LICENSE |
| pyinstaller | 6.18.0 | GPLv2+ **含 bootloader 例外** | metadata 原文（构建期依赖，不入运行时产物） |

**唯一阻塞项 = PyMuPDF。**

## 6. PyMuPDF：AGPL 是硬约束，不是"评估项"

**事实**（可复跑）：`requirements.txt` 锁定 `PyMuPDF==1.26.7`；`import fitz` 出现在
`api/jobs/page_image.py`、`api/jobs/upload.py`、`core/ocr_client.py`、`core/mineru_client.py`、
`core/pipeline/ocr_support.py`、`core/pipeline/self_heal.py` 等**入包源码**；产物由 PyInstaller
冻结成 `dist/pbc-server`（`dist/_internal` 内为 `pbc-server` 目录形态）。

**问题**：AGPL-3.0 §5 要求"整体作品"以 AGPL 分发并提供完整对应源码。`import fitz` 是**同进程链接**，不是可分离的聚合 ⇒ 冻结产物是**衍生作品**。因此：

> 以 Apache-2.0 分发本仓库的**源码**是成立的（那是你自己的代码）；
> 以 Apache-2.0 分发**含 PyMuPDF 的冻结产物**是不成立的。

**三个选项**（必须由用户选，本 agent 不做商业决策）：

| 选项 | 代价 | 后果 |
|---|---|---|
| ① 买 Artifex 商业许可 | 付费（按产品/装机量） | 唯一能保留现有代码、且产物可闭源分发的路 |
| ② 换掉 PyMuPDF（`pypdfium2`，BSD-3/Apache 系） | 需重写 6 处调用点的渲染/文本抽取 API + 重跑全部产物级 e2e | 一劳永逸；但 `get_text("dict")` 之类语义要逐处核对 |
| ③ 整个产品按 AGPL-3.0 分发 | 免费 | **与 GMP 客户的采购政策直接冲突**（药企普遍禁 AGPL）；等于放弃企业市场 |

**2026-10-28 决定：选 ③**（用户裁定，理由见 §1.1）。`LICENSE` / `NOTICE` / `package.json` /
`package-lock.json` 四处已同步为 `AGPL-3.0-only`；本节的事实陈述保留，不再是待决项。

⚠️ **未验证**：仓库里是否存在 Artifex 的商业许可凭据（`LICENSE-COMMERCIAL` / 采购合同）。若有，① 已成立，本节降级为"补一份声明"。

⚠️ **此前为何漏掉**：`docs/ROADMAP_v1.1.md` §7.2 与 `docs/PLAN_v1.1_EXECUTION.md` §T7.4 **认真评估过** docling / MinerU / Marker 的 AGPL 风险，并做了勘误 —— 但评估的是**可选对照引擎**；**真正入包的 PyMuPDF 从未进入这份评估**。`docs/DEPENDENCY_AUDIT.json` 只有 `packages_with_vulns` / `totals`，**没有 license 字段**；`scripts/release_gate.py` 的 `check_dependency_vulns` 也只查 CVE。⇒ 许可合规**全链路无检查点**。

## 7. 边界（不得读成已通过）

- 本轮的许可证结论覆盖**仓库源码 + 含 PyMuPDF 的冻结产物**（整体 AGPL-3.0-only，§1.1）。
  但**已推送历史**里的真实批号/姓名/密钥**不因换证而消失**（§2、§7 末条）。
- 依赖表是**直接依赖**；**传递依赖未扫**（`pip` 解析出来的那批）。
- 数据脱敏**未做**（§2）。LICENSE 落地**不**等于可以对外发布了。
- 密钥**已轮换**（见 `MEMORY.md` §0：旧 key 402→401，新 key 实测 200），但**历史仍在**，且 GitHub 仓库是**公开**的。
