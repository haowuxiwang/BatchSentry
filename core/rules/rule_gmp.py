"""M4 —— R11–R17：GMP 运营合规规则（批记录高频、此前完全未检查的系统空洞）。

设计原则（与既有 rule_* 模块一致）
----------------------------------
1. **触发信号必须真实存在**：每条规则的触发键都经真实 51 页批记录
   （job 33495b33-761）逐字段核查后才落定，不做"理论上应该检查"的空规则。
   定位结论（原始证据见 docs/PLAN_v1.1_EXECUTION.md M4 与提交说明）：
   - 收率/物料平衡项以 `parameters[]` 承载（name=浓缩滤液收率/制备收率/
     SP-2收率/精制收率/提取总收率/PE袋物料平衡率，含 spec_range 与 value）；
   - 操作人/复核人以 `step.operator`/`step.reviewer` 与 `signatures[]`
     （role 混杂 operator/review/draft/reviewer）承载 → 1 处真实"自检自核"；
   - 设备/清洁确认项是 `parameters[]` 里的"是/否"题（如
     「设备及管路是否及时清洗干净」「状态标志使用是否正确」）；
   - 环境监测在洁净区页落为「压差是否符合要求」「洁净区层流区压差」确认项；
   - `page_info.file_code` 跨页**合法地各不相同**（R20/R22/R23… 是不同的
     表单），故**不能**做"全页同编号"判定；真实缺陷是"同一 file_code 出现
     两个版本"（R23→{R23,09}、R27→{11,10}）；
   - 异常/偏差以「生产有无异常」「偏差号」承载；
   - 涂改痕迹（涂改/划改/更正）在本记录为 0 → R17 规格驱动、真实数据不误报。
2. **保守触发、宁缺勿滥**：只报"可机检的硬信号"（已声明未填写 / 明确否 /
   版本冲突 / 同类人），模糊情形交给 LLM 语义支路。这不改变"不得静默标记
   成功"的原则——规则层沉默不等于该页合规，而是无规则级证据。
3. **不与 R3 重复**：收率/物料平衡若同时有 spec_range 与 value，由 R3 判
   越界；R11 只补 R3 判不到的"缺实测值 / 缺合格范围"。
"""
from __future__ import annotations

import logging
import re

from core.rules.parsing import _parse_spec

logger = logging.getLogger(__name__)


# ===========================================================================
# 通用工具
# ===========================================================================

# 人名归一：真实数据的操作人字段含 '#'（OCR 噪声）、空格、全角空格与标点，
# 直接比较会漏判"同一人"。归一后比较可把 "#王志清" 与 "王志清" 视为同人。
_NAME_NOISE_RE = re.compile(r"[\s\u3000.,，。;；:：'\"“”()（）/\\\-_#*]+")

# 是/否 选择题的取值范围（value 归一后）
_NEGATIVE_VALUES = frozenset({
    "否", "不", "不是", "不符合", "不合格", "不通过", "未通过", "未完成",
    "无", "没有", "异常", "有异常", "×", "x", "✗",
})

# 判定"该参数是选择题模板"的 spec 特征（是/否、√、□）
_CHOICE_SPEC_MARKS = ("是", "否", "√", "☑", "□", "✔")


def _norm_person(name) -> str:
    """归一操作人/复核人姓名以便同人判定；非字符串返回空串。"""
    if not isinstance(name, str):
        return ""
    return _NAME_NOISE_RE.sub("", name).strip()


