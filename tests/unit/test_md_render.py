# -*- coding: utf-8 -*-
"""core.md_render 单测（round-61 T1）—— 报告查看页的 md→html 渲染器。

测试口径与渲染器的两个契约对齐：
1. **子集正确性**：标题/列表/嵌套/粗体/行内代码/hr/段落，逐形态精确断言
   （不是"含 <ul>"这种弱断言，而是完整 HTML 串 —— 结构错位即红）。
2. **安全模型**：生成端 esc() 已转义，实体必须**原样透传**（`&lt;` 不得
   被解回 `<`、不得被二次处理）—— 这是"渲染器信任前置转义"成立的前提。

另附真实报告片段的端到端渲染（与 api/report.py::_generate_markdown 的
输出形态对齐：标题 + 嵌套列表 + 粗体 + 行内代码 + hr + emoji）。
"""
from core.md_render import md_to_html


class TestEmptyInput:
    def test_empty_string(self):
        assert md_to_html("") == ""

    def test_whitespace_only(self):
        assert md_to_html("\n\n  \n\t\n") == ""


class TestHeadings:
    def test_h1_to_h6(self):
        assert md_to_html("# 一级") == "<h1>一级</h1>"
        assert md_to_html("## 二级") == "<h2>二级</h2>"
        assert md_to_html("### 三级") == "<h3>三级</h3>"
        assert md_to_html("#### 四级") == "<h4>四级</h4>"
        assert md_to_html("##### 五级") == "<h5>五级</h5>"
        assert md_to_html("###### 六级") == "<h6>六级</h6>"

    def test_seven_hashes_degrade_to_paragraph(self):
        # CommonMark：标题最多 6 个 #，7 个 # 不是标题 → 按段落降级
        # （生成器只产出 #/##/###，此为超子集防御）
        assert md_to_html("####### 七个") == "<p>####### 七个</p>"

    def test_hash_without_space_is_paragraph(self):
        # md 规范：# 后必须有空格才是标题；生成器不会产这种行，
        # 但渲染器对超子集输入必须走段落降级而非误判
        assert md_to_html("#无空格") == "<p>#无空格</p>"

    def test_heading_with_inline(self):
        assert md_to_html("## **关键**发现") == "<h2><strong>关键</strong>发现</h2>"


class TestHorizontalRule:
    def test_standalone_hr(self):
        assert md_to_html("---") == "<hr>"

    def test_hr_with_leading_whitespace(self):
        # 生成器 hr 顶格；带前导空格的 --- 语义上仍是 hr（strip 后判定）
        assert md_to_html("  ---") == "<hr>"

    def test_three_dashes_with_text_is_not_hr(self):
        # "--- 文本" 是段落（生成器的列表项/段落形态），不得误判为 hr
        assert md_to_html("--- 文本") == "<p>--- 文本</p>"

    def test_two_dashes_is_paragraph(self):
        assert md_to_html("--") == "<p>--</p>"


class TestFlatList:
    def test_single_item(self):
        assert md_to_html("- 唯一") == "<ul><li>唯一</li></ul>"

    def test_consecutive_items_grouped_into_one_ul(self):
        # 连续列表行必须聚合为一个 <ul>（逐项各自成 ul 会破坏视觉分组）
        assert md_to_html("- a\n- b\n- c") == "<ul><li>a</li><li>b</li><li>c</li></ul>"

    def test_list_then_paragraph_closes_ul(self):
        # 列表结束后必须闭合 </ul>，否则后续段落被浏览器吞进列表
        assert md_to_html("- a\n\n段落") == "<ul><li>a</li></ul><p>段落</p>"

    def test_list_then_heading_closes_ul(self):
        assert (
            md_to_html("- a\n## 标题") == "<ul><li>a</li></ul><h2>标题</h2>"
        )


class TestNestedList:
    def test_nested_item_structure(self):
        # 一级项的 li 已闭合；嵌套作为紧随的兄弟 li 内容（渲染器有意简化，
        # 生成器的嵌套项总是紧跟父项，视觉分组一致）。此处锁定精确结构，
        # 防止后续改动悄悄破坏闭合配对。
        assert (
            md_to_html("- 父项\n  - 子项")
            == "<ul><li>父项</li><li><ul><li>子项</li></ul></li></ul>"
        )

    def test_multiple_nested_items_share_one_sub_ul(self):
        # 连续多个子项聚合为一个嵌套 <ul>，而非每项一个
        assert (
            md_to_html("- 父\n  - a\n  - b")
            == "<ul><li>父</li><li><ul><li>a</li><li>b</li></ul></li></ul>"
        )

    def test_back_to_top_level_after_nested(self):
        # 子项后回到一级：嵌套 </ul></li> 必须先闭合，再开一级 <li>
        assert (
            md_to_html("- a\n  - b\n- c")
            == "<ul><li>a</li><li><ul><li>b</li></ul></li><li>c</li></ul>"
        )

    def test_nested_list_closed_by_blank_line(self):
        assert (
            md_to_html("- a\n  - b\n\n段落")
            == "<ul><li>a</li><li><ul><li>b</li></ul></li></ul><p>段落</p>"
        )


