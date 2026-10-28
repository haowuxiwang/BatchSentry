"""合成横置页 e2e 样本（产物 e2e_rot.pdf 不入 git，可再生）— round-23 A。

模拟真实缺陷场景：扫描时纸横放 → 内容相对页面呈 90°/270° 横置
（非 /Rotate 元数据，元数据路径 Stage 0 已处理）。VL OCR 对横置文本
可能直接识别（e2e 断言兼容两条路径）；识别稀疏/空时走自愈链：
切片重跑仍空 → 旋转探测 90/270/180° 重渲染 → 采纳恢复（rotation_deg
落 ocr_diagnostics，审计 stage1_rotation_recovered）。

页布局（4 页）：
- 页1 正常封面（批号 B2025001 — 批号基准）
- 页2 内容横置 90°（工序表，含标记「横置九十度工序表」）
- 页3 内容横置 270°（参数表，含标记「横置二百七十度参数表」）
- 页4 正常 QA 复核页
"""
import fitz

_W, _H = 595, 842  # A4


def _content_page(doc_src: fitz.Document, lines: list[str], title: str):
    """正常方向的源内容页（写入临时 doc，供旋转嵌入）。"""
    p = doc_src.new_page(width=_W, height=_H)
    y = 60
    p.insert_text((50, y), title, fontsize=14, fontname="china-s")
    y += 30
    for ln in lines:
        p.insert_text((50, y), ln, fontsize=11, fontname="china-s")
        y += 22
    return p


def _embed_rotated(doc: fitz.Document, src: fitz.Document, angle: int):
    """把 src 第 1 页旋转 angle 度后嵌入 doc（横置内容页）。"""
    rect = src[0].rect
    w, h = (rect.height, rect.width) if angle in (90, 270) else (rect.width, rect.height)
    np = doc.new_page(width=w, height=h)
    np.show_pdf_page(np.rect, src, 0, rotate=angle)


doc = fitz.open()

# 页1 正常封面
p1 = doc.new_page(width=_W, height=_H)
p1.insert_text((50, 60), "批生产记录 封面（横置自愈 e2e）", fontsize=14, fontname="china-s")
for i, ln in enumerate([
    "产品: 注射用头孢曲松钠 1.0g",
    "批号: B2025001",
    "生产日期: 2026-09-04",
    "规格: 1000 支/批",
]):
    p1.insert_text((50, 100 + i * 22), ln, fontsize=11, fontname="china-s")

# 页2 横置 90°（工序表 — 正常排版后整体旋转嵌入）
src2 = fitz.open()
_content_page(src2, [
    "工序编号 | 工序名称 | 开始时间 | 结束时间 | 操作人 | 复核人",
    "1 | 称量 | 2026-09-04 08:00 | 2026-09-04 08:30 | 张三 | 李四",
    "2 | 配液 | 2026-09-04 09:00 | 2026-09-04 10:00 | 张三 | 李四",
    "3 | 灌装 | 2026-09-04 13:00 | 2026-09-04 14:00 | 王五 | 李四",
], "横置九十度工序表")
_embed_rotated(doc, src2, 90)
src2.close()

# 页3 横置 270°（参数表）
src3 = fitz.open()
_content_page(src3, [
    "参数检查项目 | 规格范围 | 实测值 | 判定",
    "灌装温度 | 25-30 ℃ | 28 ℃ | 合格",
    "冻干真空度 | 0.1-0.3 mbar | 0.2 mbar | 合格",
    "压塞压力 | 2.0-4.0 bar | 3.0 bar | 合格",
], "横置二百七十度参数表")
_embed_rotated(doc, src3, 270)
src3.close()

# 页4 正常 QA 复核页
p4 = doc.new_page(width=_W, height=_H)
p4.insert_text((50, 60), "QA 复核记录", fontsize=14, fontname="china-s")
for i, ln in enumerate([
    "复核项目 | 结果 | 复核人 | 复核时间",
    "物料平衡复核 | 是 | QA钱七 | 2026-09-05 09:00",
    "批记录完整性复核 | 是 | QA钱七 | 2026-09-05 09:30",
]):
    p4.insert_text((50, 100 + i * 22), ln, fontsize=11, fontname="china-s")

doc.save("e2e_rot.pdf")
print("e2e_rot.pdf:", doc.page_count, "pages (p2=rot90, p3=rot270)")
