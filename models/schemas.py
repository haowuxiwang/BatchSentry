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
    TIME_ANOMALY = "time_anomaly"
    YEAR_CONTRADICTION = "year_contradiction"
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
    name: str
    spec_range: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    in_spec: Optional[bool] = None


class Step(BaseModel):
    step_no: Optional[str] = None
    operation: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    parameters: list[Parameter] = []
    operator: Optional[str] = None
    reviewer: Optional[str] = None
    handwritten: list[str] = []
    anomalies: list[str] = []


class PageResult(BaseModel):
    page_number: int
    title: Optional[str] = None
    file_code: Optional[str] = None
    version: Optional[str] = None
    batch_no: Optional[str] = None
    production_date: Optional[str] = None
    steps: list[Step] = []
    findings: list[dict] = []
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