class TestInline:
    def test_bold(self):
        assert md_to_html("**粗体**") == "<p><strong>粗体</strong></p>"

    def test_unclosed_bold_passthrough(self):
        # 未配对的 ** 原样透传（生成端不会产，但不得崩/不得吞字符）
        assert md_to_html("** 单星") == "<p>** 单星</p>"

    def test_inline_code(self):
        assert md_to_html("`code`") == "<p><code>code</code></p>"

    def test_unclosed_backtick_passthrough(self):
        assert md_to_html("` 没闭合") == "<p>` 没闭合</p>"

    def test_bold_and_code_same_line(self):
        assert (
            md_to_html("**关键**：`value`")
            == "<p><strong>关键</strong>：<code>value</code></p>"
        )

    def test_bold_inside_list_item(self):
        assert (
            md_to_html("- **critical**：2")
            == "<ul><li><strong>critical</strong>：2</li></ul>"
        )


class TestSecurityPassthrough:
    """安全模型核心：实体原样透传，渲染器不二次处理。"""

    def test_entities_pass_through_untouched(self):
        # 生成端 esc() 已把 < > 转成 &lt; &gt; —— 渲染器必须原样透传，
        # 不得"帮倒忙"做反转义或再转义（&amp;lt; 会把 &lt; 显示成字面 "<"）
        assert md_to_html("&lt;img src=x&gt;") == "<p>&lt;img src=x&gt;</p>"

    def test_double_escaped_entity_not_reprocessed(self):
        assert md_to_html("&amp;lt;") == "<p>&amp;lt;</p>"

    def test_md_image_metachars_neutralized_by_passthrough(self):
        # 生成端已把 ![...](...) 的元字符转义为实体；渲染器无 <img> 语法，
        # 该行按段落透传 —— 断言输出不含任何标签注入面
        html = md_to_html("&amp;#91;&amp;#33;图&amp;#93;")
        assert html == "<p>&amp;#91;&amp;#33;图&amp;#93;</p>"
        assert "<img" not in html.replace("&lt;img", "")


class TestParagraph:
    def test_plain_paragraph(self):
        assert md_to_html("普通段落") == "<p>普通段落</p>"

    def test_consecutive_lines_render_as_separate_paragraphs(self):
        # 渲染器逐行成段（生成器每段之间有空行，视觉一致）
        assert md_to_html("第一行\n第二行") == "<p>第一行</p><p>第二行</p>"

    def test_blank_line_separates_paragraphs(self):
        assert md_to_html("a\n\nb") == "<p>a</p><p>b</p>"

    def test_emoji_passthrough(self):
        # 报告的 severity 行含 emoji（🔴🟡🔵⏳），必须原样透传
        assert md_to_html("- 🔴 critical：2") == "<ul><li>🔴 critical：2</li></ul>"

    def test_trailing_whitespace_stripped(self):
        assert md_to_html("段落  ") == "<p>段落</p>"


class TestDegradation:
    """超子集输入按段落降级 —— 渲染器永不抛错。"""

    def test_table_row_degrades_to_paragraph(self):
        assert md_to_html("| 列一 | 列二 |") == "<p>| 列一 | 列二 |</p>"

    def test_blockquote_degrades_to_paragraph(self):
        # 渲染器无引用块语法；> 开头的行按段落透传（安全模型：转义在生成端）
        assert md_to_html("> 引用") == "<p>> 引用</p>"

    def test_bare_symbols_never_raise(self):
        # 模糊输入（各种半截结构）不得抛异常
        out = md_to_html("#\n-\n--\n**\n` \n###\n  -\n\t- x")
        assert isinstance(out, str)

    def test_cjk_and_mixed_content(self):
        assert md_to_html("温度 42.1 ℃ 超出规格") == "<p>温度 42.1 ℃ 超出规格</p>"


class TestRealReportSnippet:
    """与 _generate_markdown 输出形态对齐的端到端片段（结构完备性）。"""

    MD = (
        "# 批生产记录合规检查报告\n"
        "\n"
        "**文件名**：`test.pdf`\n"
        "\n"
        "---\n"
        "\n"
        "## 一、汇总\n"
        "\n"
        "- 🔴 critical：1\n"
        "  - T2101a 进料压力 0.16 MPa 超出规格范围\n"
        "- 🟡 warning：2\n"
        "\n"
        "## 二、明细\n"
        "\n"
        "### 第 1 页\n"
        "\n"
        "温度 42.1 超出规格范围。\n"
    )

    def test_full_structure_rendered(self):
        html = md_to_html(self.MD)
        assert html.startswith("<h1>")
        assert "<h2>一、汇总</h2>" in html
        assert "<h3>第 1 页</h3>" in html
        assert "<hr>" in html
        assert "<p><strong>文件名</strong>：<code>test.pdf</code></p>" in html
        # 嵌套子项
        assert "<li>T2101a 进料压力 0.16 MPa 超出规格范围</li>" in html
        # 闭合配对完整：h1 恰好 1 对、ul 数与闭合数一致
        assert html.count("<h1>") == 1 and html.count("</h1>") == 1
        assert html.count("<ul>") == html.count("</ul>")
        assert html.count("<li>") == html.count("</li>")
        assert html.count("<p>") == html.count("</p>")
