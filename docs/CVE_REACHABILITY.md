# CVE 可达性表（B11-4）

> ## ⚠️ 本表已退役（2026-09-23）
>
> **原因**：`docs/DEPENDENCY_AUDIT.json` 现为 **0 条公告** —— 表中 4 个包
> （`python-multipart` / `pillow` / `python-dotenv` / `requests`）已按 **B11-7（W2）**
> 全部升级到安全版本，**已经没有公告需要判定可达性**。**§5** 的 npm/Electron 侧同理：
> `electron-builder` 升到 **26.15.3** 后 `npm audit` **0 条**（B11-21）。
>
> **下表是历史记录，不再维护** —— 保留它作为"当时是怎么判的"审计留痕。
>
> **退役 ≠ 可以忽略**：门禁 `dependency_vulns` 的判据**仍是"有公告即 FAIL"**。
> 一旦重扫出现新公告，`tests/unit/test_gate_supply_chain.py::
> test_cve_reachability_table_covers_snapshot` 会**立刻变红**
> （`snap_ids` 非空 ⇒ 要求本表**逐条覆盖**）⇒ 强制重新判定可达性。
> 该护栏**同时**钉住本文件必须保有上面这段"已退役"声明 ——
> 否则"**表没人管了**"与"**表已按流程退役**"在文件层面**无法区分**
> （PITFALLS §二十六 的恒真退化）。

> **Python 侧**：数据源 `docs/DEPENDENCY_AUDIT.json`（pip-audit 快照，`generated_at` 见该文件）
> ｜**24 条唯一公告**（pip-audit 原始列了 47 条 —— 同一条被重复列 2 份，脚本已按 id 去重）
> ｜4 个包：`python-multipart` / `pillow` / `python-dotenv` / `requests`
>
> **Node / Electron 侧**：见 **§5**（`npm audit` 18 个包 + 按产物 `app.asar` 头分层的分发面）。
>
> ⚠️ **这张表不用于"豁免"**。门禁 `dependency_vulns` 的判定依然是"**有公告即 FAIL**"
> —— 因为可达性判定是**人工证据**，会过时、会判错；用它来豁免等于把"人工判断失误"
> 直接变成"发布放行"。本表的用途只有一个：**给升级排优先级、并说清风险敞口**。

## 0. 判定方法（先定位，再结论）

可达性 = "攻击者可控字节能否走到出问题的那个代码路径"。为此用了三组**互相对照**的证据：

| 证据 | 手段 | 关键产出 |
|---|---|---|
| A. 调用面 | 全仓 grep 生产代码（排除 `tests/`、`devlogs/`） | 哪些危险 API 从未被调用 |
| B. 解码面 | `devlogs/_verify/probe_pillow_surface.py`（含**正对照**） | Pillow 能认领哪些格式 |
| C. 入站闸门 | 读 `api/jobs/upload.py` + `api/jobs/__init__.py` | 用户字节进解码器前要过哪些校验 |

**B 组探针首轮是错的，必须记下来**：`Image.OPEN` 是**惰性**填充的，未 `Image.init()`
时读到 **0 个格式** ⇒ 连 PNG 都报 `openable=False`。若不做**正对照**（塞一张真 PNG
必须被认领），就会得出"Pillow 什么都解不了"的荒谬结论。修正后：44 个格式可读。
（这就是"探测不到 X"要先证明"探针自己没坏"。）

**C 组的结论推翻了本轮的初始假设**：我原以为"`Image.open` 按**内容**嗅探 ⇒ 扩展名
白名单不构成格式约束，GD/JP2/PDF 都能借道"。
实际 `api/jobs/upload.py:160-181` 在**解码前**做 **magic bytes 白名单**校验
（`_IMAGE_MAGIC_PREFIXES`，`api/jobs/__init__.py:94-101`）：

```
JPEG \xff\xd8\xff | PNG \x89PNG\r\n\x1a\n | BMP BM
TIFF II*\x00 / MM\x00* | WEBP RIFF....WEBP
```

⇒ **JP2 / GD / PCF / BDF / PDF 的文件头根本进不来**（伪装扩展名也拦得住）。
这一条把 Pillow 的可达面从"很多"压到"只剩 TIFF"。

## 1. 汇总