def _norm_value(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_negative(value) -> bool:
    """value 是否表否定/异常（是/否题的"否"侧）。"""
    v = _norm_value(value).lower()
    if not v:
        return False
    if v in _NEGATIVE_VALUES:
        return True
    # "否"/"不合格"开头（如 "否（已处理）"）；"无异常"不视为否定。
    return v.startswith("否") or v.startswith("不合格") or v.startswith("不符合")


def _is_choice_spec(spec) -> bool:
    """spec 是否像"是/否"选择题模板（用于判"已声明但未填写"）。"""
    s = _norm_value(spec)
    return bool(s) and any(m in s for m in _CHOICE_SPEC_MARKS)


def _iter_params(page: dict):
    """产出 (step, param) —— 仅 dict 元素（_normalize_pages 已兜底，双保险）。"""
    for step in page.get("steps") or []:
        for p in step.get("parameters") or []:
            if isinstance(p, dict):
                yield step, p


def _iter_measurement_cols(page: dict):
    """产出 (step, col_name, cell) —— 参数矩阵列单元格。"""
    for step in page.get("steps") or []:
        for m in step.get("measurements") or []:
            if not isinstance(m, dict):
                continue
            for col, cell in (m.get("values") or {}).items():
                if isinstance(cell, dict):
                    yield step, str(col), cell


# ===========================================================================
# R11 —— 物料平衡/收率（mass_balance）
# ===========================================================================

_BALANCE_KEYWORDS = ("收率", "物料平衡", "平衡率", "得率", "回收率", "收得率")

# 工序文本里"声明"了收率/物料平衡要求（如「浓缩滤液收率应≥50%」）
_BALANCE_DECL_RE = re.compile(
    r"(收率|物料平衡|平衡率|得率|回收率)[^。；;\n]{0,16}?[≥≤<>]=?\s*\d"
)


def _check_mass_balance(pages: list[dict]) -> list[dict]:
    """R11：收率/物料平衡可否核定。

    与 R3 的分工：spec+value 同时存在 → R3 判越界，本规则不重复。本规则只补
    R3 覆盖不到的两类空洞：
      (a) 已声明合格范围（spec_range 可解析）但**未记录实测值** → warning；
      (b) 有实测值但**未声明合格范围** → info（无法判断是否合格）；
      (c) 工序文本声明收率/物料平衡要求，但该页**无任何**收率类参数 → warning
          （要求被声明却未见可核定的结构化实测值）。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        declared_in_text = False
        has_balance_param = False
        for step in page["steps"]:
            op = _norm_value(step.get("operation"))
            if op and _BALANCE_DECL_RE.search(op):
                declared_in_text = True
            step_no = step.get("step_no", "?")
            for p in step.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                name = _norm_value(p.get("name"))
                if not name or not any(k in name for k in _BALANCE_KEYWORDS):
                    continue
                has_balance_param = True
                value = _norm_value(p.get("value"))
                spec = _norm_value(p.get("spec_range"))
                bounds = _parse_spec(spec)
                if not value and bounds is not None:
                    findings.append({
                        "page": pno,
                        "type": "mass_balance",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} {name} 已声明合格范围 "
                            f"{spec} 但未记录实测值，无法核定物料平衡/收率是否合格"
                        ),
                        "ocr_text": f"{name}: spec={spec} value=空",
                        "operator": "",
                        "source": "rule",
                    })
                elif value and not spec:
                    # 仅在**确实未声明**合格范围时报；spec 存在但无法解析时
                    # 交给 R3 的 LLM 兜底（不得谎报"未声明"—— M4 实测发现
                    # "99%~101%" 曾被误报为未声明）。
                    findings.append({
                        "page": pno,
                        "type": "mass_balance",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} {name}={value} 未声明合格范围，"
                            f"无法核定物料平衡/收率是否合格，请确认规格来源"
                        ),
                        "ocr_text": f"{name}: value={value} spec=空",
                        "operator": "",
                        "source": "rule",
                    })
        if declared_in_text and not has_balance_param:
            findings.append({
                "page": pno,
                "type": "mass_balance",
                "severity": "warning",
                "description": (
                    f"第{pno}页 记录文本声明收率/物料平衡要求，但未见可核定的实测值"
                    f"（请确认收率/物料平衡率是否已记录并结构化提取）"
                ),
                "ocr_text": "operation 声明收率/物料平衡，无对应参数",
                "operator": "",
                "source": "rule",
            })
    return findings


# ===========================================================================
# R12 —— 操作人 ≠ 复核人（self_review）
# ===========================================================================

_OPERATOR_ROLES = frozenset({
    "operator", "操作人", "操作员", "操作者", "draft", "填表人", "记录人",
})
_REVIEWER_ROLES = frozenset({
    "reviewer", "review", "复核人", "复核员", "复核", "审核人", "审核",
    "workshop_reviewer", "supervisor", "qa", "qa_reviewer", "质量保证",
    "批准人", "放行人",
})


def _check_self_review(pages: list[dict]) -> list[dict]:
    """R12：操作人与复核人不得为同一人（ALCOA+ Attributable / 独立复核）。

    两个证据源合并判定：
      - `step.operator` vs `step.reviewer`（结构化字段）；
      - `signatures[]` 中 operator 角色集合 与 reviewer/QA 角色集合的交集。
    只有两侧都非空且归一后相等才报 critical（避免空字段误报）。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            step_no = step.get("step_no", "?")
            op = _norm_person(step.get("operator"))
            rv = _norm_person(step.get("reviewer"))
            same: set[str] = set()
            if op and rv and op == rv:
                same.add(op)
            ops: set[str] = set()
            revs: set[str] = set()
            for sig in step.get("signatures") or []:
                if not isinstance(sig, dict):
                    continue
                role = _norm_value(sig.get("role")).lower()
                person = _norm_person(sig.get("name"))
                if not person:
                    continue
                if role in _OPERATOR_ROLES:
                    ops.add(person)
                elif role in _REVIEWER_ROLES:
                    revs.add(person)
            same |= (ops & revs)
            for person in sorted(same):
                findings.append({
                    "page": pno,
                    "type": "self_review",
                    "severity": "critical",
                    "description": (
                        f"第{pno}页 工序{step_no} 操作人与复核人为同一人「{person}」，"
                        f"独立复核失效（ALCOA+ Attributable：记录须可归因到不同责任人）"
                    ),
                    "ocr_text": f"operator=reviewer={person}",
                    "operator": person,
                    "source": "rule",
                })
    return findings


# ===========================================================================
# R13 —— 设备/清洁状态完整性（equipment_state）
# ===========================================================================

_EQUIP_KEYWORDS = ("状态标志", "清洗", "清洁", "设备", "消毒", "灭菌", "清场", "管路")


def _check_equipment_state(pages: list[dict]) -> list[dict]:
    """R13：设备/清洁状态确认项的完备性与结论。

    - 确认项取值表否定（否/不合格/异常）→ warning（设备状态或清洁未通过）；
    - 确认项带"是/否"模板（spec）却**未填写** → info（已声明未填写）。
    不做"生产页必须有清洁项"的宽泛完整性判定 —— 真实记录中「清洗」字样遍布
    （本记录 314 次），宽泛判定会制造大量噪音。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        for step, p in _iter_params(page):
            name = _norm_value(p.get("name"))
            if not name or not any(k in name for k in _EQUIP_KEYWORDS):
                continue
            step_no = step.get("step_no", "?")
            value = _norm_value(p.get("value"))
            spec = _norm_value(p.get("spec_range"))
            if _is_negative(value):
                findings.append({
                    "page": pno,
                    "type": "equipment_state",
                    "severity": "warning",
                    "description": (
                        f"第{pno}页 工序{step_no} 设备/清洁确认项「{name[:40]}」"
                        f"填为「{value}」，设备或清洁状态未通过，请核实是否已处理"
                    ),
                    "ocr_text": f"{name[:40]}={value}",
                    "operator": "",
                    "source": "rule",
                })
            elif not value and _is_choice_spec(spec):
                findings.append({
                    "page": pno,
                    "type": "equipment_state",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 工序{step_no} 设备/清洁确认项「{name[:40]}」"
                        f"已声明（{spec}）但未填写，请确认设备状态与清洁结果已记录"
                    ),
                    "ocr_text": f"{name[:40]}: spec={spec} value=空",
                    "operator": "",
                    "source": "rule",
                })
    return findings


# ===========================================================================
# R14 —— 环境监测完备性（env_monitor）
# ===========================================================================

_ENV_KEYWORDS = (
    "压差", "温湿度", "湿度", "洁净度", "尘埃", "悬浮粒子", "沉降菌",
    "浮游菌", "环境监测", "层流", "换气次数",
)
# 洁净区工序文本里声明了环境要求（真实：p24/p26「洁净环境符合要求」）
_ENV_DECL_RE = re.compile(r"(洁净环境|环境监测|洁净区|层流|压差|温湿度)")


def _check_env_monitor(pages: list[dict]) -> list[dict]:
    """R14：环境监测项的完备性与结论。

    - 环境确认项取值表否定 → warning；
    - 环境确认项带"是/否"模板却未填写 → info；
    - 工序文本声明洁净环境要求，但该页**既无环境确认项也无环境测量列**
      → info（未见环境监测数据；请确认是否另行记录）。
    注意：不把「温度」纳入环境关键词 —— 该记录工艺温度测量遍布全页，
    纳入会使规则永不触发且语义混乱（工艺温度 ≠ 洁净区环境监测）。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        has_env_param = False
        has_env_measure = False
        declared = False
        for step, p in _iter_params(page):
            name = _norm_value(p.get("name"))
            if not name or not any(k in name for k in _ENV_KEYWORDS):
                continue
            has_env_param = True
            step_no = step.get("step_no", "?")
            value = _norm_value(p.get("value"))
            spec = _norm_value(p.get("spec_range"))
            if _is_negative(value):
                findings.append({
                    "page": pno,
                    "type": "env_monitor",
                    "severity": "warning",
                    "description": (
                        f"第{pno}页 工序{step_no} 环境监测项「{name[:40]}」"
                        f"填为「{value}」，环境指标不符合要求，请核实处理与放行影响"
                    ),
                    "ocr_text": f"{name[:40]}={value}",
                    "operator": "",
                    "source": "rule",
                })
            elif not value and _is_choice_spec(spec):
                findings.append({
                    "page": pno,
                    "type": "env_monitor",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 工序{step_no} 环境监测项「{name[:40]}」"
                        f"已声明（{spec}）但未填写，请确认环境监测数据已记录"
                    ),
                    "ocr_text": f"{name[:40]}: spec={spec} value=空",
                    "operator": "",
                    "source": "rule",
                })
        for step in page["steps"]:
            if _ENV_DECL_RE.search(_norm_value(step.get("operation"))):
                declared = True
                break
        if not has_env_param:
            for _step, col, _cell in _iter_measurement_cols(page):
                if any(k in col for k in _ENV_KEYWORDS):
                    has_env_measure = True
                    break
        if declared and not has_env_param and not has_env_measure:
            findings.append({
                "page": pno,
                "type": "env_monitor",
                "severity": "info",
                "description": (
                    f"第{pno}页 工序文本声明洁净环境要求，但未见环境监测数据"
                    f"（压差/温湿度/洁净度），请确认环境监测是否已记录"
                ),
                "ocr_text": "operation 声明洁净环境要求，无环境监测参数/测量列",
                "operator": "",
                "source": "rule",
            })
    return findings


