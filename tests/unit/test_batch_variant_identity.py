"""B1-10 跨页批号：OCR 变体不得被当成"不同批号"。

定位依据（真实 51 页批记录 `丝裂霉素提取批记录.pdf`，job `6f80145a-47f`）：
同一批号 `1127011N 250101` 被判成 **5 组不同批号**（critical），逐组看却是同一批号的
OCR 写法：

| 页面 | 原始读数 | 归一化 | 噪声来源 |
|---|---|---|---|
| p1/p2/p24/… | `1127011N 250101` | `1127011N250101` | 空格分隔 |
| p22 | `1127011N ^4 250101 -07` | `1127011N4250101-07` | 页面上 `N` 后的**角标**被读成数字 `4` |
| p5 | `11270111/250101-02` | `11270111250101-02` | `N `被读成 `1/`（N→1 单字符） |
| p13 | `1127011/250101-05` | `1127011250101-05` | `N `整段丢失 |
| p3/p4/…（14 页） | `112701` | `112701` | LLM 只抽出前缀，**尾段被丢掉** |
| p43 | `1127011N` | `1127011N` | 表格单元格把批号切成两格，LLM 只取第一格 |

**原图核验（已做）**：渲染 p22/p43 原页可见 `产品批号：1127011N ' 250101 -07`，
`^4` 是角标；p43 的第二格 `250101-06` 与第一格同属一行。⇒ 上述全部为**同一批号**。

p50 例外：原页可见 `清场批号： 1127011N` + 蓝字 `241203`（整页无 `250101`），是
**真实的另一个值**（清场记录引用他批），因此仍报 critical —— 这不是变体，是差异。

不变式：
* 归一化是**对称、无顺序依赖**的（旧实现按长度吸收 ⇒ 组标签落在噪声串上）；
* **日期段不同 ⇒ 永不合并**（`241203` vs `250101`）；
* 双方都无日期段时**只认前缀包含、不认编辑距离**（保护 `B202201`/`B202202`）；
* 投票不决定性 ⇒ 一条都不吸收（fail-open，宁可多报）。
"""

from __future__ import annotations

import pytest

from core.rules.parsing import (
    _normalize_batch_no,
    batch_keys_compatible,
    parse_batch_key,
)
from core.rules.rule_doc import _BATCH_VOTE_MIN_SHARE, _check_batch_consistency


def _pages(readings: list[str], start: int = 1) -> list[dict]:
    return [
        {"page": start + i, "page_info": {"batch_no": v}}
        for i, v in enumerate(readings)
    ]


# ---------------------------------------------------------------------------
# 1) 批号身份解析
# ---------------------------------------------------------------------------


class TestParseBatchKey:
    @pytest.mark.parametrize("raw,expect_prefix,expect_date", [
        ("1127011N250101", "1127011N", "250101"),
        # 角标被读成数字 ⇒ 末尾整段数字是 4250101，后 6 位才是日期段
        ("1127011N4250101", "1127011N4", "250101"),
        ("11270111250101", "11270111", "250101"),
        ("1127011250101", "1127011", "250101"),
        ("1127011N241203", "1127011N", "241203"),
        ("1071011N260501", "1071011N", "260501"),
    ])
    def test_dated_readings(self, raw, expect_prefix, expect_date):
        assert parse_batch_key(raw) == (expect_prefix, expect_date)

    @pytest.mark.parametrize("raw", [
        "112701",        # 末尾 6 位 112701 → 月 27 非法
        "1127011N",      # 末尾数字只有 2 位
        "B2024001",      # 024001 → 月 40 非法
        "B2025001",      # 025001 → 月 50 非法
        "",              # 空
    ])
    def test_undated_readings_keep_full_prefix(self, raw):
        assert parse_batch_key(raw) == (raw, None)


# ---------------------------------------------------------------------------
# 2) 兼容关系（对称性 / 日期闸 / 编辑距离闸）
# ---------------------------------------------------------------------------


