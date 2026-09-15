"""上传限额（体积 + 页数）的单一真值与边界语义护栏。

**背景（2026-09-15，用户提问"超出能力边界是否给良好提示"时查出）**：

1. **页数完全没有上限。** 体积上限（200MB）只挡得住"巨文件"，挡不住
   "低密度但极长"的 PDF —— 数字排版件可能 50KB/页，200MB 能装下数千页，而
   体积检查毫无察觉。按实测 9.0 s/页 OCR（真实 51 页批记录 Stage1
   460.5s），数千页会先撞上 ``core/ocr_client.POLL_TIMEOUT_MAX=3600s``
   （100 页即封顶）→ 用户等满 1 小时后才收到"轮询超时"。上传时就以明确
   提示拒绝，远好于让用户在服务端超时后才得知。

2. **同一个 200MB 被四处各自写死**：``api/jobs/__init__._MAX_PDF_BYTES``、
   ``api/jobs/upload.py`` 的错误消息、``static/upload.js`` 的比较式、
   ``templates/upload.html`` 的提示文字。任一处调整都会静默漂移，产生
   "前端放行、后端拒绝"（或反之）。

3. **还有一处更深的独立限制**：``core/mineru_client.py`` 有 MinerU 通道的
   厂商 200MB 上限。它与我们的策略数值相同纯属巧合 —— 一旦我们的准入上限
   超过它，就会出现"上传放行、流程中途被 MinerU 拒绝"（MinerU 还是 Paddle
   提交失败时的 failover 备选）。

本文件：
* 纯函数边界语义（含"页数读取失败一律放行"这条既有契约）；
* 单一真值的派生关系（后端常量、页面注入）；
* **源码扫描护栏**：限额数值与提示文案不得再散落各处 —— 只能出现在
  ``config.py``（策略本体）与 ``core/mineru_client.py``（厂商上限）；
* 策略上限 ≤ 厂商上限的关系护栏；
* **引用纪律护栏**：禁止把"每页耗时"归给第三方厂商（该数字无人公布），
  并要求外部成本数字留在带出处的文档里 —— 见本文件第 7 节由来。
"""
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import UPLOAD_LIMITS, _normalize_upload_limits, check_upload_page_limits  # noqa: E402

# ── 1. 出厂默认值是被文档与推导说明钉住的契约 ────────────────────────


def test_shipped_defaults_are_the_documented_ones():
    """默认值即契约：改动必须同步更新本测试 + docs/UPLOAD_LIMITS.md 的推导。

    页数上限的定标依据（实测，非照抄云厂商）：云厂商的 1,000~3,000 页不可
    移植 —— 那是"单页成本"的函数（三大云纯文本抽取约 $1.50/1,000 页；本链路
    每页跑手写 OCR + 逐页 LLM，按调用性质属 $10~50/1,000 页的生成式档，实测
    9.0 s/页 OCR、22.8 s/页 LLM）。**可移植的量是"墙钟时间预算"，不是页数。**
    200 页对应 OCR 预估 1800s，对既有 3600s 轮询上限留 2× 余量。
    """
    assert UPLOAD_LIMITS["max_bytes"] == 200 * 1024 * 1024
    assert UPLOAD_LIMITS["max_pages"] == 200
    assert UPLOAD_LIMITS["warn_pages"] == 80
    assert UPLOAD_LIMITS["warn_pages"] < UPLOAD_LIMITS["max_pages"]


# ── 2. 纯函数边界语义 ────────────────────────────────────────────────


@pytest.mark.parametrize("pages", [0, -1, -999])
def test_unknown_page_count_is_never_rejected(pages):
    """页数未知/读取失败一律放行 —— 既有契约，不得因新增上限而收紧。

    部分损坏 PDF 云端 OCR（Paddle/MinerU 各有容错）可能仍能处理，拦掉等于
    把"可能成功"变成"必然失败"。这也是上限必须基于"已读到的真实页数"而非
    估算的原因。
    """
    assert check_upload_page_limits(pages) == (None, None)


