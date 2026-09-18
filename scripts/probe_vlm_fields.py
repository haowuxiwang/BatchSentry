"""VLM 读取能力定点探测：只问上一轮已被 OCR 读错的 5 个字段。

背景：2026-09-17 的审查结论是「精确性瓶颈在 OCR 层，不在规则层」——
规则假阳性均由 OCR 系统性误读放大而来。因此 v1.1.9 提精确性的第一
个待验假设是「换 VLM 直读像素能否读对」。

本脚本不是产品代码（同 compare_vlm_reader.py，属研究工具）。它只做
一件事：把 test1.jpg 交给候选模型，问与 OCR 同源的字段，与**人工视觉
真值**逐格比对。字段刻意收窄到已知误读点，这样单次调用便宜、结论锐利。

真值来自 agent 直读 test1.jpg（2026-09-18），见 GROUND_TRUTH。

用法：
    python scripts/probe_vlm_fields.py
    python scripts/probe_vlm_fields.py --model Qwen/Qwen3-VL-32B-Instruct
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

def _config_path() -> Path:
    """定位运行时真值配置。APPDATA 在 Git Bash 下未必导出，故留多路回退。"""
    for env_key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(env_key)
        if not base:
            continue
        for sub in ("PBC", "AppData/Roaming/PBC"):
            p = Path(base) / sub / "config.json"
            if p.exists():
                return p
    return REPO_ROOT / "config.json"


CFG = _config_path()

# 人工视觉真值（agent 直读 test1.jpg）。左为字段语义，右为真值。
GROUND_TRUTH = {
    "时间_序号1": "19:55",
    "时间_序号2": "21:50",
    "时间_序号5": "04:10",
    "时间_序号6": "06:12",
    "温度_序号1": "50",
    "温度_序号2": "52",
    "温度_序号3": "54",
    "温度_序号4": "54",
    "真空度_序号4": "-0.088",
    "尿苷投料量": "30.0",
    "环尿苷重量": "21.5",
    "质量收率": "71.67",
    "操作者": "眭东鹏",
}

PROMPT = """这是 GMP 批生产记录的一页。请逐项读出下列字段的**实际值**（手写优先，印刷固定列除外）。

需要读出的字段：
1. 附表2 时间行：序号 1、2、5、6 各自的时间
2. 附表2 温度行：序号 1、2、3、4 各自的温度
3. 附表2 真空度行：序号 4 的真空度
4. 环尿苷收率计算公式中：尿苷投料量(kg)、环尿苷重量(kg)
5. 质量收率(%)（记录数据列）
6. 操作者姓名

严格要求：
- 只输出 JSON，不要 Markdown 代码块，不要解释
- 看不清的字段值填空字符串，**不要猜测或补全**
- 数字逐位照抄，不要做任何单位换算或补零

