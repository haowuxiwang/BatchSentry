"""P0-3 区域级证据锚 —— 人工核对辅助工具（把"抽 5 页目视"变成可复现证据）。

为什么需要它
------------
单测能证明**算法与数据**正确（归一化落在单位方格、坐标系如实记录、SSR 与
AJAX 同源），但证明不了"人眼看到的框位置对不对" —— 那必须在真实页面上看。
本工具把这一步变成：**用生产代码算出页面空间框 → 画到真实渲染页上 →
输出图片与清单**，于是核对变成"看图"，可重复、可归档、可回归。

画的是生产路径
--------------
* 区域载荷：``core.pipeline.regions.extract_regions``（与 stage1 落库同源）；
* 锚定：``core.pipeline.regions.region_anchor``（与 SSR / AJAX 同源）；
* 画框：``page_bbox``（页面空间）按页面矩形等比放大 —— 与前端
  ``static/review.js::positionRegionOverlay`` 的 ``offsetWidth/offsetHeight``
  像素换算等价（前端不做任何旋转，旋转已在后端完成）。

``--induce-naive`` 会额外输出一张"按 OCR 空间原样叠加"的对照图：旋转页上
它必然错位 —— 这是"修好了"最直观的证明（同时也说明宽高比闸门的必要性）。

用法::

    python scripts/verify_region_anchor.py --pages 3,7,8,19,35 --induce-naive

产物落在 ``--out``（默认 ``devlogs/region_anchor_check``，该目录被 gitignore：
页面图源自真实批记录，**不入库**）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.pipeline.regions import (  # noqa: E402
    anchor_tokens,
    extract_regions,
    map_bbox_to_page,
    region_anchor,
)

DEFAULT_JOB = "0c5cfb00-897"
DEFAULT_PAGES = "3,7,8,19,35"          # 含第 8 页（服务端转正过的那页）
_ANCHOR_COLOR = (0.85, 0.1, 0.1)       # 锚中的区域：红
_PLAIN_COLOR = (0.15, 0.35, 0.85)      # 其它区域：蓝
_MISS_COLOR = (0.85, 0.35, 0.05)       # 对照图（错位）：橙


def _job_dir(job: str) -> Path:
    return Path(os.environ.get("APPDATA", "")) / "PBC" / "output" / job


def _load_pages(jsonl: Path) -> list[dict | None]:
    """逐页 ``extract_regions``（真实产物 → 生产载荷）。"""
    pages: list[dict | None] = []
    with open(jsonl, encoding="utf-8") as f:
        for line in f:
            for pr in json.loads(line)["result"]["layoutParsingResults"]:
                pages.append(extract_regions(pr))
    return pages


def _probe_text(payload: dict, index: int) -> str:
    """构造"类 finding 文案"：取该区域自身文本里特征词最密的片段。

    核对的是**几何映射**（框画在哪），不是锚定语义 —— 后者由单测覆盖。
    用区域自身文本做探针，等价于"若有一条 finding 谈的是这里，框该落哪"。
    """
    text = payload["regions"][index]["text"]
    return text[:400] or ""


def _best_probe_index(payload: dict) -> int:
    """挑特征词最多的区域作探针（最有代表性、最容易肉眼确认）。"""
    best_i, best_n = 0, -1
    for i, r in enumerate(payload["regions"]):
        n = len(anchor_tokens(r["text"]))
        if n > best_n:
            best_i, best_n = i, n
    return best_i


def _render(fitz, doc, pno: int, zoom: float, boxes: list[tuple], out: Path) -> None:
    """在**单页临时副本**上画框并渲染成 JPEG。

    用临时副本而非直接画在原 doc 上：同一页可能要出两张图（映射后 + 对照），
    直接画会让两张图互相污染。

    ``boxes`` = [(bbox, color, width, label)]，bbox 为**页面空间**归一化坐标。
    画法是 ``归一化 × 页面矩形`` —— 与前端 ``positionRegionOverlay`` 的
    ``offsetWidth/offsetHeight`` 像素换算等价（前端不做旋转）。
    """
    tmp = fitz.open()
    try:
        tmp.insert_pdf(doc, from_page=pno - 1, to_page=pno - 1)
        page = tmp[0]
        w, h = page.rect.width, page.rect.height
        for bbox, color, width, label in boxes:
            rect = fitz.Rect(
                bbox[0] * w, bbox[1] * h, bbox[2] * w, bbox[3] * h,
            )
            page.draw_rect(rect, color=color, width=width)
            if label:
                page.insert_text(
                    fitz.Point(rect.x0 + 0.8, max(8.0, rect.y0 - 1.5)),
                    label, fontsize=7, color=color,
                )
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        pix.save(str(out))
    finally:
        tmp.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="区域级证据锚的人工核对辅助工具")
    ap.add_argument("--job", default=DEFAULT_JOB)
    ap.add_argument("--pages", default=DEFAULT_PAGES,
                    help="要核对的页码（1 起，逗号分隔）")
    ap.add_argument("--out", default=str(REPO / "devlogs" / "region_anchor_check"))
    ap.add_argument("--zoom", type=float, default=1.6)
    ap.add_argument("--induce-naive", action="store_true",
                    help="额外输出未做旋转逆映射的对照图（旋转页上必然错位）")
    args = ap.parse_args()

    job_dir = _job_dir(args.job)
    jsonl = job_dir / "paddle_original.jsonl"
    pdfs = sorted(job_dir.glob("*_normalized.pdf"))
    if not jsonl.exists() or not pdfs:
        print(f"SKIP 真实产物不在场：{job_dir}")
        print("     （需要 <job>/paddle_original.jsonl 与 <job>/*_normalized.pdf）")
        return 0

    try:
        import fitz
    except ImportError:
        print("SKIP 需要 PyMuPDF（fitz）：请在装了依赖的解释器下运行")
        return 0

    payloads = _load_pages(jsonl)
    pages = [int(p) for p in args.pages.split(",") if p.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdfs[0]))
    report, failures = [], []

    for pno in pages:
        if not (1 <= pno <= len(payloads)) or pno > doc.page_count:
            failures.append(f"p{pno}: 页码超出范围（产物 {len(payloads)} 页 / PDF {doc.page_count} 页）")
            continue
        payload = payloads[pno - 1]
        rendered = doc[pno - 1]
        page_aspect = round(
            rendered.rect.width / rendered.rect.height, 4,
        )

        if payload is None:
            report.append({"page": pno, "regions": 0, "note": "该页无区域载荷"})
            failures.append(f"p{pno}: 无区域载荷（regions_json 为空）")
            continue

        rot = payload.get("space_rotation")
        # 页面空间框（生产函数；与 SSR / AJAX / 前端同源）
        page_boxes = [map_bbox_to_page(r["bbox"], rot) for r in payload["regions"]]
        bad = [b for b in page_boxes if b is None]
        if bad:
            failures.append(f"p{pno}: {len(bad)} 个区域无法映射到页面空间")
        if any(not (0.0 <= v <= 1.0) for b in page_boxes if b for v in b):
            failures.append(f"p{pno}: 映射结果越出单位方格")

        idx = _best_probe_index(payload)
        anchor = region_anchor(_probe_text(payload, idx), payload)

        # 机检：锚中的区域必须真的命中探针的特征词（语义不变式）
        anchor_ok = False
        if anchor is None:
            failures.append(f"p{pno}: 探针（区域 {idx}）锚不上")
        else:
            toks = anchor_tokens(_probe_text(payload, idx))
            rtext = payload["regions"][anchor["index"]]["text"]
            anchor_ok = any(t in rtext.lower().replace(" ", "") for t in toks)
            if not anchor_ok:
                failures.append(
                    f"p{pno}: 锚中区域 {anchor['index']} 未命中探针特征词（选优逻辑可疑）"
                )

        # 闸门判定（与 review.js 同口径）：page_aspect 对得上渲染图才画
        gate_ok = anchor is not None and abs(
            (anchor["page_aspect"] or 0) - page_aspect
        ) / page_aspect <= 0.02

        boxes = []
        for i, (b, r) in enumerate(zip(page_boxes, payload["regions"])):
            if b is None:
                continue
            hit = anchor is not None and i == anchor["index"]
            boxes.append((
                b, _ANCHOR_COLOR if hit else _PLAIN_COLOR,
                2.5 if hit else 0.8, str(i),
            ))
        name = f"p{pno:02d}"
        _render(fitz, doc, pno, args.zoom, boxes, out_dir / f"{name}.jpg")

        naive_name = None
        if args.induce_naive and rot in (90, 180, 270):
            # 对照：按 OCR 空间原样叠加（不做逆映射）—— 旋转页上必然错位
            naive = [(r["bbox"], _MISS_COLOR, 1.2, "") for r in payload["regions"]]
            naive_name = f"{name}_naive.jpg"
            _render(fitz, doc, pno, args.zoom, naive, out_dir / naive_name)

        report.append({
            "page": pno,
            "regions": len(payload["regions"]),
            "space": payload["space"],
            "space_aspect": payload["space_aspect"],
            "space_rotation": rot,
            "rendered_aspect": page_aspect,
            "page_aspect_expected": anchor["page_aspect"] if anchor else None,
            "gate_pass": bool(gate_ok),
            "rotated": bool(anchor["rotated"]) if anchor else None,
            "probe_index": idx,
            "anchored_index": anchor["index"] if anchor else None,
            "anchored_label": anchor["label"] if anchor else None,
            "anchored_bbox_ocr": anchor["bbox"] if anchor else None,
            "anchored_bbox_page": anchor["page_bbox"] if anchor else None,
            "anchor_semantics_ok": anchor_ok,
            "image": f"{name}.jpg",
            "control_image": naive_name,
        })

    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps({"job": args.job, "pages": report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"产物目录: {out_dir}")
    print(f"{'页':>4} {'区域':>4} {'space':>11} {'rot':>5} {'页面aspect':>10} "
          f"{'闸门':>5} {'锚中':>5}  说明")
    for e in report:
        if "note" in e:
            print(f"{e['page']:>4} {e['regions']:>4}  —           —        —        "
                  f"  —     —   {e['note']}")
            continue
        print(f"{e['page']:>4} {e['regions']:>4} "
              f"{str(e['space'][0]) + 'x' + str(e['space'][1]):>11} "
              f"{e['space_rotation']!s:>5} {e['rendered_aspect']:>10} "
              f"{'PASS' if e['gate_pass'] else 'FAIL':>5} "
              f"{e['anchored_index']!s:>5}  "
              f"{'已映射(旋转页)' if e['rotated'] else '直映'}"
              f"{' + 对照图' if e['control_image'] else ''}")
    print(f"清单: {report_path}")

    if failures:
        print("\n机检失败:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\n机检全部通过。请打开上面的图片，人眼确认每个框落在其编号对应的版面上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