def test_boundary_warn_is_strictly_greater_than_threshold():
    """软阈值取"严格大于" —— 等于阈值不告警，否则阈值语义会被放大一页。"""
    warn_at = UPLOAD_LIMITS["warn_pages"]
    assert check_upload_page_limits(warn_at) == (None, None)
    reject, warn = check_upload_page_limits(warn_at + 1)
    assert reject is None and warn


def test_boundary_max_is_inclusive():
    """硬上限是**含**的：等于上限放行，超出一页才拒绝。"""
    max_pages = UPLOAD_LIMITS["max_pages"]
    reject, warn = check_upload_page_limits(max_pages)
    assert reject is None
    assert warn  # 上限必然超过软阈值 → 必然带告警

    reject, warn = check_upload_page_limits(max_pages + 1)
    assert reject is not None and warn is None


def test_reject_message_is_actionable():
    """拒绝提示须含**真实页数 + 上限 + 可执行动作**。

    对标成熟产品做法：amaise 用 "Too many pages (max 2,000) — split the PDF"
    拒绝且给出下一步；Azure F0 只回前 2 页、不解释原因，被业界引为反面教材。
    只报"文件太大"不说怎么办，等于把问题丢回给用户。
    """
    reject, _ = check_upload_page_limits(999)
    assert "999" in reject
    assert str(UPLOAD_LIMITS["max_pages"]) in reject
    assert "拆分" in reject


def test_warn_message_carries_estimate():
    """软告警须给出预估耗时 —— 只说"很大"无法支撑用户的决策。"""
    _, warn = check_upload_page_limits(UPLOAD_LIMITS["max_pages"])
    assert "预估" in warn and "分钟" in warn


def test_limits_are_parameterizable():
    """判定接受显式 limits —— 便于按环境调整与单测，不读隐式全局。"""
    custom = {"max_pages": 10, "warn_pages": 5, "sec_per_page_total": 60.0}
    assert check_upload_page_limits(4, custom) == (None, None)
    reject, warn = check_upload_page_limits(6, custom)
    assert reject is None and "预估耗时约 6 分钟" in warn
    reject, _ = check_upload_page_limits(11, custom)
    assert reject is not None and "上限 10 页" in reject


# ── 3. 误配置收敛（env 可覆盖，非法值不得崩溃或产生荒谬语义）─────────


def test_normalize_rejects_absurd_max_pages():
    """1 页上限 = 拒绝一切多页批记录，必是误配置 → 收敛到默认。"""
    assert _normalize_upload_limits({"max_pages": 1, "warn_pages": 1})["max_pages"] == 200
    assert _normalize_upload_limits({"max_pages": "x", "warn_pages": 80})["max_pages"] == 200
    assert _normalize_upload_limits({"max_pages": 0, "warn_pages": 0})["max_pages"] == 200


def test_normalize_clamps_warn_below_max():
    """软告警阈值必须严格小于硬上限 —— 等于/超过会让告警永不触发。"""
    for bad in (10, 11, 100, 0, -5, "x"):
        out = _normalize_upload_limits({"max_pages": 10, "warn_pages": bad})
        assert 1 <= out["warn_pages"] < out["max_pages"], f"warn_pages={bad} 未被收敛"


# ── 4. 单一真值的派生关系（实物而非文档）────────────────────────────


def test_backend_byte_limit_derives_from_single_source():
    import api.jobs as jobs

    assert jobs._MAX_PDF_BYTES == UPLOAD_LIMITS["max_bytes"]


def test_upload_module_enforces_page_limits():
    """上传端点必须真的调用页数判定 —— 防止限额只增不减地"写在常量里没人用"。"""
    from api.jobs import upload

    assert upload.check_upload_page_limits is check_upload_page_limits
    assert upload.UPLOAD_LIMITS is UPLOAD_LIMITS
    src = (_ROOT / "api" / "jobs" / "upload.py").read_text(encoding="utf-8")
    assert "check_upload_page_limits(" in src
    # 判定结果必须真的进入拒绝路径，而不是只算不判
    assert "raise HTTPException(400, reject_detail)" in src


def test_main_injects_limits_into_upload_page():
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert '"limits": UPLOAD_LIMITS' in src


