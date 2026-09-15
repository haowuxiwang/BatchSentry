"""Research tool: compare a natively-multimodal model against the OCR+LLM chain.

**Not part of the application.** This exists so the "should we replace
OCR+LLM with a VLM?" decision is reproducible instead of resting on vendor
benchmarks. It renders a page of a real PDF and asks a multimodal model for
the *same* structured schema the pipeline asks its OCR→text-LLM chain for,
then diffs the two.

Why render from the PDF rather than reuse `page_cache.raw_html`:
the point is to measure the *document-native* path (pixels → structure), so
the model must see the same pixels a human reviewer sees. Embedded rasters are
preferred over vector re-render (`extract_image`) because it avoids
`/Rotate` and re-sampling differences — see docs/M8_VISUAL_VERIFICATION.md.

Usage (needs a provider key; reads %APPDATA%/PBC/config.json by default):

    python scripts/compare_vlm_reader.py --pdf 丝裂霉素提取批记录.pdf --page 9
    python scripts/compare_vlm_reader.py --pdf x.pdf --page 9 \
        --model zai-org/GLM-4.5V --model Qwen/Qwen3-VL-32B-Instruct
    # compare against a finished job's stored result:
    python scripts/compare_vlm_reader.py --pdf x.pdf --page 9 \
        --job-id 95a27d88-52b --db %TEMP%/pbc_e2e_appdata/PBC/data.db

Cost note: one 3000x4000 page at ~2200px costs ~10-13k tokens and 150-160s on
the models measured 2026-09. Budget accordingly before sweeping all pages.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ── 与 core/page_analyzer.PROMPTS["v4"] 同源，仅把「HTML 表格（OCR 产物）」
#    换成「页面图像」—— 这是让两条路线可比的最小改动。schema 刻意保持一致，
#    否则差异会来自提示词而非读取方式。
SYSTEM = """你是一个 GMP 批生产记录数据提取专家。
给定一页批生产记录的页面图像，请提取结构化数据。

## 核心原则（半自动定位）
- 能判的合规异常直接产 finding（结构化），不要塞 ocr_noise 文本字段
- 不能判的标注低置信度，留给人工复核
- 优先低误报：漏报可由人工兜底，误报浪费人工时间

## 列级可信度（value_source 必填）
每个 parameters[].value 与 measurements[].values{}.actual 必须同时输出
value_source 字段，取值为 "printed"（印刷固定列：规格范围/操作指导/标准）|
"handwritten"（手写录入列：实际值/实测/记录数据/签名/日期/复核意见）|
"unknown"。

## 矩阵示例
表格多行时间 × N 设备 × M 指标，应抽成：
measurements: [{"time":"11:04","values":{"设备A_流速":{"spec":"0.5-1.0","actual":"0.974","unit":"m³/h","in_spec":true,"value_source":"handwritten"}}}]
列名格式 "{设备编号}_{指标}"，编号和指标名从实际表格中提取，不要臆造。

## 关键约束
- 数值一律以图像中的实际笔迹为准；看不清的单元格 actual 留空、in_spec 置 null，
  不要编造合理值
- 严格输出 JSON，不要 Markdown 代码包裹"""

USER_TMPL = """这是本批记录的第 {page} 页（PDF 物理页码，1-indexed）。当前日期 {today}。
findings[].page 字段必须等于该页码，不得填写其他页码或留空。

输出 JSON 格式：
{{"page_info":{{"title":"","file_code":"","version":"","batch_no":"","production_date":""}},
 "steps":[{{"step_no":"","operation":"","start_time":"","end_time":"",
   "parameters":[{{"name":"","spec_range":"","value":"","unit":"","in_spec":true,"value_source":"printed|handwritten|unknown"}}],
   "measurements":[{{"time":"","values":{{"列名":{{"spec":"","actual":"","unit":"","in_spec":true,"value_source":"printed|handwritten|unknown"}}}}}}],
   "operator":"","reviewer":"",
   "signatures":[{{"role":"operator","name":"","sign_time":"","confidence":"high"}}]}}],
 "findings":[{{"page":{page},"type":"time_reversal|year_contradiction|signature_time_anomaly|suspicious_date|param_out_of_spec|completeness","severity":"critical|warning|info","description":"","ocr_text":""}}],
 "overall_confidence":"high|medium|low"}}"""


def _config() -> dict:
    """Read the frozen app's config (same source the server uses)."""
    if os.name == "nt":
        p = Path(os.environ.get("APPDATA", "")) / "PBC" / "config.json"
    else:
        p = Path.home() / ".local" / "share" / "PBC" / "config.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    local = REPO_ROOT / "config.json"
    return json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}


