"""`MAX_CONCURRENT_JOBS` 默认值：全库"活文件"的说法必须等于代码（防 sediment）。

R87 定位
--------
R85 把默认值从 **3 抬到 5**（为满足"5 个文件高并发审核"），但只扫了
`README.md` 与 `tests/unit/test_upload_quota_contract.py` 的 docstring。
全库仍残留 7 处"默认 3"，其中两处代价最高：

  · `CLAUDE.md` —— **agent 指令文件**，每次会话注入。写错的默认值会误导
    后续 agent 对并发上限的判断（与 R86 修的 OCR failover 阈值**同一类**缺陷）；
  · `static/upload.js` —— **入包源**（`scripts/bundle_manifest.py::BUNDLE_SOURCES`），
    随包分发给用户，注释里的错值会被下游当成"产品事实"。

同一次改动还**蕴含**了两处叙述变成假命题：默认 5 时空位够，
"用户选 5 份 ⇒ 第 4、5 份必然吃 409" 不再成立 —— **由默认值派生的序数/条数
本身就是一种副本**，不该写进文档（写完即随下次调参过期）。

判据不写死字面量：默认值从 `api/jobs/__init__.py` **重算**（与
`tests/unit/test_upload_quota_contract.py` 同源），因此下次调参时本文件
自动跟随，只拦"与代码不一致"的说法。

判据只认**显式**默认值声明（`默认 N` / `(default N)`）
-------------------------------------------------------
`MAX_CONCURRENT_JOBS=<N>` 有两种语义：①陈述默认值；②教用户**覆盖**它
（`PORTABLE_README.txt` 的 `set MAX_CONCURRENT_JOBS=2 && …` 示例）。
正则无法区分 ⇒ 本仓库约定：**陈述默认值一律写显式 `默认 N`**，
`=N` 只留给覆盖示例（见 `test_equals_form_is_reserved_for_override_examples`）。
（该歧义是 R87a 首跑在 `PORTABLE_README.txt` 上**假红**后定位的。）
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: 默认值的**单一真值**所在（与 tests/unit/test_upload_quota_contract.py 同源）
_QUOTA_SRC = _ROOT / "api" / "jobs" / "__init__.py"

#: "活文件"集合 —— 描述**当前**系统的文件。
#: 刻意排除：带日期的历史评审（docs/ADVERSARIAL_REVIEW_2026-*.md 等，是**快照**）、
#: `CHANGELOG.md`（记录变更史，含 "3 → 5" 属**正确**表述）、`devlogs/**`、
#: `dist*/**`、`htmlcov/**`。把这些也扫进来会产生大量假红。
_LIVING_FILES = (
    "CLAUDE.md",
    "README.md",
    "PORTABLE_README.txt",
    "DEPLOYMENT.md",
    "static/upload.js",
    "core/procpool.py",
    "api/jobs/listings.py",
    "main.py",
    "tests/unit/test_procpool.py",
    "tests/unit/test_upload_quota_js.py",
    "tests/unit/test_upload_multifile_js.py",
    "tests/unit/test_upload_quota_contract.py",
)

#: 行内**显式**默认值声明：`MAX_CONCURRENT_JOBS` 之后 40 字符内出现
#: 「默认 N」（允许"默认均为 5"这类 ≤8 个非数字的插入语）或「(default N)」。
_DEFAULT_CLAIM = re.compile(
    r"MAX_CONCURRENT_JOBS[^\n]{0,40}?"
    r"(?:默认[^0-9\n]{0,8}(\d+)|\(default\s+(\d+)\))"
)

#: README 配置表形态：`| \`MAX_CONCURRENT_JOBS\`（env） | 说明 | \`5\` |`
_README_TABLE_ROW = re.compile(
    r"^\|\s*`MAX_CONCURRENT_JOBS`[^|]*\|[^|]*\|\s*`?(\d+)`?\s*\|",
    re.MULTILINE,
)

#: `=N` 形态（跨行也允许，用于 PORTABLE_README 的换行排版）
_EQUALS_FORM = re.compile(r"MAX_CONCURRENT_JOBS\s*=\s*(\d+)", re.S)


def _true_default() -> int:
    """从 `api/jobs/__init__.py` 解析默认值（不写死字面量）。"""
    src = _QUOTA_SRC.read_text(encoding="utf-8")
    m = re.search(r'os\.getenv\(\s*"MAX_CONCURRENT_JOBS"\s*,\s*"(\d+)"\s*\)', src)
    assert m, (
        "未从 api/jobs/__init__.py 解析到 MAX_CONCURRENT_JOBS 的默认值 —— "
        "`os.getenv(\"MAX_CONCURRENT_JOBS\", \"N\")` 的读取方式可能已过时"
    )
    return int(m.group(1))


def _claimed(m: re.Match[str]) -> int:
    return int(next(g for g in m.groups() if g))


# ─── 1. 真值与解析器 ─────────────────────────────────────────────


def test_true_default_is_at_least_five():
    """产品要求：默认须支持 5 个文件并发审核（企业就绪 F10）。"""
    assert _true_default() >= 5, (
        f"MAX_CONCURRENT_JOBS 默认 {_true_default()} < 5 —— 不满足'5 个文件并发审核'"
    )


def test_claim_parser_is_not_vacuous():
    """**反空转**：正则必须真的命中声明形态，否则下面的"无陈旧"是假绿。

    同时反向验证：陈旧值必须被判为不一致（否则"全绿"只是解析器失灵）。
    """
    n = _true_default()
    forms = [
        f"| `MAX_CONCURRENT_JOBS` | Max simultaneous active pipelines (default {n}) |",
        f"// 是 `MAX_CONCURRENT_JOBS`（默认 {n}）。",
        f"#   ① `MAX_CONCURRENT_JOBS` 默认 {n} ⇒ 并发 job 是**设计内**场景",
        f"        # （MAX_CONCURRENT_JOBS 默认 {n}）只是白占内存。",
        f"   时不再每个任务各开一条连接（MAX_CONCURRENT_JOBS 默认 {n} 时多标签页",
    ]
    # 注：PORTABLE_README 的**跨行**声明（"…MAX_CONCURRENT_JOBS，\n 默认均为 N"）
    # 不在本列表 —— 行内正则刻意不跨行（见 _DEFAULT_CLAIM），该形态由
    # test_portable_readme_states_the_true_default 用 re.S 单独钉住。
    for s in forms:
        m = _DEFAULT_CLAIM.search(s)
        assert m, f"正则未命中已知声明形态（解析器失灵）：{s!r}"
        assert _claimed(m) == n, f"命中值不等于真值：{s!r}"

    stale = f"| `MAX_CONCURRENT_JOBS` | Max simultaneous active pipelines (default {n - 2}) |"
    m = _DEFAULT_CLAIM.search(stale)
    assert m and _claimed(m) != n, "陈旧值未被识别为不一致 —— 护栏无判别力"


# ─── 2. 主护栏：活文件内不得出现与代码不一致的默认值 ──────────────


def test_living_files_do_not_claim_a_stale_default():
    n = _true_default()
    bad: list[str] = []
    hits = 0
    for rel in _LIVING_FILES:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for m in _DEFAULT_CLAIM.finditer(text):
            hits += 1
            if _claimed(m) != n:
                bad.append(f"{rel}: 声称 {_claimed(m)}（真值 {n}）→ {m.group(0)!r}")
    assert hits >= 6, (
        f"活文件集合内只命中 {hits} 处默认值声明（预期 >=6）—— "
        "要么声明被大删，要么解析器已过时（此时本用例是假绿）"
    )
    assert not bad, "以下位置的默认值与代码不一致：\n  " + "\n  ".join(bad)


def test_readme_config_table_states_the_true_default():
    """README 配置表是**另一种形态**（值在独立单元格），单独钉住。"""
    n = _true_default()
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    m = _README_TABLE_ROW.search(text)
    assert m, "README 配置表里找不到 `MAX_CONCURRENT_JOBS` 行 —— 表结构可能已变"
    assert int(m.group(1)) == n, f"README 配置表写默认 {m.group(1)}，代码真值 {n}"


def test_portable_readme_states_the_true_default():
    """随包 README 的声明**跨行**（"MAX_CONCURRENT_JOBS，\\n 默认均为 N"），
    行内正则扫不到 ⇒ 单独钉住（`re.S` 允许跨行）。"""
    n = _true_default()
    text = (_ROOT / "PORTABLE_README.txt").read_text(encoding="utf-8")
    m = _DEFAULT_CLAIM.search(text) or re.search(
        r"MAX_CONCURRENT_JOBS[^0-9]{0,40}?默认[^0-9]{0,8}(\d+)", text, re.S
    )
    assert m, "PORTABLE_README.txt 未声明 MAX_CONCURRENT_JOBS 的默认值"
    assert _claimed(m) == n, (
        f"PORTABLE_README.txt 声称默认 {_claimed(m)}，代码真值 {n}"
    )


def test_claude_md_states_the_true_default():
    """CLAUDE.md 是 agent 指令文件 —— 值错会误导后续 agent 的判断。"""
    n = _true_default()
    text = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert f"`MAX_CONCURRENT_JOBS` env var (default {n})" in text, (
        f"CLAUDE.md 未把 MAX_CONCURRENT_JOBS 默认值写成 (default {n})"
    )
    assert (
        f"| `MAX_CONCURRENT_JOBS` | Max simultaneous active pipelines (default {n}) |"
        in text
    ), f"CLAUDE.md 环境变量表未把默认值写成 (default {n})"


def test_js_in_package_source_states_the_true_default():
    """`static/upload.js` 是**入包源** —— 注释会随包分发给用户。"""
    n = _true_default()
    text = (_ROOT / "static" / "upload.js").read_text(encoding="utf-8")
    assert f"`MAX_CONCURRENT_JOBS`（默认 {n}）" in text, (
        f"static/upload.js 未把默认值写成（默认 {n}）"
    )


# ─── 3. 约定与已否证说法 ─────────────────────────────────────────


def test_equals_form_is_reserved_for_override_examples():
    """`MAX_CONCURRENT_JOBS=<N>` 在活文件里**只允许**作为覆盖示例出现。

    判据：含该形态的行必须同时含 `set `（大小写不敏感）—— 即"教用户设置
    环境变量"的示例。否则它就是在陈述默认值，应改写为显式 `默认 N`
    （否则与本文件的正则语义冲突，护栏会失去判别力）。
    """
    offenders = []
    for rel in _LIVING_FILES:
        for lineno, line in enumerate(
            (_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1
        ):
            if _EQUALS_FORM.search(line) and "set " not in line.lower():
                offenders.append(f"{rel}:{lineno}: {line.strip()!r}")
    assert not offenders, (
        "以下位置用 `=N` 陈述默认值（应改为显式 `默认 N`，"
        "`=N` 只留给 `set …` 覆盖示例）：\n  " + "\n  ".join(offenders)
    )


def test_derived_ordinal_narrative_is_gone():
    """`选 5 份 ⇒ 第 4、5 份必然吃 409` 已随默认 3→5 变成假命题。

    这类"由默认值派生出的序数"是副本，写完即随调参过期 —— 已改为
    "活跃数已达额度时，排在空位之外的几份"（与默认值无关）。
    """
    offenders = [
        rel
        for rel in _LIVING_FILES
        if re.search(r"第\s*4、5\s*份", (_ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "以下文件仍含已随默认值变化而失真的序数叙述"
        f"（`第 4、5 份必然吃 409`）：{offenders}"
    )