| 包 | 唯一公告 | 可达 | 不可达 | 处置 |
|---|---:|---:|---:|---|
| `python-multipart` 0.0.21 | 6 | **5** | 1 | 升级**必要**（DoS 面在关键路径上） |
| `pillow` 10.1.0 | 16 | **1** | 15 | 升级**必要**（TIFF 越界读） |
| `python-dotenv` 1.2.1 | 1 | 0 | 1 | 升级=卫生（非可达性驱动） |
| `requests` 2.32.4 | 1 | 0 | 1 | 升级=卫生（非可达性驱动） |
| **合计** | **24** | **6** | **18** | |

## 2. python-multipart（上传解析 —— 在关键路径上）

产品所有上传都走 `multipart/form-data`，因此该解析器的 DoS 面**默认就在链路上**。

| 公告 | CVE | 触发条件 | 可达 | 依据 |
|---|---|---|---|---|
| PYSEC-2026-1852 | CVE-2026-24486 | `UPLOAD_DIR` + `UPLOAD_KEEP_FILENAME=True` 下文件名穿越写盘 | **否** | 该非默认配置**全仓未使用**（grep 为空）；框架默认路径不构造用户文件名 |
| PYSEC-2026-3038 | CVE-2026-40347 | 恶意 preamble/epilogue 致解析低效（DoS） | **是** | 任意 multipart 上传即可进入该解析路径 |
| PYSEC-2026-3039 | CVE-2026-42561 | 海量/超大 part header 致 CPU 耗尽（DoS） | **是** | 同上 |
| PYSEC-2026-3040 | CVE-2026-53540 | **负数 `Content-Length`** ⇒ 有界读退化为读到 EOF | **是**（已被 B11-1 缓解） | 库层可达；但 B11-1 新增的 ASGI 守卫对非法 `Content-Length` 直接 **400**，构成应用层纵深防御 |
| PYSEC-2026-3036 | CVE-2026-53539 | urlencoded 体用 `;` 且无 `&` ⇒ 每字段全缓冲失败扫描（平方级 CPU） | **是** | 同一解析器入口 |
| PYSEC-2026-3037 | CVE-2026-53538 | `;` 被当作字段分隔符 ⇒ **解析器差分** | **是** | 同上；产品不依赖 `;` 语义，但差分本身即风险 |

## 3. Pillow（仅图片路径；扩展名+magic 双重闸门后）

| 公告 | CVE | 触发条件 | 可达 | 依据 |
|---|---|---|---|---|
| PYSEC-2026-3493 | CVE-2026-54058 | **未压缩 TIFF**、tile 用 `raw` codec、**从文件名打开** ⇒ `map.c` 行指针越界读 | **是** | TIFF 头在 magic 白名单内；且产品正是 `Image.open(src)` **按文件名**打开（`upload.py:221`），与公告前置条件完全一致 |
| PYSEC-2026-2874 | CVE-2026-42310 | `PdfParser` 跟 Prev 指针 ⇒ 死循环 100% CPU | 否 | **PDF 头被 magic 白名单拦下**；且实测 `Image.open` **不认领 PDF**（`PDF openable=False`）——`PdfParser` 只服务于**保存** PDF，产品只存 PNG |
| PYSEC-2026-3495 | CVE-2026-59200 | `PdfStream.decode` 用 `bufsize` 当上限 ⇒ 内存膨胀 | 否 | 同上 |
| PYSEC-2026-3496 | CVE-2026-59204 | JPEG2000 tile 宽度累加 ⇒ 缓冲虚涨 | 否 | **JP2 magic 不在白名单**（`openable=True` 但头进不来） |
| PYSEC-2026-2256 | CVE-2026-55380 | `GdImageFile._open` 未做 bomb 检查 ⇒ 过量分配 | 否 | **GD magic 不在白名单**（`GD openable=False`） |
| PYSEC-2026-457 | CVE-2023-50447 | `ImageMath.eval(env=...)` 任意代码执行 | 否 | `ImageMath` **从未被调用**（grep 为空） |
| PYSEC-2026-1793 | CVE-2024-28219 | `_imagingcms.c` `strcpy` 缓冲溢出 | 否 | `ImageCms` **从未被调用** |
| PYSEC-2026-3453 | CVE-2026-59205 | `ImageCmsTransform.apply` 输出模式不匹配 ⇒ 堆破坏 | 否 | 同上 |
| PYSEC-2026-3451 | CVE-2026-59199 | `paste/crop/alpha_composite` 传入近 32 位极限坐标 ⇒ 越界写 | 否 | 产品仅 `background.paste(rgba, mask=...)`，**坐标为默认 (0,0)**，非攻击者可控数字 |
| PYSEC-2026-3454 | CVE-2026-59197 | `ImageFilter.RankFilter(4294967295)` ⇒ 越界写 | 否 | `ImageFilter` **从未被调用** |
| PYSEC-2026-3494 | CVE-2026-59198 | TGA RLE **编码器** mode `"1"` 读越界 | 否 | 编码路径：产品`save(format="PNG")`**写死**，从不存 TGA |
| PYSEC-2026-165 | CVE-2026-42308 | 字形 advance 极大 ⇒ 位置整数溢出 | 否 | 需字体排版（`ImageDraw`/`ImageFont`），**从未被调用** |
| PYSEC-2026-2253 | CVE-2026-54059 | `PcfFontFile._load_bitmaps` 未做 bomb 检查 | 否 | 需加载 **PCF 字体**；字体 API 未调用且 `PCF openable=False` |
| PYSEC-2026-2255 | CVE-2026-55379 | `BdfFontFile.bdf_char` 未做 bomb 检查 | 否 | 需加载 **BDF 字体**；同上（`BDF openable=False`） |
| PYSEC-2026-2254 | CVE-2026-54060 | `FontFile.compile()` 合成位图未做 bomb 检查 | 否 | 需字体 API；未调用 |
| PYSEC-2026-2257 | CVE-2026-55798 | `WindowsViewer.get_command()` 把路径拼进 `shell=True` ⇒ 命令注入 | 否 | 需 `Image.show()`/查看器路径；**从未被调用**（且仅在 Windows 查看器场景） |