def test_frontend_consumes_injected_limits_and_has_no_fallback_copy():
    """前端只认注入值，**不得有兜底数值副本**。

    兜底副本会随服务端调整而漂移，"前端放行、后端拒绝"正是本次要根除的
    缺陷。注入缺失时跳过前端预检，交由服务端判定（服务端本来就权威）。
    """
    js = (_ROOT / "static" / "upload.js").read_text(encoding="utf-8")
    assert "ctx.limits" in js
    assert re.search(r"if \(LIMITS && file\.size > LIMITS\.max_bytes\)", js), (
        "体积预检必须以 LIMITS 存在为前提（否则说明兜底值又回来了）"
    )

    html = (_ROOT / "templates" / "upload.html").read_text(encoding="utf-8")
    assert "limits | tojson" in html, "限额未注入前端"
    assert "limits.max_pages" in html, "页面文案未从限额派生"


# ── 5. 源码扫描护栏：限额数值与文案不得再散落各处 ─────────────────────

_MB = UPLOAD_LIMITS["max_bytes"] // 1024 // 1024  # 由真值派生，改限额自动跟随

# 数值以 "N * 1024 * 1024" 形态出现 —— 刻意**只扫数值字面量**。
#
# 教训（本护栏第一版就踩了）：最初把 "上限 200MB" 这类短语也纳入扫描，结果
# 立刻命中 core/pipeline/ocr_support.py 的一句**解释性注释**（"系统 200MB
# 上限且拖垮上游提交…"，讲的是上游约束，不是被强制的值）。把散文纳入扫描会
# 逼着后人改写注释来"过检"，护栏随即变成负担并被人绕开 —— 这与本项目对
# "假绿灯"的警惕同源。文案的派生关系改用下面的**定点断言**覆盖。
#
# 数值字面量本身不会出现在散文里（没人会在注释里写 "200 * 1024 * 1024"），
# 故无需剥离注释即可零假阳性。
_FORBIDDEN_PATTERNS = (
    re.compile(rf"\b{_MB}\s*\*\s*1024\s*\*\s*1024\b"),
)

_SCAN_DIRS = ("api", "core", "db", "llm", "models", "scripts", "static", "templates")
_SCAN_FILES = ("main.py", "config.py")
_SUFFIXES = {".py", ".js", ".html", ".ps1", ".json"}

# 允许出现的位置：
#   config.py                  —— 策略本体（单一真值）
#   core/mineru_client.py      —— MinerU 通道的**厂商上限**，非本产品策略
_ALLOWED_REL = {"config.py", "core/mineru_client.py"}


def _scanned_files():
    for d in _SCAN_DIRS:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _SUFFIXES:
                continue
            parts = set(p.relative_to(_ROOT).parts)
            if parts & {"__pycache__", "node_modules", "dist", "build", "htmlcov"}:
                continue
            yield p.relative_to(_ROOT)
    for name in _SCAN_FILES:
        p = _ROOT / name
        if p.is_file():
            yield Path(name)


def _hits(text: str):
    for pat in _FORBIDDEN_PATTERNS:
        for m in pat.finditer(text):
            return m.group(0)
    return None


