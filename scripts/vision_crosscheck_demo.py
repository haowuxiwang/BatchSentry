"""#159 验收脚本：对 `test1.jpg` 跑一次**真实**的定向视觉互证。

产出 #159 的验收条款所要求的东西：

    「温度行若干单元格 OCR 与视觉不一致」的**显式输出**

它走的是**产品代码路径**（`core.vision_crosscheck.crosscheck_cells` →
`llm/client.py:chat_json` → `llm/adapters/openai_adapter`），不是另写一套 HTTP
调用 —— 只有这样才能同时验证 adapter 的多模态签名确实打通了。

配置解析沿用 `scripts/probe_vlm_fields.py` 的多路回退（Git Bash 下 `%APPDATA%`
未必导出）。VLM 的 model 名通过**临时注册一个 provider** 覆盖（`config` 是
dict），因此仍然走真实的 `LLMClient.__init__` + `get_adapter`。

用法：
    python scripts/vision_crosscheck_demo.py
    python scripts/vision_crosscheck_demo.py --model Qwen/Qwen3-VL-32B-Instruct
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config import ProviderConfig, config  # noqa: E402
from core.vision_crosscheck import Cell, crosscheck_cells  # noqa: E402
from llm.client import LLMClient  # noqa: E402

# 上一轮（2026-09-18）由 agent 直读 test1.jpg 得到的人工视觉真值，
# **仅用于事后对照**（说明互证是否指向了正确的位置），不参与判定。
HUMAN_TRUTH = {
    "温度_序号1": "50", "温度_序号2": "52",
    "温度_序号3": "54", "温度_序号4": "54",
}

# OCR 链路当时读出的值（F7 实测：温标 50-60，读成 32/34/34/35/36
# ⇒ R3 会放大出 5 条**假阳性超差**）。序号 1 是对的，用来当反例。
OCR_VALUES = {
    "温度_序号1": "50", "温度_序号2": "32", "温度_序号3": "34",
    "温度_序号4": "34", "温度_序号5": "35", "温度_序号6": "36",
}


def _config_path() -> Path:
    for env_key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(env_key)
        if not base:
            continue
        for sub in ("PBC", "AppData/Roaming/PBC"):
            p = Path(base) / sub / "config.json"
            if p.exists():
                return p
    return REPO_ROOT / "config.json"


def _default_image() -> Path:
    tmp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    scaled = tmp / "test1_fit.jpg"       # 上一轮探测留下的缩放件
    return scaled if scaled.exists() else REPO_ROOT / "test1.jpg"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=str(_default_image()))
    ap.add_argument("--model", default="Qwen/Qwen3-VL-32B-Instruct")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    cfg_path = _config_path()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    key = cfg.get("SILICONFLOW_API_KEY", "")
    base_url = cfg.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    if not key:
        print("no SILICONFLOW_API_KEY in", cfg_path)
        return 2

    image = Path(args.image)
    if not image.exists():
        print("image missing:", image)
        return 2

    # 临时注册一个 VLM provider（不落盘），仍走真实构造路径。
    name = "_vision_crosscheck_probe"
    config["providers"][name] = ProviderConfig(
        name=name, protocol="openai", api_key=key,
        base_url=base_url, model=args.model,
    )
    client = LLMClient(name)

    cells = [
        Cell(key=k, kind="number", ocr_value=v,
             context=f"附表2 干燥温度行 序号{k.split('_')[-1]}（规格 50-60 ℃）")
        for k, v in OCR_VALUES.items()
    ]

    print(f"[xcheck] image={image} ({image.stat().st_size} bytes)")
    print(f"[xcheck] model={args.model}")
    t0 = time.time()
    result = None
    try:
        import asyncio
        result = asyncio.run(crosscheck_cells(
            client, image.read_bytes(), cells,
            media_type="image/jpeg", timeout=args.timeout,
        ))
    except Exception as e:  # noqa: BLE001 —— 脚本要如实报错，不是静默
        print(f"[xcheck] FAILED: {type(e).__name__}: {e}")
        return 1
    dt = time.time() - t0

    print(f"\n=== 互证结果（{dt:.1f}s）===\n")
    print(result.format_report())

    if result.discrepancies:
        print(f"\n=== 不一致单元格（{len(result.discrepancies)} 个）"
              " ⇒ 待人工核对（不采信任何一方）===")
        for v in result.discrepancies:
            ref = HUMAN_TRUTH.get(v.key, "—")
            print(f"  {v.key:<14} OCR={v.ocr_value:<4} 视觉={v.visual_value:<4} "
                  f"人工真值(参考)={ref}")

    out = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    out = out / "vision_crosscheck_result.json"
    out.write_text(json.dumps({
        "model": args.model, "image": str(image), "elapsed_s": round(dt, 1),
        "verdicts": [v.__dict__ for v in result.verdicts],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[done]", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
