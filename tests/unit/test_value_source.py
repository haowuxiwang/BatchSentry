"""value_source 兜底推断与规则接入单元测试。

覆盖：
- _infer_value_source: 列名关键词 → handwritten/printed/unknown
- _backfill_value_source: LLM 缺省时按列名补齐，已有值不覆盖
- rule_spec._severity_for_out_of_spec: printed 值超差不降噪（warning），
  handwritten/unknown 边缘超差降 info
"""
from core.rules.base import _infer_value_source, _backfill_value_source
from core.rules.rule_spec import _severity_for_out_of_spec, _parse_spec


class TestInferValueSource:
    def test_handwritten_keywords(self):
        assert _infer_value_source("实际值") == "handwritten"
        assert _infer_value_source("实测温度") == "handwritten"
        assert _infer_value_source("记录结果") == "handwritten"
        assert _infer_value_source("温度实测") == "handwritten"

    def test_printed_keywords(self):
        assert _infer_value_source("规格范围") == "printed"
        assert _infer_value_source("操作要求") == "printed"
        assert _infer_value_source("标准") == "printed"
        assert _infer_value_source("设备A_流速_标准") == "printed"

    def test_no_keyword_unknown(self):
        assert _infer_value_source("设备A_流速") == "unknown"
        assert _infer_value_source("") == "unknown"
        assert _infer_value_source(None) == "unknown"

    def test_custom_fallback(self):
        assert _infer_value_source("", "printed") == "printed"


class TestBackfillValueSource:
    def test_missing_field_backfilled(self):
        data = {"steps": [
            {"parameters": [
                {"name": "实际温度", "value": "25"},
                {"name": "规格范围", "value": "20-30"},
            ]},
            {"measurements": [
                {"time": "10:00", "values": {
                    "实测流速": {"actual": "0.97"},
                    "设备A_流速": {"actual": "0.95"},
                }},
            ]},
        ]}
        _backfill_value_source(data)
        p = data["steps"][0]["parameters"]
        assert p[0]["value_source"] == "handwritten"
        assert p[1]["value_source"] == "printed"
        vals = data["steps"][1]["measurements"][0]["values"]
        assert vals["实测流速"]["value_source"] == "handwritten"
        assert vals["设备A_流速"]["value_source"] == "unknown"

    def test_existing_value_not_overwritten(self):
        data = {"steps": [
            {"parameters": [
                {"name": "实际温度", "value": "25", "value_source": "printed"},
            ]},
        ]}
        _backfill_value_source(data)
        assert data["steps"][0]["parameters"][0]["value_source"] == "printed"

    def test_unrecognized_marker_overrides_llm(self):
        """MinerU 低置信度标记是机器事实：LLM 标 printed 也要被覆盖为
        handwritten（该单元格内容实际不可识别）。"""
        data = {"steps": [
            {"parameters": [
                {"name": "规格范围", "value": "[手写内容未识别]",
                 "value_source": "printed"},
            ]},
            {"measurements": [
                {"time": "10:00", "values": {
                    "设备A_流速": {"actual": "[手写内容未识别]0.97",
                                  "value_source": "printed"},
                }},
            ]},
        ]}
        _backfill_value_source(data)
        assert data["steps"][0]["parameters"][0]["value_source"] == "handwritten"
        vals = data["steps"][1]["measurements"][0]["values"]
        assert vals["设备A_流速"]["value_source"] == "handwritten"

    def test_marker_only_when_present(self):
        """无标记 + LLM 标注不触碰；marker 检查对非字符串安全。"""
        data = {"steps": [
            {"parameters": [
                {"name": "实际温度", "value": 25.0, "value_source": "printed"},
            ]},
            {"measurements": [
                {"time": "10:00", "values": {
                    "实测流速": {"actual": None},
                }},
            ]},
        ]}
        _backfill_value_source(data)
        assert data["steps"][0]["parameters"][0]["value_source"] == "printed"
        vals = data["steps"][1]["measurements"][0]["values"]
        assert vals["实测流速"]["value_source"] == "handwritten"


