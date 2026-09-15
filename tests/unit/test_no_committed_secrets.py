"""禁止把真实密钥提交进仓库（源码扫描护栏）。

**由来（真实事故）**：`tests/e2e_frozen.py` / `e2e_manual.py` / `e2e_quick.py` 曾把同一把
真实 DeepSeek key 写死并入库（自 `81964a3` 起在 git 历史中且已推送）。删除文件**不能**
抹掉历史 —— 只能到服务商处轮换（流程见 `DEPLOYMENT.md` 的「Secret 轮换流程」）。
本护栏防的是"下一次"，不是"上一次"。

**与 `DEPLOYMENT.md` 同源**：该文档第 4 步给出的自查命令是 `git grep -nE
"sk-[A-Za-z0-9]{32,}"`，本文件的 `_KEY_RE` **就是该模式**，并由
`test_documented_scan_command_matches_this_guard` 做双向绑定 —— 改一边必须改另一边。
（此前该文档声称"有测试固化此规则"，而护栏确实存在于 `test_e2e_proc_helper.py`；
但两边各自演化、口径可能不一致，复核者照文档自查未必得到与 CI 相同的结论。现已收拢到本文件。）

**口径（刻意收窄，避免假阳性）**：只认 `sk-` 前缀 + 其后 ≥32 个**连续字母数字**。
  - 不认带分隔符的串：`sk-test-fake-key-xxxx…`、`sk-test-key-for-unit-test-only`
    这类夹具天然不命中（`sk-` 后 4 个字符即遇 `-`）。**无需维护"夹具白名单"** ——
    那类白名单一旦用短子串判定就会**漏报**，而安全护栏漏报比假阳性更糟。
  - 不认词中/URL 中的 `sk-`：真实仓库里
    `…/review/risk-controlled-medical-entity-extraction-across-clinical-domains`
    曾被误命中 —— 其 `sk-` 后只有 `controlled` 即遇 `-`，同样靠"连续长度"天然排除。

**漏报风险如实声明**：只认 `sk-` 前缀形态。其他形态（Paddle 40 位无前缀 token、
其他厂商自定义前缀、私钥 PEM 块等）**不在覆盖范围内**。
**不要因为本文件全绿就认为"仓库里没有任何密钥"。**
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# 唯一真值：与 DEPLOYMENT.md 第 4 步记录的命令**同一模式**（有测试做绑定）
_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{32,}")

# 曾经随提交进入 git 历史的真实 LLM 密钥（2026-09-15 发现）。
# **故意用拼接**构造，使本文件自身不含连串的 `sk-` 字面量 —— 否则只能靠豁免放行，
# 而豁免会让"往这个文件里加密钥"也逃过检查。
_LEAKED_KEY = "sk-vprnpmjfzbcinduybbsboaw" + "tjxtrnrhfldbargfwzkieuczu"

# 扫描范围：所有"应当无密钥"的源码 / 文档 / 配置模板目录
_SCAN_DIRS = (
    "api", "core", "db", "llm", "models", "scripts", "static", "templates",
    "tests", "tools", "docs", "electron",
)
_SCAN_SUFFIXES = {".py", ".js", ".html", ".ps1", ".bat", ".md", ".json", ".txt", ".sql", ".yml", ".yaml"}
# 产物/缓存目录：精确名 → 集合；成族目录（dist、dist-electron、dist-electron-m8…）
# → 按**前缀**排除。不罗列具体名 —— 构建失败会自愈到备用输出目录，名字会变
# （历史上正是这么积出 4 个 dist-electron* 的）。
_SKIP_DIRS = {"__pycache__", "node_modules", "build", "htmlcov", "output", "devlogs", "logs", ".git"}
_SKIP_DIR_PREFIX = ("dist",)
# 唯一允许持有真实密钥的文件：运行时配置，被 `.gitignore` 忽略。
# 这个"排除"本身是有前提的 —— 由 `test_config_json_is_gitignored` 检查。
_ROOT_EXEMPT = {"config.json"}


def _skip(rel: Path) -> bool:
    """目录部分命中跳过规则即跳过（只看目录，文件名不参与判定）。"""
    parts = rel.parts[:-1]
    return bool(set(parts) & _SKIP_DIRS) or any(pt.startswith(_SKIP_DIR_PREFIX) for pt in parts)


def _scanned_files():
    """产出**应当无密钥**的文件（相对 `_ROOT` 的路径）。"""
    for d in _SCAN_DIRS:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _SCAN_SUFFIXES:
                continue
            rel = p.relative_to(_ROOT)
            if _skip(rel):
                continue
            yield rel
    # 仓库根目录**不写白名单** —— 白名单必然漂移（初版就漏了 `server.py` / `ui_e2e.py`，
    # 靠历史扫描才暴露），而根目录下的脚本正是本事故的发生地。改为"根目录下所有相关
    # 后缀的文件，仅排除运行时配置"。
    for p in sorted(_ROOT.iterdir()):
        if not p.is_file() or p.name in _ROOT_EXEMPT:
            continue
        if p.suffix.lower() in _SCAN_SUFFIXES:
            yield Path(p.name)


def find_secrets(text: str) -> list[str]:
    """纯函数：返回文本中形似真实密钥的串。可被单测直接调用。"""
    return [m.group(0) for m in _KEY_RE.finditer(text)]


def _read(rel: Path) -> str | None:
    try:
        return (_ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ── 护栏本体 ────────────────────────────────────────────────────────


def test_no_leaked_key_in_source_tree():
    """工作树任何源文件都不得出现那把**已泄漏的真实密钥**。

    与下面的通用扫描**并列而非重复**：本用例用字符串直接比对，不依赖 `_KEY_RE`。
    即便有人把正则改松（甚至改到认不出 32 位形态），这条仍然守得住。
    """
    offenders = [
        rel.as_posix() for rel in _scanned_files()
        if (text := _read(rel)) is not None and _LEAKED_KEY in text
    ]
    assert not offenders, (
        "以下文件仍硬编码着已泄漏的 LLM 密钥（应改为从环境变量读取）：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n注意：删除**不能**抹掉 git 历史，唯一补救是到服务商处轮换。"
    )


def test_no_real_looking_secret_in_source_tree():
    """源码/文档树里不得出现形似真实密钥的串（`config.json` 除外，它被 gitignore）。"""
    offenders = []
    for rel in _scanned_files():
        text = _read(rel)
        if text is None:
            continue
        posix = rel.as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            for token in find_secrets(line):
                # 只报位置与前 8 字符 —— 报告本身不该成为新的泄露源
                offenders.append(f"{posix}:{i}  {token[:8]}…（len={len(token)}）")
    assert not offenders, (
        "以下位置疑似写入了真实密钥。删除并改为从环境变量 / config.json 读取；"
        "若该密钥曾入库，**必须到服务商处轮换**（删文件不能抹掉历史）：\n  "
        + "\n  ".join(offenders)
    )


# ── 防空转：阳性对照 ────────────────────────────────────────────────


def test_secret_scan_positive_control():
    """阳性对照：护栏必须真的会报 —— 否则"全绿"只是空转。

    本仓库对"护栏空转"有过教训（豁免/白名单把检查变成摆设）。这里用**拼接**构造
    样本，故本文件自身不含命中串，也就不需要任何豁免。
    """
    # ① 与真实事故同形态、同量级（48 位）：拼接构造
    synthetic = "DEEPSEEK_API_KEY = " + '"sk-' + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + '"'
    assert find_secrets(synthetic), "护栏未能识别形如真实密钥的串（会给出虚假的干净结论）"
    # ② 那把真事故密钥本身也必须被通用规则认出（与上面的直接比对互为印证）
    assert find_secrets("k=" + _LEAKED_KEY), "护栏未能识别已泄漏密钥的形态"
    # ③ 反向：夹具 / 占位符（`sk-` 后很快遇 `-` 或不足 32 位）不得命中
    assert not find_secrets("sk-test-fake-key-" + "x" * 32)
    assert not find_secrets("sk-test-key-for-unit-test-only")
    assert not find_secrets("sk-realkey1234567890abcdef")  # 24 位 < 32
    # ④ 反向：词中 / URL 里的 `sk-` 不得命中（真实仓库里踩过）
    assert not find_secrets(
        "https://www.themoonlight.io/es/review/conformal-prediction-for-"
        "risk-controlled-medical-entity-extraction-across-clinical-domains"
    )
    # ⑤ 反向：短串不是密钥
    assert not find_secrets('api_key = "sk-short"')


def test_scan_scope_covers_repo_root_scripts():
    """范围自检：根目录脚本必须在扫描范围内（本事故正是发生在根目录脚本上）。

    防的是"范围被无意收窄" —— 例如有人把 `_scanned_files()` 改回白名单，
    或给 `_SKIP_DIR_PREFIX` 加上过宽的项。
    """
    scanned = {rel.as_posix() for rel in _scanned_files()}
    for expected in ("main.py", "config.py", "build.ps1", "e2e_run.py"):
        if (_ROOT / expected).is_file():
            assert expected in scanned, f"{expected} 未进入密钥扫描范围（根目录脚本是事故高发处）"
    # 产物目录必须被排除 —— 否则会扫到冻结产物里的第三方文件
    assert not any(s.startswith("dist") or "/dist" in s for s in scanned)
    assert "config.json" not in scanned, "config.json 是运行时配置，不应被扫描（前提见下）"


# ── 防漂移：与文档同源 ──────────────────────────────────────────────


def test_documented_scan_command_matches_this_guard():
    """`DEPLOYMENT.md` 记录的轮换自查命令必须与本护栏的 `_KEY_RE` **同源**。

    背景：文档曾声称"有测试固化此规则"，护栏也确实存在，但两者各自演化过 ——
    照文档自查会得到与 CI 不同的结论。绑定之后，改一处必须改另一处。
    """
    doc = _ROOT / "DEPLOYMENT.md"
    assert doc.is_file(), "DEPLOYMENT.md 缺失 —— Secret 轮换流程的载体不在"
    text = doc.read_text(encoding="utf-8", errors="replace")
    assert _KEY_RE.pattern in text, (
        f"DEPLOYMENT.md 未记录本护栏的模式 {_KEY_RE.pattern!r} —— "
        "文档命令与护栏口径已漂移，请同步"
    )


def test_config_json_is_gitignored():
    """`config.json` 合法持有真实密钥，因此**必须**被 gitignore。

    这条是上面扫描"排除 config.json"的**前提**：一旦它不再被忽略，本护栏的口径
    就出现一个真实存在的盲区 —— 所以前提本身也要被检查。
    """
    ignore = _ROOT / ".gitignore"
    if not ignore.is_file():
        pytest.skip(".gitignore 不存在（非常规检出）")
    lines = {
        ln.strip() for ln in ignore.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    assert "config.json" in lines, (
        ".gitignore 不再忽略 config.json —— 真实密钥会被提交，"
        "而本护栏刻意不扫该文件（见 test_scan_scope_covers_repo_root_scripts）"
    )