class TestCompatibility:
    def test_same_date_and_prefix_relation_is_compatible(self):
        # 尾段截断读法（14 页只读出 `112701`）
        assert batch_keys_compatible("112701", "1127011N250101")
        assert batch_keys_compatible("1127011N", "1127011N250101")
        # 角标数字粘进来
        assert batch_keys_compatible("1127011N4250101", "1127011N250101")
        # 前缀被少读一位
        assert batch_keys_compatible("1127011250101", "1127011N250101")
        # 单字符近邻 N↔1（弱证据，调用方需加投票闸）
        assert batch_keys_compatible("11270111250101", "1127011N250101")

    def test_relation_is_symmetric(self):
        pairs = [
            ("112701", "1127011N250101"),
            ("1127011N", "1127011N250101"),
            ("1127011N4250101", "1127011N250101"),
            ("11270111250101", "1127011N250101"),
            ("B202201", "B202202"),
            ("1127011N241203", "1127011N250101"),
        ]
        for a, b in pairs:
            assert batch_keys_compatible(a, b) == batch_keys_compatible(b, a), (
                f"{a!r} / {b!r} 兼容关系不对称"
            )

    def test_relation_is_symmetric_in_structural_only_mode(self):
        """Tier 1 用的是 `allow_edit_distance=False` 档 —— 它**也必须对称**，
        否则"谁当锚"会改变结论（旧实现的顺序依赖正是这么来的）。
        注意默认档有编辑距离兜底，能掩盖单向前缀判据，所以必须单独钉这一档。"""
        pairs = [
            ("112701", "1127011N250101"),
            ("1127011N", "1127011N250101"),
            ("1127011N4250101", "1127011N250101"),
            ("1127011250101", "1127011N250101"),
            ("11270111250101", "1127011N250101"),   # 结构档应为 False（弱证据）
        ]
        for a, b in pairs:
            assert (batch_keys_compatible(a, b, allow_edit_distance=False)
                    == batch_keys_compatible(b, a, allow_edit_distance=False)), (
                f"{a!r} / {b!r} 结构档兼容关系不对称"
            )

    def test_different_date_segment_is_never_merged(self):
        """日期段是独立佐证：p50 的 `241203` 与其余页的 `250101` 不同 ⇒ 绝不合并。"""
        assert not batch_keys_compatible("1127011N241203", "1127011N250101")
        assert not batch_keys_compatible(
            "1127011N241203", "1127011N250101", allow_edit_distance=True
        )

    def test_dateless_pairs_never_use_edit_distance(self):
        """`B202201`/`B202202` 是**各 1 页的真实混批** —— 无日期段时编辑距离
        会把它们并掉，必须在关系层就否决（不能只靠投票闸）。"""
        assert not batch_keys_compatible("B202201", "B202202")
        assert not batch_keys_compatible(
            "B202201", "B202202", allow_edit_distance=True
        )

    def test_structural_only_mode_rejects_single_char_neighbour(self):
        """Tier 1（结构档）不得认单字符近邻 —— 它靠投票闸兜底，属弱证据。"""
        assert not batch_keys_compatible(
            "11270111250101", "1127011N250101", allow_edit_distance=False
        )
        # 但前缀关系仍成立
        assert batch_keys_compatible(
            "112701", "1127011N250101", allow_edit_distance=False
        )

    def test_short_bare_fragment_not_used_to_swallow(self):
        """残串短于 `_BATCH_MIN_PREFIX_LEN` 时不许反向吞掉完整串。"""
        assert not batch_keys_compatible("1127", "1127011N250101")


# ---------------------------------------------------------------------------
# 3) 验收：p1 原 finding 的 5 个读数 ⇒ 不产出 critical
# ---------------------------------------------------------------------------


class TestAcceptanceP1Variants:
    def test_p1_five_variants_produce_no_finding(self):
        """验收条目：p1 `batch_inconsistency` 的 5 个原始读数必须被认成同一批号。"""
        readings = [
            "1127011N 250101 -07",
            "1127011N",
            "1127011",
            "1127011N 250101",
            "1127011N^4 250101",
        ]
        assert _check_batch_consistency(_pages(readings)) == []


# ---------------------------------------------------------------------------
# 4) 真实 51 页回放（读数内联自 job 6f80145a-47f 的 page_cache）
# ---------------------------------------------------------------------------

# (page, raw batch_no) —— 46 页有批号的页，按原 job 逐页抄录
_REAL_51P: list[tuple[int, str]] = [
    (1, "1127011N 250101"), (2, "1127011N 250101"), (3, "112701"), (4, "112701"),
    (5, "11270111/250101-02"), (6, "1127011N250101-03"), (7, "112701"),
    (8, "112701"), (9, "1127011N250101-04"), (10, "112701"),
    (11, "1127011N·250101-04"), (12, "112701"), (13, "1127011/250101-05"),
    (14, "1127011N250101 -05"), (15, "1127011N 250101-05"), (16, "112701"),
    (17, "1127011N250101 -06"), (19, "112701"), (20, "1127011N 250101 -06"),
    (21, "1127011N250101-07"), (22, "1127011N ^4 250101 -07"),
    (23, "1127011N250101-07"), (24, "1127011N 250101"), (25, "112701"),
    (27, "112701"), (28, "1127011N250101"), (29, "1127011N250101"),
    (30, "1127011N250101"), (31, "1127011N250101"), (32, "1127011N250101"),
    (33, "1127011N250101"), (35, "1127011N250101"), (36, "1127011N 250101"),
    (37, "112701"), (38, "112701"), (39, "1127011N250101"),
    (41, "1127011N 250101-04"), (42, "1127011N250101"), (43, "1127011N"),
    (44, "112701"), (45, "1127011N 250101-07"), (46, "112701"),
    (48, "1127011N.250101"), (49, "1127011N 250101"), (50, "1127011N 241203"),
    (51, "1127011N250101"),
]


