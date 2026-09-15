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
    normalize_bbox,
    region_anchor,
)

REGIONS_SRC = REPO / "core" / "pipeline" / "regions.py"


def _paddle_page(blocks, w=1440, h=1920):
    return {"prunedResult": {"width": w, "height": h, "parsing_res_list": blocks}}


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
        """实测：51 页中第 8 页 Paddle 返回 1920×1440（服务端旋转）——
        坐标系宽高比必须如实记录，呈现层据此放弃高亮而不是画错位的框。"""
        out = extract_regions(_paddle_page(
            [{"block_label": "text", "block_bbox": [10, 10, 200, 200],
              "block_content": "x"}], w=1920, h=1440,
        ))
        assert out["space"] == [1920, 1440]
        assert out["space_aspect"] == round(1920 / 1440, 4)  # 1.3333 ≠ 0.75

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


@pytest.mark.skipif(
    not os.path.exists(_REAL_PADDLE), reason="真实 Paddle 产物不在场（可选用例）"
)
def test_real_paddle_artifact_replays():
    """用真实 51 页产物复算：全部 bbox 落在单位方格内，宽高比可分类。"""
    payloads, n_landscape, n_pages = [], 0, 0
    with open(_REAL_PADDLE, encoding="utf-8") as f:
        for line in f:
            for pg in json.loads(line)["result"]["layoutParsingResults"]:
                n_pages += 1
                out = extract_regions(pg)
                if out is None:
                    continue
                payloads.append(out)
                if out["space_aspect"] > 1.0:
                    n_landscape += 1
                for r in out["regions"]:
                    assert 0.0 <= r["bbox"][0] < r["bbox"][2] <= 1.0
                    assert 0.0 <= r["bbox"][1] < r["bbox"][3] <= 1.0
                    assert r["label"] in CANONICAL_LABELS
    assert n_pages == 51
    assert payloads, "真实产物必须能抽出区域"
    # 实测：50 页竖向（0.75）+ 1 页横向（1.3333，服务端旋转）
    assert n_landscape == 1