**⚠️ 对唯一可达项的诚实说明**：产品上报的像素上限（`_MAX_IMAGE_PIXELS = 1e8`，
`__init__.py:91`）**不能**缓解 PYSEC-2026-3493 —— 该漏洞是**构造行指针时的越界读**
（内存安全），不是"图太大"（资源耗尽）。像素上限只拦后者。

## 4. python-dotenv / requests（读路径，写接口未用）

| 公告 | CVE | 触发条件 | 可达 | 依据 |
|---|---|---|---|---|
| PYSEC-2026-2270 | CVE-2026-28684 | `set_key()`/`unset_key()` 跟随符号链接改写 `.env`（跨设备 rename 兜底） | **否** | 产品只用 `load_dotenv` / `dotenv_values`（**只读**）；`set_key`/`unset_key` **全仓未调用** |
| PYSEC-2026-2275 | CVE-2026-25645 | `requests.utils.extract_zipped_paths()` 用可预测临时文件名，本地攻击者可预置同名文件 | **否** | `extract_zipped_paths` **全仓未调用**；且需**本地**对临时目录有写权限 + zip 形式的 CA 包（`verify=True` 走 certifi `.pem`，非 zip） |

## 5. Node / Electron 侧（`npm audit` —— 本轮首次跑）

`npm audit --json` 结果：**18 个包有公告（17 high + 1 critical）**。
但"有公告"≠"会随产物交付" —— 必须按**分发面**分层，否则会把构建机的风险
当成用户风险。分层证据来自**解析产物 `app.asar` 的头**（不是看 `package.json`）：

| 层 | 是否随产物交付 | 包 | 判定 |
|---|---|---|---|
| **运行时** | **是**（`BatchSentry.exe` + Chromium DLL） | `electron` 33.4.11 | **必须升** —— 见下 |
| **构建期** | 否（只在构建机执行） | `electron-builder` / `app-builder-lib` / `dmg-builder` / `builder-util(-runtime)` / `electron-publish` / `@electron/rebuild` / `node-gyp` / `cacache` / `make-fetch-happen` / **`tar`（critical）** / `extract-zip` / `js-yaml` / `@xmldom/xmldom` / `brace-expansion` / `postcss`→`nanoid@3.3.16` | 卫生/CI 风险，**非分发阻断** |
| **随包但死代码** | **是**（201 个条目 ≈6 MB） | `docx@9.7.1` + `jszip` / `pako` / `xml-js` / `hash.js` / `nanoid@5.1.16` … | **应删**（见下） |

### 5.1 `electron` 是唯一随分发的漏洞面

