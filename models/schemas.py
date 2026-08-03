"""Pydantic data models for the pharma batch checker."""
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class JobStatus(str, Enum):
    PENDING = "pending"
    OCR_RUNNING = "ocr_running"
    OCR_DONE = "ocr_done"
    ANALYZING = "analyzing"
    REVIEW = "review"
    DONE = "done"
    ERROR = "error"


class FindingType(str, Enum):
    # Legacy / generic
    TIME_ANOMALY = "time_anomaly"
    YEAR_CONTRADICTION = "year_contradiction"
    # Phase 1 additions — required by PLAN.md Phase 2 rules
    TIME_REVERSAL = "time_reversal"
    SIGNATURE_TIME_ANOMALY = "signature_time_anomaly"
    SUSPICIOUS_DATE = "suspicious_date"
    # Existing
    PARAM_OUT_OF_SPEC = "param_out_of_spec"
    COMPLETENESS = "completeness"
    HANDWRITING = "handwriting"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class FindingStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CORRECTED = "corrected"


class Parameter(BaseModel):
    """Single-value parameter (e.g. batch yield, total output).

    Used for table-level scalar values where spec and actual are each a single
    value. For matrix-style parameters (multiple timepoints x multiple
    columns), use Measurement instead.
    """
    name: str
    spec_range: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    in_spec: Optional[bool] = None


class MeasurementValue(BaseModel):
    """One cell in a measurement matrix: actual value at one timepoint for one column.

    spec/actual are strings (not numbers) to preserve OCR text fidelity; rule
    layer parses them. in_spec may be null when rule layer cannot judge.
    """
    spec: Optional[str] = None
    actual: Optional[str] = None
    unit: Optional[str] = None
    in_spec: Optional[bool] = None


class Measurement(BaseModel):
    """One row of a time-series matrix: a timestamp + values for each column.

    Example: page9 SP-1 resin absorption has 9 timepoints (11:04..19:09) x
    8 columns (T2101a-d flow + T2101a-d pressure). Each cell has spec/actual.
    Column name format: "{equipment}_{metric}" e.g. "T2101a_流速", "T2101a_压力".
    """
    time: str
    values: dict[str, MeasurementValue] = {}


class Signature(BaseModel):
    """A signature block on the page: role + name + sign time.

    sign_time may be null when the page has only a name (no date). confidence
    is low for handwritten/glued text (e.g. "庞明女署2027.01.17" where the
    name and date are concatenated).
    """
    role: Optional[str] = None  # operator | reviewer | issuer | qa_reviewer | ...
    name: Optional[str] = None
    sign_time: Optional[str] = None
    confidence: str = "high"  # high | medium | low


class Step(BaseModel):
    step_no: Optional[str] = None
    operation: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    # Single-value parameters (table-level scalars)
    parameters: list[Parameter] = []
    # Time-series matrix (multi-row x multi-column parameters)
    measurements: list[Measurement] = []
    # Operator/reviewer signature on this step (legacy single-value fields)
    operator: Optional[str] = None
    reviewer: Optional[str] = None
    # Structured signatures (Phase 1: parse glued text like "庞明女署2027.01.17")
    signatures: list[Signature] = []
    handwritten: list[str] = []
    anomalies: list[str] = []


class EventYearGroups(BaseModel):
    """Years grouped by event type so the rule layer compares only within a type.

    A single page can legitimately have different years for different events
    (e.g. page2: draft=2022, production=2015, review=2025, issue=2027).
    The legacy "all-page mode year" rule flagged all of these as year_contradiction;
    Phase 2 replaces it with per-type comparison.
    """
    draft: list[int] = []        # 起草
    production: list[int] = []   # 生产
    review: list[int] = []       # 车间/QA 审核
    approval: list[int] = []     # 批准
    issue: list[int] = []        # 记录发放
    other: list[int] = []        # 其他（执行日期、版本日期等）


class PageResult(BaseModel):
    page_number: int
    title: Optional[str] = None
    file_code: Optional[str] = None
    version: Optional[str] = None
    batch_no: Optional[str] = None
    production_date: Optional[str] = None
    steps: list[Step] = []
    # Phase 1: per-page findings (LLM produces structured findings directly,
    # not just text in ocr_noise). This is critical for catching time_reversal
    # on page2 where v2 prompt stuffed "开始 2015.01.27 晚于结束 2015.01.25"
    # into ocr_noise instead of producing a finding.
    findings: list[dict] = []
    # Phase 1: years grouped by event type (replaces all-page mode year rule)
    event_year_groups: Optional[EventYearGroups] = None
    time_anomalies: list[str] = []
    overall_confidence: str = "medium"


class Finding(BaseModel):
    id: Optional[int] = None
    job_id: str
    page: int
    type: FindingType | str
    severity: Severity | str
    description: str
    ocr_text: Optional[str] = None
    operator: Optional[str] = None
    reviewer_note: Optional[str] = None
    status: FindingStatus | str = FindingStatus.PENDING
    corrected_text: Optional[str] = None


class JobStatusResponse(BaseModel):
    id: str
    filename: str
    status: JobStatus | str
    total_pages: Optional[int] = None
    pages_ocr_done: int = 0
    pages_analyzed: int = 0
    total_findings: int = 0
    review_findings: int = 0
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_message: Optional[str] = None


class FindingUpdate(BaseModel):
    status: Optional[str] = None
    reviewer_note: Optional[str] = None
    corrected_text: Optional[str] = None
