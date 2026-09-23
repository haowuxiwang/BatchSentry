"""性能回归**判据**（B11-5）—— 只做**相对**判定，绝不设绝对 SLA。

为什么不能设绝对阈值
--------------------
"上传吞吐 ≥ 300 MB/s" 这种绝对阈值在本项目里必然变成**恒真或恒假**的判据：
同一份产物在开发机、CI、用户的机械盘上差好几倍；阈值定低了永远绿（无判别力），
定高了永远红（被无视）。两者都等于没有判据。

⇒ 判据设计成 **相对**：`本次正常路径吞吐 / 参考基线 ≥ min_ratio`。
参考基线是**同一台机器、同一形态**下此前记录的数（见 `record_baseline`），
因此磁盘/CPU 差异在分子分母里**同时出现、被约掉**。

三态（与门禁同风格，缺一不可）
------------------------------
- `PASS`          —— 相对比值达标
- `FAIL`          —— 相对比值跌破阈值（**具名**给出比值与两侧数值）
- `UNJUDGEABLE`   —— 取不到任一侧数值 / 基线缺失 / 数值非法

⚠️ `UNJUDGEABLE` **不得**被读成 `PASS`：那正是本项目反复踩的
"没测过" == "没问题"。调用方必须显式处理第三种状态（例如打 WARN 或 SKIP）。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median

MIN_RATIO_DEFAULT = 0.8

# 浮点容差。⚠️ 实测教训：`(80*0.001)/(100*0.001) == 0.7999999999999999`，而
# `80/100 == 0.8` —— 同一个比值仅因**缩放就先经过一次舍入**而落到阈值下方，
# 于是"恰好达标"被判成 FAIL。吞吐比值的有效位只有 2~3 位，1 ULP 的差没有任何
# 业务含义，不该决定结论。容差取 1e-9（远小于任何真实测量噪声），
# 因此**不可能**掩盖真实回归（0.799 仍然会红）。
_TOL = 1e-9

PASS = "PASS"
FAIL = "FAIL"
UNJUDGEABLE = "UNJUDGEABLE"


@dataclass(frozen=True)
class Verdict:
    status: str          # PASS / FAIL / UNJUDGEABLE
    detail: str
    ratio: float | None = None


def _usable(v) -> bool:
    """数值可用 = 是数字、有限、且 > 0（吞吐为 0 或负数说明测量本身坏了）。"""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v > 0)


def judge_throughput(normal_mbps, reference_mbps,
                     min_ratio: float = MIN_RATIO_DEFAULT) -> Verdict:
    """判定"正常上传路径"的吞吐是否相对参考基线显著退化。

    边界语义：`ratio == min_ratio` 记 **PASS**（`>=`）—— 阈值是"不低于"。
    """
    if not _usable(normal_mbps) or not _usable(reference_mbps):
        return Verdict(
            UNJUDGEABLE,
            f"数值不可用（本次={normal_mbps!r}，参考={reference_mbps!r}）"
            "⇒ 判不了，**不得记为 PASS**")
    ratio = normal_mbps / reference_mbps
    if ratio >= min_ratio * (1 - _TOL):
        return Verdict(
            PASS,
            f"{normal_mbps:.1f} / {reference_mbps:.1f} = {ratio:.2f}×"
            f" ≥ {min_ratio:.2f}×", ratio)
    return Verdict(
        FAIL,
        f"{normal_mbps:.1f} / {reference_mbps:.1f} = {ratio:.2f}×"
        f" **跌破** {min_ratio:.2f}×", ratio)


def summarize_series(samples: list[float]) -> float | None:
    """把一串重复测量压成一个代表值：取**中位数**（抗单点抖动/GC 暂停）。

    返回 None 表示"一个可用样本都没有"（调用方须走 UNJUDGEABLE）。
    """
    good = [s for s in samples if _usable(s)]
    if not good:
        return None
    return float(median(good))


def load_reference(path: Path) -> dict[str, float] | None:
    """读参考基线文件。缺失/损坏/结构不对 ⇒ **None**（fail-closed，不猜）。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ref = (doc or {}).get("reference_mbps")
    if not isinstance(ref, dict):
        return None
    out = {k: float(v) for k, v in ref.items() if _usable(v)}
    return out or None


def record_baseline(path: Path, reference_mbps: dict[str, float],
                    *, note: str = "") -> None:
    """写入/覆盖参考基线。**只在人工确认这次测量是健康的之后**才该调用。"""
    doc = {
        "schema": 1,
        "note": note or "正常上传路径吞吐的参考基线（同机同形态相对比较用）",
        "reference_mbps": {k: float(v) for k, v in reference_mbps.items()},
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def judge_series(normal_samples, reference: dict[str, float], key: str,
                 min_ratio: float = MIN_RATIO_DEFAULT) -> Verdict:
    """便捷入口：把一串本次样本与基线里某个 key 比较。"""
    if not isinstance(reference, dict) or key not in reference:
        return Verdict(UNJUDGEABLE, f"基线里没有 {key!r} 这一项 ⇒ 判不了")
    return judge_throughput(summarize_series(list(normal_samples)),
                            reference.get(key), min_ratio)
