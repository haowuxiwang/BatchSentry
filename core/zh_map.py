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

# Finding 类型（findings.type）
FINDING_TYPE_ZH = {
    "time_reversal": "时间倒序",
    "year_contradiction": "年份矛盾",
    "signature_time_anomaly": "签名时间异常",
    "suspicious_date": "可疑日期",
    "param_out_of_spec": "参数越界",
    "completeness": "内容不完整",
}


def zh_severity(key: str) -> str:
    """severity → 中文；未知值原样返回（不掩盖未来新增枚举）。"""
    return SEVERITY_ZH.get(key, key)


def zh_finding_status(key: str) -> str:
    """finding status → 中文；未知值原样返回。"""
    return FINDING_STATUS_ZH.get(key, key)


def zh_job_status(key: str) -> str:
    """job status → 中文；未知值原样返回。"""
    return JOB_STATUS_ZH.get(key, key)


def zh_finding_type(key: str) -> str:
    """finding type → 中文；未知值原样返回。"""
    return FINDING_TYPE_ZH.get(key, key)