class TestLowConfSignal:
    """Round 7：_ocr_low_conf_cols 列/标签信号是机器事实，优先级高于
    LLM 标注（含显式 printed）与关键词启发。"""

    def test_column_token_overrides_llm_printed(self):
        data = {"_ocr_low_conf_cols": ["审核意见", "实测值"], "steps": [
            {"parameters": [
                {"name": "组织部门负责人审核意见", "value": "2022.05.07",
                 "value_source": "printed"},
            ]},
            {"measurements": [
                {"time": "10:00", "values": {
                    "设备A_实测值": {"actual": "0.97", "value_source": "printed"},
                }},
            ]},
        ]}
        _backfill_value_source(data)
        p = data["steps"][0]["parameters"][0]
        assert p["value_source"] == "handwritten"
        v = data["steps"][1]["measurements"][0]["values"]["设备A_实测值"]
        assert v["value_source"] == "handwritten"

    def test_exact_and_reverse_containment(self):
        data = {"_ocr_low_conf_cols": ["实测值"], "steps": [
            {"parameters": [
                {"name": "实测值", "value": "5.4", "value_source": "printed"},
                {"name": "设备B_实测值", "value": "6.1", "value_source": "printed"},
            ]},
            {"measurements": [
                {"time": "9:00", "values": {
                    "实测值记录": {"actual": "1.2", "value_source": "printed"},
                }},
            ]},
        ]}
        _backfill_value_source(data)
        params = data["steps"][0]["parameters"]
        assert params[0]["value_source"] == "handwritten"
        assert params[1]["value_source"] == "handwritten"
        v = data["steps"][1]["measurements"][0]["values"]["实测值记录"]
        assert v["value_source"] == "handwritten"

    def test_unrelated_column_untouched(self):
        data = {"_ocr_low_conf_cols": ["审核意见"], "steps": [
            {"parameters": [
                {"name": "设备A_流速", "value": "0.95", "value_source": "printed"},
            ]},
            {"measurements": [
                {"time": "10:00", "values": {
                    "设备A_流速_标准": {"actual": "1.0", "value_source": "printed"},
                }},
            ]},
        ]}
        _backfill_value_source(data)
        assert data["steps"][0]["parameters"][0]["value_source"] == "printed"
        v = data["steps"][1]["measurements"][0]["values"]["设备A_流速_标准"]
        assert v["value_source"] == "printed"

    def test_keyword_would_print_but_signal_wins(self):
        # '规格范围' 关键词 → printed；但 OCR 信号说该列有手写未识别单元格。
        # 无 LLM 标注时信号层先于关键词兜底生效。
        data = {"_ocr_low_conf_cols": ["规格范围"], "steps": [
            {"parameters": [
                {"name": "规格范围", "value": "20-30"},
            ]},
        ]}
        _backfill_value_source(data)
        assert data["steps"][0]["parameters"][0]["value_source"] == "handwritten"

    def test_no_signal_key_unchanged(self):
        data = {"steps": [
            {"parameters": [
                {"name": "规格范围", "value": "20-30", "value_source": "printed"},
            ]},
        ]}
        _backfill_value_source(data)
        assert data["steps"][0]["parameters"][0]["value_source"] == "printed"

    def test_non_str_tokens_filtered(self):
        data = {"_ocr_low_conf_cols": [None, 123, "审核意见"], "steps": [
            {"parameters": [
                {"name": "组织部门负责人审核意见", "value": "2022.05.07",
                 "value_source": "printed"},
            ]},
        ]}
        _backfill_value_source(data)
        assert data["steps"][0]["parameters"][0]["value_source"] == "handwritten"


class TestOutOfSpecValueSource:
    def _sev(self, spec, actual, vs):
        bounds = _parse_spec(spec)
        return _severity_for_out_of_spec(bounds, float(actual), spec, vs)

    def test_printed_edge_stays_warning(self):
        sev, hint = self._sev("≤5.0℃", 5.4, "printed")
        assert sev == "warning"
        assert hint == ""

    def test_handwritten_edge_softened(self):
        sev, hint = self._sev("≤5.0℃", 5.4, "handwritten")
        assert sev == "info"
        assert "手写 OCR 误读" in hint

    def test_unknown_edge_softened(self):
        sev, _ = self._sev("≤5.0℃", 5.4, "unknown")
        assert sev == "info"

    def test_handwritten_large_stays_warning(self):
        sev, _ = self._sev("≤5.0℃", 25, "handwritten")
        assert sev == "warning"