# ===========================================================================
# R15 —— 文件版本一致性（doc_version）
# ===========================================================================

_VERSION_DIGIT_RE = re.compile(r"\d+")


def _norm_version(ver) -> str:
    """版本号归一：抽取数字串（去前导零）后以 '-' 连接，使 "R23"≡"23"、"09"≡"9"。

    无论文数字时退化为小写原文（如 "A版"）。
    """
    s = _norm_value(ver)
    if not s:
        return ""
    parts = _VERSION_DIGIT_RE.findall(s)
    if not parts:
        return s.lower()
    return "-".join(str(int(p)) for p in parts)


def _check_doc_version(pages: list[dict]) -> list[dict]:
    """R15：同一文件编号（file_code）不得出现多个版本。

    **不做"全页同编号"判定** —— 真实批记录里各表单编号本就不同
    （H3-MPD-10133-R20/R22/R23…），全页同编号会产生海量误报。真实缺陷形态是
    "同一 file_code 跨页出现两个版本"（本记录实测 R23→{R23,09}、R27→{11,10}），
    正是"使用了作废/旧版文件"的机器可检证据。
    """
    findings: list[dict] = []
    groups: dict[str, dict[str, list[int]]] = {}
    for page in pages:
        pi = page.get("page_info") or {}
        code = _norm_value(pi.get("file_code"))
        ver = _norm_value(pi.get("version"))
        if not code or not ver:
            continue
        norm = _norm_version(ver)
        if not norm:
            continue
        groups.setdefault(code, {}).setdefault(norm, []).append(page["page"])
    for code, vmap in groups.items():
        if len(vmap) <= 1:
            continue
        first = min(p for ps in vmap.values() for p in ps)
        detail = "；".join(
            f"{v}(第{','.join(str(x) for x in sorted(ps))}页)"
            for v, ps in sorted(vmap.items())
        )
        findings.append({
            "page": first,
            "type": "doc_version",
            "severity": "warning",
            "description": (
                f"文件编号 {code} 出现 {len(vmap)} 个不同版本：{detail} — "
                f"可能存在作废/旧版文件在用，请核对文件版本控制"
            ),
            "ocr_text": f"file_code={code} versions={sorted(vmap)}",
            "operator": "",
            "source": "rule",
        })
    return findings