def test_no_duplicate_hardcoded_limits_outside_single_source():
    """限额数值与提示文案只允许出现在两个登记在案的位置。

    这是本仓库既有的护栏风格（见 tests/unit/test_e2e_proc_helper.py 的
    "禁止硬编码本仓库路径"）：把不变式写成可执行的检查，而不是靠人记得改。
    """
    offenders = []
    for rel in _scanned_files():
        if rel.as_posix() in _ALLOWED_REL:
            continue
        try:
            text = (_ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hit = _hits(text)
        if hit:
            offenders.append(f"{rel}: {hit!r}")
    assert not offenders, (
        "限额被重复写死（应改为从 config.UPLOAD_LIMITS 派生）：\n  "
        + "\n  ".join(offenders)
    )


def test_scan_guard_has_positive_controls():
    """防空过：扫描器必须真能命中，且豁免名单不是空集。

    没有正向对照的扫描护栏会在正则写错时静默变成"永远通过"——本项目对
    "假的绿灯"格外警惕（见 docs/REGION_ANCHOR_VISUAL_CHECK.md 对伪测试的
    排除记录）。
    """
    assert _hits(f"limit = {_MB} * 1024 * 1024  # 硬上限") is not None
    assert _hits(f"_MAX = {_MB}*1024*1024") is not None
    # 反向：与其他数值/散文注释不得误命中（_CHUNK_SIZE = 8 * 1024 * 1024 是
    # 合法且无关的常量；注释里的"200MB PDF"是散文）
    assert _hits("_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB") is None
    assert _hits("开销 < 100ms 即使 200MB PDF。") is None
    assert _hits("系统 200MB 上限且拖垮上游提交") is None
    assert _ALLOWED_REL == {"config.py", "core/mineru_client.py"}


def test_byte_limit_message_is_derived_not_literal():
    """体积拒绝消息里的数值必须由常量派生。

    定点断言而非全仓扫描：文案天然与散文注释混同（见 _FORBIDDEN_PATTERNS
    上方的教训），但这一处的漂移是**用户可见**的 —— 上限改成 500MB 而消息
    仍写 200MB，用户会按错误的数字去拆文件。
    """
    src = (_ROOT / "api" / "jobs" / "upload.py").read_text(encoding="utf-8")
    m = re.search(r"文件过大（上限 [^）]*）", src)
    assert m, "未找到体积拒绝消息（是否被改写？请同步本测试）"
    assert "_MAX_PDF_BYTES" in m.group(0), f"消息内数值被写死：{m.group(0)!r}"


def test_page_limit_message_is_derived_not_literal():
    """页数拒绝消息同理 —— 数值必须来自 limits，不得写死。"""
    from config import check_upload_page_limits

    reject, _ = check_upload_page_limits(UPLOAD_LIMITS["max_pages"] + 1)
    assert str(UPLOAD_LIMITS["max_pages"]) in reject
    # 换一组自定义限额，消息必须跟着变（证明确实是派生而非写死）
    reject2, _ = check_upload_page_limits(7, {"max_pages": 6, "warn_pages": 3,
                                              "sec_per_page_total": 30.0})
    assert "上限 6 页" in reject2 and "上限 200 页" not in reject2


def test_single_source_actually_contains_the_value():
    """正向对照：真值本体确实写着限额 —— 否则上面的扫描会毫无意义地通过。"""
    text = (_ROOT / "config.py").read_text(encoding="utf-8")
    assert _hits(text) is not None, "config.py 应含 UPLOAD_LIMITS 的限额字面量"


# ── 6. 策略上限 ≤ 厂商上限（涉及 failover，必须保守）──────────────────


def test_policy_limit_does_not_exceed_mineru_vendor_limit():
    """准入上限不得超过 MinerU 厂商上限。

    MinerU 是 Paddle 提交失败时的 **failover 备选**：一份被我们放行、却被
    MinerU 拒收的文件，会在流程中途失败 —— 用户已经等过一轮 OCR 才发现。
    当前两者都是 200MB（数值巧合），本护栏防的是将来只改一边。
    """
    from core.mineru_client import MINERU_MAX_UPLOAD_BYTES

    assert UPLOAD_LIMITS["max_bytes"] <= MINERU_MAX_UPLOAD_BYTES, (
        f"准入上限 {UPLOAD_LIMITS['max_bytes']} 超过 MinerU 厂商上限 "
        f"{MINERU_MAX_UPLOAD_BYTES} —— failover 到 MinerU 时会在流程中途失败"
    )


# ── 7. 引用外部数字必须带出处、且不得张冠李戴量纲 ─────────────────────
#
# 由来（2026-09-15 自查）：初版把这个限额的"不可移植"论证写成了
# 「云厂商 1–2 s/页，比本链路快 10~30 倍」—— 这个数字纯属编造。核实后的
# 事实是：三大云纯文本抽取**价格**统一约 $1.50/1,000 页，结构化/生成式档
# $10~50/1,000 页，**相差 10~30 倍的是"价格"而不是"速度"**，而且各厂商
# 公布的是价格、根本不公布平均延迟。我把一个成本比值错记成了耗时比值，
# 并写进了 8 处文档/代码/测试。
#
# 教训可复用：**别把从别处借来的数字当自己的实测值**。因此这里钉两条：
#   ① 外部成本数字必须落在带出处的文档里（出处块不可被静默删除）；
#   ② 禁止把"每页耗时"归给第三方厂商 —— 那是我们**不可能有**的数据。
_VENDOR_TOKENS = (
    "云厂商",
    "Azure",
    "AWS",
    "Textract",
    "Document AI",
    "Mistral",
    "Veryfi",
    "amaise",
    "Autodesk",
)

# 形如 "9.0 s/页" / "1–2 s/页" / "37 秒/页" —— 每页耗时的中文工程写法
_PER_PAGE_TIME = re.compile(r"\d+(?:\.\d+)?\s*(?:[–~—\-]\s*\d+(?:\.\d+)?\s*)?(?:s|秒)\s*/\s*页")

_SOURCED_DOC = "docs/UPLOAD_LIMITS.md"
_CITATION_MARKERS = ("本节出处", "$1.50", "1,000 页")


def _iter_text_files():
    """扫描范围：单一真值 + 全部文档（第三方数字只可能出现在这些地方）。"""
    yield Path("config.py")
    for p in sorted((_ROOT / "docs").rglob("*.md")):
        yield p.relative_to(_ROOT)
    for name in ("CLAUDE.md", "CHANGELOG.md", "README.md", "DEPLOYMENT.md"):
        p = _ROOT / name
        if p.is_file():
            yield Path(name)


def test_vendor_latency_claims_are_forbidden():
    """禁止把"每页耗时"归给第三方厂商 —— 据我们所知无人公布该数字。

    触发条件收紧到**同一行内**同时出现厂商名与每页耗时：本项目的自测耗时
    （9.0 s/页 等）从不与厂商名同行，故正常内容零假阳性。
    若确有出处的厂商延迟，应写进 UPLOAD_LIMITS.md 的出处块并在此登记例外。
    """
    offenders = []
    for rel in _iter_text_files():
        try:
            lines = (_ROOT / rel).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines, 1):
            if any(v in line for v in _VENDOR_TOKENS) and _PER_PAGE_TIME.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "以下位置把「每页耗时」归给了第三方厂商 —— 该数字我们无从得知，"
        "请改用有出处的价格（如 $1.50/1,000 页）或本项目自测值：\n  "
        + "\n  ".join(offenders)
    )


