"""core.hw_signal OCR 手写结构化信号提取单元测试。

覆盖：
- _extract_low_conf_tokens: HTML 表格列映射（th/td 表头、colspan 禁用列映射）
- 管道表列映射（分隔行跳过、短行按索引对齐）
- 标签信号（标签:标记 前缀提取，: 与 ：两种冒号）
- token 去重、纯数字/超短噪声拒绝、markdown 加粗清洗
- 无标记/非字符串输入 → []
- _has_unrecognized_marker 基础行为
"""
from core.hw_signal import (
    _UNRECOGNIZED_MARKER,
    _extract_low_conf_tokens,
    _has_unrecognized_marker,
)

M = f"[{_UNRECOGNIZED_MARKER}]"


class TestHasMarker:
    def test_str_with_marker(self):
        assert _has_unrecognized_marker(f"审核意见:{M}")

    def test_str_without_marker(self):
        assert not _has_unrecognized_marker("审核意见: 同意")

    def test_non_str(self):
        assert not _has_unrecognized_marker(None)
        assert not _has_unrecognized_marker(123)


class TestExtractTokens:
    def test_empty_and_no_marker(self):
        assert _extract_low_conf_tokens("") == []
        assert _extract_low_conf_tokens("普通文本，无标记") == []
        assert _extract_low_conf_tokens(None) == []

    def test_label_token_plain_line(self):
        text = f"审核意见:{M} 2022.05.07"
        assert _extract_low_conf_tokens(text) == ["审核意见"]

    def test_label_token_halfwidth_colon(self):
        text = f"审核意见:{M}"
        assert "审核意见" in _extract_low_conf_tokens(text)

    def test_label_token_without_colon(self):
        # 真实记录形态：表头列 '签名' 紧跟标记，无冒号（页 39/40/41/47）
        assert _extract_low_conf_tokens(f"签名{M}") == ["签名"]

    def test_description_text_terminated_by_punctuation(self):
        # 描述文字被中文标点终止 → 只捕获紧跟标记的标签，不吞整句
        text = f"设备状态标志使用是否正确，签名{M}"
        assert _extract_low_conf_tokens(text) == ["签名"]

    def test_label_deduplicated(self):
        text = f"审核意见:{M}\nQA产品:\n{f'复核意见:{M}'}"
        tokens = _extract_low_conf_tokens(text)
        assert tokens.count("复核意见") == 1
        assert tokens[0] == "审核意见"

    def test_pure_digit_label_rejected(self):
        assert _extract_low_conf_tokens(f"2025.01.1:{M}") == []

    def test_short_label_rejected(self):
        assert _extract_low_conf_tokens(f"A:{M}") == []

    def test_html_table_column_mapping_th(self):
        text = (f"<table><tr><th>设备A</th><th>实测值</th></tr>"
                f"<tr><td>1</td><td>{M}</td></tr></table>")
        assert "实测值" in _extract_low_conf_tokens(text)

    def test_html_table_column_mapping_td_header(self):
        # 无 <th> 时第一行 <tr> 视为表头
        text = (f"<table><tr><td>设备A</td><td>实测值</td></tr>"
                f"<tr><td>1</td><td>{M}</td></tr></table>")
        assert "实测值" in _extract_low_conf_tokens(text)

    def test_html_table_colspan_disables_column_mapping(self):
        # colspan 破坏列对齐 → 不产出列 token；但单元格内标签照常
        text = (f"<table><tr><th>设备A</th><th>实测值</th></tr>"
                f"<tr><td colspan=\"2\">审核意见:{M} 2022.05.07</td></tr></table>")
        tokens = _extract_low_conf_tokens(text)
        assert "实测值" not in tokens
        assert "审核意见" in tokens

    def test_html_table_no_header_invalid_tokens(self):
        # 无有效表头时不产出列 token（'1' 过短、标记单元格自身被排除）
        text = (f"<table><tr><td>1</td><td>{M}</td></tr>"
                f"<tr><td>2</td><td>{M}</td></tr></table>")
        assert _extract_low_conf_tokens(text) == []

    def test_pipe_table_column_mapping(self):
        text = (f"| 时间 | 实测值 |\n|---|---|\n| 10:00 | {M} |")
        assert "实测值" in _extract_low_conf_tokens(text)

    def test_pipe_table_without_separator(self):
        text = f"| 时间 | 实测值 |\n| 10:00 | {M} |"
        assert "实测值" in _extract_low_conf_tokens(text)

    def test_pipe_table_short_row_indexed(self):
        text = (f"| 时间 | 实测值 | 规格 |\n|---|---|---|\n| 10:00 | {M} |")
        tokens = _extract_low_conf_tokens(text)
        assert "实测值" in tokens
        assert "规格" not in tokens

    def test_markdown_bold_header_cleaned(self):
        text = f"| **实测值** | 规格 |\n|---|---|\n| {M} | 20-30 |"
        assert "实测值" in _extract_low_conf_tokens(text)

    def test_multiple_tables_and_lines_combined(self):
        text = (f"审核意见:{M}\n"
                f"<table><tr><th>A</th><th>实际值</th></tr>"
                f"<tr><td>1</td><td>{M}</td></tr></table>\n"
                f"| 时间 | 记录结果 |\n|---|---|\n| 9:00 | {M} |")
        tokens = _extract_low_conf_tokens(text)
        assert "审核意见" in tokens
        assert "实际值" in tokens
        assert "记录结果" in tokens