def render_page(pdf: Path, page_no: int, out_dir: Path, max_side: int) -> Path:
    """Extract the page's embedded raster (fallback: vector render), then fit."""
    import fitz
    from PIL import Image

    doc = fitz.open(str(pdf))
    try:
        if not 1 <= page_no <= doc.page_count:
            raise SystemExit(f"页码 {page_no} 超出范围（该 PDF 共 {doc.page_count} 页）")
        pg = doc[page_no - 1]
        out_dir.mkdir(parents=True, exist_ok=True)
        imgs = pg.get_images(full=True)
        if imgs:
            info = doc.extract_image(imgs[0][0])
            raw_path = out_dir / f"page{page_no}_embed.{info['ext']}"
            raw_path.write_bytes(info["image"])
        else:
            pix = pg.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            raw_path = out_dir / f"page{page_no}_render.png"
            pix.save(str(raw_path))
    finally:
        doc.close()

    im = Image.open(raw_path).convert("RGB")
    w, h = im.size
    scale = max_side / max(w, h)
    if scale < 1:
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    fitted = out_dir / f"page{page_no}_fit{max_side}.jpg"
    im.save(fitted, "JPEG", quality=88)
    print(f"[render] {raw_path.name} {w}x{h} -> {fitted.name} {im.size} "
          f"{fitted.stat().st_size/1e6:.2f}MB")
    return fitted


def ask_vlm(model: str, image: Path, page_no: int, cfg: dict,
            base_url: str, key: str, timeout: float) -> dict | None:
    from datetime import date

    url = ("data:image/jpeg;base64,"
           + base64.b64encode(image.read_bytes()).decode())
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text",
                 "text": USER_TMPL.format(page=page_no, today=date.today().isoformat())},
            ]},
        ],
        "max_tokens": 8000,
        "temperature": 0,
    }
    print(f"[vlm] {model} <- {image.name} ...", flush=True)
    t0 = time.time()
    r = requests.post(f"{base_url}/chat/completions",
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    dt = time.time() - t0
    if r.status_code != 200:
        print(f"[vlm] {model} HTTP {r.status_code} in {dt:.1f}s: {r.text[:300]}")
        return None
    body = r.json()
    choice = body["choices"][0]
    print(f"[vlm] {model} HTTP 200 in {dt:.1f}s "
          f"finish_reason={choice.get('finish_reason')} usage={body.get('usage')}")
    text = choice["message"]["content"].strip()
    tag = re.sub(r"[^A-Za-z0-9]+", "_", model)
    # 原始输出一律落盘：VLM 跨次不稳定（同页同提示，实测一次给 72 格合法
    # JSON、一次给截断 JSON），不留原始输出就无法事后判定是"读不出"还是
    # "输出被截断" —— 而这两者对决策的含义完全不同。
    raw_file = image.parent / f"page{page_no}_{tag}.raw.txt"
    raw_file.write_text(text, encoding="utf-8")
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        print(f"[vlm] 未能提取 JSON；原始输出见 {raw_file}")
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        # 截断是最常见的失败形态（输出 token 用尽）。与项目内
        # llm/client.py::_repair_truncated_json 同因 —— VLM 路线同样需要
        # 这套 JSON 容错链，否则解析失败率直接暴露给用户。
        print(f"[vlm] JSON 解析失败: {e}")
        print(f"[vlm] 原始输出见 {raw_file}"
              f"（finish_reason={choice.get('finish_reason')}；"
              f"若为 length 即输出被截断，需提高 max_tokens）")
        return None


def load_pipeline_result(db: Path, job_id: str, page_no: int) -> dict | None:
    if not db.exists():
        return None
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT structured_json FROM page_cache WHERE job_id=? AND page=?",
            (job_id, page_no)).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row and row[0] else None