- 公告范围 `<=40.10.2 || 41.0.0-alpha.1-41.7.1 || 42.0.0-alpha.1-42.3.3 || 43.0.0-alpha.1-43.0.0-beta.8`；
  修复版本 **`electron@44.4.4`**（semver major）。
- 与门禁 `runtime_eol`（读**产物二进制**得到 33.4.11，不在 `[41,42,43]`）**两条独立证据一致**
  ⇒ Electron 升级（B11-8）是**分发阻断项**，不是可选项。
- ⚠️ 这层**不需要**新增门禁项：`runtime_eol` 已覆盖（Electron 版本即产物运行时）。
  再加一条 `npm_audit` 门禁会与它**重复判定同一件事** —— 正是本项目"三份重复实现"的坑。

### 5.2 ★ 两个"差一点写错"的教训（已实证纠正）

1. **同名 ≠ 同物**：初判把 `npm audit` 的 `nanoid`（范围 `<3.3.18`）当成"随 `docx` 分发的
   nanoid 有洞"。核对 `nodes` 字段后真相是 **`node_modules/postcss/node_modules/nanoid@3.3.16`**
   —— 一条**构建期**依赖链。而**随包**的那份是 `nanoid@5.1.16`，**不在**受影响范围。
   ⇒ 判"某包是否有洞"必须看**具体安装路径与版本**，不能按包名匹配。
2. **`grep -c node_modules app.asar` 会骗人**：它返回 402，看起来"asar 里有 node_modules"，
   但那是**二进制里恰好出现的字符串**。真值要看 **asar 头**的 JSON 文件树（解析后：
   203 条目，其中 201 条在 `node_modules/` 下）。顺带：本条命令里的 `sort -u`
   被 Windows 的 `sort.exe` 抢占（`-u系统找不到指定的文件`）—— 又一次 PATH 抢占。

### 5.3 应该删掉 `docx`（新发现）

`electron/main.js` 只 `require` 内建模块 + `electron`；全仓（排除 `node_modules`/`package-lock`）
**没有任何** `require('docx')`。但 `package.json` 把它列为 **`dependencies`** ⇒
`electron-builder` 把它的**完整生产依赖树打进 asar**（201 条目 / ~6 MB）。

后果：每个用户都白拿一份从不执行的代码树，且它是**永久的供应链面**
（一旦其中某个包日后爆洞，就自动变成"已分发"）。**处置：从 `dependencies` 删除 `docx`**
（登记为 B11-17）。删后 asar 应从 ~6 MB 降到主进程量级。

## 6. 结论 → 对升级优先级的影响（衔接 B11-7）

- **必须升**（可达）：
  - `python-multipart` 0.0.21 → **≥0.0.31** —— 5 条可达 DoS/解析器差分，在关键路径上。
  - `pillow` 10.1.0 → **≥12.3.0** —— 1 条可达 TIFF 越界读。
  - `electron` 33.4.11 → **44.4.4** —— 唯一随分发的 Node 侧漏洞面（B11-8）。
- **可升可不升**（不可达，纯卫生）：`python-dotenv` → ≥1.2.2、`requests` → ≥2.33.0。
  仍建议一并升（成本低、缩小解释面），但**不要**把"升了"当成"漏洞没了"——
  不可达结论建立在**当前代码调用面**上，一旦将来引入 `ImageFont`/`ImageCms`/
  `ImageMath` 或开始保存 PDF/TGA，这张表**会立刻失效**。
- **建议删**（零消费负债）：`package.json` 的 `docx` 依赖。
- **构建期**：`tar`（critical）等 16 个包建议随 `electron-builder` 升到 26.15.3+ 一并清掉，
  但**不与"能否分发"挂钩**（它们不在用户机器上执行）。

## 7. 这张表会腐烂 —— 所以有护栏

`tests/unit/test_gate_supply_chain.py::test_cve_reachability_table_covers_snapshot`
断言本表的**公告 id 集合**与 `docs/DEPENDENCY_AUDIT.json` **完全一致**（双向）。
后果是**故意的**：重跑 `audit_deps.py` 后若有新增公告，该测试立刻红 ⇒
强制人工重新判定可达性，而不是让一张过时的表继续"看起来有效"。
（Node 侧用的是 GHSA 编号，不参与该断言 —— 它的真值来自 `npm audit` 快照，见 §5。）

