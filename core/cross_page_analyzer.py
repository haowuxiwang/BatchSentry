"""Stage 3 — cross-page analysis (module refactor shim).

Split into the core/rules/ package (2026-08): parsing / base / rule_time /
rule_spec / rule_doc / llm_checks, each ≤ ~400 lines, with the orchestration
entry point analyze_cross_page living in core/rules/__init__.py.

This file exists only for backward compatibility — pipeline.py and existing
tests import from core.cross_page_analyzer. New code should import from
core.rules directly.
"""
from __future__ import annotations

from core.rules import SpecBounds, analyze_cross_page  # noqa: F401
# Existing tests patch get_llm_client on this module — keep it reachable here.
from llm.client import get_llm_client  # noqa: F401
from core.rules.base import (  # noqa: F401
    _collect_per_page_findings,
    _normalize_pages,
    _only_dicts,
    _sanitize_year_groups,
)
from core.rules.llm_checks import (  # noqa: F401
    _build_summary,
    _flag_llm_queue_for_review,
    _llm_based_check,
    _llm_fallback_check,
    _user_rules_section,
)
from core.rules.parsing import (  # noqa: F401
    _expand_power_notation,
    _extract_unit,
    _extract_year,
    _judge,
    _parse_number,
    _parse_spec,
    _parse_time,
    _try_unit_normalize,
)
from core.rules.rule_doc import (  # noqa: F401
    _check_batch_consistency,
    _check_completeness,
    _check_handwritten_notes,
    _check_low_confidence_params,
    _check_measurement_column_consistency,
    _check_measurement_time_sequence,
)
from core.rules.rule_spec import (  # noqa: F401
    _check_param_out_of_spec,
    _judge_cell,
    _judge_param,
)
from core.rules.rule_time import (  # noqa: F401
    _check_signature_time_anomaly,
    _check_suspicious_dates,
    _check_time_reversal_cross_page,
    _check_time_reversal_in_page,
    _check_year_contradiction,
    _collect_all_date_strings,
    _step_sort_key,
)