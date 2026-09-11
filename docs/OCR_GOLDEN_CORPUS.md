# OCR 金标回归集

本目录定义批记录 OCR 的发布门禁。任何 OCR 后端、PDF 预处理、Markdown
拼接或规则提取改动，都必须在此回归集通过后才能打包。

## 样本登记

受 GMP 文件权限限制，原件不提交到 Git。测试机在受控目录配置样本，并在
本表登记文件内容哈希、页数与标注版本。

| 样本 ID | 输入特征 | 最低验收 |
| --- | --- | --- |
| mitomycin-51 | 丝裂霉素提取批记录；51 页；异常 3000×4000pt 页面盒 | 51 页均有正文或明确人工豁免；第 48 页表格不得为 `simple_table`/缺失占位 |
| standard-a4 | 标准 A4 扫描 PDF | 页数一致、页序一致、关键字段均可回跳原页 |
| landscape-table | 横向参数矩阵 | 表头、所有数据列与测量时间完整 |
| photographed | 旋转/透视/轻度畸变照片 | 方向校正生效；正文不被页眉页脚替代 |
| low-dpi | 低 DPI 扫描件 | 进入低置信度/人工复核，不得静默标记成功 |
| image-single | jpg/png/webp/bmp/tif 单页输入 | 转 PDF 后页数为 1、方向正确、原图保留 |
| unsupported-multipage-image | 多页 TIFF 或动画 WEBP | 上传明确拒绝，不得静默丢弃后续帧 |

## 页级标注格式

每个样本配套一个不含业务原文的 JSON 标注文件：

```json
{
  "source_sha256": "...",
  "pages": {
    "48": {
      "expected_body": true,
      "min_tables": 1,
      "required_tokens": ["丝裂霉素", "清洗记录"],
      "manual_exception": null
    }
  }
}
```

`expected_body=true` 的页面只要发生页眉页脚孤岛、表格缺失占位、空页或 OCR
后端丢弃块，即判定失败；不得以“PDF 页数与 OCR 页数相等”豁免。

## 合成样本（可重复最小回归）

真实样本受权限限制不能入库；每类尺寸/形态缺陷另配一个**确定性合成样本**
（`scripts/gen_ocr_samples.py`，无随机性），供单测与离线预检回归。生成：

```bash
python scripts/gen_ocr_samples.py            # → devlogs/ocr_samples/*.pdf
python tests/e2e_ocr_integrity.py            # 离线完整性 e2e（20 条断言）
```

| 样本名 | 类别 | 结构特征 | 期望（不静默成功） |
| --- | --- | --- | --- |
| `o1_small_box` | O1 微型盒 | 200×120pt + 200×120px 栅格 + 8pt 标签 | 重渲染到 300dpi；页盒不变、有效 DPI ≥250、`integrity=ok` |
| `o2_extreme_aspect` | O2 极端长宽比 | 250×2000pt（8:1）条状页 | 放宽长边上限并抬升短边（≥1024px）；`extreme_aspect` + `integrity=incomplete` |
| `o3_mixed_size` | O3 混合尺寸 | A4@300dpi + 3000×4000pt + A4 横向 | 仅超大盒页重渲染为 720×960pt；三页均 `integrity=ok` |
| `o3_normal` | O3 对照 | 全 A4@300dpi | 不产生工作副本、无告警 |
| `o6_small_font_low_dpi` | O6 小字号+低 DPI | A4 + 72dpi 栅格 + 4pt 文本 | `low_dpi`+`small_font` 结构化键 + `integrity=incomplete` |
| `guard_small_text_only` | 反例守护 | 200×120pt 纯文本微型页 | 不重渲染（保留矢量保真）、无假告警 |

同一类别在真实受限样本上的验收见上表 `low-dpi` 行；冻结包的真实样本端到端
复跑用 `python e2e_run.py --rounds robust`（断言“该页不得静默标记成功”）。

## 发布门禁

1. 原始文件、规范化工作副本、后端原始产物和页级诊断均可追溯。
2. 每页必须为 `integrity=ok`，或带有人工确认的豁免及原因。
3. 双后端输出存在关键字段差异时，任务进入人工复核而非自动通过。
4. 冻结包执行本集的离线预检与至少一份真实样本端到端回归。
