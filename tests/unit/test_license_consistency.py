"""许可证的**单一真值**：`LICENSE` 是法律文本，`package.json` 是元数据，两者必须同源。

**由来（2026-10-28 实测）**：仓库自建立起**没有** `LICENSE` 文件，而 `package.json` 里写着
`"license": "ISC"` —— 那是 npm init 的默认占位符，从没人核过。后果是三重的：

1. **公开仓库 + 无 LICENSE ⇒ 默认保留所有权利**。外人能读、不能合法用，连 fork 都算侵权。
2. **`package.json` 声明了 ISC** ⇒ 任何读元数据的工具（electron-builder、依赖扫描器、
   企业 OSPO 的自动化）都会把本产品登记成 ISC —— 与"没有许可证"是**两个不同的法律状态**，
   而**没有任何机检**能发现它们不一致。
3. 这正是本项目在 R126 刚删掉的那类负债：**一处声明 + 一处实现 = 两个真值**，
   而声明方**没有消费者校验点** ⇒ 可以长期漂移而无人发现。

**本文件锁的不变式**（改任何一条都必须同时改这里，否则判红）：

- A. `LICENSE` 存在，且**正文逐字节等于**登记在 `_CANONICAL_SHA256` 里的官方文本
  （当前：AGPL-3.0-only，LF 归一化后比对 sha256）。
- B. `package.json` 的 `license` 与 `LICENSE` 正文反推出的 SPDX 标识符**完全相同**。
- C. `NOTICE` 存在，且**明确写出授权范围不含第三方数据/商标**。
- D. 仓库里**不存在**与之矛盾的旧声明（`ISC` / `MIT` / `BSD` 出现在 license 字段）。

⚠️ **许可证变更史（2026-10-28）**：先按 Apache-2.0 落地，同日改为 **AGPL-3.0-only** ——
因为 `PyMuPDF` 是 AGPL 且被冻进产物，改协议比改 6 处渲染调用点更快闭环。
换证只动了 `LICENSE` / `NOTICE` / 两处 `license` 字段与本文件，**没有**任何断言被删。

⚠️ **刻意不做的两件事**（写清楚，免得下一轮有人"顺手补上"）：

1. **不给 `LICENSE` 加版权行**。要写归属就写 `NOTICE`。理由是 A 条要能机检
   "正文没被改动" —— 一旦允许在正文末尾追加，sha256 断言就得退化成前缀匹配，
   而前缀匹配挡不住"在中间改一句话"。
2. **不往 `pyproject.toml` 加 `[project]` 表**。本仓库不以 Python 包分发（产物是
   PyInstaller 冻结 exe），`[project].license` 会是一个**没有消费者**的声明 ——
   与 R126 删掉的 `RuleSpec.severity` 完全同型。缺声明是"没有"，不是"矛盾"。
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_LICENSE = _ROOT / "LICENSE"
_NOTICE = _ROOT / "NOTICE"
_PACKAGE_JSON = _ROOT / "package.json"

# 官方许可证文本的**外部真值**：spdx → (LF 归一化后的 sha256, 字节数)。
# 只登记我们**真的可能用**的那几份，登记一条是一行、可评审。
# ⚠️ 换许可证时必须同步登记 —— 否则 A 组只校验"非空 + 可识别"，**正文完整性无人校验**
#    （测试会 skip，不是通过；这是刻意保留的已知缺口）。
_CANONICAL_SHA256: dict[str, tuple[str, int]] = {
    # apache/licenses 的 LICENSE-2.0.txt（官方文本，非"我抄一份再算哈希"）
    "Apache-2.0": (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
        11358,
    ),
    # spdx/license-list-data 的 text/AGPL-3.0-only.txt（与 gnu.org 版本仅差换行，
    # 空白折叠后逐词相同）。当前项目实际使用的就是这一份。
    "AGPL-3.0-only": (
        "d8a6cc31abc16b6748c7a21f21611f5a1ec33f67d22ca23d7da1c19b95496bee",
        34020,
    ),
}

# 各许可证的标志性条款：正文被截断或换成别的许可证时，这几条最先消失。
_APACHE2_MARKERS = (
    "Apache License",
    "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
    # §6 商标条款 —— 本仓库含第三方商标，这条必须真的在文本里
    "This License does not grant permission to use the trade names, trademarks",
)

# AGPL-3.0 的关键条款：§13 网络交互（本产品带本地 HTTP 界面 ⇒ 必须真的在文本里）、
# §5 对应源码、§15 免责声明。
_AGPL3_MARKERS = (
    "GNU AFFERO GENERAL PUBLIC LICENSE",
    "Remote Network Interaction",
    "Corresponding Source",
    "THERE IS NO WARRANTY FOR THE PROGRAM",
)

_MARKERS_BY_SPDX: dict[str, tuple[str, ...]] = {
    "Apache-2.0": _APACHE2_MARKERS,
    "AGPL-3.0-only": _AGPL3_MARKERS,
}


def _current_markers() -> tuple[str, ...]:
    """当前 `LICENSE` 应当具备的关键条款。

    识别不出许可证时返回一个**必然缺席**的哨兵 —— fail-closed：宁可判红，
    也不要因为"认不出是什么证"而静默跳过（`skip` 在报告里长得像通过）。
    """
    return _MARKERS_BY_SPDX.get(_spdx_from_license() or "",
                                ("\x00license-not-recognized",))


#: SPDX → NOTICE 里必须出现的许可证**标题**（人读的那一句，不是标识符）。
_LICENSE_TITLE: dict[str, str] = {
    "Apache-2.0": "Apache License, Version 2.0",
    "AGPL-3.0-only": "GNU Affero General Public License",
}

# SPDX 反推表：**从 LICENSE 正文**判定它到底是什么许可证。
# 顺序有意义 —— AGPL 正文里也含 "GNU GENERAL PUBLIC LICENSE" 的子串，必须先判 AGPL。
# ⚠️ 这里**不是白名单**：加一个新许可证只需加一行；没有条目 ⇒ 返回 None ⇒ 上层 fail-closed。
_SPDX_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AGPL-3.0-only", ("GNU AFFERO GENERAL PUBLIC LICENSE", "Version 3")),
    ("GPL-3.0", ("GNU GENERAL PUBLIC LICENSE", "Version 3")),
    ("Apache-2.0", ("Apache License", "Version 2.0", "TERMS AND CONDITIONS FOR USE")),
    ("MIT", ("MIT License", "Permission is hereby granted, free of charge")),
    ("BSD-3-Clause", ("Redistribution and use in source and binary forms", "Neither the name")),
)


def _spdx_from_license(_text: str | None = None) -> str | None:
    """从许可证正文反推 SPDX 标识符；识别不出返回 `None`（调用方必须 fail-closed）。

    `_text` 参数只为**自测**（防空转用例要喂合成文本），生产路径读真实 `LICENSE`。
    """
    text = _text if _text is not None else _normalized(_LICENSE).decode("utf-8")
    collapsed = " ".join(text.split())
    for spdx, needles in _SPDX_SIGNATURES:
        if all(n in collapsed for n in needles):
            return spdx
    return None


def _normalized(path: Path) -> bytes:
    """按 LF 归一化读字节 —— EOL 不是许可证内容的一部分（`core.autocrlf=true` 会来回翻）。"""
    return path.read_bytes().replace(b"\r\n", b"\n")


def _pkg() -> dict:
    return json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))


class TestLicenseFileIsTheCanonicalText:
    """不变式 A：`LICENSE` 是**某份标准许可证的原文**，没有被人"顺手改一句"。

    ⚠️ **本组被对抗性审查改过一次**（2026-10-28，由变异 M9 阴性对照当场抓出）：
    初版把 `sha256 == Apache-2.0 官方` 写成**无条件**断言，等于把"当前是 Apache-2.0"
    焊死 —— 任何**合法**的换许可证决策都会让护栏变红，逼人删掉护栏。这和我刚批评的
    "黑名单越权"是同一个病。

    现在的口径：
      - **无条件**：正文非空、且能被 `_spdx_from_license()` 识别成某个已知许可证；
      - **有条件**：**只有在 `_CANONICAL_SHA256` 里登记过的许可证**才做逐字节比对。
    ⇒ 换许可证时**必须**把官方文本的 sha256 登记进 `_CANONICAL_SHA256`（一行，可评审）。
    没登记时本组只保证"非空 + 可识别"，**正文是否被篡改无人校验** —— 这是**已知缺口**，
    不是"已通过"。
    """

    def test_license_exists_and_is_not_empty(self):
        assert _LICENSE.is_file(), "仓库根缺 LICENSE —— 公开仓库无许可证 = 默认保留所有权利"
        # 门槛只用来抓"空文件 / 明显截断"。**不能**定得太高：真实 MIT 正文约 1 060 B，
        # 定 1 000 会让它擦边通过、定 1 200 会误杀合法许可证。真正的完整性由
        # `test_license_body_matches_upstream_when_registered` 的 sha256 负责。
        assert _LICENSE.stat().st_size > 500, "LICENSE 小得不像一份完整许可证"

    def test_license_is_recognizable(self):
        """无条件：必须能认出是哪一份许可证（认不出 ⇒ 无法做任何一致性校验）。"""
        assert _spdx_from_license() is not None, (
            "无法从 LICENSE 正文识别出 SPDX 标识符。要么正文被改坏了，"
            "要么这是 `_SPDX_SIGNATURES` 未收录的许可证 —— 两种情况都必须显式处理"
        )

    def test_license_body_matches_upstream_when_registered(self):
        spdx = _spdx_from_license()
        canonical = _CANONICAL_SHA256.get(spdx or "")
        if canonical is None:
            pytest.skip(
                f"{spdx!r} 的官方文本哈希未登记 ⇒ 正文完整性**未校验**（已知缺口，不是通过）。"
                "换许可证时请把官方文本的 sha256 与字节数登记进 _CANONICAL_SHA256"
            )
        body = _normalized(_LICENSE)
        assert len(body) == canonical[1], (
            f"LICENSE 长度 {len(body)}B ≠ {spdx} 官方 {canonical[1]}B ⇒ 正文被改动过。"
            "要写版权归属请改 NOTICE，不要动 LICENSE（见模块 docstring）"
        )
        assert hashlib.sha256(body).hexdigest() == canonical[0], (
            f"LICENSE 内容与 {spdx} 官方文本不一致（sha256 不同）"
        )

    @pytest.mark.parametrize("marker", _current_markers())
    def test_key_clauses_are_present(self, marker: str):
        # 在**空白折叠后**的文本上匹配：官方文本是硬折行的，
        # 直接子串匹配会让"跨行"的条款假红。
        collapsed = " ".join(_normalized(_LICENSE).decode("utf-8").split())
        assert marker in collapsed, f"LICENSE 缺关键条款（{_spdx_from_license()}）：{marker!r}"


class TestMetadataAgreesWithTheLicense:
    """不变式 B：`package.json` 的 SPDX 标识符必须与 `LICENSE` 同一份。"""

    # ⚠️ 这里**故意没有** `test_package_json_declares_apache_2_0`（2026-10-28 删除）：
    # 它把 "Apache-2.0" 写死，与下面那条 SPDX 一致性断言**重复**，而且会让合法换证变红。
    # 重复的断言不会增加保障，只会增加"换证时必须删测试"的诱惑。

    def test_metadata_spdx_matches_what_license_actually_is(self):
        """不变式 D：`package.json` 的 SPDX 标识符必须**等于 LICENSE 正文所对应的许可证**。

        ⚠️ **这条被对抗性审查改过一次**（2026-10-28）：初版写成
        「`license` 不得 ∈ {ISC, MIT, GPL-3.0, AGPL-3.0, …}」—— 那是**黑名单**，
        它把"整个项目改判 AGPL"这一**合法决策**（TODO 0-67 的选项③）**提前否决**了。
        护栏的职责是抓"两处漂移"，不是替人做决策。
        改为**从 LICENSE 正文反推 SPDX**，再断言两处相等：
          - LICENSE 换 MIT 而 package.json 仍 Apache-2.0 ⇒ 红（抓漂移）
          - 全项目合法改成 AGPL 且两处同步 ⇒ 绿（不越权）
          - 旧值 ISC 残留 ⇒ 红（本轮的真实缺陷）
        """
        detected = _spdx_from_license()
        assert detected is not None, (
            "无法从 LICENSE 正文识别出 SPDX 标识符（fail-closed）⇒ 本断言没有判据，"
            "不能当作通过。请扩展 _SPDX_SIGNATURES 而不是删掉这条断言"
        )
        lic = _pkg().get("license")
        assert lic == detected, (
            f"package.json license={lic!r}，而 LICENSE 正文是 {detected!r} —— 两处真值漂移。"
            "历史上这里写着 npm init 的默认占位 'ISC'，从没有消费者校验过它"
        )

    def test_spdx_detector_is_not_vacuous(self):
        """防空转：检测器必须能**区分**不同许可证，否则上面那条会退化成恒真。"""
        assert _spdx_from_license(_text="MIT License\n\nPermission is hereby granted, free of charge") == "MIT"
        assert _spdx_from_license(
            _text="GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007"
        ) == "AGPL-3.0-only"
        assert _spdx_from_license(_text="这不是任何许可证") is None

    def test_pyproject_declares_no_license_field(self):
        """刻意不加：`[project].license` 在本仓库**没有消费者**（见模块 docstring 第 2 条）。

        这条断言是**反向**的 —— 它挡的是"好心人"下一次把它加回来，
        从而制造出第二个没有校验点的真值。
        """
        text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert not re.search(r"^\s*license\s*=", text, re.MULTILINE), (
            "pyproject.toml 出现了 license 声明。本仓库不以 Python 包分发 ⇒ 该字段无消费者，"
            "是负债而不是保障（同 R126 的 RuleSpec.severity）"
        )


class TestNoticeStatesTheScope:
    """不变式 C：`NOTICE` 必须写明授权**不含**第三方数据与商标。

    这条不是形式主义：仓库里（及其 git 历史里）确实存在第三方批记录内容与商标，
    而许可证（Apache-2.0 §6 / AGPL 全篇）**不会**替你声明"数据不在授权范围内"。
    """

    def test_notice_exists(self):
        assert _NOTICE.is_file(), "缺 NOTICE —— 分发时必须随附归属声明"

    def test_notice_names_the_license(self):
        """NOTICE 必须点名**当前**许可证（不是某个历史版本）。"""
        spdx = _spdx_from_license()
        title = _LICENSE_TITLE.get(spdx or "")
        assert title is not None, f"{spdx!r} 的许可证标题未登记 ⇒ 本断言无判据"
        text = _NOTICE.read_text(encoding="utf-8")
        assert title in text, f"NOTICE 未点名当前许可证：{title!r}"

    def test_notice_disclaims_third_party_data_and_trademarks(self):
        text = _NOTICE.read_text(encoding="utf-8")
        for needle in ("batch production record", "trademark", "does NOT cover"):
            assert needle in text, f"NOTICE 未声明授权范围排除项：{needle!r}"

    def test_notice_flags_the_agpl_dependency(self):
        """PyMuPDF 是 AGPL-3.0/商业双许可，且是**硬运行时依赖**。

        NOTICE 里不点名它，读者会把"Apache-2.0"读成"整个产物都是 Apache-2.0"。

        ⚠️ **这条被对抗性审查改成条件式**（2026-10-28）：初版是**无条件**要求 NOTICE
        含 `PyMuPDF` + `AGPL`。但 TODO 0-67 的**正确修复之一就是换掉 PyMuPDF**
        —— 那时 NOTICE 该删这段，而初版护栏会**把正确修复判红**，逼人保留一句
        已经不成立的声明。护栏不许固化当下的错误。所以改成**双向**：
        依赖在 ⇒ 必须声明；依赖不在 ⇒ 声明必须消失（陈旧声明同样是缺陷）。
        """
        req = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
        notice = _NOTICE.read_text(encoding="utf-8")
        has_dep = bool(re.search(r"^\s*PyMuPDF\s*==", req, re.MULTILINE))
        if has_dep:
            assert "PyMuPDF" in notice and "AGPL" in notice, (
                "requirements.txt 仍依赖 PyMuPDF（AGPL-3.0），但 NOTICE 没有点名它"
            )
        else:
            assert "PyMuPDF" not in notice, (
                "requirements.txt 已不含 PyMuPDF，但 NOTICE 仍在声明它是 AGPL 依赖 —— "
                "陈旧声明与实现不符"
            )
