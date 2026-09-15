"""`scripts/verify_anchor_orientation.py` 单测 —— 核验工具自身的判据。

为什么必须测工具：它是"旋转方向对不对"的**唯一独立判据**。工具判错 =
给出一份虚假的"已验证"结论 —— 比没有工具更糟。故用合成图案（已知旋转）
确定性地检验它：

* 合成图与其 0/90/180/270 旋转 → 工具必须报出正确的 `best` 与 `axis`；
* 180° 自对称图案 → 工具必须**如实**报"方向不可分辨"，而不是硬给一个胜者；
* `jsonl` 解析：`-1`/缺失/非法 angle 一律归 `None`（不得当作"没转"）。

`scripts/` 非包 → 用 importlib 从文件路径加载（沿用 `test_release_gate.py` 的惯例）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")   # 判据依赖 numpy；缺失则跳过（不伪绿）

_TOOL = Path(__file__).resolve().parents[2] / "scripts" / "verify_anchor_orientation.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_anchor_orientation", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify_anchor_orientation"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


tool = _load()


def _pattern(h: int = 240, w: int = 160) -> "np.ndarray":
    """竖向"纸面 + 非对称文字行"合成图（uint8，0=墨 / 255=纸）。

    非对称很关键：0° 与 180° 必须能被区分开，才谈得上"方向可分辨"。
    """
    img = np.full((h, w), 255, dtype=np.uint8)
    bands = [(12, 26, 10, 150), (40, 52, 10, 90), (70, 84, 30, 150),
             (110, 122, 10, 120), (150, 160, 60, 150), (200, 214, 10, 70)]
    for y0, y1, x0, x1 in bands:
        img[y0:y1, x0:x1] = 0
    img[30:36, 120:126] = 0          # 刻意加一个小不对称记号
    return img


def _mask(img) -> "np.ndarray":
    return img <= 140


class TestRegisterRotation:
    """合成图案 → 工具必须报出正确的旋转与轴。"""

    @pytest.mark.parametrize("k,expect_rot", [(0, 0), (1, 90), (2, 180), (3, 270)])
    def test_identifies_applied_rotation(self, k, expect_rot):
        """参考图 = 输入图旋转 k*90°CCW ⟹ 工具应报 best == k*90。"""
        src = _pattern()
        ref = _mask(np.rot90(src, k))
        res = tool._register(src, ref)
        assert res["best"] == expect_rot, res
        assert res["axis"] == expect_rot % 180, res

    def test_axis_is_decisive_for_transposed_reference(self):
        """参考图为 90° 转置时，轴必须判为 90（而非 0）—— 这是致命失败的拦截点。"""
        src = _pattern()
        res = tool._register(src, _mask(np.rot90(src, 1)))
        assert res["axis"] == 90
        assert res["axis_margin"] is not None and res["axis_margin"] >= 1.5, res

    def test_scores_cover_all_four_rotations(self):
        res = tool._register(_pattern(), _mask(_pattern()))
        assert sorted(res["scores"], key=int) == ["0", "90", "180", "270"]

    def test_asymmetric_content_resolves_direction(self):
        """非对称图案：同轴内 0 与 180 必须能拉开差距。"""
        src = _pattern()
        res = tool._register(src, _mask(src))
        assert res["direction_is_resolvable"] is True, res
        assert res["best"] == 0, res


class TestRegisterHonesty:
    """对称内容必须"认怂"，不得伪造胜者。"""

    @staticmethod
    def _symmetric(h: int = 240, w: int = 160) -> "np.ndarray":
        """**构造上保证** 180° 自对称的图案。

        不用"看起来对称"的手写格线 —— 格线起点/奇偶很容易让镜像落空
        （第一版就是这么写错的，结果被判"可分辨"）。直接取
        ``min(img, R180(img))``：由构造满足 ``R180(x) == x``，无歧义。
        """
        img = np.full((h, w), 255, dtype=np.uint8)
        for y in range(15, h - 15, 37):
            img[y:y + 3, 18:w - 18] = 0
        for x in range(15, w - 15, 37):
            img[18:h - 18, x:x + 3] = 0
        img[40:60, 30:70] = 0                      # 任意斑块
        return np.minimum(img, np.rot90(img, 2))   # 取与自身 180° 镜像的并集

    def test_symmetric_helper_is_really_symmetric(self):
        """先证明测试夹具本身成立，否则下面的"认怂"断言毫无意义。"""
        src = self._symmetric()
        assert np.array_equal(src, np.rot90(src, 2))

    def test_symmetric_content_reports_unresolvable_direction(self):
        src = self._symmetric()
        res = tool._register(src, _mask(src))
        assert res["axis"] == 0, res
        assert res["direction_is_resolvable"] is False, res
        # 轴仍然（且必须）被判对：认怂只在"方向"这一级
        assert res["axis_margin"] is not None and res["axis_margin"] >= 1.5, res

    def test_symmetric_rotation_180_still_same_axis(self):
        src = self._symmetric()
        res = tool._register(src, _mask(np.rot90(src, 2)))
        assert res["axis"] == 0, res


class TestRotationTableIsCanonical:
    def test_rotations_are_the_canonical_four(self):
        assert tool._ROTATIONS == (0, 90, 180, 270)

    def test_axis_margin_threshold_is_not_trivially_permissive(self):
        """阈值本身要被守住：放松到 1.0 会让任何噪声都"通过"。"""
        assert tool._DEFAULT_MARGIN >= 1.2
        assert tool._DIRECTION_MARGIN >= 1.1


class TestLoadPages:
    """产物 → (上报角, 回传图 URL) 的解析。"""

    @staticmethod
    def _write(tmp_path: Path, angle, imgs=True) -> Path:
        pruned = {"doc_preprocessor_res": {"angle": angle}, "parsing_res_list": []}
        entry = {"prunedResult": pruned, "inputImage": "http://x/in.jpg"}
        if imgs:
            entry["outputImages"] = {"layout_det_res": "http://x/lay.jpg"}
        p = tmp_path / "paddle_original.jsonl"
        p.write_text(json.dumps({"result": {"layoutParsingResults": [entry]}}),
                     encoding="utf-8")
        return p

    @pytest.mark.parametrize("angle,expect", [(0, 0), (90, 90), (180, 180), (270, 270)])
    def test_canonical_angles_pass_through(self, tmp_path, angle, expect):
        pages = tool._load_pages(self._write(tmp_path, angle))
        assert pages[0]["angle"] == expect

    @pytest.mark.parametrize("angle", [-1, 45, 360, None, "abc", [270]])
    def test_non_canonical_angle_becomes_none(self, tmp_path, angle):
        """`-1`（未启用朝向分类）绝不能当"没转" —— 那会静默画出错位的框。"""
        pages = tool._load_pages(self._write(tmp_path, angle))
        assert pages[0]["angle"] is None
        assert pages[0]["raw_angle"] == angle

    def test_urls_are_read(self, tmp_path):
        pages = tool._load_pages(self._write(tmp_path, 0))
        assert pages[0]["input_url"] == "http://x/in.jpg"
        assert pages[0]["layout_url"] == "http://x/lay.jpg"

    def test_missing_output_images_is_tolerated(self, tmp_path):
        pages = tool._load_pages(self._write(tmp_path, 0, imgs=False))
        assert pages[0]["layout_url"] is None


class TestReportIsJsonSafe:
    """报告的数值可能来自 numpy —— 必须可序列化（曾因此崩在收尾处）。"""

    def test_numpy_scalars_serialize(self):
        res = tool._register(_pattern(), _mask(_pattern()))
        res["np_bool"] = np.bool_(True)
        res["np_int"] = np.int64(3)
        out = json.dumps(
            {"pages": [res]}, ensure_ascii=False, indent=2,
            default=lambda o: o.item() if hasattr(o, "item") else str(o),
        )
        assert '"np_bool": true' in out
        assert '"np_int": 3' in out


class TestDownloadIsIdempotent:
    """已有在场缓存不得重复下载（离线复算依赖它）。"""

    def test_existing_cache_is_reused(self, tmp_path, monkeypatch):
        dest = tmp_path / "cached.jpg"
        dest.write_bytes(b"already-here")
        called = []

        def _boom(*a, **k):        # 任何真实网络调用都应被阻止
            called.append(a)
            raise AssertionError("不应发起网络请求")

        monkeypatch.setattr(tool.urllib.request, "urlopen", _boom)
        assert tool._download("http://example.invalid/x.jpg", dest) is True
        assert called == []

    def test_empty_url_returns_false(self, tmp_path):
        assert tool._download("", tmp_path / "n.jpg") is False