# ===========================================================================
# R16 —— 超限偏差关联（deviation_link）
# ===========================================================================

_ANOMALY_NAME_KEYWORDS = ("生产有无异常", "有无异常", "是否异常", "异常情况")
_DEVIATION_REF_KEYWORDS = ("偏差号", "偏差编号", "偏差单号", "偏差记录", "deviation", "dev_no")
_POSITIVE_VALUES = frozenset({"有", "是", "异常", "有异常", "yes", "y", "√", "☑"})


def _check_deviation_link(pages: list[dict], oos_pages: set[int] | None = None) -> list[dict]:
    """R16：存在异常/超限时，必须关联偏差编号（偏差管理可追溯）。

    触发条件（保守，经真实数据与评测双重校准）：
      - **显式异常**：工序/参数声明"有异常"（如「生产有无异常=有」）→ 直接触发；
      - **超限 + 有偏差号字段**：该页存在 R3 判定的参数越界（`oos_pages`），
        **且**该页表单本身设有"偏差号"类字段（无论是否填写）→ 触发。
        若表单根本没有偏差号字段，则不报（该记录可能无此栏目，避免误报）。

    触发后：若"偏差号"类字段**未填写**（或显式异常页无该字段）→ warning。

    设计取舍：早期版本让"任何越界页"都触发，实测会与 R3 重复放大噪音
    （本记录 26 条 param_out_of_spec → +26 条），故收敛为"表单要求关联却留空"
    这一可机检的硬缺口。
    """
    oos_pages = oos_pages or set()
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        explicit = False
        has_ref_field = False
        ref_ok = False
        anomaly_desc = ""
        for _step, p in _iter_params(page):
            name = _norm_value(p.get("name"))
            value = _norm_value(p.get("value"))
            low = name.lower()
            if any(k in name for k in _ANOMALY_NAME_KEYWORDS):
                if value.lower() in _POSITIVE_VALUES:
                    explicit = True
                    anomaly_desc = f"{name}={value}"
            if any(k in name or k in low for k in _DEVIATION_REF_KEYWORDS):
                has_ref_field = True
                if value:
                    ref_ok = True
        triggered = explicit or (pno in oos_pages and has_ref_field)
        if triggered and not ref_ok:
            findings.append({
                "page": pno,
                "type": "deviation_link",
                "severity": "warning",
                "description": (
                    f"第{pno}页 存在异常/超限"
                    f"（{anomaly_desc or '参数超出规格'}）但未记录偏差编号，"
                    f"未关联偏差处理流程，请核实是否已启动偏差"
                ),
                "ocr_text": anomaly_desc or "oos on page",
                "operator": "",
                "source": "rule",
            })
    return findings


