"""定向视觉互证（#159 原型）—— **只对数值/时间单元格**，不做全页替代。

## 为什么是"定向"而不是"多模态替换"

`docs/VISUAL_CROSSCHECK.md` 已定案：多模态**不能当降噪手段**（第 9 页三方
72/72 逐格一致 ⇒ 替代零收益，而坐标/互证/成本净损约 4.2×）。本模块走的是
另一条路 —— **定向互证**：OCR 已经给出了抽取结果，只把**它最可能读错的那
几类单元格**拿去与像素核对，不一致就降级为"待人工核对"。

## 语义（三条硬约束，都不是我拍的，是实测得来的）

1. **不一致 ≠ 采信视觉**。处置是"降级为待人工核对"，不能直接采纳任一方的值。
   OCR 会读错，VLM 同样会读错（实测 13 个字段里最多读对 12 个）。
2. **中文姓名不得由视觉裁决**（Round 35 实测硬约束）：7 个模型给出
   `胖东鹏 / 胖东朋朋 / 胖乐鹏 / 薛东鹏 / 周小英`，**没有一个读对"眭"**。
   ⇒ 姓名类单元格只标 `must_verify`，永不出现 agree/disagree。
3. **成本随"问几个字段"走**，与页数无关 ⇒ 只问数值/时间，不整页重读。

## 状态（诚实的边界）

这是**原型**：**没有**接入 `core/pipeline`，不影响任何既有流程，也不写审计表。
接入产品需要的是「job 级触发条件 + 审计表 + 前端待核对视图」，见 `docs/TODO.md`
的 #159 / #160 —— 那是独立的一步。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from llm.adapters.base import ImagePart

logger = logging.getLogger(__name__)

# 只有这两类允许由视觉裁决（见模块 docstring 的硬约束 2）。
ADJUDICABLE_KINDS = ("number", "time")

# 单元格状态
AGREE = "agree"            # 两侧一致
DISAGREE = "disagree"      # 两侧不一致 ⇒ 待人工核对（**不采信任何一方**）
UNREADABLE = "unreadable"  # 模型未给出值（看不清/漏答）⇒ 同样待人工核对
MUST_VERIFY = "must_verify"  # 该类单元格不由视觉裁决（如中文姓名）

_NEED_HUMAN = (DISAGREE, UNREADABLE, MUST_VERIFY)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_UNITS = ("kg", "KG", "Kg", "g", "%", "℃", "MPa", "m³/h", "ml", "mL", "L")


@dataclass(frozen=True)
class Cell:
    """一个待互证的单元格。

    key:     稳定标识（页内唯一，也是模型回答里的回填键）
    kind:    "number" | "time" | "text"
    ocr_value: 规则/抽取链路已有的值
    context: 人可读的定位说明（表名 + 行名 + 序号）。视觉模型靠它找位置 ——
             实测表明给"附表2 温度行 序号3"比给坐标稳得多。
    """
    key: str
    kind: str
    ocr_value: str
    context: str = ""


@dataclass
class CellVerdict:
    key: str
    kind: str
    ocr_value: str
    visual_value: str
    status: str
    reason: str = ""

    @property
    def needs_human(self) -> bool:
        return self.status in _NEED_HUMAN


@dataclass
class CrossCheckResult:
    verdicts: list[CellVerdict] = field(default_factory=list)
    model: str = ""
    raw_response: str = ""

    @property
    def discrepancies(self) -> list[CellVerdict]:
        """**只**含"两侧读到不同值"的单元格（不含未答/不裁决）。"""
        return [v for v in self.verdicts if v.status == DISAGREE]

    @property
    def needs_human(self) -> list[CellVerdict]:
        return [v for v in self.verdicts if v.needs_human]

    def format_report(self) -> str:
        if not self.verdicts:
            return "（无可互证的单元格）"
        lines = []
        for v in self.verdicts:
            mark = {
                AGREE: "  ",
                DISAGREE: "!!",
                UNREADABLE: " ?",
                MUST_VERIFY: " 。",
            }.get(v.status, " ?")
            lines.append(
                f"{mark} [{v.status:<10}] {v.key:<16} "
                f"OCR={v.ocr_value!r:<12} 视觉={v.visual_value!r:<12} {v.reason}"
            )
        n_ok = sum(1 for v in self.verdicts if v.status == AGREE)
        lines.append(
            f"—— 一致 {n_ok} / 不一致 {len(self.discrepancies)} / "
            f"待人工核对 {len(self.needs_human)} （共 {len(self.verdicts)}）"
        )
        return "\n".join(lines)


# ── 比对口径（单一真值；研究脚本也复用这里）────────────────────────────


def _strip_units(s: str) -> str:
    s = s.strip().replace(" ", "").replace("：", ":")
    for unit in _UNITS:
        if s.endswith(unit):
            s = s[: -len(unit)]
    return s


def _same_number(a: str, b: str) -> bool:
    """数值等价：能解析成数就按**数值**比，否则退回字符串比。

    按字符串比会把 `30` 与 `30.0` 记成不一致 —— 口径错会导致结论错
    （本项目在 OCR 误读上踩过的同类坑）。但**符号必须敏感**：
    `-0.088` 与 `0.088` 是两个值。
    """
    x, y = _strip_units(a), _strip_units(b)
    try:
        return float(x) == float(y)
    except (TypeError, ValueError):
        return x == y


def _same_time(a: str, b: str) -> bool:
    """时间等价：`2:03` 与 `02:03` 是同一时刻（补零差异不算不一致）。"""
    x, y = _strip_units(a), _strip_units(b)
    mx, my = _TIME_RE.match(x), _TIME_RE.match(y)
    if mx and my:
        return (int(mx.group(1)), int(mx.group(2))) == (
            int(my.group(1)), int(my.group(2))
        )
    return x == y


def values_agree(kind: str, ocr_value: str, visual_value: str) -> bool:
    """两侧是否一致（按 kind 分派口径）。"""
    if kind == "time":
        return _same_time(ocr_value, visual_value)
    if kind == "number":
        return _same_number(ocr_value, visual_value)
    return _strip_units(ocr_value) == _strip_units(visual_value)


# ── 提示词 ────────────────────────────────────────────────────────────

PROMPT_HEADER = (
    "这是 GMP 批生产记录的一页扫描件。请**只**读出下面列出的单元格的实际值"
    "（手写优先）。\n"
    "严格要求：\n"
    "- 只输出 JSON 对象，不要 Markdown 代码块，不要任何解释\n"
    "- **看不清就填空字符串，绝对不要猜测或补全**\n"
    "- 数字逐位照抄，不要换算单位、不要补前导零\n"
    "- 时间按 `时:分` 原样输出\n"
)


def build_prompt(cells: list[Cell]) -> str:
    lines = [PROMPT_HEADER, "需要读出的单元格："]
    for c in cells:
        who = {"number": "数值", "time": "时间", "text": "文字"}.get(c.kind, c.kind)
        lines.append(f"- {c.key}（{who}）：{c.context or c.key}")
    keys = ", ".join(f'"{c.key}": ""' for c in cells)
    lines.append(f"\n输出格式（键名必须原样照抄）：{{{keys}}}")
    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────────────


def _stringify(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


async def crosscheck_cells(
    client,
    image: bytes,
    cells: list[Cell],
    media_type: str = "image/jpeg",
    max_tokens: int = 1500,
    timeout: float = 180.0,
    audit_ctx: dict | None = None,
) -> CrossCheckResult:
    """把 `cells` 交给视觉模型直读，与 OCR 值逐格比对。

    返回**逐格判定**，不返回"谁对"—— 不一致的处置是"待人工核对"。
    调用方须是 `LLMClient` 形状（`chat_json(system, user_content, ...)`）。
    """
    if not cells:
        return CrossCheckResult()

    prompt = build_prompt(cells)
    # 图像先于文本（沿用上一轮探测脚本验证过的排列），协议差异由 adapter 消化。
    parsed = await client.chat_json(
        "你是严谨的 GMP 记录核对助手，只报告你**确实看到**的内容。",
        [ImagePart(data=image, media_type=media_type), prompt],
        max_tokens=max_tokens,
        temperature=0.0,
        audit_ctx=audit_ctx,
    )
    if not isinstance(parsed, dict):
        logger.warning("vision crosscheck: 模型未返回 JSON 对象，判为全部待核对")
        parsed = {}

    result = CrossCheckResult(
        model=getattr(client, "model", ""),
        raw_response=json.dumps(parsed, ensure_ascii=False),
    )
    for c in cells:
        visual = _stringify(parsed.get(c.key, ""))
        if c.kind not in ADJUDICABLE_KINDS:
            # 硬约束 2：这类单元格不由视觉裁决（实测：中文姓名 7 个模型全读错）。
            result.verdicts.append(CellVerdict(
                key=c.key, kind=c.kind, ocr_value=c.ocr_value,
                visual_value=visual, status=MUST_VERIFY,
                reason="该类型不可由视觉自动裁决，须人工核对",
            ))
            continue
        if not visual:
            result.verdicts.append(CellVerdict(
                key=c.key, kind=c.kind, ocr_value=c.ocr_value,
                visual_value="", status=UNREADABLE,
                reason="视觉未读出（不得据此判定 OCR 正确）",
            ))
            continue
        if values_agree(c.kind, c.ocr_value, visual):
            result.verdicts.append(CellVerdict(
                key=c.key, kind=c.kind, ocr_value=c.ocr_value,
                visual_value=visual, status=AGREE,
            ))
        else:
            result.verdicts.append(CellVerdict(
                key=c.key, kind=c.kind, ocr_value=c.ocr_value,
                visual_value=visual, status=DISAGREE,
                reason="两侧读数不同 ⇒ 待人工核对（不采信任何一方）",
            ))
    return result


__all__ = [
    "ADJUDICABLE_KINDS",
    "AGREE", "DISAGREE", "UNREADABLE", "MUST_VERIFY",
    "Cell", "CellVerdict", "CrossCheckResult",
    "build_prompt", "crosscheck_cells", "values_agree",
]