输出格式：
{"时间":{"1":"","2":"","5":"","6":""},
 "温度":{"1":"","2":"","3":"","4":""},
 "真空度":{"4":""},
 "尿苷投料量":"","环尿苷重量":"","质量收率":"",
 "操作者":""}"""


def ask(model: str, image: Path, base_url: str, key: str, timeout: float) -> dict | None:
    url = "data:image/jpeg;base64," + base64.b64encode(image.read_bytes()).decode()
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": PROMPT},
            ]},
        ],
        "max_tokens": 2000,
        "temperature": 0,
    }
    print(f"[vlm] {model} ...", flush=True)
    t0 = time.time()
    try:
        r = requests.post(f"{base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        print(f"[vlm] {model} EXC {type(e).__name__}: {e}")
        return None
    dt = time.time() - t0
    if r.status_code != 200:
        print(f"[vlm] {model} HTTP {r.status_code} in {dt:.1f}s: {r.text[:200]}")
        return None
    body = r.json()
    txt = body["choices"][0]["message"]["content"].strip()
    print(f"[vlm] {model} 200 in {dt:.1f}s usage={body.get('usage')}")
    return {"text": txt, "dt": dt}


def norm(v: str) -> str:
    """比对口径：剥单位/空格后再比。

    模型答 "30.0kg"、真值 "30.0" —— 数值本身没错，错的是比对函数。
    若按字面比对会把它记成 WRONG，从而**低估 VLM 能力**、把结论引向错误
    方向（这正是本项目在 OCR 误读上踩过的同类坑：口径错 → 结论错）。
    """
    s = str(v).strip().replace(" ", "").replace("：", ":")
    for unit in ("kg", "KG", "Kg", "g", "%", "℃", "MPa", "m³/h"):
        if s.endswith(unit):
            s = s[: -len(unit)]
    # -0.088 / -.088 / 0.088 视为同一量（符号与省略前导零）
    return s


def flatten(d: dict) -> dict:
    out = {}
    for k, v in (d.get("时间") or {}).items():
        out[f"时间_序号{k}"] = v
    for k, v in (d.get("温度") or {}).items():
        out[f"温度_序号{k}"] = v
    for k, v in (d.get("真空度") or {}).items():
        out[f"真空度_序号{k}"] = v
    for k in ("尿苷投料量", "环尿苷重量", "质量收率", "操作者"):
        out[k] = d.get(k, "")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    ap.add_argument("--image", default=str(Path(tmp) / "test1_fit.jpg"))
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--repeat", type=int, default=1,
                    help="同一模型重复调用次数（VLM 跨次不稳定，需测重现性）")
    args = ap.parse_args()

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    base_url = cfg.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    key = cfg.get("SILICONFLOW_API_KEY", "")
    if not key:
        print("no SILICONFLOW_API_KEY in", CFG)
        return 2

    image = Path(args.image)
    if not image.exists():
        print("image missing:", image)
        return 2
    models = args.model or [
        "Qwen/Qwen3-VL-32B-Instruct",
        "zai-org/GLM-4.5V",
    ]

    summary = {}
    for m in models:
        runs = []
        for i in range(max(1, args.repeat)):
            got = ask(m, image, base_url, key, args.timeout)
            if not got:
                runs.append(None)
                continue
            txt = got["text"]
            txt = txt.replace("```json", "").replace("```", "").strip()
            try:
                parsed = json.loads(txt)
            except json.JSONDecodeError:
                print(f"[vlm] {m} JSON 解析失败，原始输出前 300 字：\n{txt[:300]}")
                runs.append({"parse_error": True, "raw": txt[:500]})
                continue
            flat = flatten(parsed)
            hits = wrong = miss = 0
            rows = []
            for field, truth in GROUND_TRUTH.items():
                got_v = str(flat.get(field, "")).strip()
                if not got_v:
                    verdict, miss = "MISS", miss + 1
                elif norm(got_v) == norm(truth):
                    verdict, hits = "OK  ", hits + 1
                else:
                    verdict, wrong = "WRONG", wrong + 1
                rows.append((verdict, field, truth, got_v))
            tag = f"  第 {i+1}/{args.repeat} 次" if args.repeat > 1 else ""
            print(f"\n=== {m}{tag}  正确 {hits} / 错误 {wrong} / 未答 {miss} ===")
            for verdict, field, truth, got_v in rows:
                mark = " " if verdict == "OK  " else "!"
                print(f" {mark} {verdict} {field:<16} 真值={truth:<10} 读出={got_v}")
            runs.append({"hits": hits, "wrong": wrong, "miss": miss,
                         "dt": round(got["dt"], 1), "text": txt})
        if args.repeat > 1:
            ok = [r for r in runs if r and "hits" in r]
            if ok:
                print(f"[summary] {m}: 各次正确数 {[r['hits'] for r in ok]} "
                      f"（满分 {len(GROUND_TRUTH)}）")
        summary[m] = runs[0] if args.repeat == 1 else runs

    out = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "vlm_probe_result.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[done]", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
