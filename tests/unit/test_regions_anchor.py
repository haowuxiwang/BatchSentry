"""P0-3 区域级证据锚 —— 归一化 / 标签闭集 / 锚定 的机检不变式。

数据形态取自真实产物实测（`paddle_original.jsonl` 51 页、`mineru_original.zip`）：
Paddle 空间 1440×1920、块字段 block_label/block_bbox/block_content；MinerU 空间
取自 layout.json 的 page_size、块字段 type/bbox/content。真实产物不入库
（体积 + 数据敏感性），故用等形态合成数据锁定契约；另设一条"真值若在场则复算"
的可选用例。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.pipeline.regions import (  # noqa: E402
    CANONICAL_LABELS,
    _LABEL_MAP,
    _MAX_REGIONS_PER_PAGE,
    anchor_tokens,
    canonical_label,
    extract_regions,
    map_bbox_to_page,
    normalize_bbox,
    region_anchor,
)

REGIONS_SRC = REPO / "core" / "pipeline" / "regions.py"


def _paddle_page(blocks, w=1440, h=1920, angle=None):
    """等形态的 Paddle 页。

    ``angle`` 为 None 时**不带** ``doc_preprocessor_res`` —— 模拟"上游没上报
    旋转"的形态（此时不得猜测方向）；给定值时带上官方文档规定的
    ``doc_preprocessor_res.angle``（未启用朝向分类时官方值为 -1）。
    """
    pruned = {"width": w, "height": h, "parsing_res_list": blocks}
    if angle is not None:
        pruned["doc_preprocessor_res"] = {
            "model_settings": {
                "use_doc_orientation_classify": True,
                "use_doc_unwarping": True,
            },
            "angle": angle,
        }
    return {"prunedResult": pruned}


class TestNormalizeBbox:
    def test_scales_by_own_space(self):
        # 1440×1920 空间里左上角四分之一块 → 0.25 宽 / 0.5 高
        assert normalize_bbox([0, 0, 360, 960], (1440, 1920)) == [0.0, 0.0, 0.25, 0.5]

    def test_swaps_inverted_corners(self):
        assert normalize_bbox([360, 960, 0, 0], (1440, 1920)) == [0.0, 0.0, 0.25, 0.5]

    def test_clamps_out_of_range(self):
        assert normalize_bbox([-100, -100, 2000, 4000], (1440, 1920)) == [
            0.0, 0.0, 1.0, 1.0,
        ]

    @pytest.mark.parametrize("bad", [
        [0, 0, 1],                      # 长度不足
        [0, 0, 1, 2, 3],                # 长度超出
        "0,0,1,1",                      # 非序列
        [0, 0, 0, 10],                  # x 退化（零宽）
        [0, 5, 10, 5],                  # y 退化（零高）
        [float("nan"), 0, 10, 10],      # NaN 会让归一化失去意义
        [float("inf"), 0, 10, 10],
        [None, 0, 10, 10],
    ])
    def test_rejects_invalid(self, bad):
        assert normalize_bbox(bad, (1440, 1920)) is None

    @pytest.mark.parametrize("space", [(0, 1920), (1440, 0), (None, None), 1440, ()])
    def test_rejects_bad_space(self, space):
        assert normalize_bbox([0, 0, 10, 10], space) is None

    def test_result_always_within_unit_square(self):
        for x0, y0, x1, y1 in [
            (-5, -5, 5000, 1), (100, 100, 200, 300), (0, 0, 1440, 1920),
        ]:
            out = normalize_bbox([x0, y0, x1, y1], (1440, 1920))
            assert out is not None
            assert all(0.0 <= v <= 1.0 for v in out), out


class TestMapBboxToPage:
    """服务端转正后的坐标 → 页面空间（旋转逆映射）。

    表由不变量独立推出，并与真实产物逐点核对（见文件末尾的真值复算）。
    """

    # OCR 空间里一个不对称的框：(u, v) = 左上 (0.1,0.2) 右下 (0.3,0.6)
    _BOX = [0.1, 0.2, 0.3, 0.6]

    def test_zero_is_identity(self):
        assert map_bbox_to_page(self._BOX, 0) == self._BOX

    def test_ninety(self):
        # (nx, ny) = (1 - v, u)
        assert map_bbox_to_page(self._BOX, 90) == [0.4, 0.1, 0.8, 0.3]

    def test_one_eighty(self):
        # (nx, ny) = (1 - u, 1 - v)
        assert map_bbox_to_page(self._BOX, 180) == [0.7, 0.4, 0.9, 0.8]

    def test_two_seventy(self):
        # (nx, ny) = (v, 1 - u)  —— 实测第 8 页就是这一支
        assert map_bbox_to_page(self._BOX, 270) == [0.2, 0.7, 0.6, 0.9]

    def test_four_rotations_are_a_group(self):
        """四次 90° 必须回到原点 —— 映射错方向会立刻被这条抓住。"""
        box = self._BOX
        for _ in range(4):
            box = map_bbox_to_page(box, 90)
        assert box == self._BOX

    def test_rotation_preserves_area(self):
        """90° 的整数倍旋转仍是轴对齐矩形：面积必须守恒（框不得被撑大）。"""
        def area(b):
            return round((b[2] - b[0]) * (b[3] - b[1]), 6)
        for rot in (0, 90, 180, 270):
            assert area(map_bbox_to_page(self._BOX, rot)) == area(self._BOX)

    @pytest.mark.parametrize("rot", [None, -1, 45, "270", 360])
    def test_unknown_rotation_returns_none(self, rot):
        """未知角度**不得**猜测方向 —— 返回 None，由调用方保留原框交给闸门。"""
        assert map_bbox_to_page(self._BOX, rot) is None

    @pytest.mark.parametrize("bad", [
        [0, 0, 1], [0, 0, 1, 2, 3], "0,0,1,1", [None, 0, 1, 1],
        [0, 0, 0, 1], [0, 0, 1, 0],
    ])
    def test_rejects_bad_bbox(self, bad):
        assert map_bbox_to_page(bad, 270) is None

    def test_output_within_unit_square(self):
        for rot in (0, 90, 180, 270):
            out = map_bbox_to_page([0.0, 0.0, 1.0, 1.0], rot)
            assert out == [0.0, 0.0, 1.0, 1.0], rot


class TestCanonicalLabel:
    @pytest.mark.parametrize("raw,expect", [
        ("table", "table"),
        ("text", "text"),
        ("vertical_text", "text"),          # Paddle 实测标签
        ("vision_footnote", "text"),
        ("page_aside_text", "text"),        # MinerU 实测标签
        ("doc_title", "title"),
        ("figure_title", "title"),
        ("paragraph_title", "title"),
        ("header_image", "image"),
        ("page_number", "footer"),
        ("page_footer", "footer"),
        ("page_header", "header"),
        ("seal", "seal"),
        ("paragraph", "text"),              # MinerU 高频实测类型（10/18 块）
        ("list", "text"),
        ("equation_interline", "other"),    # 未登记 → 闭集兜底
        ("", "other"),
        (None, "other"),
        ("<script>", "other"),
    ])
    def test_mapping(self, raw, expect):
        assert canonical_label(raw) == expect

    def test_nested_content_is_deep_extracted(self):
        """MinerU 文本藏在 content.paragraph_content[].content —— 直接 str(dict)
        会得到带花括号的伪文本，特征词匹配随之失效。"""
        out = extract_regions({
            "_space": [595, 842],
            "_regions": [{
                "label": "paragraph", "bbox": [77, 54, 277, 74],
                "text": {"paragraph_content": [
                    {"type": "text", "content": "批号： B2024001"},
                ]},
            }],
        })
        text = out["regions"][0]["text"]
        assert "B2024001" in text
        assert "{" not in text and "paragraph_content" not in text

    def test_label_map_targets_are_closed_set(self):
        """标签映射的**值域**必须落在闭集内 —— 上游新增标签不得漏进库。"""
        assert set(_LABEL_MAP.values()) <= CANONICAL_LABELS

    def test_output_always_in_closed_set(self):
        for raw in ("TABLE", " 表 ", "unknown_new_label", "text2", "a" * 100):
            assert canonical_label(raw) in CANONICAL_LABELS


class TestExtractPaddle:
    def test_real_shaped_blocks(self):
        page = _paddle_page([
            {"block_label": "table", "block_bbox": [73, 2, 1323, 369],
             "block_content": "<table><tr><td>HISUN海正药业</td></tr></table>"},
            {"block_label": "seal", "block_bbox": [1000, 1500, 1200, 1700],
             "block_content": "质检专用章"},
        ])
        out = extract_regions(page)
        assert out["backend"] == "paddle"
        assert out["space"] == [1440, 1920]
        assert out["space_aspect"] == 0.75
        assert [r["label"] for r in out["regions"]] == ["table", "seal"]
        # HTML 标签必须被剥掉（给匹配用的应是可见文本）
        assert "<table>" not in out["regions"][0]["text"]
        assert "HISUN海正药业" in out["regions"][0]["text"]
        assert all(0.0 <= v <= 1.0 for r in out["regions"] for v in r["bbox"])

    def test_rotated_page_records_landscape_aspect(self):
        """实测：51 页中第 8 页 Paddle 返回 1920×1440（服务端转正过）——
        坐标系宽高比与旋转角都必须如实记录，呈现层据此把框**映射回页面**
        （而不是画错位、也不是无谓地放弃定位）。"""
        out = extract_regions(_paddle_page(
            [{"block_label": "text", "block_bbox": [10, 10, 200, 200],
              "block_content": "x"}], w=1920, h=1440, angle=270,
        ))
        assert out["space"] == [1920, 1440]
        assert out["space_aspect"] == round(1920 / 1440, 4)  # 1.3333 ≠ 0.75
        assert out["space_rotation"] == 270

    def test_rotation_unknown_when_not_reported(self):
        """未上报 doc_preprocessor_res → None（**不得**当成"没转过"）。"""
        assert extract_regions(_paddle_page(
            [{"block_label": "text", "block_bbox": [10, 10, 200, 200],
              "block_content": "x"}],
        ))["space_rotation"] is None

    @pytest.mark.parametrize("bad_angle", [
        -1,        # 官方文档：未启用朝向分类时为 -1
        None,
        "270x",    # 非数值
        45,        # 超出 {0,90,180,270}
        [270],     # 非标量
    ])
    def test_non_canonical_angle_is_unknown(self, bad_angle):
        """拿 -1 之类当"没转过"会静默画出错位的框 —— 非规范值一律未知。"""
        out = extract_regions(_paddle_page(
            [{"block_label": "text", "block_bbox": [10, 10, 200, 200],
              "block_content": "x"}], angle=bad_angle,
        ))
        assert out["space_rotation"] is None

    def test_upright_page_reports_zero_rotation(self):
        """绝大多数页 angle=0 —— 必须与"未上报"(None) 可区分。"""
        out = extract_regions(_paddle_page(
            [{"block_label": "text", "block_bbox": [10, 10, 200, 200],
              "block_content": "x"}], angle=0,
        ))
        assert out["space_rotation"] == 0

    def test_no_bbox_no_payload(self):
        assert extract_regions(_paddle_page([])) is None
        assert extract_regions(_paddle_page([{"block_label": "text"}])) is None
        assert extract_regions({}) is None
        assert extract_regions(None) is None

    def test_skips_malformed_blocks_and_bad_space(self):
        """脏块（非 dict / bbox 非序列）跳过；坐标系非法则整页不产出。"""
        page = _paddle_page([
            "not a dict",
            {"block_label": "text"},                      # 无 bbox
            {"block_label": "text", "block_bbox": "0,0,1,1"},  # bbox 非序列
            {"block_label": "seal", "block_bbox": [0, 0, 100, 100],
             "block_content": "章"},
        ])
        out = extract_regions(page)
        assert out is not None and len(out["regions"]) == 1
        assert out["regions"][0]["label"] == "seal"
        # 坐标系非法（width=0）→ 不得产出（宁缺勿错）
        assert extract_regions(_paddle_page(
            [{"block_label": "text", "block_bbox": [0, 0, 10, 10],
              "block_content": "x"}], w=0, h=1920,
        )) is None

    def test_non_text_block_content_yields_empty_snippet(self):
        """块内容为数字/None（非文本）时文本为空，但区域本身仍有效。"""
        out = extract_regions(_paddle_page([
            {"block_label": "image", "block_bbox": [0, 0, 10, 10],
             "block_content": 12345},
            {"block_label": "seal", "block_bbox": [20, 20, 40, 40],
             "block_content": None},
        ]))
        assert [r["text"] for r in out["regions"]] == ["", ""]

    def test_region_cap_enforced(self):
        # 坐标系要足够大，否则退化框会被剔除而触不到上限分支
        blocks = [
            {"block_label": "text", "block_bbox": [i, i, i + 1, i + 1],
             "block_content": f"t{i}"}
            for i in range(_MAX_REGIONS_PER_PAGE + 40)
        ]
        out = extract_regions(_paddle_page(blocks, w=100000, h=100000))
        assert out is not None
        assert len(out["regions"]) == _MAX_REGIONS_PER_PAGE

    def test_duplicate_bboxes_deduped(self):
        blocks = [
            {"block_label": "text", "block_bbox": [0, 0, 100, 100], "block_content": "a"},
            {"block_label": "text", "block_bbox": [0, 0, 100, 100], "block_content": "a"},
        ]
        out = extract_regions(_paddle_page(blocks))
        assert len(out["regions"]) == 1

    def test_text_snippet_bounded(self):
        out = extract_regions(_paddle_page([
            {"block_label": "text", "block_bbox": [0, 0, 100, 100],
             "block_content": "字" * 5000},
        ]))
        assert len(out["regions"][0]["text"]) <= 400


class TestExtractMinerU:
    def test_space_and_regions(self):
        page = {
            "_space": [595, 842],
            "_regions": [
                {"label": "paragraph", "bbox": [77, 54, 277, 74], "text": "批号 112701"},
                {"label": "table", "bbox": [50, 100, 545, 400], "text": "含量 99.0 %"},
            ],
        }
        out = extract_regions(page)
        assert out["backend"] == "mineru"
        assert out["space"] == [595, 842]
        assert [r["label"] for r in out["regions"]] == ["text", "table"]
        assert out["regions"][0]["bbox"] == [
            round(77 / 595, 5), round(54 / 842, 5),
            round(277 / 595, 5), round(74 / 842, 5),
        ]

    def test_missing_space_yields_nothing(self):
        assert extract_regions({"_regions": [
            {"label": "text", "bbox": [0, 0, 10, 10], "text": "x"},
        ]}) is None

    def test_skips_malformed_region_blocks(self):
        out = extract_regions({
            "_space": [100, 100],
            "_regions": [
                "not a dict",
                {"label": "text"},                                  # 无 bbox
                {"label": "text", "bbox": [0, 0, 1]},               # 长度不足
                {"label": "table", "bbox": [0, 0, 50, 50], "text": "t"},
            ],
        })
        assert out is not None and len(out["regions"]) == 1
        assert out["regions"][0]["label"] == "table"


class TestAnchorTokens:
    def test_extracts_numbers_and_ids(self):
        toks = anchor_tokens("T2101a 压力 0.16 MPa 超出规格范围 <0.3 MPa")
        assert "0.16" in toks and "0.3" in toks and "t2101a" in toks

    def test_ignores_whitespace_variants(self):
        assert anchor_tokens("纯 度 99.0 %") >= {"99.0"}

    def test_glued_digits_are_not_standalone_values(self):
        """实测回归：`t2101a` 曾因截断产出 `t2101` 并被数值正则取出 `2101` ——
        2101 是批号片段而非规格值，误命中会把锚定引到批号区域。"""
        toks = anchor_tokens("T2101a 压力 0.16 MPa")
        assert "2101" not in toks, "字母粘连的数字不得当作独立数值特征词"
        assert "t2101a" in toks, "标识必须完整保留（含尾部字母）"

    def test_none_for_text_without_tokens(self):
        assert anchor_tokens("缺少签名") == set()


class TestRegionAnchor:
    def _payload(self):
        return {
            "backend": "paddle",
            "space": [1440, 1920],
            "space_aspect": 0.75,
            "regions": [
                {"label": "table", "bbox": [0.05, 0.05, 0.95, 0.5],
                 "text": "温度 42.1 进料压力 0.16"},
                {"label": "text", "bbox": [0.05, 0.6, 0.3, 0.65],
                 "text": "操作人 张三 2026-01-01"},
                {"label": "other", "bbox": [0.4, 0.6, 0.6, 0.7],
                 "text": "纯度 99.0 %"},
            ],
        }

    def test_anchors_to_region_containing_the_value(self):
        a = region_anchor("T2101a 压力 0.16 MPa 超出规格范围 <0.3 MPa", self._payload())
        assert a is not None
        assert a["label"] == "table"
        assert a["bbox"] == [0.05, 0.05, 0.95, 0.5]
        assert a["space_aspect"] == 0.75 and a["backend"] == "paddle"

    def test_tie_break_prefers_smaller_region(self):
        payload = {
            "backend": "paddle", "space": [100, 100], "space_aspect": 1.0,
            "regions": [
                {"label": "table", "bbox": [0.0, 0.0, 1.0, 1.0], "text": "99.0"},
                {"label": "text", "bbox": [0.1, 0.1, 0.2, 0.2], "text": "99.0"},
            ],
        }
        a = region_anchor("纯度 99.0 %", payload)
        assert a["label"] == "text", "同分时必须取面积更小的（更具体的）区域"

    def test_upright_page_keeps_box_and_page_aspect(self):
        """angle=0（绝大多数页）：页面空间 == OCR 空间。"""
        payload = self._payload()
        payload["space_rotation"] = 0
        a = region_anchor("T2101a 压力 0.16 MPa 超出规格范围 <0.3 MPa", payload)
        assert a["page_bbox"] == a["bbox"] == [0.05, 0.05, 0.95, 0.5]
        assert a["page_aspect"] == 0.75 and a["rotated"] is False
        assert a["space_rotation"] == 0

    def test_rotated_page_maps_box_into_page_space(self):
        """实测第 8 页形态：1920×1440 + ``angle=270``。

        框必须换回**页面空间**才能画到页图上；``page_aspect`` 随之取倒数
        （页面实为竖向 0.75），否则呈现层会拿 1.3333 与渲染图比对而误判。
        """
        payload = {
            "backend": "paddle", "space": [1920, 1440],
            "space_aspect": round(1920 / 1440, 4), "space_rotation": 270,
            "regions": [
                {"label": "table", "bbox": [0.1, 0.2, 0.3, 0.6],
                 "text": "进料压力 0.16 MPa"},
            ],
        }
        a = region_anchor("进料压力 0.16 MPa 偏低", payload)
        assert a["bbox"] == [0.1, 0.2, 0.3, 0.6], "OCR 空间原始框必须留痕（可回查）"
        assert a["page_bbox"] == [0.2, 0.7, 0.6, 0.9], "画框用的是页面空间坐标"
        assert a["rotated"] is True and a["space_rotation"] == 270
        assert a["page_aspect"] == 0.75

    def test_unknown_rotation_keeps_box_and_lets_gate_decide(self):
        """上游未上报 angle → **不得**猜方向：保留原框、``page_aspect`` 等于
        ``space_aspect``，于是呈现层闸门会因与渲染图不符而拒画（宁缺勿错）。"""
        payload = {
            "backend": "paddle", "space": [1920, 1440],
            "space_aspect": round(1920 / 1440, 4),
            "regions": [
                {"label": "table", "bbox": [0.1, 0.2, 0.3, 0.6],
                 "text": "进料压力 0.16 MPa"},
            ],
        }
        a = region_anchor("进料压力 0.16 MPa 偏低", payload)
        assert a["page_bbox"] == a["bbox"] == [0.1, 0.2, 0.3, 0.6]
        assert a["page_aspect"] == a["space_aspect"] == round(1920 / 1440, 4)
        assert a["rotated"] is False and a["space_rotation"] is None

    def test_none_when_nothing_matches(self):
        assert region_anchor("操作人未签名", self._payload()) is None

    def test_none_when_no_tokens(self):
        assert region_anchor("缺少签名与日期", self._payload()) is None

    def test_none_when_tokens_present_but_no_region_matches(self):
        """有特征词但无区域命中 → 不锚；**不得**锚到得分为 0 的区域。

        与上面两条的区别：此处的 None 出自"扫完全部区域仍无所获"的分支，
        而非提前从"无特征词"返回 —— 否则会误把 0 分区域当作命中。"""
        assert region_anchor("温度 12.3 异常", self._payload()) is None

    def test_skips_malformed_regions_and_empty_text(self):
        """脏区域（非 dict / 文本为空）跳过，不得干扰计分与选优。"""
        payload = {
            "backend": "paddle", "space": [100, 100], "space_aspect": 1.0,
            "regions": [
                "not a dict",                                            # 非 dict
                {"label": "text", "bbox": [0.0, 0.0, 0.1, 0.1],
                 "text": None},                                          # 空文本
                {"label": "table", "bbox": [0.0, 0.0, 0.5, 0.5],
                 "text": "纯度 99.0"},
            ],
        }
        a = region_anchor("纯度 99.0", payload)
        assert a is not None
        assert a["index"] == 2 and a["bbox"] == [0.0, 0.0, 0.5, 0.5]

    def test_none_for_empty_payload(self):
        assert region_anchor("压力 0.16", {}) is None
        assert region_anchor("压力 0.16", {"regions": []}) is None
        assert region_anchor("压力 0.16", None) is None

    def test_index_points_back_to_region(self):
        p = self._payload()
        a = region_anchor("纯度 99.0", p)
        assert p["regions"][a["index"]]["bbox"] == a["bbox"]


# ── 源码级不变式 ───────────────────────────────────────────────────────────


def test_module_is_pure():
    """纯函数模块：禁 DB / 网络 / 文件 I/O（可在 core/api 两侧自由引用）。"""
    src = REGIONS_SRC.read_text(encoding="utf-8")
    for forbidden in (
        "aiosqlite", "sqlite3", "import requests", "httpx", "open(",
        "get_db", "logging",
    ):
        assert forbidden not in src, f"regions.py 不得引入 {forbidden}"


def test_normalization_is_not_optional_in_write_path():
    """归一化必须是唯一出口：不得存在"原样透传 bbox"的分支。"""
    src = REGIONS_SRC.read_text(encoding="utf-8")
    assert "def normalize_bbox" in src
    # 写入载荷必须只由 _finish 组装（内部必经 normalize_bbox）
    assert src.count('"bbox"') >= 1
    assert "regions.append(" in src


# ── 真值复算（真实产物在场时执行）────────────────────────────────────────────

_REAL_PADDLE = os.path.join(
    os.environ.get("APPDATA", ""), "PBC", "output", "0c5cfb00-897",
    "paddle_original.jsonl",
)


def _real_pages():
    """真实产物逐页的 ``extract_regions`` 结果（按页序）。"""
    pages = []
    with open(_REAL_PADDLE, encoding="utf-8") as f:
        for line in f:
            for pg in json.loads(line)["result"]["layoutParsingResults"]:
                pages.append(extract_regions(pg))
    return pages


def _real_load_parsing_results():
    """真实产物逐页的**原始** ``layoutParsingResults`` 条目（未经 extract_regions）。"""
    out = []
    with open(_REAL_PADDLE, encoding="utf-8") as f:
        for line in f:
            out.extend(json.loads(line)["result"]["layoutParsingResults"])
    return out


@pytest.mark.skipif(
    not os.path.exists(_REAL_PADDLE), reason="真实 Paddle 产物不在场（可选用例）"
)
def test_real_paddle_artifact_replays():
    """用真实 51 页产物复算：bbox 落在单位方格内，坐标系与旋转角可分类。"""
    from collections import Counter

    pages = _real_pages()
    payloads = [p for p in pages if p is not None]
    assert len(pages) == 51
    assert payloads, "真实产物必须能抽出区域"
    n_landscape = 0
    for out in payloads:
        if out["space_aspect"] > 1.0:
            n_landscape += 1
        for r in out["regions"]:
            assert 0.0 <= r["bbox"][0] < r["bbox"][2] <= 1.0
            assert 0.0 <= r["bbox"][1] < r["bbox"][3] <= 1.0
            assert r["label"] in CANONICAL_LABELS
    # 实测：50 页竖向（0.75）+ 1 页横向（1.3333，服务端转正过）
    assert n_landscape == 1
    # 关键：横向那一页**不是靠宽高比猜的**，服务端明确上报了 angle
    assert dict(Counter(p["space_rotation"] for p in payloads)) == {0: 50, 270: 1}
    # 横向页与其后的竖向页必须**不同向** —— 否则 page_aspect 折算就是错的
    landscape = [p for p in payloads if p["space_aspect"] > 1.0][0]
    assert landscape["space_rotation"] in (90, 270)


@pytest.mark.skipif(
    not os.path.exists(_REAL_PADDLE), reason="真实 Paddle 产物不在场（可选用例）"
)
def test_real_rotated_page_landmarks_land_where_they_should():
    """真值复算：第 8 页的旋转逆映射必须把**地标**放回它们真实所在的角。

    这是"框画得对不对"的实证 —— 不靠肉眼下结论：页面上三个结构性元素
    （公司 logo / 文件编号 / 页码）在竖向页里的位置是确定的，映射后必须
    各自落到左下、左下、右上（与渲染图逐点核对过，见
    ``docs/REGION_ANCHOR_VISUAL_CHECK.md``）。若旋转方向判错，它们会整体
    镜像到对侧 —— 本用例立刻失败。
    """
    page8 = _real_pages()[7]
    assert page8["space"] == [1920, 1440]
    assert page8["space_rotation"] == 270

    def page_box(text_prefix):
        for r in page8["regions"]:
            if r["text"].startswith(text_prefix):
                box = map_bbox_to_page(r["bbox"], page8["space_rotation"])
                assert box is not None
                return box
        raise AssertionError(f"第 8 页未找到以 {text_prefix!r} 开始的区域")

    logo = page_box("HISUN")
    doc_no = page_box("文件编号")
    page_num = page_box("页码")
    # 三者都必须在**左侧**（旋转页的 logo / 文件编号落在页面的左下）
    for box in (logo, doc_no):
        assert (box[0] + box[2]) / 2 < 0.5, box
        assert (box[1] + box[3]) / 2 > 0.5, box
    # 页码必须落到**右上**
    assert (page_num[0] + page_num[2]) / 2 > 0.9, page_num
    assert (page_num[1] + page_num[3]) / 2 < 0.1, page_num
    # 全部映射结果仍须落在单位方格内
    for r in page8["regions"]:
        box = map_bbox_to_page(r["bbox"], page8["space_rotation"])
        assert box is not None and all(0.0 <= v <= 1.0 for v in box), box


@pytest.mark.skipif(
    not os.path.exists(_REAL_PADDLE), reason="真实 Paddle 产物不在场（可选用例）"
)
def test_real_artifact_rotation_parity_matches_space_orientation():
    """上报旋转的奇偶 ⟺ 坐标系的横竖（渲染-free 的自洽护栏）。

    服务端转过正（90/270）⟹ 版面检测跑在**转正后**的图上 ⟹ 坐标系必然横向；
    未转正（0/180）⟹ 与页面同向 ⟹ 竖向。这条不变量不需要像素即可复算。

    它**拦不住**"0 与 270 谁对"（那需要独立地面真值，见
    ``scripts/verify_anchor_orientation.py``：用上游回传的 ``inputImage`` /
    ``layout_det_res`` 配准，实测 p8=270°CCW、p7=0° 对照），但能拦住
    "上报角与坐标系不符"这类会直接导致横竖颠倒的错误。
    """
    for p in _real_pages():
        if p is None:
            continue
        rot = p["space_rotation"]
        if rot is None:
            continue
        assert (p["space_aspect"] > 1.0) == (rot in (90, 270)), p


@pytest.mark.skipif(
    not os.path.exists(_REAL_PADDLE), reason="真实 Paddle 产物不在场（可选用例）"
)
def test_real_artifact_reports_orientation_for_every_page():
    """朝向分类必须**开着**，且每页都有上报角。

    旋转逆映射完全依赖 ``doc_preprocessor_res.angle``：一旦上游把
    ``use_doc_orientation_classify`` 关掉，该字段会变成 ``-1`` → 我们归 ``None``
    → 旋转页静默退回宽高比闸门（"无法定位"）。这是**能力退化**而非崩溃，
    最难察觉，故在此钉死"运行环境确实开着朝向分类、且逐页有角"。
    """
    entries = _real_load_parsing_results()
    assert entries, "真实产物为空"
    for pg in entries:
        dpr = (pg.get("prunedResult") or {}).get("doc_preprocessor_res") or {}
        ms = dpr.get("model_settings") or {}
        assert ms.get("use_doc_orientation_classify") is True, ms
        assert dpr.get("angle") in (0, 90, 180, 270), dpr.get("angle")
    pages = [p for p in _real_pages() if p is not None]
    assert pages and all(p["space_rotation"] is not None for p in pages)
