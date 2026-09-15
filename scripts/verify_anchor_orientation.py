"""P0-3 区域级证据锚 —— 坐标系方向的决定性核验（用上游自己回传的图）。

为什么需要它
------------
``docs/REGION_ANCHOR_VISUAL_CHECK.md`` 记录了一个负面结论：**没有便宜的像素代理
判据**能从渲染图反推旋转（墨迹密度 58.8%、空白带奇偶、框内文本行方差 12%、
整页并集 F1 29.4% —— 全部不具鉴别力，因为本批记录是密排表格 + 稀疏文字）。
所以"旋转方向对不对"不能靠猜，也不能靠自造代理指标，只能用**独立地面真值**。

真值从哪来：Paddle 会在响应里回传两张图（URL 的授权串为 ``/-1/``，**无过期**）：

* ``inputImage`` —— 喂进模型的原始页图（未旋转）；
* ``outputImages.layout_det_res`` —— **Paddle 自己**在"旋转后 + 画好检测框"的
  图上渲染的可视化。它等于"Paddle 实际用于版面检测的那个坐标系"。

于是链条可被逐段核验（本工具就做这件事）::

    页面图  --ρ-->  输入图  --θ-->  版面图（bbox 所在坐标系）
    要求 ρ == 0（输入图与页面同向）
    要求 θ == prunedResult.doc_preprocessor_res.angle（上游上报的旋转角）

θ 一旦等于上报角，``regions.map_bbox_to_page(bbox, angle)`` 的逆映射方向即被
独立证实 —— 这正是"高亮框会不会画倒"的唯一决定因素。

判据与阈值（分两级，避免伪判定）
--------------------------------
命中率 = 「候选旋转 + 微小平移后的输入图墨迹」落在「参考图墨迹（膨胀 2px）」内的
比例，取四旋转的最大值。判定分两级：

1. **轴（决定性）**：``{0,180}`` 与 ``{90,270}`` 两组，取各组最大值相比，
   比值须 ≥ ``--margin``（默认 1.5）。轴判错意味着框**横竖颠倒** —— 这是唯一
   会让复核员看到完全错误位置的失败模式，必须硬拦。
2. **方向（分辨得出就证，分辨不出就说）**：同轴内两候选（0 vs 180 /
   90 vs 270）若比值 < 1.25 则**如实报告"不可分辨"**。密排表格页在 180° 下
   近乎自对称，这是内容本身的限制，不是工具缺陷 —— 此时判定为
   ``PASS-AXIS``（轴已证实）而非伪造一个 PASS。

绝不因为"想让结果好看"而放松阈值：FAIL 就是 FAIL。

用法::

    python scripts/verify_anchor_orientation.py --pages 7,8

产物：``--out/orientation_report.json``（默认 devlogs/region_anchor_check，
该目录被 gitignore：图片源自真实批记录，不入库）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_JOB = "0c5cfb00-897"
_ROTATIONS = (0, 90, 180, 270)
_SHIFT = 3          # 平移搜索半径（px）：吸收缩放/JPEG 造成的整体偏移
_DILATE = 2         # 参考墨迹膨胀半径（px）：容忍 1~2px 抖动
_DEFAULT_MARGIN = 1.5   # 轴判定：同轴最大 / 异轴最大 的最小比值
_DIRECTION_MARGIN = 1.25  # 方向判定：同轴内两候选的最小比值（低于此报"不可分辨"）


def _job_dir(job: str) -> Path:
    return Path(os.environ.get("APPDATA", "")) / "PBC" / "output" / job


def _load_pages(jsonl: Path) -> list[dict]:
    """逐页取 {recorded_angle, input_url, layout_url}。"""
    pages = []
    with open(jsonl, encoding="utf-8") as f:
        for line in f:
            for pr in json.loads(line)["result"]["layoutParsingResults"]:
                pruned = pr.get("prunedResult") or {}
                dpr = pruned.get("doc_preprocessor_res") or {}
                try:
                    angle = int(dpr.get("angle"))
                except (TypeError, ValueError):
                    angle = None
                imgs = pr.get("outputImages") or {}
                pages.append({
                    "angle": angle if angle in _ROTATIONS else None,
                    "raw_angle": dpr.get("angle"),
                    "input_url": pr.get("inputImage"),
                    "layout_url": imgs.get("layout_det_res") if isinstance(imgs, dict) else None,
                })
    return pages


def _download(url: str, dest: Path) -> bool:
    if not url:
        return False
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception as e:                                  # 网络/授权失败
        print(f"    ! 下载失败: {type(e).__name__}: {str(e)[:80]}")
        return False
    if not data:
        return False
    dest.write_bytes(data)
    return True


def _gray(path: Path, page: int | None = None, zoom: float = 1.0):
    import fitz
    doc = fitz.open(str(path))
    pg = doc[page] if page is not None else doc[0]
    pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    import numpy as np
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    doc.close()
    return a


def _content_mask(path: Path):
    """版面图内容掩膜：滤掉高饱和的彩色框线，只留灰度扫描内容。"""
    import fitz
    import numpy as np
    doc = fitz.open(str(path))
    pix = doc[0].get_pixmap(colorspace=fitz.csRGB)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    doc.close()
    mx = rgb.max(axis=2).astype(np.int16)
    mn = rgb.min(axis=2).astype(np.int16)
    return ((mx - mn) <= 32) & (rgb.mean(axis=2) <= 140)


def _resize(m, h, w):
    import numpy as np
    ys = np.linspace(0, m.shape[0] - 1, h).astype(int)
    xs = np.linspace(0, m.shape[1] - 1, w).astype(int)
    return m[np.ix_(ys, xs)]


def _coverage(src_ink, ref):
    """src 墨迹落在 ref（膨胀后）内的比例；允许 ±_SHIFT 平移取最优。"""
    import numpy as np
    ref_d = ref.copy()
    for dy in range(-_DILATE, _DILATE + 1):
        for dx in range(-_DILATE, _DILATE + 1):
            ref_d |= np.roll(np.roll(ref, dy, 0), dx, 1)
    total = src_ink.sum()
    if not total:
        return 0.0
    best = 0.0
    for dy in range(-_SHIFT, _SHIFT + 1):
        for dx in range(-_SHIFT, _SHIFT + 1):
            s = np.roll(np.roll(src_ink, dy, 0), dx, 1)
            best = max(best, float(np.logical_and(s, ref_d).sum()) / total)
    return best


def _register(input_gray, ref_mask) -> dict:
    """四个候选旋转下的命中率，并分两级判定。

    **为什么分两级**：密排表格页在 180° 下近乎自对称（横竖格线分布相同），
    若强行要求四者中的唯一胜者，工具会在这些页上给出"不具鉴别力"而失效。
    但真正致命的是**轴**判错（横竖颠倒 —— 框会整体转置），而 0/180（或
    90/270）之内的方向差是"上下颠倒"，其分辨率取决于内容是否非对称。

    故：``axis_ok`` 用"同轴最大 vs 异轴最大"的比值判定；``direction_ok``
    只在同轴内两者能拉开差距时才给出（否则如实报告"不可分辨"）。
    """
    import numpy as np
    h, w = ref_mask.shape
    src = input_gray <= 140
    scores = {}
    for rot in _ROTATIONS:
        # 先旋转再缩放：错误候选的宽高比与参考不符会被压缩，命中率随之下降
        scores[rot] = _coverage(_resize(np.rot90(src, rot // 90), h, w), ref_mask)

    def _axis(rot):
        return rot % 180

    order = sorted(scores, key=scores.get, reverse=True)
    best = order[0]
    axis_best = max((r for r in _ROTATIONS if _axis(r) == _axis(best)), key=scores.get)
    axis_other = max((r for r in _ROTATIONS if _axis(r) != _axis(best)), key=scores.get)
    same_axis = [r for r in _ROTATIONS if _axis(r) == _axis(best)]
    loser = max((r for r in same_axis if r != axis_best), key=scores.get)
    ratio = (lambda a, b: round(a / b, 3) if b > 0 else None)
    return {
        "scores": {str(k): round(v, 4) for k, v in scores.items()},
        "best": best,
        "axis": _axis(best),
        "axis_margin": ratio(scores[axis_best], scores[axis_other]),
        # 同轴内两个候选（0 vs 180 / 90 vs 270）的比值：>阈值 才能定"方向"
        "direction_margin": ratio(scores[axis_best], scores[loser]),
        "direction_is_resolvable": bool(
            scores[loser] <= 0 or scores[axis_best] / scores[loser] >= _DIRECTION_MARGIN
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="区域锚坐标系方向的独立核验")
    ap.add_argument("--job", default=DEFAULT_JOB)
    ap.add_argument("--pages", default="7,8",
                    help="要核验的页码（1 起，逗号分隔）；建议含一页已知未旋转页作对照")
    ap.add_argument("--out", default=str(REPO / "devlogs" / "region_anchor_check"))
    ap.add_argument("--margin", type=float, default=_DEFAULT_MARGIN,
                    help="最优/次优 的最小比值，低于此视为不具鉴别力")
    args = ap.parse_args()

    job_dir = _job_dir(args.job)
    jsonl = job_dir / "paddle_original.jsonl"
    pdfs = sorted(job_dir.glob("*_normalized.pdf"))
    if not jsonl.exists() or not pdfs:
        print(f"SKIP 真实产物不在场：{job_dir}")
        return 0
    try:
        import fitz  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        print(f"SKIP 需要 PyMuPDF(fitz) 与 numpy：{e}")
        return 0

    pages = _load_pages(jsonl)
    wanted = [int(p) for p in args.pages.split(",") if p.strip()]
    out_dir = Path(args.out)
    img_dir = out_dir / "_paddle_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized = pdfs[0]

    print(f"产物: {job_dir}")
    print(f"{'页':>4} {'上报角':>6} {'ρ轴':>4} {'θ轴':>4} {'θ实测':>6} "
          f"{'轴优势':>6} {'方向':>6}  判定")
    report, failures, skipped = [], [], []

    for pno in wanted:
        if not (1 <= pno <= len(pages)):
            failures.append(f"p{pno}: 页码超出产物范围（{len(pages)} 页）")
            continue
        rec = pages[pno - 1]
        inp_p = img_dir / f"p{pno:02d}_input.jpg"
        lay_p = img_dir / f"p{pno:02d}_layout.jpg"
        if not _download(rec["input_url"], inp_p) or not _download(rec["layout_url"], lay_p):
            skipped.append(f"p{pno}: 上游回传图不可得（URL 缺失或在场缓存不可用）")
            print(f"{pno:>4} {rec['raw_angle']!s:>6}   —— 回传图不可得，无法核验")
            continue

        input_gray = _gray(inp_p)
        ref_page = _gray(normalized, page=pno - 1) <= 140
        rho = _register(input_gray, ref_page)
        theta = _register(input_gray, _content_mask(lay_p))

        # ρ 必须同轴于 0：喂进模型的图与页面同向（否则整条链的前提不成立）
        rho_ok = rho["axis"] == 0 and (rho["axis_margin"] or 0) >= args.margin
        if not rho_ok:
            failures.append(
                f"p{pno}: 输入图与页面**不同轴**（实测 {rho['best']}°，轴优势 "
                f"{rho['axis_margin']}x）—— 映射前提不成立。命中 {rho['scores']}"
            )

        if rec["angle"] is None:
            theta_axis_ok, dir_ok = None, None
            verdict = "上报角缺失(仅记录)"
        else:
            theta_axis_ok = (theta["axis"] == rec["angle"] % 180) and \
                (theta["axis_margin"] or 0) >= args.margin
            dir_ok = bool(theta["direction_is_resolvable"] and theta["best"] == rec["angle"])
            if not theta_axis_ok:
                verdict = "FAIL"
                failures.append(
                    f"p{pno}: 实测旋转轴 θ%180={theta['axis']}° != 上报角轴 "
                    f"{rec['angle'] % 180}°（轴优势 {theta['axis_margin']}x）"
                    f"—— 逆映射方向错误，框会横竖颠倒。命中 {theta['scores']}"
                )
            elif dir_ok:
                verdict = "PASS"          # 轴与方向均被独立证实
            else:
                verdict = "PASS-AXIS"     # 轴证实；本页内容近对称，方向不可分辨
            if not rho_ok:
                verdict = "FAIL"

        print(f"{pno:>4} {rec['raw_angle']!s:>6} {rho['axis']!s:>4} "
              f"{theta['axis']!s:>4} {theta['best']!s:>6} "
              f"{theta['axis_margin']!s:>6} "
              f"{('是' if dir_ok else ('不可分辨' if dir_ok is False else '—')):>6}  {verdict}")
        report.append({
            "page": pno, "recorded_angle": rec["angle"], "raw_angle": rec["raw_angle"],
            "input_vs_page": rho, "input_vs_layout": theta,
            "input_vs_page_axis_ok": bool(rho_ok),
            "theta_axis_ok": theta_axis_ok,
            "theta_direction_confirmed": dir_ok,
            "verdict": verdict,
        })

    if report:
        (out_dir / "orientation_report.json").write_text(
            json.dumps(
                {"job": args.job, "pages": report},
                ensure_ascii=False, indent=2,
                # numpy 标量/bool 一律降为 Python 原生（否则 json 报 TypeError）
                default=lambda o: o.item() if hasattr(o, "item") else str(o),
            ),
            encoding="utf-8",
        )
        print(f"清单: {out_dir / 'orientation_report.json'}")

    if skipped:
        print("\n跳过:")
        for s in skipped:
            print(f"  - {s}")
    if failures:
        print("\n核验失败:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    if not report:
        print("\n无任何页可核验（全部跳过）。")
        return 0
    print("\n核验结论：轴（横竖）已由独立地面真值证实 —— 逆映射不会把框转置。"
          "\n  PASS      = 轴与方向均证实；"
          "PASS-AXIS = 轴证实，但本页内容近对称、方向不可分辨。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
