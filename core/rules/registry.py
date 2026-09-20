"""规则注册表（M4/T4.0）—— 规则元数据的单一来源。

背景
----
在引入本模块前，规则集合以"散落的调用序列 + 各处硬编码"形式存在于
`core/rules/__init__.py`：新增一条规则要同时改 import、调用、日志、依据映射
（gmp_basis）、检索词（TYPE_QUERIES）、中文名（zh_map）、前端映射——漏一处
就会"规则跑了但前端显示英文/无依据/不可检索"。

本模块把每条规则收敛为一个 `RuleSpec`（id / type / severity / basis /
description / check / 开关），`analyze_cross_page` 由注册表驱动执行，做到：
- **可审计**：`RULE_REGISTRY` 一眼看全"我们检查什么、归为什么类型、什么依据"；
- **可配置**：`enabled_rule_specs()` 支持按 id 关闭（config.json `rules.disabled`）；
- **不漂移**：依据（basis）取自 `gmp_basis.GMP_BASIS_MAP`（单一来源），
  并有测试断言"注册表里每个 type 都有依据"。

执行序即 `RULE_REGISTRY` 的顺序（与历史行为一致）；新增规则只需在此追加，
不必改分析器主体。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from core.rules.gmp_basis import GMP_BASIS_MAP
from core.rules.rule_doc import (
    _check_batch_consistency,
    _check_check_consistency,
    _check_completeness,
    _check_handwritten_notes,
    _check_low_confidence_params,
    _check_measurement_column_consistency,
    _check_measurement_time_sequence,
)
from core.rules.rule_gmp import (
    _check_alteration,
    _check_deviation_link,
    _check_doc_version,
    _check_env_monitor,
    _check_equipment_state,
    _check_mass_balance,
    _check_self_review,
)
from core.rules.rule_spec import _check_param_out_of_spec
from core.rules.rule_time import (
    _check_signature_order,
    _check_signature_time_anomaly,
    _check_step_number_gaps,
    _check_suspicious_dates,
    _check_time_reversal_cross_page,
    _check_time_reversal_in_page,
    _check_year_contradiction,
)

logger = logging.getLogger(__name__)


@dataclass
class RuleContext:
    """规则执行上下文 —— 跨规则共享的只读/累积状态。

    - ``llm_queue``：R3 收集的"规则判不了、交 LLM 兜底"参数（既有副作用，
      显式化为上下文而非隐式列表，避免规则间靠闭包传参）。
    - ``oos_pages``：被 R3 判为越界（param_out_of_spec）的页码集合；R16 据此
      判定"该页有超限 → 应关联偏差编号"。由 ``record()`` 累积。
    """
    job_id: str = ""
    llm_queue: list = field(default_factory=list)
    oos_pages: set[int] = field(default_factory=set)

    def record(self, findings: list[dict]) -> None:
        """累积跨规则信号（当前：越界页）。"""
        for f in findings:
            if not isinstance(f, dict):
                continue
            if f.get("type") == "param_out_of_spec":
                page = f.get("page")
                if isinstance(page, int):
                    self.oos_pages.add(page)


@dataclass(frozen=True)
class RuleSpec:
    """单条规则的元数据 + 执行体。

    - ``severity``：**期望严重度**（规则内部可依上下文动态降级，如手写体
      边缘超差降 info）；此处记录用于审计与前端分级。
    - ``basis``：GMP 依据（来自 GMP_BASIS_MAP，单一来源）。
    - ``check``：``(pages, ctx) -> list[dict]``，纯函数（无 IO/网络）。
    - ``enabled_by_default``：默认是否启用（可被 config.json 的
      ``rules.disabled`` 覆盖）。
    """
    id: str
    type: str
    severity: str
    description: str
    check: Callable[[list, RuleContext], list]
    enabled_by_default: bool = True

    @property
    def basis(self) -> str:
        return GMP_BASIS_MAP.get(self.type, "")


def _wrap(fn: Callable[[list], list]) -> Callable[[list, RuleContext], list]:
    """把只吃 pages 的既有规则适配成 (pages, ctx) 统一签名。"""
    return lambda pages, _ctx: fn(pages)


def _wrap_queue(fn: Callable[[list, list], list]) -> Callable[[list, RuleContext], list]:
    """适配需要 llm_queue 的 R3：写入 ctx.llm_queue（保持副作用可见）。"""
    return lambda pages, ctx: fn(pages, ctx.llm_queue)


# 执行顺序 = 历史顺序（R1a→…→R17）。规则无副作用依赖时顺序不影响结果；
# 唯一有依赖的是 R16（需 R3 先算出 oos_pages），故 R3 置于 R16 之前。
RULE_REGISTRY: tuple[RuleSpec, ...] = (
    RuleSpec("R1a", "time_reversal", "warning",
             "页内时间倒序（开始晚于结束）", _wrap(_check_time_reversal_in_page)),
    RuleSpec("R1b", "time_reversal", "warning",
             "跨页时间倒序", _wrap(_check_time_reversal_cross_page)),
    RuleSpec("R2", "year_contradiction", "warning",
             "年份矛盾（事件年份自相矛盾）", _wrap(_check_year_contradiction)),
    RuleSpec("R4", "suspicious_date", "warning",
             "可疑日期（未来/久远/失效）", _wrap(_check_suspicious_dates)),
    RuleSpec("R5", "signature_time_anomaly", "warning",
             "签名时间异常", _wrap(_check_signature_time_anomaly)),
    RuleSpec("R9a", "signature_time_anomaly", "warning",
             "签名顺序（复核应晚于操作）", _wrap(_check_signature_order)),
    RuleSpec("R10", "step_gap", "warning",
             "工序编号缺号（缺页/漏页）", _wrap(_check_step_number_gaps)),
    RuleSpec("R6", "completeness", "warning",
             "执行步骤签名/时间缺失", _wrap(_check_completeness)),
    RuleSpec("R7", "batch_inconsistency", "critical",
             "跨页批号不一致", _wrap(_check_batch_consistency)),
    RuleSpec("R8", "completeness", "info",
             "低置信度参数（OCR 存疑）", _wrap(_check_low_confidence_params)),
    RuleSpec("R9", "handwritten", "info",
             "手写内容需人工核对", _wrap(_check_handwritten_notes)),
    RuleSpec("R8b", "completeness", "warning",
             "检查项勾选为否/无法识别", _wrap(_check_check_consistency)),
    RuleSpec("R-M1", "time_reversal", "warning",
             "跨页测量时间序列倒序", _wrap(_check_measurement_time_sequence)),
    RuleSpec("R-M2", "completeness", "info",
             "跨页参数矩阵缺列", _wrap(_check_measurement_column_consistency)),
    RuleSpec("R3", "param_out_of_spec", "warning",
             "工艺参数超出规格范围", _wrap_queue(_check_param_out_of_spec)),
    # ── M4：R11–R17 ──
    RuleSpec("R11", "mass_balance", "warning",
             "物料平衡/收率可否核定", _wrap(_check_mass_balance)),
    RuleSpec("R12", "self_review", "critical",
             "操作人=复核人（自检自核）", _wrap(_check_self_review)),
    RuleSpec("R13", "equipment_state", "warning",
             "设备/清洁状态确认", _wrap(_check_equipment_state)),
    RuleSpec("R14", "env_monitor", "warning",
             "环境监测完备性", _wrap(_check_env_monitor)),
    RuleSpec("R15", "doc_version", "warning",
             "文件版本一致性", _wrap(_check_doc_version)),
    RuleSpec("R16", "deviation_link", "warning",
             "超限偏差关联",
             lambda pages, ctx: _check_deviation_link(pages, ctx.oos_pages)),
    RuleSpec("R17", "alteration", "warning",
             "涂改规范（划改留痕）", _wrap(_check_alteration)),
)


def rule_ids() -> list[str]:
    return [s.id for s in RULE_REGISTRY]


def disabled_rule_ids() -> set[str]:
    """读取被关闭的规则 id（config.json `rules.disabled`）。读取失败=不关闭。"""
    try:
        from config import load_disabled_rule_ids as _load
        return set(_load())
    except Exception:  # config 不可用不应阻断分析
        return set()


def enabled_rule_specs() -> list[RuleSpec]:
    """按注册顺序返回启用中的规则（已剔除被 config 关闭的 id）。"""
    disabled = disabled_rule_ids()
    return [
        s for s in RULE_REGISTRY
        if s.enabled_by_default and s.id not in disabled
    ]


def rule_coverage(type_counts: dict[str, int] | None = None) -> dict:
    """系统校验覆盖面（M6/T6.4 三色复核的"绿"）。

    "系统校验通过"必须在**同一可陈述单元**上与"命中"对齐，否则数字是编的。
    规则与 finding 之间历史上没有落库 rule_id（findings 表只有 type），且
    同一 type 可能由多条规则产出（completeness = R6/R8/R8b/R-M2），因此最小
    可陈述单元是 **type** 而不是 rule id；rule id 作为可追溯信息一并给出。

    Args:
        type_counts: {finding_type: 该 job 命中条数}，由调用方一次 GROUP BY
            取得（避免此处再查库，保持本模块无 IO）。

    Returns:
        {
          "rule_total": 启用规则条数,
          "type_total": 涉及的类型数,
          "fired": [ {type, count, rule_ids[..], description} ],   # 有命中
          "passed": [ {type, rule_ids[..], description} ],         # 0 命中
          "fired_count", "passed_count",
        }
    """
    counts = type_counts or {}
    specs = enabled_rule_specs()
    by_type: dict[str, list[RuleSpec]] = {}
    for s in specs:
        by_type.setdefault(s.type, []).append(s)

    fired: list[dict] = []
    passed: list[dict] = []
    for ftype, group in by_type.items():
        entry = {
            "type": ftype,
            "rule_ids": [s.id for s in group],
            "description": group[0].description,
            "basis": group[0].basis,
        }
        n = int(counts.get(ftype, 0))
        if n > 0:
            fired.append({**entry, "count": n})
        else:
            passed.append(entry)
    # 稳定输出：按类型名排序，便于前端展示与测试断言
    fired.sort(key=lambda e: e["type"])
    passed.sort(key=lambda e: e["type"])
    return {
        "rule_total": len(specs),
        "type_total": len(by_type),
        "fired": fired,
        "passed": passed,
        "fired_count": len(fired),
        "passed_count": len(passed),
    }