# ===========================================================================
# R17 —— 涂改规范（alteration）
# ===========================================================================

_ALTERATION_KEYWORDS = ("涂改", "划改", "修改", "更正", "改错", "作废", "误写", "笔误")
# 疑似划改标记：括号里写"误/改"，或成串的删除线/叉号
_ALTERATION_MARK_RE = re.compile(r"[（(]\s*(误|改|错)\s*[)）]|~~+|××+|✗{2,}")


def _check_alteration(pages: list[dict]) -> list[dict]:
    """R17：疑似涂改必须符合"划改 + 签名 + 日期"规范（ALCOA+ Original）。

    触发：工序操作文本、手写条目或参数取值出现涂改/划改/更正痕迹或删除线标记。
    触发后：所在工序若**既无操作人签名也无执行时间** → warning（无法追溯谁在
    何时、为何修改）；有签名/时间则不报（假定已按规定划改留痕）。
    本记录无涂改痕迹 → 真实数据零误报；规则为"含涂改记录"场景准备。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            traces: list[str] = []
            op = _norm_value(step.get("operation"))
            if op and (any(k in op for k in _ALTERATION_KEYWORDS)
                       or _ALTERATION_MARK_RE.search(op)):
                traces.append("operation")
            notes = [
                _norm_value(n) for n in (step.get("handwritten") or [])
                if isinstance(n, (str, int, float))
            ]
            if any(any(k in n for k in _ALTERATION_KEYWORDS) for n in notes):
                traces.append("handwritten")
            for p in step.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                v = _norm_value(p.get("value"))
                if v and (_ALTERATION_MARK_RE.search(v)
                          or (any(k in v for k in _ALTERATION_KEYWORDS) and len(v) <= 30)):
                    traces.append(f"param:{_norm_value(p.get('name'))[:20]}")
            if not traces:
                continue
            has_signature = bool(_norm_person(step.get("operator"))) or bool(
                step.get("signatures")
            )
            has_time = bool(_norm_value(step.get("start_time"))
                            or _norm_value(step.get("end_time")))
            if has_signature or has_time:
                continue
            step_no = step.get("step_no", "?")
            findings.append({
                "page": pno,
                "type": "alteration",
                "severity": "warning",
                "description": (
                    f"第{pno}页 工序{step_no} 存在疑似涂改/划改痕迹"
                    f"（{'、'.join(traces)}）但缺操作人签名与日期，"
                    f"涂改未按规定留痕（ALCOA+ Original：原始数据须保留可追溯）"
                ),
                "ocr_text": f"alteration traces: {','.join(traces)}",
                "operator": "",
                "source": "rule",
            })
    return findings