def matrix_rows(data: dict) -> dict:
    """Flatten to {time: {metric: actual}} for a readable side-by-side diff."""
    out = {}
    for step in data.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for m in step.get("measurements") or []:
            if not isinstance(m, dict):
                continue
            cells = m.get("values") or {}
            key = str(m.get("time") or f"step{step.get('step_no')}")
            out.setdefault(key, {})
            for col, cell in cells.items():
                tag = str(col).split("_")[-1]
                out[key][tag] = (cell or {}).get("actual")
    return out


def diff(pipeline: dict, vlm: dict) -> tuple[int, int, int]:
    a, b = matrix_rows(pipeline), matrix_rows(vlm)
    keys = sorted(set(a) | set(b))
    same = diff_n = only_a = 0
    for k in keys:
        ca, cb = a.get(k, {}), b.get(k, {})
        for c in sorted(set(ca) | set(cb)):
            va, vb = ca.get(c), cb.get(c)
            if va == vb:
                same += 1
            elif c not in ca:
                diff_n += 1
            elif c not in cb:
                diff_n += 1
            else:
                only_a += 1
    return same, diff_n, only_a


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pdf", required=True, help="源 PDF 路径")
    ap.add_argument("--page", type=int, required=True, help="PDF 物理页码（1-indexed）")
    ap.add_argument("--model", action="append", default=[],
                    help="可重复；默认 glm45v + qwen3vl32b")
    ap.add_argument("--base-url", default=None, help="OpenAI 兼容端点；默认取配置")
    ap.add_argument("--api-key-env", default="PBC_E2E_SILICONFLOW_KEY")
    ap.add_argument("--max-side", type=int, default=2200, help="图像最长边像素")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--job-id", default=None, help="与已跑完任务的第 N 页结果对比")
    ap.add_argument("--db", default=None, help="该任务所在的 SQLite（默认隔离库）")
    args = ap.parse_args()

    cfg = _config()
    base_url = args.base_url or cfg.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    key = os.environ.get(args.api_key_env) or cfg.get("SILICONFLOW_API_KEY", "")
    if not key:
        raise SystemExit(
            f"缺少 API key：设环境变量 {args.api_key_env}，或在 %APPDATA%/PBC/config.json "
            f"里配置 SILICONFLOW_API_KEY")

    models = args.model or ["zai-org/GLM-4.5V", "Qwen/Qwen3-VL-32B-Instruct"]
    out_dir = Path(args.out_dir) if args.out_dir else Path(
        os.environ.get("TEMP", "/tmp")) / "pbc_vlm_compare"

    image = render_page(Path(args.pdf), args.page, out_dir, args.max_side)

    results = {}
    for model in models:
        got = ask_vlm(model, image, args.page, cfg, base_url, key, args.timeout)
        if got:
            tag = re.sub(r"[^A-Za-z0-9]+", "_", model)
            f = out_dir / f"page{args.page}_{tag}.json"
            f.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
            results[model] = got
            print(f"[out] {f}")

    if args.job_id:
        db = Path(args.db) if args.db else Path(
            os.environ.get("TEMP", "/tmp")) / "pbc_e2e_appdata" / "PBC" / "data.db"
        pipeline = load_pipeline_result(db, args.job_id, args.page)
        if pipeline is None:
            print(f"[cmp] 未在 {db} 找到 job={args.job_id} page={args.page}")
        else:
            results = {"<pipeline OCR+LLM>": pipeline, **results}

    if len(results) > 1:
        print("\n=== 单元格逐格一致性 ===")
        names = list(results)
        base = names[0]
        for other in names[1:]:
            same, diff_n, only_a = diff(results[base], results[other])
            total = same + diff_n + only_a
            pct = (same * 100 // total) if total else 0
            print(f"  {base}  vs  {other}: 一致 {same}/{total} ({pct}%)，"
                  f"单侧缺失 {diff_n}，值不同 {only_a}")
        print("\n=== 逐行对照 ===")
        flat = {n: matrix_rows(r) for n, r in results.items()}
        keys = sorted(set().union(*[set(f) for f in flat.values()])) if flat else []
        for k in keys:
            print(f"  {k}:")
            for n, f in flat.items():
                row = f.get(k, {})
                print(f"    {n:24} " + " ".join(f"{c}={v}" for c, v in row.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