def _real_pages() -> list[dict]:
    return [{"page": p, "page_info": {"batch_no": v}} for p, v in _REAL_51P]


class TestReal51PageReplay:
    def test_groups_collapse_from_five_to_two(self):
        """修前：5 组（19 个原始读法、0 个被吸收）。修后：2 组，45 页归并入基准。"""
        findings = _check_batch_consistency(_real_pages())
        assert len(findings) == 1
        desc = findings[0]["description"]
        assert "检测到 2 组不同批号" in desc
        # 基准组标签必须是**最受支持的读数**，而不是噪声串
        assert "1127011N250101(" in desc
        assert "1127011N4250101(" not in desc

    def test_only_genuine_difference_survives(self):
        """唯一存活的差异是 p50（原图核验为真实的另一个值 `241203`），
        且它必须单列并带页号，便于复核员定位。"""
        findings = _check_batch_consistency(_real_pages())
        desc = findings[0]["description"]
        assert "1127011N241203(第50页)" in desc
        assert findings[0]["severity"] == "critical"

    def test_absorbed_aliases_are_named(self):
        """不静默归一：描述给人看前 3 个，**完整别名表**放进机器可读的 ocr_text。"""
        findings = _check_batch_consistency(_real_pages())
        ocr_text = findings[0]["ocr_text"]
        assert "merged_aliases=" in ocr_text
        for alias in ("112701", "1127011N", "1127011N4250101", "11270111250101",
                      "1127011250101"):
            assert alias in ocr_text, f"被归并的别名 {alias!r} 未留痕"
        # 人类可读描述至少点名若干别名
        assert "1127011N4250101" not in findings[0]["description"]
        assert "112701" in findings[0]["description"]

    def test_normalization_is_what_collapses_them(self):
        """反证护栏：若不归一（逐字比较），同样输入必然 >2 组 —— 证明是归一/
        结构聚类在起作用，而不是数据本来就只有 2 组。"""
        raw = {r for _, r in _REAL_51P}
        assert len(raw) == 19  # 19 个原始读法
        norm = {_normalize_batch_no(r).split("-")[0] for r in raw}
        assert len(norm) > 2   # 仅做归一化仍剩 >2 个核心串（需结构聚类才能并到 2）


# ---------------------------------------------------------------------------
# 5) 保守性（fail-open）—— 绝不吃掉真实混批
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_undated_mixed_batch_still_reported(self):
        findings = _check_batch_consistency(_pages(["B202201", "B202202"]))
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"

    def test_dated_mixed_batch_with_single_char_prefix_not_swallowed(self):
        """3 vs 3 平票：前缀差 1 字符但成不了多数 ⇒ 不做 Tier 2 吸收。"""
        findings = _check_batch_consistency(
            _pages(["1127011N250101"] * 3 + ["1127012N250101"] * 3)
        )
        assert len(findings) == 1
        assert "检测到 2 组不同批号" in findings[0]["description"]

    def test_same_batch_tie_is_reported_fail_open(self):
        """已知取舍：同一批号的两种读法各 2 页（平票）时**仍报** —— 宁可多报。
        投票闸按 `_BATCH_VOTE_MIN_SHARE` 判定，低于阈值一律不吸收。"""
        findings = _check_batch_consistency(
            _pages(["1127011N250101"] * 2 + ["11270111250101"] * 2)
        )
        assert len(findings) == 1
        assert "检测到 2 组不同批号" in findings[0]["description"]

    def test_vote_threshold_is_still_the_gate(self):
        """把同一批号的变体提到阈值以上 ⇒ 归并生效（证明闸门真的在起作用）。"""
        n_major = int(0.6 / (1 - 0.6)) + 2  # 保证 major 占比 ≥ 0.6
        readings = ["1127011N250101"] * n_major + ["11270111250101"]
        assert len(readings) and n_major / len(readings) >= _BATCH_VOTE_MIN_SHARE
        assert _check_batch_consistency(_pages(readings)) == []