def test_guard_against_vendor_latency_has_controls():
    """正/负对照：护栏必须真的能报警，且不误伤自测值与价格。"""
    # 正对照①：正是当初写错的那句 → 必须命中
    assert _PER_PAGE_TIME.search("云厂商的 1,000~3,000 页建立在其 1–2 s/页的成本上")
    # 正对照②：换一种写法也要命中
    assert _PER_PAGE_TIME.search("Azure Read 很快，约 0.4 秒/页")
    # 负对照①：本项目自测耗时（无厂商名）→ 不命中
    assert not (
        any(v in "本链路实测 9.0 s/页（OCR）" for v in _VENDOR_TOKENS)
        and _PER_PAGE_TIME.search("本链路实测 9.0 s/页（OCR）")
    )
    # 负对照②：厂商的价格数字（无"每页耗时"形态）→ 不命中
    vendor_pricing = "云厂商纯文本抽取统一约 $1.50 / 1,000 页"
    assert any(v in vendor_pricing for v in _VENDOR_TOKENS)
    assert not _PER_PAGE_TIME.search(vendor_pricing)


def test_external_figures_keep_a_citation():
    """外部数字必须可追溯：出处块与其关键锚点不得被静默删除。

    数字会过期，但"有这个数字、它从哪来"这条线索不能在编辑中丢失 ——
    丢失后下一个人就只能像我一样凭印象编一个。
    """
    text = (_ROOT / _SOURCED_DOC).read_text(encoding="utf-8")
    missing = [m for m in _CITATION_MARKERS if m not in text]
    assert not missing, (
        f"{_SOURCED_DOC} 缺少出处锚点 {missing} —— 外部数字必须带来源，"
        "否则后人无法分辨哪些是实测、哪些是引用"
    )
