"""中文 UI 映射单源（Round 3 中文化收尾）。

面向中国地区构建 — 所有用户可见的枚举在 Python 侧统一出中文文案。
此前 review.html（Jinja2）、review.js、upload.js、report.py、notify.py
各自维护一份映射，新增状态/严重度时要同步多处（容易漏）。本模块是
Python 后端的单一来源；前端 JS 仍各持一份（历史原因，收敛成本高），
新增枚举时优先更新这里 + 前端同步。

枚举取值与 db/schema.sql / models/schemas.py 保持一致。
"""

# Finding 严重度（findings.severity）
SEVERITY_ZH = {
    "critical": "严重",
    "warning": "警告",
    "info": "信息",
}

# Finding 复核状态（findings.status）
FINDING_STATUS_ZH = {
    "pending": "待复核",
    "confirmed": "已确认",
    "rejected": "已拒绝",
    "corrected": "已修正",
}

# Job 状态（jobs.status）
JOB_STATUS_ZH = {
    "pending": "待处理",
    "ocr_running": "OCR 解析中",
    "ocr_done": "OCR 完成",
    "analyzing": "分析中",
    "review": "待复核",
    "partial_review": "部分完成待复核",
    "error": "失败",
    "cancelled": "已取消",
    "archived": "已归档",
}

# Finding 类型（findings.type）—— 覆盖 core.finding_quality.CANONICAL_TYPES
# 全量（不变式由 tests/unit/test_finding_quality.py 守护）。新增规范类型时必须
# 同步这里，否则前端 type_zh 会回落成英文原文。
FINDING_TYPE_ZH = {
    "time_reversal": "时间倒序",
    "year_contradiction": "年份矛盾",
    "signature_time_anomaly": "签名时间异常",
    "signature_mismatch": "签名不一致",
    "suspicious_date": "可疑日期",
    "param_out_of_spec": "参数越界",
    "completeness": "内容不完整",
    "handwritten": "手写内容",
    "batch_inconsistency": "批号不一致",
    "step_gap": "步骤缺失",
    "ocr_noise": "OCR 噪声",
    "spec_unverifiable": "规格无法核定",
    # ── M4：R11–R17 ──
    "mass_balance": "物料平衡/收率",
    "self_review": "自检自核",
    "equipment_state": "设备/清洁状态",
    "env_monitor": "环境监测",
    "doc_version": "文件版本",
    "deviation_link": "偏差关联",
    "alteration": "涂改规范",
    "user_rule": "用户规则",
    "uncategorized": "未分类",
}

# OCR 后端显示名（jobs.ocr_backend_used，GMP 审计字段 — 前端徽章/任务行
# 展示用。API 仍返回小写原始值 + ocr_backend_display 中文名，前端零映射）
OCR_BACKEND_ZH = {
    "mineru": "MinerU",
    "paddle": "PaddleOCR",
    "cached": "缓存复用",
}


def zh_ocr_backend(key: str) -> str:
    """ocr_backend_used → 显示名；未知值原样返回（不掩盖新增后端）。"""
    return OCR_BACKEND_ZH.get(key, key)


def zh_severity(key: str) -> str:
    """severity → 中文；未知值原样返回（不掩盖未来新增枚举）。"""
    return SEVERITY_ZH.get(key, key)


def zh_finding_status(key: str) -> str:
    """finding status → 中文；未知值原样返回。"""
    return FINDING_STATUS_ZH.get(key, key)


def zh_job_status(key: str) -> str:
    """job status → 中文；未知值原样返回。"""
    return JOB_STATUS_ZH.get(key, key)


# job status → 状态点颜色类（Tailwind class 片段）。
#
# 与 `static/status.js` 的 `statusDotClass` **必须逐值等价**（由
# `tests/unit/test_status_js.py` 机检锁定：两侧对全部状态枚举 + 未知值
# 输出同名 class）。SSR 用它渲染首屏，前端用它做 SSE 实时更新 ——
# 此前只有前端一份，导致模板只能硬编码 `bg-success`（缺陷 #133，P0：
# error/partial_review 的复核页显示"绿点+出错"，GMP 假阴性表面）。
#
# 排序按"用户会不会误读为成功"，不按"状态看起来像不像完成了"：
# partial_review 的**定义**就是"存在失败页或双后端差异"，永远不是成功态。
STATUS_DOT_CLASS = {
    "review": "bg-success",
    "done": "bg-success",
    "partial_review": "bg-warning",
    "error": "bg-destructive",
    "cancelled": "bg-muted-foreground/40",
    "cancelling": "bg-muted-foreground/40",
    "archived": "bg-muted-foreground/40",
}
# 非终态/未知状态 → 中性蓝（"还在跑"）。与 JS 侧 `bg-info` 默认分支一致。
_STATUS_DOT_DEFAULT = "bg-info"


def status_dot_class(key: str | None) -> str:
    """job status → 状态点 Tailwind 颜色类。未知状态 → `bg-info`。

    空值也走默认分支（SSR 时 status 理论上非空，但模板不该因脏数据渲染出
    一个"无色点"—— 那比错色更难察觉）。
    """
    return STATUS_DOT_CLASS.get(key or "", _STATUS_DOT_DEFAULT)


def zh_finding_type(key: str) -> str:
    """finding type → 中文；未知值原样返回。"""
    return FINDING_TYPE_ZH.get(key, key)